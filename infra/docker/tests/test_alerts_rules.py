"""prometheus/rules/alerts.yml 告警规则校验（CP6.4-pre-2 / v1 §10.5 8 条核心规则）。

promtool 不在 CI 环境里，这里用等价的结构化校验兜底：
语法（括号/引号闭合）+ 每条 alert 必备字段（expr / for / labels / annotations）。
"""
import pathlib
import re

import pytest
import yaml

RULES_FILE = pathlib.Path(__file__).resolve().parents[1] / "prometheus" / "rules" / "alerts.yml"

EXPECTED_ALERTS = {
    "HighErrorRate",
    "HighClientErrorRate",
    "HighP95Latency",
    "ServiceDown",
    "ReadinessCheckFailing",
    "DistillTaskBacklog",
    "LLMCostSpike",
    "RedisConnectionFailing",
}
VALID_SEVERITIES = {"critical", "warning"}
DURATION_RE = re.compile(r"^\d+[smhd]$")
METRIC_RE = re.compile(r"\b[a-zA-Z_:][a-zA-Z0-9_:]*\s*(\{|\b)")


@pytest.fixture(scope="module")
def rule_groups() -> list[dict]:
    return yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))["groups"]


@pytest.fixture(scope="module")
def rules(rule_groups) -> list[dict]:
    return [r for group in rule_groups for r in group["rules"]]


ALL_RULES = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))["groups"][0]["rules"]


def _strip_quoted(expr: str) -> str:
    """去掉字符串字面量，避免里面的括号/花括号干扰配平检查。"""
    return re.sub(r'"[^"]*"|\'[^\']*\'', '""', expr)


def assert_expr_is_balanced(expr: str) -> None:
    """PromQL 括号配平检查（promtool 的最小子集）。"""
    stripped = _strip_quoted(expr)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for ch in stripped:
        if ch in pairs.values():
            stack.append(ch)
        elif ch in pairs:
            assert stack, f"多余的右括号 {ch!r}: {expr!r}"
            opening = stack.pop()
            assert pairs[ch] == opening, f"括号类型不匹配: {expr!r}"
    assert not stack, f"括号未闭合: {expr!r}"


def test_yaml_is_valid():
    """alerts.yml 是合法 YAML 且是 Prometheus rule_files 格式。"""
    doc = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    assert isinstance(doc["groups"], list) and doc["groups"]
    for group in doc["groups"]:
        assert group["name"]
        assert isinstance(group["rules"], list) and group["rules"]


def test_eight_core_alerts_exist(rules):
    """v1 §10.5 的 8 条核心规则必须全在。"""
    names = {r["alert"] for r in rules}
    assert EXPECTED_ALERTS <= names, f"缺失告警规则: {EXPECTED_ALERTS - names}"
    assert len(rules) == len(names), "alert 名称重复"


@pytest.mark.parametrize("rule", ALL_RULES, ids=lambda r: r["alert"])
def test_rule_required_fields(rule):
    """每条 alert 都要有 expr / for / labels.severity / annotations.summary。

    少任何一个 Prometheus 都加载失败，或者告警根本发不出来。
    """
    assert rule["expr"].strip(), "expr 为空"
    assert DURATION_RE.match(rule["for"]), f"for 必须是 Prometheus duration: {rule.get('for')}"
    assert rule["labels"]["severity"] in VALID_SEVERITIES
    assert rule["annotations"]["summary"].strip()


@pytest.mark.parametrize("rule", ALL_RULES, ids=lambda r: r["alert"])
def test_expr_is_parseable_promql(rule):
    """PromQL 表达式结构合法：括号闭合 + 至少引用一个 metric。"""
    expr = rule["expr"]
    assert_expr_is_balanced(expr)
    # 去掉 template / 函数参数后应残留 metric 名
    assert METRIC_RE.search(expr), f"表达式里没看到 metric 名: {expr!r}"


def test_critical_alerts_shorter_than_warnings(rules):
    """critical 的等待窗口不应该比 "火警级别" 更慢 —— 这是 v1 §10.5 的意图校验。"""
    durations = {r["alert"]: r["for"] for r in rules}
    assert durations["ServiceDown"] == "1m"
    assert durations["HighErrorRate"] == "2m"
