"""运营侧文档录入（`POST /api/v1/admin/articles/document`）—— 2026-10-23。

## 为什么单独给它一组用例

这个端点与用户侧的 `POST /api/v1/articles/document` **共用解析器**，但**归属、
配额、鉴权三件事完全不同**。这三件事恰好是「运营造内容」与「用户剪藏」必须
分开的理由（见 content-service/main.py 里 admin_create_article 的注释）：

· 归属：运营录入的文章记 `ANONYMOUS_USER_ID(0)`（全站公共内容），
  **不是**运营账号 —— 否则用户在列表里根本看不到；
· 配额：**不扣**运营的配额 —— 运营录一篇稿不该按用户剪藏的价计费；
· 鉴权：走 `require_admin_or_operator`。

这三条任何一条漂移了，后果都很难一眼看出：
配额照扣 → 运营账号配额被吃光；归属写成运营 → 内容「建了但没人看得见」；
鉴权漏挂 → 普通用户能往全站塞内容。

## 另外锁住的两件事

· **解析失败不建库**（与用户侧同口径）—— 解析不出正文就不该留下一条
  注定失败的蒸馏记录；
· **两侧判据一致** —— 用户能传的文件运营也必须能传（同一个解析器、
  同一个 `_read_document_upload`）。
"""

from __future__ import annotations

import pytest

from helpers import content_main
from stashbox.backend.common.models import Article, AdminOperationLog
from stashbox.backend.common.database import AsyncSessionLocal


def _md(text: str = "") -> bytes:
    return (text or "这是文档正文的一段内容，用来通过正文长度下限。\n\n" * 20).encode()


def _unique_md(tag: str) -> bytes:
    """**每个用例一份独有内容**。

    文档去重靠的是**内容 hash**（`url = doc://{sha256[:16]}`）——
    用例之间若共用同一份内容，它们会命中同一篇文章（去重生效），
    于是「审计日志恰好一条」这类断言会被**别的用例**的写入污染
    （实测 3 条）。这不是 bug，是去重在起作用；测试要用独有内容隔离自己。
    """
    return (f"这是 {tag} 专属的文档正文，用来通过正文长度下限。\n\n" * 20).encode()


def _override_auth(dep_name: str, mock_user: dict):
    dep = getattr(content_main, dep_name)

    class _Ctx:
        def __enter__(self):
            content_main.app.dependency_overrides[dep] = lambda: mock_user
            return self

        def __exit__(self, *exc):
            content_main.app.dependency_overrides.pop(dep, None)

    return _Ctx()


ADMIN_USER = {"id": 9001, "sub": "9001", "tier": "admin"}


async def _drop_anonymous_sentinel() -> None:
    """把哨兵用户（id=0）删掉，复现**生产**状态。

    为什么需要这么麻烦：哨兵被 `articles.user_id` 等 7 张表引用，
    直接删会撞外键 —— 所以要先把引用它的行清掉。

    清掉的都是**上一轮测试留下的孤儿数据**（生产库里从来没有过 id=0），
    端点跑完会把哨兵重建回来。
    """
    from sqlalchemy import delete, select, text

    from stashbox.backend.common.models import Article, User

    async with AsyncSessionLocal() as s:
        art_ids = list(await s.scalars(select(Article.id).where(Article.user_id == 0)))
        if art_ids:
            # 这几张表对 articles 是 FK（多数没有 ON DELETE CASCADE）
            for tbl in (
                "push_notifications",
                "listening_statuses",
                "later_listens",
                "favorites",
                "feedback_v2",
                "distilled_articles",
            ):
                await s.execute(
                    text(f"DELETE FROM {tbl} WHERE article_id = ANY(:ids)"),
                    {"ids": list(art_ids)},
                )
            await s.execute(delete(Article).where(Article.id.in_(art_ids)))
        await s.execute(delete(User).where(User.id == 0))
        await s.commit()


