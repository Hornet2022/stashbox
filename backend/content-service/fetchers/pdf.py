"""PDF 抓取器（v1 §11.2 CP11.0.8 P3.3）。

处理用户提交的 .pdf 直链：
1. httpx 下载 PDF（accept: application/pdf）
2. pypdf 抽文本（逐页累加，保留换行）
3. Title 启发式：第一页第一行非空文本 / Content-Disposition filename / URL basename
4. 返回 FetchResult，source="pdf"

为什么不重 PDF OCR（图片版 PDF）：本期只支持文本型 PDF。图片版由 ai-service
蒸馏阶段兜底（多模态 LLM 直接吃图片），不增加 fetcher 复杂度。
"""
from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .base import (
    FetchResult,
    Fetcher,
    FetcherError,
    FetcherErrorCode,
)

log = logging.getLogger(__name__)


# Content-Type 白名单（宽松匹配：有的服务器给 octet-stream）
_PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/octet-stream"})

# 单 PDF 大小上限（100 MB）— 防 OOM / 恶意大文件
MAX_PDF_BYTES = 100 * 1024 * 1024

# Title fallback：文件名（去扩展名 + 替换分隔符）
_FILENAME_RE = re.compile(r"[^A-Za-z0-9\u4e00-\u9fa5._-]+")


class PdfFetcher(Fetcher):
    """PDF 直链抓取器（v1 §11.2 CP11.0.8 P3.3）。

    支持：任何 HTTP(S) 链接，Content-Type 是 application/pdf（或兜底 octet-stream）。
    catch-all 性低（只在 URL 看起来是 .pdf 时接），所以放在 generic_url 前面。
    """

    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 StashBox/0.1"
    )
    TIMEOUT = 60.0   # PDF 下载给 60s（大文件需要更久）

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    @property
    def name(self) -> str:
        return "pdf"

    def supports(self, url: str) -> bool:
        """URL path 以 .pdf 结尾才接。其他走 generic_url。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        return parsed.path.lower().endswith(".pdf")

    async def fetch(self, url: str, *, timeout: float = TIMEOUT) -> FetchResult:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise FetcherError(
                code=FetcherErrorCode.UNSUPPORTED,
                message=f"unsupported url scheme: {url!r}",
                source=self.name,
            )

        # 1. 下载 PDF
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.UA,
                "Accept": "application/pdf,*/*;q=0.8",
            },
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise FetcherError(
                code=FetcherErrorCode.NETWORK,
                message=f"timeout after {timeout}s",
                source=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            raise FetcherError(
                code=FetcherErrorCode.NETWORK,
                message=f"request failed: {type(exc).__name__}: {exc}",
                source=self.name,
            ) from exc

        if response.status_code in (404, 410):
            raise FetcherError(
                code=FetcherErrorCode.NOT_FOUND,
                message=f"http {response.status_code}",
                source=self.name,
            )
        if response.status_code >= 400:
            raise FetcherError(
                code=FetcherErrorCode.NETWORK,
                message=f"http {response.status_code}",
                source=self.name,
            )

        # 2. Content-Type 校验
        content_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        if content_type and content_type not in _PDF_CONTENT_TYPES:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"content-type not pdf: {content_type!r}",
                source=self.name,
            )

        body = response.content
        if len(body) > MAX_PDF_BYTES:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"pdf too large: {len(body)} bytes > {MAX_PDF_BYTES}",
                source=self.name,
            )
        if len(body) < 100:
            # 太短不像 PDF
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"pdf too small: {len(body)} bytes",
                source=self.name,
            )

        # 3. pypdf 解析
        try:
            reader = PdfReader(BytesIO(body))
            page_texts: list[str] = []
            for page in reader.pages:
                try:
                    page_text = page.extract_text() or ""
                except Exception as exc:   # 单页解析失败不影响其他页
                    log.warning("[pdf] page extract failed: %s", exc)
                    page_text = ""
                page_texts.append(page_text)
            content_text = "\n\n".join(t for t in page_texts if t).strip()
        except PdfReadError as exc:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"pdf read failed: {exc}",
                source=self.name,
            ) from exc
        except Exception as exc:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message=f"pdf parse failed: {type(exc).__name__}: {exc}",
                source=self.name,
            ) from exc

        if not content_text:
            raise FetcherError(
                code=FetcherErrorCode.PARSE,
                message="no text extracted (可能为图片型 PDF,本期不支持 OCR)",
                source=self.name,
            )

        # 4. Title 启发式
        title = self._extract_title(
            page_texts=page_texts,
            content_disposition=response.headers.get("content-disposition", ""),
            url=url,
        )

        # 5. 元数据（pypdf 文档属性）
        raw_metadata: dict[str, Any] = {
            "final_url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "page_count": len(page_texts),
            "text_length": len(content_text),
        }
        try:
            doc_meta = reader.metadata or {}
            raw_metadata["pdf_metadata"] = {
                str(k): str(v) for k, v in doc_meta.items() if v is not None
            }
        except Exception:
            raw_metadata["pdf_metadata"] = {}

        # 6. 构造 FetchResult（PDF 无 HTML，正文段落转 <p>）
        #     content_html 用 <p> 包裹每页文本，便于后续蒸馏 LLM 解析。
        paragraphs = [t.replace("\n", "<br/>") for t in page_texts if t]
        content_html = "".join(f"<p>{p}</p>" for p in paragraphs)

        return FetchResult(
            url=url,
            title=title,
            content_html=content_html,
            content_text=content_text,
            author=None,
            publish_time=None,
            media_urls=[],
            source=self.name,
            raw_metadata=raw_metadata,
        )

    @staticmethod
    def _extract_title(
        *,
        page_texts: list[str],
        content_disposition: str,
        url: str,
    ) -> str:
        """Title 启发式（优先级递减）：
        1. Content-Disposition 的 filename
        2. 第一页前 3 行里最长的非空行
        3. URL basename 去 .pdf
        """
        # 1. Content-Disposition filename
        if content_disposition:
            m = re.search(r"filename\*?=(?:UTF-8'')??[\"]?([^\";]+)", content_disposition)
            if m:
                return _FILENAME_RE.sub(" ", m.group(1).strip()).strip()

        # 2. 第一页前 3 行
        if page_texts and page_texts[0]:
            lines = [
                line.strip()
                for line in page_texts[0].splitlines()[:3]
                if line.strip()
            ]
            if lines:
                # 取最长行（通常是标题）
                return max(lines, key=len)[:200]

        # 3. URL basename
        path = urlparse(url).path
        basename = path.rsplit("/", 1)[-1]
        if basename.lower().endswith(".pdf"):
            basename = basename[:-4]
        return _FILENAME_RE.sub(" ", basename).strip() or "未命名 PDF"


__all__ = ["PdfFetcher"]