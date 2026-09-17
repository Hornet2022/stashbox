"""v1 §10.5 业务告警覆盖验证（CP6.5.1）。"""
from pathlib import Path
import yaml

ALERTS_PATH = Path(__file__).parents[3] / "infra/docker/prometheus/rules/alerts.yml"


def test_alerts_yaml_legal():
    """alerts.yml YAML 合法可解析"""
    data = yaml.safe_load(ALERTS_PATH.read_text())
    assert "groups" in data


def test_alerts_total_at_least_14():
    """alert 总数 >= 14（11 已有 + 3 新）"""
    data = yaml.safe_load(ALERTS_PATH.read_text())
    alerts = []
    for g in data["groups"]:
        for r in g["rules"]:
            if "alert" in r:
                alerts.append(r["alert"])
    assert len(alerts) >= 14, f"只有 {len(alerts)} alerts: {alerts}"


def test_v10_5_new_alerts_present():
    """5 新 alert 名字存在（3 真接 + 2 注释占位也算）"""
    text = ALERTS_PATH.read_text()
    for name in [
        "DistillSuccessRateLow",
        "DistillDurationP95High",
        "StepFailureRateHigh",
        "OSSStorageHigh",  # 注释里也要出现
        "NodeCPUHigh",  # 注释里也要出现
    ]:
        assert name in text, f"alert {name} 不在 alerts.yml"
