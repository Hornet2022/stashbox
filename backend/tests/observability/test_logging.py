"""CP6.4-pre：structlog JSON 输出 + X-Request-ID 链路。"""
import json

from stashbox.backend.common import logging as common_logging


def _json_events(captured: str) -> list[dict]:
    """capsys 抓到的 stdout 里挑合法的 JSON 日志行。"""
    events = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


async def test_setup_logging_emits_json(capsys):
    common_logging.setup_logging("test-service", level="INFO")
    log = common_logging.get_logger("unit")

    log.info("hello_event", foo=1, bar="x")

    data = json.loads(capsys.readouterr().out.strip())
    assert data["service"] == "test-service"
    assert data["level"] == "info"
    assert data["event"] == "hello_event"
    assert data["foo"] == 1
    assert data["bar"] == "x"
    assert "timestamp" in data


async def test_log_level_is_honored(capsys):
    common_logging.setup_logging("test-service", level="WARNING")
    log = common_logging.get_logger("unit")

    log.info("should_be_dropped")

    out = capsys.readouterr().out.strip()
    assert out == ""


async def test_new_request_id_binds_context(capsys):
    common_logging.setup_logging("test-service", level="INFO")
    rid = common_logging.new_request_id()
    log = common_logging.get_logger("unit")

    log.info("with_request_id")

    data = json.loads(capsys.readouterr().out.strip())
    assert rid.startswith("req_")
    assert common_logging.get_request_id() == rid
    assert data["request_id"] == rid


async def test_bind_request_id_reuses_upstream(capsys):
    common_logging.setup_logging("test-service", level="INFO")
    common_logging.bind_request_id("req_upstream_123")
    log = common_logging.get_logger("unit")

    log.info("proxying")

    data = json.loads(capsys.readouterr().out.strip())
    assert data["request_id"] == "req_upstream_123"


async def test_clear_context_drops_request_id(capsys):
    common_logging.setup_logging("test-service", level="INFO")
    common_logging.new_request_id()
    common_logging.clear_context()
    log = common_logging.get_logger("unit")

    log.info("no_request_id")

    data = json.loads(capsys.readouterr().out.strip())
    assert "request_id" not in data


async def test_request_id_is_injected_and_echoed(content_app, asgi_client):
    async with asgi_client(content_app) as c:
        r = await c.get("/healthz")

    rid = r.headers["x-request-id"]
    assert rid.startswith("req_"), rid


async def test_upstream_request_id_is_propagated(content_app, asgi_client):
    async with asgi_client(content_app) as c:
        r = await c.get("/healthz", headers={"X-Request-ID": "req_from_gateway"})

    assert r.headers["x-request-id"] == "req_from_gateway"


async def test_access_log_line_carries_request_id(content_app, asgi_client, capsys):
    common_logging.setup_logging("content-service")  # service 字段来自最后一次 setup_logging
    async with asgi_client(content_app) as c:
        r = await c.get("/healthz")

    rid = r.headers["x-request-id"]
    events = _json_events(capsys.readouterr().out)
    completed = [e for e in events if e.get("event") == "request_completed"]

    assert completed, "中间件没打 request_completed 日志"
    line = completed[-1]
    assert line["request_id"] == rid
    assert line["method"] == "GET"
    assert line["path"] == "/healthz"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], float)
    assert line["service"] == "content-service"
