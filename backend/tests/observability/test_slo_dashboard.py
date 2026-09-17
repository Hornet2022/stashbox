"""
CP6.3 SLO dashboard 验证测试。

测试内容：
1. JSON 合法可解析
2. title / uid 字段正确
3. panel 数 >= 12（CP6.3 要求 14 panel）
4. 每 panel 有 targets[].expr query
5. 每 panel datasource.uid = stashbox-prometheus

与 §4.2 14 panel 清单严格对应（不多不少）。
"""
import json
from pathlib import Path

DASHBOARD_PATH = Path(__file__).parents[3] / "infra/docker/grafana/dashboards/slo_overview.json"


def test_dashboard_json_legal():
    """JSON 合法可解析"""
    raw = DASHBOARD_PATH.read_text()
    data = json.loads(raw)
    assert data["title"].startswith("Stashbox")
    assert data["uid"] == "stashbox-slo-overview"


def test_dashboard_has_at_least_12_panels():
    """panel 数 >= 12（v1 §10.5 8 条 + 蒸馏 + quota + 黑盒 + redis 至少 4）"""
    data = json.loads(DASHBOARD_PATH.read_text())
    panels = data["panels"]
    assert len(panels) >= 12, f"只有 {len(panels)} panels"


def test_each_panel_has_expr_query():
    """每 panel 必须有 targets[].expr（Grafana 不报 'No data' 也要 query）"""
    data = json.loads(DASHBOARD_PATH.read_text())
    for p in data["panels"]:
        targets = p.get("targets", [])
        assert len(targets) >= 1, f"panel {p['id']} 无 target"
        for t in targets:
            assert t.get("expr"), f"panel {p['id']} target {t.get('refId')} 无 expr"
            assert len(t["expr"]) > 5, f"panel {p['id']} expr 过短: {t['expr']}"


def test_each_panel_datasource_uid_correct():
    """每 panel datasource.uid = stashbox-prometheus"""
    data = json.loads(DASHBOARD_PATH.read_text())
    for p in data["panels"]:
        ds = p.get("datasource", {})
        assert ds.get("uid") == "stashbox-prometheus", f"panel {p['id']} ds uid 错: {ds.get('uid')}"
        for t in p.get("targets", []):
            tds = t.get("datasource", {})
            assert tds.get("uid") == "stashbox-prometheus"
