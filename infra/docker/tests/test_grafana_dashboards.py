"""Grafana dashboard JSON 校验（CP6.4-pre-2）。

Dashboard 是 provisioning 自动加载的：JSON 写错 Grafana 只会静默不显示，
没有别的报错渠道，所以这里做前置校验。
"""
import json
import pathlib

import pytest

DASHBOARD_DIR = pathlib.Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
PROMETHEUS_UID = "stashbox-prometheus"

DASHBOARDS = ["request_overview.json", "distill_pipeline.json"]


@pytest.fixture(scope="module")
def dashboards() -> dict[str, dict]:
    return {
        name: json.loads((DASHBOARD_DIR / name).read_text(encoding="utf-8")) for name in DASHBOARDS
    }


def _datasources(panel: dict) -> list[dict]:
    """面板级 datasource + 每个 target 自己的 datasource。"""
    ds = panel.get("datasource")
    return ([ds] if ds else []) + [t.get("datasource") for t in panel.get("targets", [])]


@pytest.mark.parametrize("name", DASHBOARDS)
def test_dashboard_json_is_valid(name):
    """JSON 可解析 + 必备顶层字段。"""
    doc = json.loads((DASHBOARD_DIR / name).read_text(encoding="utf-8"))
    assert doc["uid"], "uid 缺失会导致每次重载生成新 dashboard"
    assert doc["title"]
    assert doc["schemaVersion"]


@pytest.mark.parametrize(
    "name,min_panels",
    [("request_overview.json", 4), ("distill_pipeline.json", 5)],
)
def test_panel_count(dashboards, name, min_panels):
    """任务包要求：request_overview ≥ 4 个 panel，distill_pipeline ≥ 5 个。"""
    panels = dashboards[name]["panels"]
    assert len(panels) >= min_panels


@pytest.mark.parametrize("name", DASHBOARDS)
def test_panels_have_unique_ids(dashboards, name):
    ids = [p["id"] for p in dashboards[name]["panels"]]
    assert len(ids) == len(set(ids)), "panel id 重复会让部分 panel 渲染不出来"


@pytest.mark.parametrize("name", DASHBOARDS)
def test_every_panel_targets_prometheus(dashboards, name):
    """所有 panel / target 都引用 Prometheus 数据源（类型 + uid 都对得上）。"""
    for panel in dashboards[name]["panels"]:
        sources = _datasources(panel)
        assert sources, f"panel {panel.get('title')} 没声明 datasource"
        for ds in sources:
            assert ds["type"] == "prometheus"
            assert ds["uid"] == PROMETHEUS_UID


@pytest.mark.parametrize("name", DASHBOARDS)
def test_panels_have_queries(dashboards, name):
    for panel in dashboards[name]["panels"]:
        assert panel["title"], "panel 必须有 title"
        assert panel["type"], "panel 必须有 type"
        targets = panel["targets"]
        assert targets, f"panel {panel['title']} 没有 target"
        for target in targets:
            assert target["expr"].strip(), f"panel {panel['title']} 有空 PromQL"
            assert target["refId"]


def test_datasources_provisioning_matches_dashboard_uid():
    """dashboard 引用的 uid 必须和 provisioning 里配的 datasource uid 一致。"""
    ds_file = pathlib.Path(__file__).resolve().parents[1] / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
    content = ds_file.read_text(encoding="utf-8")
    assert PROMETHEUS_UID in content, f"{ds_file} 里没有 uid={PROMETHEUS_UID}"
