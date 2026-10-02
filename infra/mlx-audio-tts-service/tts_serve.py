"""听匣独立 TTS 服务入口。

在 `mlx_audio.server`（自带 FastAPI OpenAI 兼容 server）之上打两层补丁后启动：

1. **#2312 sampler 修复（必须，否则会踩 oMLX 那个「空转 / 零音频」坑）**

   `mlx_lm.sample_utils` 把 `categorical_sampling` / `apply_top_k` /
   `apply_top_p` / `apply_min_p` 都装饰了
   `@partial(mx.compile, inputs=mx.random.state, outputs=mx.random.state)`。
   这个装饰器在第一次调用后**不再推进全局 RNG 状态**，于是 TTS sampler
   重放同一个冻结的随机数。当冻结值偏向 codec EOS 列时，talker 在 step 0
   就发 EOS，之后每次 `/v1/audio/speech` 都返回**零音频**，而且因为 RNG
   状态和 compile 缓存是进程级、跨模型重载存活的，**只有重启进程才能恢复**。

   表现就是：50% CPU 空转、13 字短文本也零响应。oMLX 用
   `omlx/patches/mlx_audio_sampling.py` 绕开了它（`ensure_uncompiled_tts_samplers`）。
   这里做同样的事，替换实现是 `omlx_sampling`（Apache-2.0，oMLX 原样拷贝，
   纯函数、只依赖 mlx.core）。

2. **模型别名解析（不加的话会静默从 HuggingFace 下载几十 GB）**

   `mlx_audio.utils.get_model_path` 的语义是「本地不存在就从 HF 拉」。
   后端 `INDEXTTS_MODEL` 发的是短名 `Qwen3-TTS-12Hz-0.6B-Base-bf16`，
   本地并没有这个目录 —— 直接调会触发下载。这里用
   `MLX_AUDIO_MODEL_ALIASES`（`短名=绝对路径` 逗号分隔）先把短名映射成本地
   路径，让权重继续从 `/Volumes/AIWorker` 读，**不搬任何模型文件**。

启动：
    python -m tts_serve --host 127.0.0.1 --port 8010
    MLX_AUDIO_MODEL_ALIASES="Qwen3-TTS-12Hz-0.6B-Base-bf16=/Volumes/AIWorker/..." \\
        python -m tts_serve --port 8010
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import sys

log = logging.getLogger("tts_serve")

# ---------------------------------------------------------------------------
# 补丁 3：ref_audio 适配（base64 → 临时文件）
# ---------------------------------------------------------------------------
#
# 后端 `indextts.py` 按 oMLX 的契约发 `ref_audio` = **base64 音频字节**；
# 而 `mlx_audio.server` 的 `SpeechRequest.ref_audio` 期望的是**文件路径**
# （server.py:601 `if not os.path.exists(ref_audio)` → 400）。
#
# 这层转换原本由 oMLX 在它自己的 455 行引擎壳里做（所以 oMLX 收 base64 直接能用）。
# 现在自己写，就放在这里 —— 后端一行不用改，回滚只需把 INDEXTTS_BASE_URL
# 改回 8000。这也正是「切到自己代码」的实际收益。
#
# 文件按内容 sha256 命名放在固定目录：同一个参考音频（听匣只有一个
# tingting_ref.wav）在进程生命周期内只会落盘一次，后续请求直接命中路径。
# 写临时目录而不是项目目录，进程退出即失效，不会留下需要清理的产物。

_REF_DIR = os.getenv("TTS_SERVE_REF_DIR", "/tmp/tts-serve-refs")
_REF_CACHE: dict[str, str] = {}

# 补丁 3b：response_format 缺省值。
#
# `SpeechRequest.response_format` 默认是 **"mp3"**，而 oMLX 那边默认给 WAV，
# 后端 `indextts.py` 是用 `wave.open` 解析返回字节的（还要算时长做截断校验）。
# 调用方不显式传 response_format 时，这里补成 wav，保持与 oMLX 一致的契约。
_DEFAULT_FORMAT = os.getenv("TTS_SERVE_DEFAULT_FORMAT", "wav")


def _with_format(body: bytes) -> bytes:
    """给不带 response_format 的 /v1/audio/speech 请求体补上默认格式。"""
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(parsed, dict) or "response_format" in parsed:
        return body
    parsed["response_format"] = _DEFAULT_FORMAT
    return json.dumps(parsed).encode()


def _materialize_ref_audio(b64: str) -> str:
    """把 base64 音频写成临时文件，返回路径。已解码过的直接落盘。"""
    try:
        raw = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"ref_audio 既不是可读路径也不是合法 base64: {e}") from e
    if not raw:
        raise ValueError("ref_audio base64 解码后为空")

    digest = hashlib.sha256(raw).hexdigest()[:32]
    cached = _REF_CACHE.get(digest)
    if cached and os.path.exists(cached):
        return cached

    # 扩展名按 magic bytes 推；mlx_audio 的 load_audio 靠后缀/内容判格式。
    if raw[:4] == b"RIFF":
        ext = ".wav"
    elif raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        ext = ".mp3"
    elif raw[:4] == b"OggS":
        ext = ".ogg"
    elif raw[:4] == b"fLaC":
        ext = ".flac"
    else:
        ext = ".wav"

    os.makedirs(_REF_DIR, exist_ok=True)
    path = os.path.join(_REF_DIR, f"{digest}{ext}")
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)  # 原子替换，避免并发请求读到半个文件
    _REF_CACHE[digest] = path
    return path


class RefAudioAdapter:
    """ASGI 中间件：把请求体里的 base64 ref_audio 换成临时文件路径。

    只拦 `POST /v1/audio/speech`；已经是路径的 payload 原样放过，
    所以这个适配对两种调用方式都安全。
    """

    PATH = "/v1/audio/speech"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != self.PATH
        ):
            await self.app(scope, receive, send)
            return

        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break

        payload = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                payload = parsed
        except (ValueError, UnicodeDecodeError):
            payload = None

        if payload is None or not payload.get("ref_audio"):
            await self.app(scope, _replayer(_with_format(body)), send)
            return

        ref = payload["ref_audio"]
        if not os.path.exists(ref):
            try:
                payload["ref_audio"] = _materialize_ref_audio(ref)
            except ValueError as e:
                # 转换不了就交回原 body，让 FastAPI 按自己的规则报 400，
                # 而不是在我们这层抛出一个对调用方没有意义的异常。
                log.warning("ref_audio 适配失败，交回原始请求体: %s", e)
            else:
                payload.setdefault("response_format", _DEFAULT_FORMAT)
                body = json.dumps(payload).encode()

        # content-length 必须跟着改，否则 Starlette 只会读到原始长度。
        headers = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.lower() != b"content-length"
        ]
        headers.append((b"content-length", str(len(body)).encode()))
        new_scope = dict(scope)
        new_scope["headers"] = headers
        await self.app(new_scope, _replayer(body), send)


def _replayer(body: bytes):
    """造一个投递一次 body 的 receive()，供改写后的请求体复用。

    body 投递完之后**挂起**而不是回 `http.disconnect`：
    Starlette 的 StreamingResponse 会起一个 `listen_for_disconnect` 任务反复
    调 receive()，收到 `http.disconnect` 就认为客户端断了，直接掐断响应 ——
    实测表现为路由已返回 200 但客户端收到 `IncompleteRead(0 bytes read)`、
    服务端打 `ASGI callable returned without completing response`。
    回空 request 也不行，那会让监听任务变成忙等、吃满一个核。
    挂起则是标准做法：响应结束时 uvicorn 会 cancel 这些 task。
    """
    import asyncio

    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    return receive


# ---------------------------------------------------------------------------
# 补丁 1：#2312 sampler 重绑
# ---------------------------------------------------------------------------

_SAMPLERS = ("categorical_sampling", "apply_top_k", "apply_top_p", "apply_min_p")


def reroute_tts_samplers() -> int:
    """把 mlx-lm 的编译版 sampler 换成 omlx_sampling 的自由版。

    返回重绑的绑定点数量。幂等。
    """
    import mlx_lm.sample_utils as sample_utils
    import omlx_sampling as free

    # 替换前先留一份原件引用，用来按「身份」区分「从 sample_utils 导入的」
    # 和「backend 自己定义的同名函数」（moss_tts 等就这么干，后者不该动）。
    originals = {name: getattr(sample_utils, name) for name in _SAMPLERS}

    count = 0
    for name in _SAMPLERS:
        replacement = getattr(free, name)
        if getattr(sample_utils, name) is not replacement:
            setattr(sample_utils, name, replacement)
            count += 1

    by_original = {id(orig): getattr(free, name) for name, orig in originals.items()}
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not mod_name.startswith("mlx_audio.tts."):
            continue
        for attr_name, attr_value in list(vars(mod).items()):
            replacement = by_original.get(id(attr_value))
            if replacement is not None:
                setattr(mod, attr_name, replacement)
                count += 1

    return count


# ---------------------------------------------------------------------------
# 补丁 2：模型别名 → 本地绝对路径
# ---------------------------------------------------------------------------


def install_model_aliases() -> dict[str, str]:
    """让短名模型解析到本地目录，阻止 mlx_audio 静默从 HF 下载。"""
    import mlx_audio.tts.utils as tts_utils
    import mlx_audio.utils as core_utils

    raw = os.getenv("MLX_AUDIO_MODEL_ALIASES", "").strip()
    aliases: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, path = pair.partition("=")
        name, path = name.strip(), path.strip()
        if not name or not path:
            continue
        if not os.path.isdir(path):
            raise SystemExit(
                f"MLX_AUDIO_MODEL_ALIASES: {name} 指向的目录不存在 -> {path}"
            )
        aliases[name] = path

    if not aliases:
        return aliases

    core_real = core_utils.get_model_path
    tts_real = tts_utils.get_model_path

    def wrap(real):
        def resolve(path_or_repo, *args, **kwargs):
            return real(aliases.get(path_or_repo, path_or_repo), *args, **kwargs)

        return resolve

    core_utils.get_model_path = wrap(core_real)
    # tts/utils.py 是 `from mlx_audio.utils import get_model_path` 导入的绑定，
    # 不改它的话 base_load_model 走新函数、tts 侧仍走旧函数。
    tts_utils.get_model_path = wrap(tts_real)

    log.info("模型别名已生效: %s", aliases)
    return aliases


class ErrorDetailSanitizer:
    """把 5xx 响应里的内部异常细节换成一句中性提示。

    ## 为什么需要（2026-10-03 实测发现）

    打错模型名时（传了不存在的 `tingting`）服务返回 500，body 是：

        {"detail": "Failed to load model 'tingting': Got: ConnectTimeout:
         [Errno 60] Operation timed out\\nAn error happened while trying to
         locate the files on the Hub, and we cannot find the appropriate
         snapshot folder for the specified revision on the local disk."}

    这不是我们写的文案 —— mlx_audio 抛的异常被 FastAPI 默认包成
    `{"detail": str(exc)}` 原样回给了调用方。它泄露的是：连了哪个 Hub、
    本地快照目录怎么命名、有没有走网络、什么 revision。

    风险等级不高（没有密钥、没有真实绝对路径），但 TTS 服务在内网监听
    127.0.0.1:8010，任何能访问它的进程都能拿到这些内部拓扑信息。
    详细错误**保留在服务端日志**里，排障不受影响。

    4xx 不动 —— 那些是调用方自己的问题（参数错、格式不支持），原样回传才有
    助于客户端给出正确提示。
    """

    #: 5xx 一律替换成这句。调用方只需知道"服务端合成失败"。
    PUBLIC_DETAIL = "TTS 合成失败，请检查服务日志"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start_msg: dict = {}
        body_parts: list[bytes] = []
        out_headers: list[tuple[bytes, bytes]] = []

        async def _buffer(message):
            """先把内层响应**缓冲**下来，不急着转发。

            5xx 的判定要等到 `http.response.start` 才知道，而 body 是在它之后
            才发过来的 —— 所以必须整段收完再决定转发什么。边转发边判断会导致
            「先发原始 500，再补一份脱敏 500」，客户端收到两份响应。

            ⚠️ 这里必须 `nonlocal out_headers`：闭包里裸赋值会被当成**新的局部
            变量**，外层的 headers 永远是空列表 —— 2xx 的 content-type
            会全丢（ruff F841 抓到过这个）。
            """
            nonlocal out_headers
            if message.get("type") == "http.response.start":
                start_msg.update(message)
                out_headers = list(message.get("headers", []))
            elif message.get("type") == "http.response.body":
                body_parts.append(message.get("body", b""))

        try:
            await self.app(scope, receive, _buffer)
        except Exception as exc:
            # 异常一路冒到 ASGI 层（ServerErrorMiddleware 之外）的情况
            log.exception("TTS 请求处理失败: %s", exc)
            await _json_response(send, 500, self.PUBLIC_DETAIL)
            return

        status = start_msg.get("status", 200)

        if status >= 500:
            log.error("TTS 返回 %d，已对调用方隐藏内部细节", status)
            await _json_response(send, status, self.PUBLIC_DETAIL)
            return

        # 正常响应：原样转发（含音频字节流，一个字节都不能动）
        body = b"".join(body_parts)
        if not any(k.lower() == b"content-length" for k, _ in out_headers):
            out_headers.append((b"content-length", str(len(body)).encode()))

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": out_headers,
            }
        )
        await send({"type": "http.response.body", "body": body})


async def _json_response(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def main() -> None:
    import argparse

    logging.basicConfig(
        level=os.getenv("TTS_SERVE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="听匣独立 TTS 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()

    # 第一轮：此时 mlx_audio 还没 import，改 mlx_lm.sample_utils 就够了。
    n = reroute_tts_samplers()
    log.info("sampler 重绑（import 前）: %d 处", n)

    import mlx_audio.server as server  # 触发各 backend 模块被 import

    # 第二轮：万一某个 backend 在 import 时就把编译版 sampler 绑进了自己的
    # 命名空间，按身份扫描把它们换掉。
    n = reroute_tts_samplers()
    log.info("sampler 重绑（import 后）: %d 处", n)
    if n == 0:
        log.info(
            "sampler 重绑 0 处属正常：backend 是 load_model 时才 import 的，"
            "那时 mlx_lm.sample_utils 已经是自由版"
        )

    aliases = install_model_aliases()
    if not aliases:
        log.warning(
            "未设 MLX_AUDIO_MODEL_ALIASES：后端发短名时 mlx_audio 会尝试从 "
            "HuggingFace 下载模型"
        )

    # ref_audio 适配层挂在最外层：它要在 FastAPI 解析 pydantic 之前改写 body。
    #
    # 这里没有调 mlx_audio.server.main()：它内部是
    # `uvicorn.run("mlx_audio.server:app", ...)`，用**字符串**导入 app，
    # 那样会绕过我们上面挂的适配层。自己 run 并直接传 app 对象才可控。
    import uvicorn

    # 挂载顺序：ErrorDetailSanitizer 在最外层，才能兜住内层一切 5xx
    # （内层抛出的异常会冒到 ASGI 层，被这里统一转成中性提示）。
    app = ErrorDetailSanitizer(RefAudioAdapter(server.app))
    log.info("ref_audio base64→文件 适配层已挂载（目录 %s）", _REF_DIR)
    log.info("5xx 内部细节脱敏层已挂载（调用方只看到中性提示）")
    log.info("监听 http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, workers=1, loop="asyncio")


if __name__ == "__main__":
    main()
