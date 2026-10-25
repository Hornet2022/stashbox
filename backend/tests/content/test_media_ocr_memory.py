"""正文配图识图的**内存闸门**（2026-10-25 生产事故回归）。

## 事故经过（实测，不是推演）

2026-10-25 12:23，内测用户剪藏一篇微信文（`mp.weixin.qq.com/s/PoTh33...`）：

    12:23:50  抓取成功（tier=http）
    12:23:52  12 张配图下载完（合计 9.1MB）
    12:23:56  开始识图（VLM 调用）
    12:24:02  最后一次 VLM 调用
    …         然后**再没有任何日志** —— 请求没返回，也没报错

生产 content-service 的 cgroup 限额是 `MemoryHigh=128M / MemoryMax=176M`，
而那个进程当时的 RSS 是 166MB ⇒ **已经越过 high 水位**。
内核的 cgroup 回收路径把进程**冻在 D 状态**（`wchan=mem_cgroup_handle_over_high`）：

    ① 剪藏请求永远不返回（用户看到转圈到天荒地老）
    ② **整个 content-service 都不响应了** —— 连 `GET /api/v1/articles`
       （列表查询，几毫秒的事）也一起 45s 超时
    ③ 进程在 D 状态 ⇒ **SIGKILL 都杀不掉** ⇒ 不会自愈、不会告警

`memory.events` 里 `high 19414`（超水位被节流 19414 次）、`oom 0`
（压根没到 OOM killer 那一步）—— 也就是说：**软限不是「回收压力」，
是「永久冻死」**。

## 根因与修法

`_ocr_one_image` 把**原始图片字节**直接 base64 塞进请求体 ⇒
每张图的字节被复制 4~5 遍（原始 → base64 串 → JSON 串 → utf-8 字节 → 请求体）。
12 张图 ⇒ 瞬时几十 MB。实测同一篇文章：

| | 修前 | 修后 |
|---|---|---|
| 稳态边际成本（第 2 次剪藏的 RSS 增量） | +48MB | **+21MB** |

两道闸门：

1. **缩图后再送 VLM**（`ocr.shrink_image_for_vlm`）——
   请求体降一个数量级，**顺带省视觉 token**（VLM 的视觉 token 按图面积算）。
   收口在 `_ocr_one_image`（**唯一**拼请求体的地方），不放在调用点。
2. **单篇配图总字节预算**（`media_ocr.MAX_MEDIA_TOTAL_BYTES`）——
   只限张数限不住内存（12×5MB 与 12×50KB 在「张数」上一样，内存差 10 倍），
   有了总量闸，**内存上界与文章有多少张图解耦**。

## 全部不打真网络

同 `test_media_ocr.py`：图片下载与识图请求都走 `httpx.MockTransport`。
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import io
import os
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
CONTENT_DIR = BACKEND_DIR / "content-service"
if str(CONTENT_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_DIR))

_spec = importlib.util.spec_from_file_location(
    "cs_mem_fetchers",
    CONTENT_DIR / "fetchers" / "__init__.py",
    submodule_search_locations=[str(CONTENT_DIR / "fetchers")],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cs_mem_fetchers"] = _mod
_spec.loader.exec_module(_mod)

media_ocr = importlib.import_module("cs_mem_fetchers.media_ocr")
ocr_mod = importlib.import_module("cs_mem_fetchers.ocr")

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload" * 8


class _NoThrottle:
    async def wait(self, host: str) -> float:  # noqa: ARG002
        return 0.0


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(media_ocr, "HOST_THROTTLE", _NoThrottle())


def _config_stub():
    async def _stub():
        return "https://provider.example/v1", "k", "m1"

    return _stub


@pytest.fixture
def fake_provider(monkeypatch):
    monkeypatch.setattr(ocr_mod, "_resolve_config", _config_stub())


def _noisy_png(width: int = 900, height: int = 1100) -> bytes:
    """一张**不可压缩**的 PNG（随机像素）⇒ 字节数接近原始尺寸。

    纯色 PNG 会被压成几 KB，测不出「缩图省了多少」。
    """
    from PIL import Image

    raw = os.urandom(width * height * 3)
    img = Image.frombytes("RGB", (width, height), raw)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _b64_size_of_request(request: httpx.Request) -> int:
    """从 provider 请求体里取出 base64 图片的**解码后**字节数。"""
    body = json.loads(request.content)
    content = body["messages"][0]["content"]
    data_url = content[1]["image_url"]["url"]
    _, b64 = data_url.split(",", 1)
    return len(base64.b64decode(b64))


import json  # noqa: E402 —— 只为上面的请求体解析（放在函数后面避免顶格）


# ---------------------------------------------------------------------------
# 1. 缩图：送进 VLM 的必须是缩过的
# ---------------------------------------------------------------------------
def test_大图被缩到_vlm_用得上的尺寸():
    """一张 3MB 的 PNG → 缩到最长边 1536 且字节数大降。"""
    big = _noisy_png()
    assert len(big) > ocr_mod.VLM_SHRINK_MIN_BYTES, "样本要够大才有意义"

    out, mime = ocr_mod.shrink_image_for_vlm(big, "image/png")

    assert mime == "image/jpeg"
    assert len(out) < len(big) / 4, f"缩图后应显著变小：{len(big)} → {len(out)}"

    from PIL import Image

    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) <= ocr_mod.VLM_MAX_SIDE, f"最长边应 ≤ {ocr_mod.VLM_MAX_SIDE}：{im.size}"


def test_小图原样送_不做无谓的有损转码():
    """小于阈值的图**不动** —— 省 CPU，也不掉画质。"""
    small = PNG_BYTES
    out, mime = ocr_mod.shrink_image_for_vlm(small, "image/png")
    assert out is small
    assert mime == "image/png"


def test_解不开的图原样送_缩图失败不许让识图失败():
    """缩图是优化不是功能：解不开的字节照样该送。"""
    junk = b"not-an-image-at-all" * 1000
    out, mime = ocr_mod.shrink_image_for_vlm(junk, "image/png")
    assert out is junk
    assert mime == "image/png"


def test_透明底图贴白底_不能把透明区变黑():
    """透明 PNG 直接转 RGB 会让文字糊在黑底上 ⇒ 先贴白底。"""
    from PIL import Image

    # 全透明但 RGB 随机 ⇒ 不可压缩（字节数够大，缩图会真的触发），
    # 同时保留「透明」这个待验证的性质。
    size = 700
    data = bytearray(os.urandom(size * size * 4))
    for i in range(3, len(data), 4):
        data[i] = 0  # alpha = 0
    buf = io.BytesIO()
    Image.frombytes("RGBA", (size, size), bytes(data)).save(buf, format="PNG")
    raw = buf.getvalue()
    assert len(raw) > ocr_mod.VLM_SHRINK_MIN_BYTES, "样本要够大才会触发缩图"

    out, _mime = ocr_mod.shrink_image_for_vlm(raw, "image/png")
    with Image.open(io.BytesIO(out)) as im:
        assert im.convert("RGB").getpixel((5, 5))[0] > 200, "透明区应变白而不是黑"


@pytest.mark.asyncio
async def test_provider_收到的是缩过的图(fake_provider):
    """**端到端**：provider 拿到的图片字节数必须远小于原图。

    这是「修复前红」的那条：缩图前 provider 收到的是原始 3MB。
    """
    big = _noisy_png(600, 700)
    assert (
        ocr_mod.VLM_SHRINK_MIN_BYTES < len(big) <= media_ocr.MAX_MEDIA_BYTES
    ), f"样本要落在「够大该缩、又没超单图上限」之间：{len(big)} 字节"
    seen: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            seen["image_bytes"] = _b64_size_of_request(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "图注"}}]})
        return httpx.Response(200, content=big)

    refs = [{"url": "https://cdn.example.com/fig1.png", "token": "\x00IMG1\x00"}]
    await media_ocr.caption_media(refs, transport=httpx.MockTransport(handler))

    assert seen["image_bytes"] < len(big) / 4, (
        f"provider 收到的应是缩过的图：原图 {len(big)} 字节，"
        f"实际收到 {seen['image_bytes']} 字节"
    )


# ---------------------------------------------------------------------------
# 2. 单篇配图总字节预算
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_超过单篇字节预算的图不再下载(fake_provider, monkeypatch):
    """总量闸：预算用完 ⇒ 后面的图**不下载**（记原因）。"""
    monkeypatch.setattr(media_ocr, "MAX_MEDIA_TOTAL_BYTES", 8 * 1024)
    downloaded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "图注"}}]})
        downloaded.append(str(request.url))
        return httpx.Response(200, content=PNG_BYTES + b"x" * 3000)

    refs = [
        {"url": f"https://cdn.example.com/fig{i}.png", "token": f"\x00IMG{i}\x00"} for i in range(6)
    ]
    out = await media_ocr.caption_media(refs, transport=httpx.MockTransport(handler))

    # 每张图 3KB，预算 8KB ⇒ 最多 3 张能下完
    assert len(downloaded) <= 4, f"预算 8KB 不该下载这么多张：{len(downloaded)}"
    over = [c for c in out if "字节预算" in (c.get("error") or "")]
    assert over, "超预算的图应记下原因（而不是静默失败）"


@pytest.mark.asyncio
async def test_预算内_图照常识(fake_provider, monkeypatch):
    """预算够时行为不变（图照常下载、照常出图注）。"""
    monkeypatch.setattr(media_ocr, "MAX_MEDIA_TOTAL_BYTES", 8 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "图注"}}]})
        return httpx.Response(200, content=PNG_BYTES)

    refs = [
        {"url": f"https://cdn.example.com/fig{i}.png", "token": f"\x00IMG{i}\x00"} for i in range(3)
    ]
    out = await media_ocr.caption_media(refs, transport=httpx.MockTransport(handler))
    assert all(c.get("caption") == "图注" for c in out), out


# ---------------------------------------------------------------------------
# 3. 生产常量不许退回旧值（防回归）
# ---------------------------------------------------------------------------
def test_单图上限不许退回_5mb():
    """5MB × (队列 3 + 在途 4) = 35MB 的下载侧峰值，太肥。

    缩图后大图会被压到 ~250KB，原图只在下载那一瞬间在内存里 ⇒ 2MB 足够。
    """
    assert media_ocr.MAX_MEDIA_BYTES <= 2 * 1024 * 1024


def test_单篇总字节预算存在且有界():
    """没有总量闸 ⇒ 内存上界与文章图数耦合（12 张 5MB 的文章就顶穿水位线）。"""
    assert 0 < media_ocr.MAX_MEDIA_TOTAL_BYTES <= 8 * 1024 * 1024


def test_缩图上限不许无限放大():
    """缩图是为了省内存与 token；上限太大就失去意义了。"""
    assert 0 < ocr_mod.VLM_MAX_SIDE <= 2048
