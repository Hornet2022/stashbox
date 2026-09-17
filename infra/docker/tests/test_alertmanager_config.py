"""alertmanager.yml 路由配置校验（CP6.5.2）。

不依赖 alertmanager 进程 / 网络，只校验配置文件结构 —— 保证容器起来时
不会因为路由、receiver 或抑制规则写错直接 crash。

配套规则文件见 prometheus/rules/alerts.yml（severity 只有 critical / warning 两种）。
"""
import pathlib

import pytest
import yaml

AM_DIR = pathlib.Path(__file__).resolve().parents[1] / "alertmanager"
AM_CONFIG = AM_DIR / "alertmanager.yml"

# alerts.yml 里实际用到的 severity 取值
SEVERITIES = ("critical", "warning")


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(AM_CONFIG.read_text(encoding="utf-8"))


def _severity_receivers(route: dict) -> dict[str, str]:
    """递归收集 route 树里 severity -> receiver 的映射。"""
    mapping: dict[str, str] = {}
    for sub in route.get("routes", []) or []:
        match = sub.get("match", {}) or {}
        if "severity" in match:
            mapping[match["severity"]] = sub["receiver"]
        mapping.update(_severity_receivers(sub))
    return mapping


def test_yaml_is_valid(config):
    """alertmanager.yml 是合法 YAML 且顶层 key 齐全。"""
    assert isinstance(config, dict)
    assert config["global"]["resolve_timeout"] == "5m"


def test_has_route_receivers_and_inhibit_rules(config):
    """route / receivers / inhibit_rules 三段都在，且分组节流参数齐全。"""
    route = config["route"]
    # 分组 + 节流：抑制告警噪音的核心参数
    assert route["group_by"] == ["alertname", "service"]
    assert route["group_wait"] == "30s"
    assert route["group_interval"] == "5m"
    assert route["repeat_interval"] == "4h"
    # 兜底 receiver 必须真实存在
    assert route["receiver"] in {r["name"] for r in config["receivers"]}

    assert isinstance(config["receivers"], list) and config["receivers"]
    assert isinstance(config["inhibit_rules"], list) and config["inhibit_rules"]


def test_critical_and_warning_have_separate_receivers(config):
    """critical / warning 各走独立 receiver，且两个 receiver 都配了 webhook。"""
    mapping = _severity_receivers(config["route"])
    assert set(SEVERITIES) <= set(mapping), f"缺 severity 路由: {mapping}"

    critical, warning = mapping["critical"], mapping["warning"]
    assert critical != warning, "critical 和 warning 不能共用同一个 receiver"

    by_name = {r["name"]: r for r in config["receivers"]}
    for name in (critical, warning):
        assert name in by_name, f"路由指向了未定义的 receiver: {name}"
        webhooks = by_name[name]["webhook_configs"]
        assert webhooks and webhooks[0]["url"], f"{name} 没有 webhook_configs"
        assert webhooks[0]["send_resolved"] is True


def test_inhibit_rule_critical_suppresses_warning(config):
    """存在 critical → warning 的抑制规则，且按 alertname + service 对齐。"""
    rules = config["inhibit_rules"]
    matching = [
        r
        for r in rules
        if r["source_match"].get("severity") == "critical"
        and r["target_match"].get("severity") == "warning"
    ]
    assert matching, f"缺 critical → warning 抑制规则: {rules}"
    assert set(matching[0]["equal"]) == {"alertname", "service"}
