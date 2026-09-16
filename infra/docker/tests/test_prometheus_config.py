"""prometheus.yml scrape config 校验（CP6.4-pre-2）。

不涉及网络，只校验配置文件结构 —— 保证 Prometheus 起来时不会因为配置写错直接 crash。
"""
import pathlib

import pytest
import yaml

PROM_DIR = pathlib.Path(__file__).resolve().parents[1] / "prometheus"
PROM_CONFIG = PROM_DIR / "prometheus.yml"
RULES_DIR = PROM_DIR / "rules"

# 4 服务本机端口（CP1.5 起的约定）：job -> port
SERVICE_PORTS = {
    "api-gateway": 8100,
    "user-service": 8101,
    "content-service": 8102,
    "ai-service": 8103,
}


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(PROM_CONFIG.read_text(encoding="utf-8"))


def _job_targets(job: dict) -> list[str]:
    return [t for sc in job.get("static_configs", []) for t in sc.get("targets", [])]


def test_yaml_is_valid():
    """prometheus.yml 是合法 YAML 且顶层 key 齐全。"""
    cfg = yaml.safe_load(PROM_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(cfg, dict)
    assert isinstance(cfg.get("global"), dict)
    assert cfg["global"]["scrape_interval"] == "15s"
    assert cfg["global"]["evaluation_interval"] == "15s"


def test_rule_files_reference_alerts(config):
    """rule_files 指向 rules 目录，且目录里真的有告警规则文件。"""
    rule_files = config["rule_files"]
    assert "rules/*.yml" in rule_files
    assert (RULES_DIR / "alerts.yml").is_file(), f"缺告警规则文件: {RULES_DIR}/alerts.yml"


def test_scrape_jobs_cover_prometheus_and_all_services(config):
    """scrape target 含 Prometheus 自身 + 4 个服务（ai-worker 允许额外存在）。"""
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert "prometheus" in jobs
    assert set(SERVICE_PORTS) <= set(jobs)


def test_alerting_routes_to_alertmanager(config):
    """告警必须发给 alertmanager:9093（同 compose 网络里的 service 名）。"""
    targets = [
        t
        for am in config["alerting"]["alertmanagers"]
        for sc in am["static_configs"]
        for t in sc["targets"]
    ]
    assert "alertmanager:9093" in targets


@pytest.mark.parametrize("job_name,port", SERVICE_PORTS.items())
def test_service_target_uses_host_docker_internal(config, job_name, port):
    """4 服务走宿主机端口：host.docker.internal:<port>（本机 dev 服务不在容器里）。"""
    job = next(j for j in config["scrape_configs"] if j["job_name"] == job_name)
    targets = _job_targets(job)
    assert targets == [f"host.docker.internal:{port}"]
    assert job["metrics_path"] == "/metrics"


@pytest.mark.parametrize("job_name", SERVICE_PORTS)
def test_service_label_present(config, job_name):
    """每个服务 job 都要带 service label —— 告警规则 & dashboard 靠它过滤。"""
    job = next(j for j in config["scrape_configs"] if j["job_name"] == job_name)
    labels = [sc.get("labels", {}) for sc in job["static_configs"]]
    assert any(sc.get("service") == job_name for sc in labels)


def test_job_names_are_unique(config):
    names = [job["job_name"] for job in config["scrape_configs"]]
    assert len(names) == len(set(names)), "job_name 重复会让 Prometheus 拒绝启动"
