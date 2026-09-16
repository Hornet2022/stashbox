"""docker-compose.observability.yml 语法 + 结构校验（CP6.4-pre-2）。

Docker 不总是可用，所以这里用 YAML 解析代替 `docker compose config`，
重点校验：端口映射、volume 声明、挂载的本地文件真实存在。
"""
import pathlib
import re

import pytest
import yaml

DOCKER_DIR = pathlib.Path(__file__).resolve().parents[1]
COMPOSE_FILE = DOCKER_DIR / "docker-compose.observability.yml"

EXPECTED_SERVICES = {"prometheus", "grafana", "alertmanager"}
EXPECTED_PORTS = {"prometheus": "9090", "grafana": "3000", "alertmanager": "9093"}
EXPECTED_VOLUMES = {"prometheus_data", "grafana_data"}
BIND_RE = re.compile(r"^\./(.+?):")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_compose_yaml_is_valid():
    """顶层结构是 docker compose schema：services + volumes 都在。"""
    doc = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(doc["services"], dict)
    assert isinstance(doc["volumes"], dict)


def test_has_three_services(compose):
    assert set(compose["services"]) == EXPECTED_SERVICES


@pytest.mark.parametrize("service", sorted(EXPECTED_SERVICES))
def test_service_declares_image_and_container_name(compose, service):
    spec = compose["services"][service]
    assert spec["image"], f"{service} 没有指定 image"
    assert spec["container_name"] == f"stashbox_{service}"
    assert spec["restart"] == "unless-stopped"


@pytest.mark.parametrize("service,port", EXPECTED_PORTS.items())
def test_ports_exposed(compose, service, port):
    """宿主机端口必须按 Prometheus 9090 / Grafana 3000 / Alertmanager 9093 暴露。"""
    published = compose["services"][service]["ports"]
    assert any(p.split(":")[0] == port for p in published), f"{service} 没暴露 {port}"


def test_declared_volumes_and_mounts(compose):
    """命名 volume 必须先在顶层声明，否则 compose 起不来。"""
    assert EXPECTED_VOLUMES <= set(compose["volumes"])
    mounts = [v for spec in compose["services"].values() for v in spec.get("volumes", [])]
    for name in EXPECTED_VOLUMES:
        assert any(m.startswith(f"{name}:/") for m in mounts), f"{name} 没被任何 service 挂载"


@pytest.mark.parametrize("service", sorted(EXPECTED_SERVICES))
def test_bind_mounted_files_exist(compose, service):
    """./xxx 挂载的本地文件必须存在，否则容器启动即退出。"""
    for mount in compose["services"][service].get("volumes", []):
        match = BIND_RE.match(mount)
        if not match:
            continue
        path = DOCKER_DIR / match.group(1)
        assert path.exists(), f"{service} 挂载的 {path} 不存在"


def test_grafana_depends_on_prometheus(compose):
    """Grafana 的 datasource 指向 prometheus 容器，depends_on 保证启动顺序。"""
    assert "prometheus" in compose["services"]["grafana"]["depends_on"]


def test_prometheus_can_reach_host(compose):
    """本机 4 服务跑在宿主机上，Prometheus 必须有 host-gateway 映射。"""
    extra_hosts = compose["services"]["prometheus"]["extra_hosts"]
    assert "host.docker.internal:host-gateway" in extra_hosts
