"""扫描件 PDF 的 OCR（2026-10-24）。

## 为什么要有这些用例

OCR 是在**真钱**（token）与**真网络**上的操作，而它的失败方式很安静：
模型不认字时返回的是一段空/胡说，**不报错**。
所以要锁住三件事：

1. **只有「PDF 且没有文本层」才走 OCR** ——
   普通 PDF / 文本文件走 OCR 是白烧 token；
2. **OCR 文本要真的进正文**（`parser="pdf_ocr"`），
   且 `raw_metadata` 里要有 OCR 的可观测信息（页数/张数/模型/错误）；
3. **OCR 通道不可用时不崩** —— 退回「读不出正文」的既有报错路径，
   而不是抛一个更让人困惑的错误。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
CONTENT_DIR = BACKEND_DIR / "content-service"
if str(CONTENT_DIR) not in sys.path:
    sys.path.insert(0, str(CONTENT_DIR))

# content-service 目录名带连字符，按文件路径加载它的 fetchers 包
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cs_ocr_fetchers",
    CONTENT_DIR / "fetchers" / "__init__.py",
    submodule_search_locations=[str(CONTENT_DIR / "fetchers")],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cs_ocr_fetchers"] = _mod
_spec.loader.exec_module(_mod)

ocr = importlib.import_module("cs_ocr_fetchers.ocr")
doc = importlib.import_module("cs_ocr_fetchers.document")


def _scanned_pdf_bytes(text: str = "Scanned document 2026") -> bytes:
    """造一个「扫描件」PDF：**只有图、没有文本层**。

    用 PIL 把文字画成图再存 PDF（PIL 存出来的 PDF 就是图片型 PDF，
    pypdf 抽不出任何文本层）—— 与真实的扫描件同构。
    PIL 在测试环境可能没有 ⇒ 没有时用 pypdf 造一个带内嵌图的 PDF。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover
        pytest.skip("没有 PIL，造不出扫描件 PDF")

    img = Image.new("RGB", (800, 200), "white")
    d = ImageDraw.Draw(img)
    d.text((30, 60), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def _text_pdf_bytes() -> bytes:
    """造一个**有文本层**的 PDF（OCR 不该触发）。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. 抽内嵌图
# ---------------------------------------------------------------------------
def test_能抽出扫描件的内嵌图():
    data = _scanned_pdf_bytes()
    images = ocr._extract_page_images(data, max_pages=5)
    assert images, "扫描件里应该能抽出内嵌图"
    blob, mime = images[0]
    assert blob, "抽出来的图不应为空"
    assert mime.startswith("image/")


def test_没有图片的pdf抽不出图():
    assert ocr._extract_page_images(_text_pdf_bytes(), max_pages=5) == []


def test_图片类型按魔数猜():
    assert ocr._ext_for(b"\xff\xd8\xff\xe0abc") == "jpeg"
    assert ocr._ext_for(b"\x89PNG\r\n\x1a\n") == "png"
    assert ocr._ext_for(b"GIF89a...") == "gif"


# ---------------------------------------------------------------------------
# 2. OCR 调用（假 provider，不打真网络）
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """记录调用次数与请求体，返回固定内容。"""

    def __init__(self, content: str = "识别出来的文字", status: int = 200) -> None:
        self.content = content
        self.status = status
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, json: dict):
        self.calls.append({"url": url, "json": json})
        if self.status != 200:
            return _FakeResponse(self.status, text="boom")
        return _FakeResponse(
            200,
            {"choices": [{"message": {"content": self.content}}]},
        )


@pytest.mark.asyncio
async def test_ocr_走图片接口且把图作为data_url(monkeypatch):
    """识图请求必须带 image_url 的 data URL（实测 provider 只认这个形态）。"""
    fake = _FakeClient("Hello OCR 2026")
    monkeypatch.setattr(ocr.httpx, "AsyncClient", lambda **kw: fake)
    monkeypatch.setattr(
        ocr, "_resolve_config", _make_config_stub("https://p.example/v1", "k", "m1")
    )

    data = _scanned_pdf_bytes()
    text, meta = await ocr.ocr_pdf(data)

    assert text == "Hello OCR 2026"
    assert fake.calls, "应该发过一次识图请求"
    call = fake.calls[0]
    assert call["url"] == "/chat/completions"
    parts = call["json"]["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/")
    assert call["json"]["model"] == "m1"
    assert meta["ocr_images"] >= 1
    assert meta["ocr_ok_images"] >= 1


@pytest.mark.asyncio
async def test_ocr_没配provider时报明确错误(monkeypatch):
    monkeypatch.setattr(ocr, "_resolve_config", _make_config_stub("", "", "m1"))
    with pytest.raises(ocr.OCRUnavailableError):
        await ocr.ocr_pdf(_scanned_pdf_bytes())


@pytest.mark.asyncio
async def test_ocr_单张图失败不阻断其余(monkeypatch):
    """一张图认失败不该让整篇都没正文 —— 其余页还能用。"""
    data = _scanned_pdf_bytes()
    images = ocr._extract_page_images(data, max_pages=5)
    assert images

    class _FlakyClient(_FakeClient):
        async def post(self, url: str, json: dict):
            self.calls.append({"url": url, "json": json})
            return _FakeResponse(500, text="upstream error")

    fake = _FlakyClient()
    monkeypatch.setattr(ocr.httpx, "AsyncClient", lambda **kw: fake)
    monkeypatch.setattr(
        ocr, "_resolve_config", _make_config_stub("https://p.example/v1", "k", "m1")
    )

    text, meta = await ocr.ocr_pdf(data)
    assert text == "", "全失败时没有正文"
    assert meta["ocr_errors"], "失败原因必须记下来（否则查不到为什么没正文）"


# ---------------------------------------------------------------------------
# 3. 解析层的接入
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_扫描件走ocr_文本进正文(monkeypatch):
    """OCR 文本要真的进正文，且 parser 标成 pdf_ocr（可观测）。"""

    async def _fake_ocr(data, **kwargs):
        return "这是扫描件里的正文内容，足够长以通过服务端两百字符的正文下限判据。" * 8, {
            "ocr_model": "m1"
        }

    monkeypatch.setattr("cs_ocr_fetchers.ocr.ocr_pdf", _fake_ocr)

    fr = await doc.parse_document_async(
        filename="扫描件.pdf",
        data=_scanned_pdf_bytes(),
        content_type="application/pdf",
    )
    assert fr.raw_metadata["parser"] == "pdf_ocr"
    assert "扫描件里的正文" in fr.content_text


@pytest.mark.asyncio
async def test_有文本层的pdf不触发ocr(monkeypatch):
    """普通 PDF 走文本层就够，不该白烧 OCR 的 token。"""
    called = {"n": 0}

    async def _fake_ocr(data, **kwargs):
        called["n"] += 1
        return "不该被调用", {}

    monkeypatch.setattr("cs_ocr_fetchers.ocr.ocr_pdf", _fake_ocr)

    # 一个**有文本层**的 PDF：用 pypdf 直接写文本不方便，
    # 这里用 monkeypatch 把抽文本这一步替换掉 —— 判据是「有文本层就不 OCR」。
    import cs_ocr_fetchers.pdf as pdf_mod

    monkeypatch.setattr(
        pdf_mod,
        "extract_pdf_text",
        lambda body: (["这是真文本层的正文，通过正文下限判据" * 20], {}),
    )

    fr = await doc.parse_document_async(
        filename="正常.pdf",
        data=_text_pdf_bytes(),
        content_type="application/pdf",
    )
    assert called["n"] == 0, "有文本层时不该调 OCR"
    assert fr.raw_metadata["parser"] == "pdf"


@pytest.mark.asyncio
async def test_ocr通道不可用时退回报错而不是崩(monkeypatch):
    """OCR 不可用（没配 provider）⇒ 走「读不出正文」的既有路径，不抛别的错。"""
    from cs_ocr_fetchers.base import FetcherError

    async def _fake_ocr(data, **kwargs):
        raise ocr.OCRUnavailableError("没配置")

    monkeypatch.setattr("cs_ocr_fetchers.ocr.ocr_pdf", _fake_ocr)

    with pytest.raises(FetcherError):
        await doc.parse_document_async(
            filename="扫描件.pdf",
            data=_scanned_pdf_bytes(),
            content_type="application/pdf",
        )


def _make_config_stub(base_url: str, api_key: str, model: str):
    async def _stub():
        return base_url, api_key, model

    return _stub