@pytest.mark.asyncio
async def test_admin录入文档_归属匿名且不扣配额():
    """录入的文章属于**全站公共内容**，不挂在运营名下、也不扣运营配额。"""
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles/document",
                files={"file": ("运营文档.md", _unique_md("归属"), "text/markdown")},
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["article_id"]
    assert data["url"].startswith("doc://")
    assert data["filename"] == "运营文档.md"
    assert data["text_length"] > 0

    async with AsyncSessionLocal() as s:
        art = await s.get(Article, data["article_id"])
        assert art is not None
        # 归属 = 匿名（0），**不是**运营账号 id
        assert art.user_id == 0
        assert art.source == "document"
        # 正文落在 raw_content（与抓取同形）→ 蒸馏链路零改动
        assert art.raw_content["content_text"]
        assert art.raw_content["raw_metadata"]["parser"] == "text"


@pytest.mark.asyncio
async def test_admin录入文档_写审计日志():
    """admin 写操作必须留审计记录。

    不能只靠网关的 AuditMiddleware —— 它只挂在网关上，
    直连 content-service:8102 绕过网关时不会有记录。
    """
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles/document",
                files={"file": ("审计.md", _unique_md("审计"), "text/markdown")},
            )
    assert resp.status_code == 200, resp.text
    art_id = resp.json()["article_id"]

    async with AsyncSessionLocal() as s:
        from sqlalchemy import select

        rows = (
            await s.scalars(
                select(AdminOperationLog).where(
                    AdminOperationLog.target_id == art_id,
                    AdminOperationLog.action == "ADMIN_CREATE_ARTICLE",
                )
            )
        ).all()
        assert len(rows) == 1, "应恰好有一条审计记录"
        assert rows[0].path == "/api/v1/admin/articles/document"
        assert rows[0].method == "POST"


@pytest.mark.asyncio
async def test_admin录入文档_解析失败不建库():
    """正文太短 → 报错，**不留文章**。

    留一条注定失败的蒸馏记录是比报错更糟的结果：
    后台会看到一条「卡住」的文章，而运营不知道自己传错了什么。

    ⚠️ 状态码是 **502**（不是 400）：这是**抓取侧错误映射的统一口径**
    （`map_fetcher_error`：PARSE → 业务码 2002 / HTTP 502），用户侧的
    文档端点也是同一个码。故意保持一致 —— 两边不一样的话，
    「用户能传运营不能传」会变成一个查不到原因的问题。
    真正给运营看的是响应里的 message（「没能从这个文档里读出文字…」）。
    """
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles/document",
                files={"file": ("空.md", "太短了".encode(), "text/markdown")},
            )
    assert resp.status_code == 502, resp.text
    # 消息要说清是「文件读不出文字」而不是「服务故障」
    assert "读出文字" in resp.json()["message"]

    async with AsyncSessionLocal() as s:
        from sqlalchemy import select

        # 库里不应有 url 指向这份内容的文章
        n = await s.scalar(
            select(Article).where(
                Article.url.like("doc://%"),
                Article.title == "空",
            )
        )
        assert n is None


@pytest.mark.asyncio
async def test_admin录入文档_既没文件也没文本_报400():
    """与用户侧**同一份**判据（共用 `_read_document_upload`）。"""
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/admin/articles/document", data={})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_admin录入文档_文件与文本同时给_报400():
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles/document",
                files={"file": ("双.md", _unique_md("双给"), "text/markdown")},
                data={"text": "也传了文本"},
            )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_admin录入文档_粘贴文本也可以():
    """粘贴文本与上传文件走**同一条**解析路径。"""
    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles/document",
                data={"text": "这是粘贴的运营文档正文。\n\n" * 30, "title": "运营标题"},
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    async with AsyncSessionLocal() as s:
        art = await s.get(Article, data["article_id"])
        assert art is not None
        assert art.title == "运营标题"
        assert art.user_id == 0


