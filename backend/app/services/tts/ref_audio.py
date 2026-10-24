"""参考音频加载：本地路径 / http(s) URL → 字节 → data: URL。

## 为什么单独抽一个模块

「音色」在本项目里不是**一个名字**，而是「(参考音频, 参考文本) 对」——
零样本克隆的音色由参考音频决定（见 `indextts.py` 的模块说明）。
于是「把参考音频喂给上游」这件事在两个 provider 里都要做：

- `indextts.py`（自建 mlx-audio）：传**裸 base64**
- `openai.py`（OpenAI 兼容托管端点）：传 **data: URL**

两边都要处理同一批输入形态（本地绝对路径 / http(s) URL / 已经是 data URL），
所以这段逻辑**只写一份**放在这里，避免各抄一份然后慢慢漂移
（这正是本次要修的那个 bug 的同类形状：同一个能力两套实现，
其中一套没跟上，于是「我们没传」被误当成「端点不支持」）。

## 形态对照（实测，2026-10-24 于生产端点 tertu.cn）

    ref_audio = 裸 base64            → 上游 504（超时，不报错 → 最难查的一种）
    ref_audio = data:audio/wav;base64 → 200
    ref_audio 缺 ref_text            → 400，且报错里点名 ref_audio 的形态要求

⇒ **data URL 不是编码洁癖，是这个端点的硬要求。**
裸 base64 会走到「上游拿不到音频 → 一直等 → 60s 墙钟砍掉」，
表现是 504 而**不是** 400：请求本身是合法的，只是内容上游认不出来。
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# 参考音频大小上限（与音色库服务层的 MAX_REF_AUDIO_BYTES 同量级）。
#
# 零样本克隆的参考音频通常 3~15s；24kHz 单声道 16bit 约 1 分钟 ≈ 2.9MB。
# 给到 10MB 足够宽松，再大基本是误传了整篇文章音频
# （实测蒸馏产物就有 27MB —— 那是成稿，不是参考音）。
MAX_REF_AUDIO_BYTES = 10 * 1024 * 1024

# 小于这个字节数基本不可能是有效音频（wav 头 + 一点波形）。
MIN_REF_AUDIO_BYTES = 1000

# MIME 嗅探：魔数 → mime。
#
# ⚠️ 用**内容**而不是扩展名判定：`ref_audio_url` 既可能是 OSS 上的
# `.wav`，也可能是本地路径，还可能是别人塞进来的 `.mp3`。
# 而上游要按 mime 决定怎么解码（有的端点只认 audio/wav），
# 猜错的后果是「上游解不出波形」—— 那种失败往往表现为超时而不是明确报错。
_MIME_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"RIFF", "audio/wav"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"\xff\xf3", "audio/mpeg"),
    (b"\xff\xf2", "audio/mpeg"),
)

# 扩展名兜底（魔数认不出来时用）。
_MIME_BY_SUFFIX: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
}


class RefAudioError(RuntimeError):
    """参考音频拿不到 / 不像音频 / 形态不对。"""


def detect_audio_mime(data: bytes, source: str = "") -> str:
    """猜这段音频的 mime。**先看魔数**，再看扩展名，最后兜底 audio/wav。"""
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    suffix = Path(source.split("?", 1)[0]).suffix.lower()
    return _MIME_BY_SUFFIX.get(suffix, "audio/wav")


def to_data_url(data: bytes, mime: str = "audio/wav") -> str:
    """字节 → ``data:<mime>;base64,<...>``。"""
    payload = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{payload}"


def is_data_url(source: str) -> bool:
    return source.strip().lower().startswith("data:")


def _read_local(path_str: str) -> bytes:
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise RefAudioError(f"参考音频文件不存在: {path}")
    return path.read_bytes()


async def _read_url(url: str, http_client, timeout: float) -> bytes:
    import httpx

    client = http_client
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(trust_env=False)
    try:
        resp = await client.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RefAudioError(f"参考音频下载失败: {url} ({exc})") from exc
    finally:
        if owns_client:
            await client.aclose()
    if resp.status_code != 200:
        raise RefAudioError(f"参考音频下载失败 HTTP {resp.status_code}: {url}")
    return resp.content


async def load_ref_audio_bytes(
    source: str,
    *,
    http_client=None,
    timeout: float = 30.0,
) -> bytes:
    """把参考音频来源读成字节。

    支持三种来源（音色库里存的就是这几种）：

    - **本地绝对路径** —— 历史行为（`indextts_ref_audio` 配的就是这个）
    - **http(s) URL** —— 音色库上传后落在存储上的地址
    - **data: URL** —— 直接解回来（已经是 base64 形态，别再编一次）

    失败一律抛 `RefAudioError`（调用方各自包成自己的异常类型）。
    """
    src = (source or "").strip()
    if not src:
        raise RefAudioError("参考音频来源为空")

    if is_data_url(src):
        raw = src.split(",", 1)[1] if "," in src else ""
        try:
            data = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise RefAudioError(f"参考音频 data URL 解码失败: {exc}") from exc
    elif src.startswith(("http://", "https://")):
        data = await _read_url(src, http_client, timeout)
    elif src.startswith("file://"):
        data = _read_local(src[len("file://") :])
    else:
        data = _read_local(src)

    if len(data) < MIN_REF_AUDIO_BYTES:
        raise RefAudioError(f"参考音频太小({len(data)}B)，可能不是有效音频: {src[:80]}")
    if len(data) > MAX_REF_AUDIO_BYTES:
        raise RefAudioError(
            f"参考音频过大({len(data) // 1024 // 1024}MB)，上限 "
            f"{MAX_REF_AUDIO_BYTES // 1024 // 1024}MB: {src[:80]}"
        )
    return data


async def load_ref_audio_data_url(
    source: str,
    *,
    http_client=None,
    timeout: float = 30.0,
) -> str:
    """参考音频来源 → ``data:<mime>;base64,...``（一步到位）。"""
    src = (source or "").strip()
    if is_data_url(src):
        # 已经是 data URL：只校验体积，不重复编码
        data = await load_ref_audio_bytes(src, http_client=http_client, timeout=timeout)
        return to_data_url(data, detect_audio_mime(data, src))
    data = await load_ref_audio_bytes(src, http_client=http_client, timeout=timeout)
    mime = detect_audio_mime(data, src)
    log.info("参考音频已加载: source=%s bytes=%d mime=%s", src[:80], len(data), mime)
    return to_data_url(data, mime)


def default_ref_audio_path() -> str:
    """本机默认参考音频（仅供本地开发/探针用，生产不要依赖它）。

    仓库里带了一份 `backend/data/voices/tingting_ref.wav` ——
    它是**开发资产**，不是部署契约：换台机器这个路径就不存在了。
    生产上的音色由音色库（`tts_voices`）提供。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data" / "voices" / "tingting_ref.wav"
        if candidate.is_file():
            return str(candidate)
    return os.getenv("STASHBOX_DEFAULT_REF_AUDIO", "")
