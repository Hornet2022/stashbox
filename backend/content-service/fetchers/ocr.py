"""扫描件 PDF 的 OCR（2026-10-24）。

## 为什么需要它

扫描件 PDF **没有文本层** ⇒ `pypdf` 抽不出文字 ⇒ 之前一律报「读不出正文」。
而模型 provider 侧**确实有能认字的模型**（用户 2026-10-24 明确要求支持 OCR）。

## 路径是**实测**出来的，不是猜的

provider：`https://tertu.cn/v1`（OpenAI 兼容）。实测结论：

1. ❌ **PDF 不能直接喂给模型** ——
   `data:application/pdf;base64,...` 会被上游拒：
   `400 {"error":"Failed to load image: cannot identify image file"}`
   ⇒ 必须先转成图片。
2. ✅ **不引入光栅化依赖** —— 扫描件 PDF 本质就是「一张张图片装在 PDF 里」，
   `pypdf` 的 `page.images` 能把内嵌图**原样**抽出来（零重编码、零画质损失）。
   实测：抽出 `image.jpg` → 识图 → 文本完全正确
   （`'Scanned document 2026\\nTotal cost was 1200 yuan'`）。
3. ✅ 模型清单（provider `GET /v1/models`）里有两个能认字的：
   - `Qwen/Qwen3-VL-32B-Instruct` —— 输出**干净**（纯文本，无 markdown 标记），
     prompt_tokens 更省（实测 270 vs 847）⇒ **默认用它**；
   - `deepseek-ai/DeepSeek-OCR-2` —— 专用 OCR 模型，但输出带 markdown 标记
     （`'## Hello OCR 2026'`）且 token 更多 ⇒ 备选。

   ⚠️ 注意 provider 上**没有** `qwen-vl-max` / `gpt-4o` 这两个名字
   （后台 tier 映射里写着它们，但实测 404 `Model not found`）——
   模型名必须**向 provider 现查**，不能照抄配置里的旧值。

## 边界（明确的取舍）

* **只对 PDF 生效**：图片文件（jpg/png）本身就该直接识图，不走这里；
* **页数上限** `MAX_OCR_PAGES`：扫描件动辄上百页，全识一遍既慢又烧钱；
* **每页图片数上限**：一页里可能有多个小图（印章、logo），只取最大的几张；
* **失败不静默**：识图失败会记 warning，并写进 `raw_metadata`，
  让「这篇为什么没正文」查得到原因。
"""

from __future__ import annotations

import base64
import logging
import os
from io import BytesIO
from typing import Any

import httpx

log = logging.getLogger("stashbox.fetch.ocr")


#: 默认 OCR 模型（provider `GET /v1/models` 实测存在，且输出最干净）。
DEFAULT_OCR_MODEL = "Qwen/Qwen3-VL-32B-Instruct"

#: 识图最多处理多少页（扫描件动辄上百页）。
MAX_OCR_PAGES = 20

#: 每页最多取几张图（只取最大的几张 —— 印章/logo 那种小图识别不出正文）。
MAX_IMAGES_PER_PAGE = 3

#: 单张图的识图超时（秒）。
OCR_TIMEOUT = 90.0

#: 识图的提示词。**要纯文本** —— 模型爱加 markdown 标记（`##`、`###`），
#: 而正文里出现这些符号会被 TTS 当成怪东西念出来。
_OCR_PROMPT = "识别图片里的全部文字，只输出文字本身，不要输出任何标记或解释。"


class OCRUnavailableError(RuntimeError):
    """OCR 通道不可用（没配 provider / 模型不存在 / 网络失败）。"""