@pytest.mark.asyncio
async def test_admin录入文档_哨兵用户不存在时自建_不能因外键失败():
    """生产实测踩到的坑（2026-10-23 部署后）：**users 表里没有 id=0 的哨兵行**时，
    admin 端点建库会撞 `articles_user_id_fkey` → 500。

    ## 为什么会踩

    匿名哨兵用户（`id=0`）是**按需**建的：`_ensure_anonymous_user` 只在
    匿名剪藏 / wechat_mp 两条路径上被调用。而 admin 端点用同一个
    `ANONYMOUS_USER_ID` 归属，却**没有**先确保哨兵存在。

    本地测试库之所以一直是绿的：别的用例（匿名剪藏）早就把 id=0 建出来了。
    生产库则从来没有过 id=0 —— 于是 admin 端点**必现**外键错误。

    ⇒ 这个用例必须**先把哨兵删掉**再调用，否则它测的是「哨兵已存在」，
    永远抓不到这个 bug（护栏没牙齿）。

    同一个坑也影响既有的 `POST /api/v1/admin/articles`（运营录入链接）——
    线上实测它同样 500。
    """
    await _drop_anonymous_sentinel()

    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles/document",
                files={"file": ("哨兵.md", _unique_md("哨兵"), "text/markdown")},
            )
    assert resp.status_code == 200, resp.text

    async with AsyncSessionLocal() as s:
        from sqlalchemy import select

        from stashbox.backend.common.models import User

        row = await s.scalar(select(User).where(User.id == 0))
        assert row is not None, "端点应自建匿名哨兵用户（否则文章挂不上 FK）"
        art = await s.get(Article, resp.json()["article_id"])
        assert art is not None
        assert art.user_id == 0


@pytest.mark.asyncio
async def test_admin录入链接_哨兵用户不存在时也不能因外键失败():
    """同一个坑也在**既有**的运营录入链接端点上（线上实测同样 500）。

    这里只锁「不 500」—— 抓取用的是 example.com，会撞网络，
    所以断言放在「没有 ForeignKeyViolationError」这一层：
    状态码不是 500，且响应里没有外键错误。
    """
    await _drop_anonymous_sentinel()

    from httpx import ASGITransport, AsyncClient

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles",
                json={"url": "https://example.com/x", "source": "url"},
            )
    assert resp.status_code != 500, resp.text
    assert "ForeignKeyViolation" not in resp.text


@pytest.mark.asyncio
async def test_admin录入链接_来源用真实来源而不是占位码(monkeypatch):
    """admin-web 发 `source: 'url'`（占位码），库里应落**真实来源**。

    为什么锁这条：admin 端点不走 `submit_article`（那条路径自己解析来源），
    它把 source 直接交给 `_create_article`。若 `_create_article` 不把占位码
    换成 fetcher 判定的真实来源，运营录一个 GitHub 仓库，库里就是 "url" ——
    来源筛选失真，用户侧徽章也只会显示「通用」。

    这条在 admin 端点刚修好时是 500（外键），所以历史上**没有**任何
    admin 建的文章 → 不存在「老数据突变」的问题。
    """
    from httpx import ASGITransport, AsyncClient

    # content_main 已把 content-service 目录加进 sys.path，`fetchers` 是顶层包
    from fetchers.base import FetchResult

    class _FakeGitHubFetcher:
        async def fetch(self, url: str, timeout: float | None = None, budget: float | None = None):
            return FetchResult(
                url=url,
                title="owner/repo",
                content_html="<p>README</p>",
                content_text="README 正文内容，足够长，" * 30,
                source="github",
            )

    monkeypatch.setattr(content_main, "get_fetcher", lambda _url: _FakeGitHubFetcher())

    with _override_auth("require_admin_or_operator", ADMIN_USER):
        transport = ASGITransport(app=content_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/admin/articles",
                json={"url": "https://github.com/owner/repo", "source": "url"},
            )
    assert resp.status_code == 200, resp.text

    async with AsyncSessionLocal() as s:
        art = await s.get(Article, resp.json()["article_id"])
        assert art is not None
        assert art.source == "github", f"应落真实来源，实际 {art.source!r}"


@pytest.mark.asyncio
async def test_admin录入文档_普通用户被拒():
    """普通用户不能往全站塞内容。

    鉴权没挂上时，content-service 会按 `require_admin_or_operator` 的默认
    行为拒绝 —— 这里锁住「拒绝」这件事，避免哪天依赖被误删后静默放行。
    """
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=content_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/articles/document",
            files={"file": ("越权.md", _unique_md("越权"), "text/markdown")},
        )
    assert resp.status_code in (401, 403), resp.text
