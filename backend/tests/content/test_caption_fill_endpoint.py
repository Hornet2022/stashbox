"""内部端点：POST /api/v1/internal/articles/{article_id}/captions（2026-10-25）。

## 这个端点为什么存在

识图（下载图片 + 调 VLM）实测一篇 12 张图要 20~30s。放在**剪藏请求**里，
用户在手机上盯着转圈 —— 内测反馈的「剪藏不成功」一半来自这里。
⇒ 改成：剪藏只留占位符，**蒸馏前**由 ai-service 调这个端点补图注。

## 这些用例锁的是三件事

1. **鉴权 fail-closed** —— 没带密钥 401、密钥不对 401、服务端没配密钥 503。
   理由很硬：未鉴权的话，任意匿名请求就能让本服务去下载任意图片、
   调任意次数的 VLM ⇒ 直接烧钱。
2. **写回正确** —— 补完图注，`articles.raw_content` 里的正文要换成图注文本，
   `media_refs` 要消失，`media_captions` 要就位。
3. **幂等** —— 再调一次是 no-op（`media_refs` 已经没了 ⇒ 不会重复烧 token）。

⚠️ 全部不打真网络：识图那一步用替身顶掉。
"""

from __future__ import annotations

import uuid

import pytest

from stashbox.backend.common import media_placeholders as placeholders
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article

from helpers import client, content_main, new_user

CAPTIONS_PATH = "/api/v1/internal/articles/{article_id}/captions"

#: 服务端配的共享密钥（测试里现配现用）。
SECRET = "test-caption-secret"


@pytest.fixture
def configured_secret(monkeypatch):
    """让 `require_callback_secret` 能通过（生产靠 CALLBACK_SHARED_SECRET）。"""
    monkeypatch.setattr(content_main.settings, "callback_shared_secret", SECRET, raising=False)
    return SECRET


async def _new_article_with_refs(user_id: int, *, urls: list[str]) -> str:
    """建一篇**带着占位符**落库的文章（就是剪藏请求刚写进去的形态）。"""
    article_id = f"art_{uuid.uuid4().hex[:24]}"
    text = "第一段。"
    for _ in urls:
        text += "\n\n" + placeholders.make_token(len(urls) and len(urls) or 1)
    raw_content = {
        "url": "https://example.com/chart",
        "source": "generic_url",
        "content_text": placeholders.to_storage_text(text),
        "raw_metadata": {
            "final_url": "https://example.com/chart",
            "media_refs": [{"url": u, "index": i + 1} for i, u in enumerate(urls)],
        },
    }
    async with AsyncSessionLocal() as session:
        session.add(
            Article(
                id=article_id,
                user_id=user_id,
                url="https://example.com/chart",
                source="web",
                status="pending",
                favorite=False,
                skip=False,
                raw_content=raw_content,
            )
        )
        await session.commit()
    return article_id


async def _read_raw(article_id: str) -> dict:
    async with AsyncSessionLocal() as session:
        art = await session.get(Article, article_id)
        return dict(art.raw_content or {})


# ---------------------------------------------------------------------------
# 1. 鉴权：fail-closed
# ---------------------------------------------------------------------------
async def test_没带密钥_401(configured_secret):
    uid, _ = await new_user()
    article_id = await _new_article_with_refs(uid, urls=["https://cdn.example.com/a.png"])
    async with client() as c:
        r = await c.post(CAPTIONS_PATH.format(article_id=article_id))
    assert r.status_code == 401, r.text


async def test_密钥不对_401(configured_secret):
    uid, _ = await new_user()
    article_id = await _new_article_with_refs(uid, urls=["https://cdn.example.com/a.png"])
    async with client(x_callback_secret="wrong") as c:
        r = await c.post(CAPTIONS_PATH.format(article_id=article_id))
    assert r.status_code == 401, r.text


async def test_服务端没配密钥_503(monkeypatch):
    """**fail-closed**：没配密钥就拒绝，而不是放行。"""
    monkeypatch.setattr(content_main.settings, "callback_shared_secret", "", raising=False)
    uid, _ = await new_user()
    article_id = await _new_article_with_refs(uid, urls=["https://cdn.example.com/a.png"])
    async with client(x_callback_secret=SECRET) as c:
        r = await c.post(CAPTIONS_PATH.format(article_id=article_id))
    assert r.status_code == 503, r.text