async def _resolve_config() -> tuple[str, str, str]:
    """(base_url, api_key, model) —— DB > env > 默认。

    base_url / api_key **复用 LLM 的那份配置**（同一个 provider、同一个 key）
    ⇒ 运营在后台改 LLM 配置，OCR 自动跟着走，不需要配第二套凭据。

    模型名可以单独覆盖：
    - `system_config.llm.ocr_model`（后台改，不用重启）；
    - 环境变量 `STASHBOX_OCR_MODEL`；
    - 默认 `Qwen/Qwen3-VL-32B-Instruct`。
    """
    base_url = ""
    api_key = ""
    try:
        from stashbox.backend.app.services.llm import current_config

        cfg = await current_config()
        base_url = str(cfg.get("base_url") or cfg.get("openai_llm_base_url") or "")
        api_key = str(cfg.get("api_key") or cfg.get("openai_llm_api_key") or "")
    except Exception as exc:  # pragma: no cover - 配置层异常不该让解析崩
        log.warning("ocr_config_resolve_failed: %s", exc)

    if not base_url:
        base_url = os.getenv("OPENAI_LLM_BASE_URL", "") or ""
    if not api_key:
        api_key = os.getenv("OPENAI_LLM_API_KEY", "") or ""

    model = ""
    try:
        from stashbox.backend.common.system_config import KEY_LLM, get_config

        stored = await get_config(KEY_LLM) or {}
        model = str(stored.get("ocr_model") or "")
    except Exception:  # pragma: no cover
        pass
    model = model or (os.getenv("STASHBOX_OCR_MODEL", "") or "") or DEFAULT_OCR_MODEL

    return base_url.rstrip("/"), api_key, model


def _extract_page_images(data: bytes, *, max_pages: int) -> list[tuple[bytes, str]]:
    """扫描件 PDF → 每页的内嵌图 [(图片字节, mime)]。

    **只取每页最大的几张**（按像素面积）：一页里的小图通常是印章/logo，
    识别它们既费 token 又没有正文。

    用 pypdf 而不是光栅化（pdf2image/poppler、PyMuPDF）：
    扫描件 PDF 的图**本来就在文件里**，原样抽出来零损失、零新依赖。
    """
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    out: list[tuple[bytes, str]] = []
    for page_no, page in enumerate(reader.pages):
        if page_no >= max_pages:
            break
        try:
            images = list(page.images)
        except Exception as exc:
            log.warning("ocr_page_images_failed page=%d: %s", page_no + 1, exc)
            continue
        if not images:
            continue
        # 按像素面积排序，取最大的几张
        sized: list[tuple[int, bytes, str]] = []
        for img in images:
            try:
                data_bytes = img.data
                width = int(getattr(img.image, "width", 0) or 0)
                height = int(getattr(img.image, "height", 0) or 0)
            except Exception as exc:
                log.warning("ocr_image_read_failed page=%d: %s", page_no + 1, exc)
                continue
            sized.append((width * height, data_bytes, f"image/{_ext_for(data_bytes)}"))
        sized.sort(key=lambda x: -x[0])
        for _, blob, mime in sized[:MAX_IMAGES_PER_PAGE]:
            if blob:
                out.append((blob, mime))
    return out


#: 图片魔数 → mime。**按字节判断**，不看扩展名/Content-Type
#: （实测 pypdf 的 image.name 不可靠 —— 有时是 'image.jpg'；
#: 图床返回的 Content-Type 也经常是 application/octet-stream）。
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def image_mime_for(blob: bytes) -> str | None:
    """按魔数判图片类型；**不是图片**返回 None。

    为什么要有"返回 None"这个分支（而不是像 `_ext_for` 那样兜底成 jpeg）：
    图床在防盗链/404 时会返回一段 **HTML**，把它当成 jpeg 塞给识图模型
    只会得到一段胡说，还白烧 token。认不出来就当"不是图片"，让调用方跳过。
    """
    if not blob:
        return None
    for magic, mime in _IMAGE_MAGIC:
        if blob.startswith(magic):
            return mime
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ext_for(blob: bytes) -> str:
    """按魔数猜图片类型（pypdf 的 image.name 不可靠 —— 实测有时是 'image.jpg'）。"""
    mime = image_mime_for(blob)
    if not mime:
        return "jpeg"
    return mime.split("/", 1)[1]


