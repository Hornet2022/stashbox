"""文档剪藏：把**用户上传的字节**解析成和抓取同形的 [FetchResult]。

## 为什么是「解析器」而不是「Fetcher」

现有 `Fetcher` 的契约是「给我一个 URL，我去抓」（`fetchers/base.py`）——
它必然先下载、再解析。而文档剪藏的输入**已经是字节**（用户上传的文件）。

硬把它做成 Fetcher 会有两个坏处：
1. 得先把上传的文件落到某个能被 HTTP 取到的地方（OSS）→ 引入「presigned URL
   会过期」「原文长期可读」等一串新问题；
2. 上传路径与抓取路径**各写一份解析** → 同一套 PDF 解析逻辑两份实现，
   很快漂移（加密 PDF 的处理、页级容错、标题启发式都要各自演进）。

所以这里做成**纯解析器**：输入字节，输出 [FetchResult]（**与 fetcher 同形**）。
下游（`articles.raw_content` → 蒸馏链路）因此**完全不用改** ——
这是本设计最关键的一点：`raw_content` 的形状不变，蒸馏、订阅、TTS 全都复用。

## 支持的格式（第一版）

| 格式 | 解析方式 | 依赖 |
|---|---|---|
| PDF | 复用 [fetchers.pdf.extract_pdf_text]（与 PDF 直链抓取**同一份**逻辑） | pypdf（已有） |
| Markdown / TXT / 其它纯文本 | 直接解码 | 无 |
| HTML | 复用 [fetchers.parser.ContentExtractor]（与网页抓取**同一份**抽取器） | 无 |
| DOCX | 标准库 `zipfile` + `xml.etree`（**不引入 python-docx**） | 无 |

**不支持**：扫描件 PDF（无文本层，需要 OCR）、加密 PDF。
两条都与现有 PDF 直链抓取的行为一致 —— 文档剪藏不应比「剪藏一个 PDF 链接」
更宽容或更苛刻。

## 长度控制

- **文件大小**：端点侧校验（见 `content-service/main.py`），与 nginx 的
  `client_max_body_size 10m` 同口径 —— 端点与网关必须一致，否则用户会
  看到「网关 413」这种没有信息量的报错。
- **正文长度**：超了要**明确截断并告警**，不能像网页那样静默砍掉后半段
  （网页的 50KB 上限是「防爆」口径，直接套到文档上会让用户以为文档读完了）。
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import zipfile
from io import BytesIO
from typing import Any

from .base import FetchResult, FetcherError, FetcherErrorCode

# ⚠️ 用**模块属性**而不是 `from .pdf import extract_pdf_text`：
# 后者在 import 时就把函数**绑定**到本模块的命名空间 ⇒
# 之后 monkeypatch `fetchers.pdf.extract_pdf_text` 改不动这里（打补丁打不到）。
# 改成 `_pdf_mod.extract_pdf_text(...)` 就是**调用时**解析 ⇒ 单一打补丁点。
from . import pdf as _pdf_mod

log = logging.getLogger("stashbox.fetch.document")

#: 文档来源的标识（写进 `articles.source`）。
#:
#: 为什么是独立码值而不是复用 "web"：安卓 `SourceBadge` 与后台列表都按
#: source 展示来源，复用 "web" 会让「文档剪藏」在列表里长得跟网页一样，
#: 用户分不清自己剪的是什么。
SOURCE = "document"

#: 对外别名（端点与调用方用这个名字，语义更明确）。
DOCUMENT_SOURCE = SOURCE

#: 上传文件的大小上限（字节）。
#:
#: **必须与 nginx 的 `client_max_body_size 10m` 同口径** —— 端点比网关宽，
#: 用户会先撞网关的 413（没有信息量的报错）；端点比网关窄，用户会撞到
#: 我们自己的提示（能说清为什么）。所以取 10MB，两边一致。
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024

#: 正文字数上限（字符）。
#:
#: 为什么比网页的 50KB 大：网页那个是「防爆」口径（防的是任意大页面把
#: LLM 上下文打爆），而文档是用户**特意**提交的内容，砍掉就是骗人。
#: 200KB 足够一篇长技术文档，且仍然能塞进改写的 prompt 预算。
#:
#: 超了会**截断 + 记一条 warning**（不是静默）——「你读到的可能不是全文」
#: 是个用户有权知道的事实。
MAX_CONTENT_CHARS = 200_000

#: 正文字数下限（低于此判为「没解析出正文」）。
#:
#: 与网页的 `MIN_ARTICLE_CHARS=200` 同量级：一个空文件 / 一个只有图片的
#: docx 都不该被当成「有内容」建库。
MIN_CONTENT_CHARS = 200

#: 支持的扩展名 → 解析器。
_EXT_PDF = {".pdf"}
_EXT_HTML = {".html", ".htm", ".xhtml"}
_EXT_DOCX = {".docx"}
#: 纯文本类（含 markdown）
_EXT_TEXT = {".md", ".markdown", ".txt", ".text", ".rst", ".log", ".csv", ".json", ".yaml", ".yml"}


def _decode_text(data: bytes) -> str:
    """字节 → 文本（尽量不炸）。

    顺序：UTF-8 → GB18030（中文文档常见）→ 兜底 utf-8 + replace。
    **不抛异常** —— 编码猜错不等于内容坏，交给下游的正文长度判据去把关。
    """
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _strip_html(html_text: str) -> str:
    """HTML → 纯文本（兜底用；正式抽取走 ContentExtractor）。"""
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html_text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _extract_docx_text(data: bytes) -> str:
    """DOCX → 纯文本（**标准库实现**，不引入 python-docx）。

    DOCX 就是一个 zip：`word/document.xml` 里是 WordprocessingML。
    取所有 `<w:t>` 文本节点，**按段落 `<w:p>` 换行**（否则全文会挤成一行，
    段落边界丢了，LLM 出的稿子就没有「节拍」）。

    为什么不引入 python-docx：仓里已有的 pypdf 之外没有第二个文档解析依赖，
    而 DOCX 的正文抽取用 zipfile + ElementTree 几十行就够 —— 为一个
    「读 w:t 节点」引入一个新第三方依赖不划算。
    """
    from xml.etree import ElementTree as ET

    with zipfile.ZipFile(BytesIO(data)) as zf:
        names = zf.namelist()
        if "word/document.xml" not in names:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message="not a docx file (缺 word/document.xml)",
                source=SOURCE,
            )
        xml_bytes = zf.read("word/document.xml")

    # WordprocessingML 命名空间
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FetcherError(
            code=FetcherErrorCode.PARSE,
            message=f"docx xml parse failed: {exc}",
            source=SOURCE,
        ) from exc

    paragraphs: list[str] = []
    for para in root.iter(f"{W}p"):
        # 一个段落里的文字可能散在多个 run（<w:r><w:t>）
        parts = [t.text or "" for t in para.iter(f"{W}t")]
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def _extract_html_text(data: bytes) -> str:
    """HTML → 正文（复用网页抓取**同一份**抽取器）。

    ⚠️ 用 [ContentExtractor.parse]（整篇解析）而不是各站点的专用解析器：
    文档 HTML 通常没有站点特征，密度启发式是这里唯一说得通的通用抽取。
    """
    from .parser import ContentExtractor

    html_text = _decode_text(data)
    # `parse` 是 HTMLParser 的喂数据入口（返回 self），正文要另调 `content_text()`
    return ContentExtractor().parse(html_text).content_text()


def _guess_title(text: str, filename: str) -> str:
    """标题启发式：文件名 → 首个 Markdown 标题 → 首个非空行。"""
    stem = filename.rsplit("/", 1)[-1]
    for suffix in (".md", ".markdown", ".txt", ".text", ".pdf", ".docx", ".html", ".htm"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if stem.strip():
        return stem.strip()[:200]

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            return s.lstrip("#").strip()[:200]
        return s[:200]
    return "未命名文档"


def _truncate(text: str) -> tuple[str, bool]:
    """正文截断。返回 (截断后文本, 是否被截断)。

    ⚠️ **截断必须可观测**：网页那侧是静默砍的（`parser.py` 的 50KB 上限），
    文档不能这样 —— 用户特意提交了一份长文档，被砍掉后半段却不知道，
    会得出「这份文档没有我要的信息」的错误结论。
    """
    if len(text) <= MAX_CONTENT_CHARS:
        return text, False
    kept = text[:MAX_CONTENT_CHARS]
    # 尽量切在段落边界上，别把半句话截断
    cut = kept.rfind("\n\n")
    if cut > MAX_CONTENT_CHARS * 0.6:
        kept = kept[:cut]
    return kept, True


def parse_document(
    *,
    filename: str,
    data: bytes,
    content_type: str = "",
    url: str = "",
    pdf_text: str | None = None,
) -> FetchResult:
    """上传的文档字节 → [FetchResult]（与抓取同形）。

    Args:
        pdf_text: **扫描件 PDF 的 OCR 文本**（由 `parse_document_async` 先识图拿到）。
            为 None 时按普通 PDF 处理（只有文本层）。

    Raises:
        FetcherError: 解析不出来（格式不支持 / 扫描件 / 加密 / 正文过短）。
    """
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    content_type = (content_type or "").split(";")[0].strip().lower()

    raw_metadata: dict[str, Any] = {
        "filename": filename,
        "content_type": content_type,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }

    # -- 格式分派 -------------------------------------------------------
    if ext in _EXT_PDF or content_type == "application/pdf":
        from pypdf.errors import PdfReadError

        try:
            page_texts, pdf_metadata = _pdf_mod.extract_pdf_text(data)
        except PdfReadError as exc:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"pdf read failed: {exc}",
                source=SOURCE,
            ) from exc
        except Exception as exc:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"pdf parse failed: {type(exc).__name__}: {exc}",
                source=SOURCE,
            ) from exc
        if pdf_text and pdf_text.strip():
            # 扫描件：文本层是空的，用 OCR 识出来的文字
            text = pdf_text.strip()
            raw_metadata["parser"] = "pdf_ocr"
        else:
            text = "\n\n".join(t for t in page_texts if t).strip()
            raw_metadata["parser"] = "pdf"
        raw_metadata["page_count"] = len(page_texts)
        raw_metadata["pdf_metadata"] = pdf_metadata

    elif ext in _EXT_DOCX or content_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        text = _extract_docx_text(data)
        raw_metadata["parser"] = "docx"

    elif ext in _EXT_HTML or content_type in ("text/html", "application/xhtml+xml"):
        text = _extract_html_text(data)
        raw_metadata["parser"] = "html"

    elif ext in _EXT_TEXT or content_type.startswith("text/") or not ext:
        text = _decode_text(data)
        raw_metadata["parser"] = "text"

    else:
        raise FetcherError(
            code=FetcherErrorCode.UNSUPPORTED,
            message=f"unsupported document type: {ext or content_type!r}",
            source=SOURCE,
        )

    text, truncated = _truncate(text)
    if truncated:
        raw_metadata["truncated"] = True
        raw_metadata["original_chars"] = len(text)
        log.warning(
            "document_content_truncated file=%s kept=%d cap=%d",
            filename,
            len(text),
            MAX_CONTENT_CHARS,
        )

    if len(text.strip()) < MIN_CONTENT_CHARS:
        raise FetcherError(
            code=FetcherErrorCode.PARSE,
            message=(
                f"content too short: {len(text.strip())} chars < {MIN_CONTENT_CHARS} "
                "(可能是扫描件/加密文件/空文档)"
            ),
            source=SOURCE,
        )

    title = _guess_title(text, filename)
    raw_metadata["text_length"] = len(text)

    # HTML 形态：给下游一个统一的可选表示（蒸馏只用 content_text）
    content_html = f"<p>{text.replace(chr(10), '<br/>')}</p>"

    return FetchResult(
        url=url or f"doc://{raw_metadata['sha256'][:16]}",
        title=title,
        content_html=content_html,
        content_text=text,
        author=None,
        publish_time=None,
        media_urls=[],
        source=SOURCE,
        raw_metadata=raw_metadata,
    )


__all__ = [
    "DOCUMENT_SOURCE",
    "parse_document_async",
    "MAX_CONTENT_CHARS",
    "MAX_DOCUMENT_BYTES",
    "MIN_CONTENT_CHARS",
    "SOURCE",
    "parse_document",
]


async def parse_document_async(
    *,
    filename: str,
    data: bytes,
    content_type: str = "",
    url: str = "",
) -> FetchResult:
    """**异步版**解析：扫描件 PDF 会先 OCR 识图，再走同步解析。

    ## 为什么需要异步版

    同步的 [parse_document] 只做**纯计算**（pypdf 抽文本），所以能在任何地方调。
    但 OCR 要**发网络请求** ⇒ 不能在同步函数里做（会把事件循环卡住）。

    ⇒ 拆成「异步壳 + 同步核」：壳负责「判断要不要 OCR + 拿 OCR 文本」，
    核仍然是 [parse_document]（**唯一的解析实现**，不会两处漂移）。

    ## 什么时候会 OCR

    仅当：**是 PDF** 且 **文本层是空的**（扫描件的判定）且 **OCR 通道可用**。
    OCR 不可用时**不报错** —— 退回「读不出正文」的既有报错路径
    （错误话术由上层统一处理），不让 OCR 的问题变成一个更 confusing 的错误。
    """
    from .ocr import OCRUnavailableError, ocr_pdf

    pdf_text: str | None = None
    if _is_pdf(filename=filename, content_type=content_type):
        try:
            page_texts, _meta = _pdf_mod.extract_pdf_text(data)
            has_text_layer = any((t or "").strip() for t in page_texts)
        except Exception:
            has_text_layer = False

        if not has_text_layer:
            # 扫描件 → 识图
            try:
                pdf_text, _ocr_meta = await ocr_pdf(data)
                log.info("document_ocr_done chars=%d", len(pdf_text or ""))
            except OCRUnavailableError as exc:
                log.warning("document_ocr_unavailable: %s", exc)
            except Exception as exc:
                log.warning("document_ocr_failed: %s", exc)

    return parse_document(
        filename=filename,
        data=data,
        content_type=content_type,
        url=url,
        pdf_text=pdf_text,
    )


def _is_pdf(*, filename: str, content_type: str) -> bool:
    """是不是 PDF（按扩展名或 content-type）。"""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    ct = (content_type or "").split(";")[0].strip().lower()
    return ext == ".pdf" or ct == "application/pdf"