async def test_文章不存在_404(configured_secret):
    async with client(x_callback_secret=SECRET) as c:
        r = await c.post(CAPTIONS_PATH.format(article_id="art_not_exist"))
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 2. 写回正确
# ---------------------------------------------------------------------------
async def test_补完图注_正文换成图注且清单被消费(configured_secret, monkeypatch):
    uid, _ = await new_user()
    article_id = await _new_article_with_refs(
        uid, urls=["https://cdn.example.com/a.png", "https://cdn.example.com/b.png"]
    )

    async def _fake_stored(raw, **_kwargs):
        new = dict(raw)
        new["content_text"] = "第一段。\n\n图表：一季度增长 30%。\n\n图表：二季度持平。"
        meta = dict(new["raw_metadata"])
        meta.pop("media_refs", None)
        meta["media_captions"] = [
            {
                "url": "https://cdn.example.com/a.png",
                "token": "\\x00IMG1\\x00",
                "caption": "一季度增长 30%。",
                "ok": True,
                "error": None,
            },
            {
                "url": "https://cdn.example.com/b.png",
                "token": "\\x00IMG2\\x00",
                "caption": "二季度持平。",
                "ok": True,
                "error": None,
            },
        ]
        new["raw_metadata"] = meta
        return new, {"images": 2, "captions": 2, "changed": True}

    monkeypatch.setattr(content_main, "caption_stored_content", _fake_stored)

    async with client(x_callback_secret=SECRET) as c:
        r = await c.post(CAPTIONS_PATH.format(article_id=article_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["captions"] == 2
    assert body["images"] == 2
    assert body["changed"] is True

    raw = await _read_raw(article_id)
    assert "图表：一季度增长 30%。" in raw["content_text"]
    assert "media_refs" not in raw["raw_metadata"], "图清单必须被消费掉（幂等的前提）"
    assert len(raw["raw_metadata"]["media_captions"]) == 2


async def test_补图注炸了_端点仍然_200(configured_secret, monkeypatch):
    """识图失败只意味着「这篇没有图表信息」——不能连累整篇蒸馏。"""

    async def _boom(raw, **_kwargs):
        raise RuntimeError("provider 炸了")

    monkeypatch.setattr(content_main, "caption_stored_content", _boom)

    uid, _ = await new_user()
    article_id = await _new_article_with_refs(uid, urls=["https://cdn.example.com/a.png"])
    async with client(x_callback_secret=SECRET) as c:
        r = await c.post(CAPTIONS_PATH.format(article_id=article_id))
    assert r.status_code == 200, r.text
    assert r.json()["captions"] == 0


# ---------------------------------------------------------------------------
# 3. 落库形态：占位符不能被剥成裸的 IMG3
# ---------------------------------------------------------------------------
async def test_落库时占位符不被剥成裸_img():
    """`_strip_nul` 会把 NUL 删掉 ⇒ 占位符会退化成裸的 `IMG3`，**再也认不回来**。

    这是最阴的一种退化：不报错、正文看着还挺正常，只是图表信息静默消失。
    ⇒ 落库前必须先把占位符换成**可见记法**。
    """
    from fetchers.base import FetchResult

    fr = FetchResult(
        url="https://example.com/chart",
        title="标题",
        content_html="",
        content_text="第一段。\n\n" + placeholders.make_token(1) + "\n\n第二段。",
        source="generic_url",
        raw_metadata={"media_refs": [{"url": "https://cdn.example.com/a.png", "index": 1}]},
    )

    raw = content_main._to_raw_content(fr)

    assert "\\x00IMG1\\x00" in raw["content_text"], "落库形态必须是可见记法"
    assert "\x00" not in raw["content_text"], "JSONB 不接受 \\u0000"
    # 图清单也要能对上号
    assert raw["raw_metadata"]["media_refs"] == [
        {"url": "https://cdn.example.com/a.png", "index": 1}
    ]


# ---------------------------------------------------------------------------
# 4. 幂等：没有 media_refs ⇒ no-op（不重复烧 token）
# ---------------------------------------------------------------------------
async def test_没有待补的图_不改动也不写库(configured_secret):
    uid, _ = await new_user()
    article_id = await _new_article_with_refs(uid, urls=[])
    before = await _read_raw(article_id)

    async with client(x_callback_secret=SECRET) as c:
        r = await c.post(CAPTIONS_PATH.format(article_id=article_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["captions"] == 0
    assert body["changed"] is False

    after = await _read_raw(article_id)
    assert after == before, "没有待补的图时 raw_content 一个字节都不该动"


async def test_重复调用_第二次是_noop(configured_secret, monkeypatch):
    """补过之后 `media_refs` 没了 ⇒ 再调一次不重复识图（不重复烧 token）。"""
    calls: list[int] = []

    async def _fake_stored(raw, **_kwargs):
        calls.append(1)
        if not raw.get("raw_metadata", {}).get("media_refs"):
            return raw, {"images": 0, "captions": 0, "changed": False}
        new = dict(raw)
        new["content_text"] = "第一段。\n\n图表：季度数据。"
        meta = dict(new["raw_metadata"])
        meta.pop("media_refs", None)
        new["raw_metadata"] = meta
        return new, {"images": 1, "captions": 1, "changed": True}

    monkeypatch.setattr(content_main, "caption_stored_content", _fake_stored)

    uid, _ = await new_user()
    article_id = await _new_article_with_refs(uid, urls=["https://cdn.example.com/a.png"])

    async with client(x_callback_secret=SECRET) as c:
        first = await c.post(CAPTIONS_PATH.format(article_id=article_id))
        second = await c.post(CAPTIONS_PATH.format(article_id=article_id))

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["captions"] == 1
    assert second.json()["captions"] == 0, "第二次不该再补一次"
    assert second.json()["changed"] is False