async def _ocr_one_image(
    client: httpx.AsyncClient,
    *,
    image: bytes,
    mime: str,
    model: str,
    prompt: str = _OCR_PROMPT,
) -> str:
    """单张图 → 文字。

    `prompt` 默认走扫描件那版；正文配图识图会传自己那版
    （图表不是纯文字段落，见 `media_ocr._MEDIA_OCR_PROMPT`）。
    请求体的拼装**只有这一份** —— 复制一份的话，provider 改了协议
    （比如 `image_url` 的写法）就会只改到一半，另一半静默失配。
    """
    b64 = base64.b64encode(image).decode("ascii")
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 2048,
        "temperature": 0,
    }
    resp = await client.post("/chat/completions", json=body)
    if resp.status_code != 200:
        raise OCRUnavailableError(f"ocr http {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return str(data["choices"][0]["message"]["content"] or "").strip()


async def ocr_config() -> tuple[str, str, str]:
    """(base_url, api_key, model) —— `_resolve_config` 的公开别名。

    给别的模块判断"识图通道是否可用"用：没配 provider 时就不该去下载图片
    （白下载一遍，最后还是识不了）。
    """
    return await _resolve_config()


async def ocr_image_bytes(
    blob: bytes,
    mime: str,
    *,
    prompt: str | None = None,
    timeout: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """单张图 → 文字（公开入口，供正文配图识图复用）。

    与 `ocr_pdf` **共用同一个 provider 通道**（同一个 base_url / api_key / 模型），
    所以运营在后台改 LLM 配置，识图自动跟着走，不需要配第二套凭据。
    """
    base_url, api_key, model = await _resolve_config()
    if not base_url or not api_key:
        raise OCRUnavailableError("没配置 LLM provider（base_url/api_key），无法识图")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout if timeout is not None else OCR_TIMEOUT,
        trust_env=False,
        transport=transport,
    ) as client:
        return await _ocr_one_image(
            client,
            image=blob,
            mime=mime,
            model=model,
            prompt=prompt or _OCR_PROMPT,
        )


async def ocr_pdf(
    data: bytes,
    *,
    max_pages: int = MAX_OCR_PAGES,
) -> tuple[str, dict[str, Any]]:
    """扫描件 PDF → (识别出的文本, 元数据)。

    Returns:
        ``(text, metadata)``；``metadata["ocr"]`` 里有识别了几页/几张图/用的哪个模型，
        出问题时查得到原因。
    """
    base_url, api_key, model = await _resolve_config()
    if not base_url or not api_key:
        raise OCRUnavailableError("没配置 LLM provider（base_url/api_key），无法 OCR")

    images = _extract_page_images(data, max_pages=max_pages)
    metadata: dict[str, Any] = {
        "ocr_model": model,
        "ocr_images": len(images),
        "ocr_pages_scanned": min(max_pages, len(images)) if images else 0,
    }
    if not images:
        raise OCRUnavailableError("PDF 里没抽出任何内嵌图片（可能不是扫描件）")

    texts: list[str] = []
    errors: list[str] = []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, timeout=OCR_TIMEOUT, trust_env=False
    ) as client:
        for blob, mime in images:
            try:
                t = await _ocr_one_image(client, image=blob, mime=mime, model=model)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                log.warning("ocr_image_failed: %s", exc)
                continue
            if t:
                texts.append(t)

    metadata["ocr_errors"] = errors
    metadata["ocr_ok_images"] = len(texts)
    return "\n\n".join(texts), metadata


__all__ = [
    "DEFAULT_OCR_MODEL",
    "MAX_OCR_PAGES",
    "OCRUnavailableError",
    "image_mime_for",
    "ocr_config",
    "ocr_image_bytes",
    "ocr_pdf",
]
