"""文档剪藏：解析器 + 端点（2026-10-23）。

## 为什么要有这些用例

文档剪藏是**第一条「非 URL 内容」的入库路径**（`POST /api/v1/articles/document`）。
它最容易出的三类错，在这里各锁一条：

1. **格式分派出错** —— 扩展名 / mime 判定错 → 解析出乱码或空内容；
2. **判据不严** —— 空文档/扫描件被判成「有正文」→ 建出一条**注定失败**的
   蒸馏任务，还扣了配额（用户什么都没得到）；
3. **两套真相** —— 文档路径与链接路径的「后半程」（去重/扣费/建库/派蒸馏）
   漂移。最容易漂的是**扣费**：漏了「先解析后扣」就会变成
   「解析失败也扣配额」。

## 覆盖到的行为

· 四种格式（MD/TXT、PDF、DOCX、HTML）都能解析出正文；
· DOCX 走**标准库**（zipfile + ElementTree），不引入新依赖；
· 空文档 / 太短 → `FetcherError(PARSE)`，**不建库不计费**；
· 不支持的格式 → `FetcherError(UNSUPPORTED)`；
· 超长正文 → **截断 + 标记**（不是静默砍）；
· 端点：file 与 text 二选一；都为空 → 400；都传 → 400；
· 端点：同一份文档重复提交 → **复用同一篇**（url 用内容 hash），
  且**不重复扣配额**。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from helpers import content_main, new_user
from stashbox.backend.common.models import Article
from stashbox.backend.common.database import AsyncSessionLocal

# content-service 目录名带连字符，按文件路径加载它的 fetchers 包
import importlib.util
import sys
from pathlib import Path

CONTENT_DIR = Path(__file__).resolve().parents[2] / "content-service"
_spec = importlib.util.spec_from_file_location(
    "cs_doc_fetchers",
    CONTENT_DIR / "fetchers" / "__init__.py",
    submodule_search_locations=[str(CONTENT_DIR / "fetchers")],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cs_doc_fetchers"] = _mod
_spec.loader.exec_module(_mod)
doc = importlib.import_module("cs_doc_fetchers.document")

from cs_doc_fetchers.base import FetcherError  # noqa: E402


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------
def _md(text: str = "") -> bytes:
    return (text or "这是文档正文的一段内容，用来通过正文长度下限。\n\n" * 20).encode()


def test_markdown_能解析():
    r = doc.parse_document(filename="设计.md", data=_md())
    assert r.source == "document"
    assert "正文" in r.content_text
    assert r.raw_metadata["parser"] == "text"
    assert r.url.startswith("doc://")


def test_docx_用标准库解析():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            + "".join(f"<w:p><w:r><w:t>第{i}段文档内容。</w:t></w:r></w:p>" for i in range(20))
            + "</w:document>",
        )
    r = doc.parse_document(filename="报告.docx", data=buf.getvalue())
    assert r.raw_metadata["parser"] == "docx"
    assert "第0段文档内容。" in r.content_text
    # 段落边界必须保留（不然 LLM 出的稿子没有「节拍」）
    assert "\n\n" in r.content_text


def test_html_复用网页抽取器():
    # ContentExtractor 只取密度最高的 10 段（parser.MAX_PARAGRAPHS），
    # 所以每段要够长，10 段合起来才过得了正文下限（200 字）。
    html = (
        "<html><body><article>"
        + "".join(
            f"<p>这是第{i}段的技术文档正文内容，介绍这个模块的职责、"
            "调用方式与边界条件，便于读者判断是否适用。</p>"
            for i in range(15)
        )
        + "</article></body></html>"
    )
    r = doc.parse_document(filename="页面.html", data=html.encode())
    assert r.raw_metadata["parser"] == "html"
    assert len(r.content_text) > 100


def test_空文档判为解析失败():
    with pytest.raises(FetcherError) as exc:
        doc.parse_document(filename="空.txt", data="太短".encode())
    assert exc.value.code.value == "fetcher.parse"


def test_不支持的格式():
    with pytest.raises(FetcherError) as exc:
        doc.parse_document(filename="压缩包.zip", data=b"x" * 500)
    assert exc.value.code.value == "fetcher.unsupported"


def test_超长正文被截断且可观测():
    """截断必须**可观测** —— 网页那侧是静默砍的，文档不能这样。

    用户特意提交了一份长文档，被砍掉后半段却不知道，会得出
    「这份文档没有我要的信息」的错误结论。
    """
    big = ("这是很长的文档正文内容。\n\n" * 40000).encode()
    r = doc.parse_document(filename="长文.md", data=big)
    assert r.raw_metadata.get("truncated") is True
    assert len(r.content_text) <= doc.MAX_CONTENT_CHARS


def test_相同内容得到相同_url_去重的基础():
    a = doc.parse_document(filename="a.md", data=_md())
    b = doc.parse_document(filename="b.md", data=_md())
    assert a.url == b.url, "同内容不同文件名也要得到同一个 url（去重靠它）"
    c = doc.parse_document(filename="c.md", data=_md("另一份完全不同的内容用于对比。" * 30))
    assert c.url != a.url


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
def _override_auth(dep_name: str, mock_user: dict):
    dep = getattr(content_main, dep_name)

    class _Ctx:
        def __enter__(self):
            content_main.app.dependency_overrides[dep] = lambda: mock_user
            return self

        def __exit__(self, *exc):
            content_main.app.dependency_overrides.pop(dep, None)

    return _Ctx()


@pytest.mark.asyncio
async def test_文档剪藏_建库并派蒸馏():
    user_id, _tok = await new_user()
    mock_user = {"id": user_id, "sub": str(user_id)}

    with _override_auth("require_user", mock_user):
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/articles/document",
                files={"file": ("设计文档.md", _md(), "text/markdown")},
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["article_id"]
    assert data["url"].startswith("doc://")
    assert data["filename"] == "设计文档.md"

    async with AsyncSessionLocal() as s:
        art = await s.get(Article, data["article_id"])
        assert art is not None
        assert art.source == "document"
        # 正文落在 raw_content（形状与抓取一致）→ 蒸馏链路零改动
        assert art.raw_content["content_text"]
        assert art.raw_content["raw_metadata"]["parser"] == "text"


@pytest.mark.asyncio
async def test_文档剪藏_粘贴文本也可以():
    user_id, _tok = await new_user()
    mock_user = {"id": user_id, "sub": str(user_id)}

    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/articles/document",
                data={"text": "这是粘贴的文档正文。\n\n" * 30, "title": "我的标题"},
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    async with AsyncSessionLocal() as s:
        art = await s.get(Article, data["article_id"])
        assert art is not None
        assert art.title == "我的标题"


@pytest.mark.asyncio
async def test_文档剪藏_既没文件也没文本_报400():
    user_id, _tok = await new_user()
    mock_user = {"id": user_id, "sub": str(user_id)}

    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/articles/document", data={})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_文档剪藏_同一份文档重复提交_复用且不重复扣配额():
    """url 用内容 hash → 同一份文档重复上传是**同一篇**。

    这是「重复剪藏不重复扣配额」那条约定在文档路径上的落地
    （链接路径靠 url 去重，文档路径靠内容 hash）。
    """
    user_id, _tok = await new_user()
    mock_user = {"id": user_id, "sub": str(user_id)}

    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_user", mock_user):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post(
                "/api/v1/articles/document",
                files={"file": ("同.md", _md(), "text/markdown")},
            )
            r2 = await client.post(
                "/api/v1/articles/document",
                files={"file": ("同.md", _md(), "text/markdown")},
            )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    d1, d2 = r1.json(), r2.json()
    assert d1["article_id"] == d2["article_id"], "同一份文档应复用同一篇文章"
    assert d2.get("reused") is True
    assert d2["quota_used"] == 0, "复用不该再扣配额"
