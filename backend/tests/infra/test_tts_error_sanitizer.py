"""ErrorDetailSanitizer：5xx 内部细节脱敏（2026-10-03）。

## 背景

TTS 服务打错模型名时（传了不存在的 `tingting`）返回 500，body 原样是：

    {"detail": "Failed to load model 'tingting': Got: ConnectTimeout:
     [Errno 60] Operation timed out\\nAn error happened while trying to
     locate the files on the Hub, and we cannot find the appropriate
     snapshot folder for the specified revision on the local disk."}

这段不是我们写的 —— mlx_audio 抛的异常被 FastAPI 默认包成 `{"detail": str(exc)}`
原样回给调用方，泄露了：连了哪个 Hub、本地快照目录怎么命名、有没有走网络、
什么 revision。

TTS 服务监听 127.0.0.1:8010，任何能访问它的进程都能拿到这些内部拓扑信息。
详细错误保留在服务端日志里，排障不受影响。
"""

import asyncio
import json
import sys
from pathlib import Path

# tts_serve.py 在 stashbox/infra/ 下（**不在** backend/infra/）——
# 路径算错会报 ModuleNotFoundError: No module named 'tts_serve'。
# parents[2] 就是 backend/，所以要往上一级再拼 stashbox/infra/。
REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_DIR = REPO_ROOT / "infra" / "mlx-audio-tts-service"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))


# ---------------------------------------------------------------------------
# ASGI 迷你驱动
# ---------------------------------------------------------------------------


def call_asgi(app, scope) -> tuple[int, bytes]:
    """跑一个 ASGI app，收集 (status, body)。"""
    out: dict = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"]


HTTP_SCOPE = {
    "type": "http",
    "method": "POST",
    "path": "/v1/audio/speech",
    "headers": [],
}


def _json_body(payload) -> bytes:
    return json.dumps(payload).encode()


# ---------------------------------------------------------------------------
# 1. 5xx 必须脱敏
# ---------------------------------------------------------------------------


def test_5xx_响应体被替换成中性提示():
    from tts_serve import ErrorDetailSanitizer

    leaked = (
        "Failed to load model 'tingting': Got: ConnectTimeout: [Errno 60] "
        "Operation timed out\nAn error happened while trying to locate the files "
        "on the Hub, and we cannot find the appropriate snapshot folder for the "
        "specified revision on the local disk."
    )

    async def leaky_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": _json_body({"detail": leaked})})

    status, body = call_asgi(ErrorDetailSanitizer(leaky_app), dict(HTTP_SCOPE))

    assert status == 500, "状态码必须保持 5xx，不能改成 200 骗调用方"
    detail = json.loads(body)["detail"]
    assert detail == ErrorDetailSanitizer.PUBLIC_DETAIL


def test_脱敏后不含任何内部关键词():
    from tts_serve import ErrorDetailSanitizer

    async def leaky_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": _json_body(
                    {"detail": "ConnectTimeout to Hub, snapshot folder /Volumes/AIWorker/x missing"}
                ),
            }
        )

    _, body = call_asgi(ErrorDetailSanitizer(leaky_app), dict(HTTP_SCOPE))
    text = body.decode()

    for leak in ("ConnectTimeout", "Hub", "snapshot", "/Volumes/", "AIWorker"):
        assert leak not in text, f"脱敏后仍泄露了内部信息：{leak}"


# ---------------------------------------------------------------------------
# 2. 4xx / 2xx 不能动
# ---------------------------------------------------------------------------


def test_4xx_原样透传():
    """参数错、格式不支持这类 4xx 是调用方自己的问题，原样回传才有用。"""
    from tts_serve import ErrorDetailSanitizer

    original = "model 'x' not found, check your request body"

    async def bad_request_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 422,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": _json_body({"detail": original})})

    status, body = call_asgi(ErrorDetailSanitizer(bad_request_app), dict(HTTP_SCOPE))

    assert status == 422
    assert json.loads(body)["detail"] == original, "4xx 的 detail 不该被改写"


def test_2xx_音频字节原样通过():
    """成功路径绝不能被中间件碰 —— 碰了就合成全废。"""
    from tts_serve import ErrorDetailSanitizer

    audio = b"ID3\x04\x00\x00\x00fake-mp3-bytes" * 100

    async def ok_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"audio/mpeg")],
            }
        )
        await send({"type": "http.response.body", "body": audio})

    status, body = call_asgi(ErrorDetailSanitizer(ok_app), dict(HTTP_SCOPE))

    assert status == 200
    assert body == audio, "2xx 的音频字节必须逐字节一致"


def test_2xx_保留内层的_content_type():
    """回归防护：闭包里漏了 `nonlocal` 会让外层 headers 永远是空列表。

    那样 2xx 的 `content-type: audio/mpeg` 会整个丢掉，播放器拿到
    `application/octet-stream` 或干脆没有类型 —— 合成没坏但播不出来。
    """
    from tts_serve import ErrorDetailSanitizer

    captured: dict = {}

    async def ok_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"audio/mpeg")],
            }
        )
        await send({"type": "http.response.body", "body": b"fake"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["headers"] = message.get("headers", [])

    asyncio.run(ErrorDetailSanitizer(ok_app)(dict(HTTP_SCOPE), receive, send))

    keys = {k.lower() for k, _ in captured.get("headers", [])}
    assert b"content-type" in keys, f"content-type 丢了，实得 headers={captured.get('headers')}"
    values = {k.lower(): v for k, v in captured["headers"]}
    assert values[b"content-type"] == b"audio/mpeg"
    assert b"content-length" in keys, "非分块响应必须补 content-length"


# ---------------------------------------------------------------------------
# 3. 内层抛异常也要兜住
# ---------------------------------------------------------------------------


def test_内层抛异常时转成_500_而不是把栈泄出去():
    from tts_serve import ErrorDetailSanitizer

    async def exploding_app(scope, receive, send):
        raise RuntimeError(
            "Failed to load model: ConnectTimeout, snapshot folder /Volumes/AIWorker missing"
        )

    status, body = call_asgi(ErrorDetailSanitizer(exploding_app), dict(HTTP_SCOPE))

    assert status == 500
    text = body.decode()
    assert "/Volumes/" not in text and "ConnectTimeout" not in text
    assert json.loads(text)["detail"] == ErrorDetailSanitizer.PUBLIC_DETAIL


def test_非_http_scope_直接透传():
    """lifespan / websocket 不该被这个中间件干扰。"""
    from tts_serve import ErrorDetailSanitizer

    seen: list[str] = []

    async def passthrough(scope, receive, send):
        seen.append(scope["type"])

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(_message):
        return None

    asyncio.run(ErrorDetailSanitizer(passthrough)({"type": "lifespan"}, receive, send))
    assert seen == ["lifespan"], "非 http scope 应原样透传给内层"
