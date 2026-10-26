"""admin 端点补测：标签 CRUD 与音色库 CRUD（2026-10-22 走查补齐）。

## 为什么补这两个

走查用「端点路径字面量在测试文件里出现过没有」统计出 **14 个端点零测试**，
其中 10 个是**客户端正在调**的：

- `GET /api/v1/tts/voices`（客户端选音色用）
- `GET/POST /api/v1/admin/tts/voices`、`PUT|DELETE /api/v1/admin/tts/voices/{voice_id}`
- `POST /api/v1/admin/tts/voices/{voice_id}/preview`
- `POST /api/v1/admin/tts/voices/import-from-config`
- `GET /api/v1/admin/tags`、`DELETE /api/v1/admin/tags/{tag_id}`

零测试不等于「有 bug」——但它等于**「坏了也没人知道」**。
后台的音色库和标签管理都是**破坏性操作**（删音色 = 全平台降级到全局 TTS 配置；
删系统标签 = 蒸馏 LLM 的对齐依据没了），所以这两组必须锁住三层：

1. **鉴权**：未登录 401；operator 与 admin 同权（后端 require_admin_or_operator）
2. **归属 / 保护**：系统标签禁删（403）；不存在的东西 404；
   音色删除必须带 ≥5 字符的 reason
3. **幂等 / 副作用**：重复创建同 slug 不该建出两条；
   删除后确实没了；删除会写审计日志
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, DistilledArticle, Tag
from stashbox.backend.common.models.tts_voice import TTSVoice

from helpers import client, new_user

ADMIN_TAGS = "/api/v1/admin/tags"
ADMIN_VOICES = "/api/v1/admin/tts/voices"


# ---------------------------------------------------------------------------
# 通用：造一个能进后台的 token
# ---------------------------------------------------------------------------
async def _staff_token(tier: str = "admin") -> str:
    """造一个 admin/operator 的 JWT。

    后端 admin 端点用 `require_admin_or_operator`（`common/auth_admin.py`），
    判据是 token 里的 tier 属于 {admin, operator}。
    """
    from stashbox.backend.common.auth import create_access_token

    uid, _ = await new_user()
    return create_access_token(str(uid), extra={"tier": tier, "role": tier})


async def _make_tag(name: str, *, is_system: bool = False) -> int:
    async with AsyncSessionLocal() as s:
        tag = Tag(
            slug=f"{name}_{uuid.uuid4().hex[:8]}",
            name=name,
            category="test",
            is_system=is_system,
        )
        s.add(tag)
        await s.commit()
        await s.refresh(tag)
        return tag.id


async def _tag_exists(tag_id: int) -> bool:
    async with AsyncSessionLocal() as s:
        return await s.get(Tag, tag_id) is not None


async def _purge_voice(voice_id: str) -> None:
    async with AsyncSessionLocal() as s:
        v = await s.get(TTSVoice, voice_id)
        if v is not None:
            await s.delete(v)
            await s.commit()


async def _make_distilled_with_tags(tags: list[str]) -> str:
    """造一篇 tags 数组里含给定标签名的已蒸馏文章（distilled_articles.tags 是 JSONB 数组）。

    返回蒸馏记录 id，供断言把验证范围锁在这篇自己造的文章上，避免被共享测试库里
    历史遗留的 tags='null'（JSONB 的 JSON null，`IS NOT NULL` 测不出来）污染。
    """
    async with AsyncSessionLocal() as s:
        uid, _ = await new_user()
        a = Article(
            id=f"art_{uuid.uuid4().hex[:24]}",
            user_id=uid,
            url=f"https://example.com/{uuid.uuid4().hex}.html",
            source="d9",
            status="ready",
            favorite=False,
            skip=False,
        )
        s.add(a)
        await s.flush()
        d = DistilledArticle(
            id=f"dst_{uuid.uuid4().hex[:24]}",
            article_id=a.id,
            status="done",
            tags=tags,
        )
        s.add(d)
        await s.commit()
        return d.id


async def _distilled_tags_contain(tag_name: str) -> list[str]:
    """返回所有 tags 数组里仍含 tag_name 的文章 id（用于断言幽灵标签被剥干净）。"""
    async with AsyncSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(DistilledArticle).where(
                        DistilledArticle.tags.is_not(None),
                        DistilledArticle.tags.op("@>")(
                            func.cast(func.json_build_array(tag_name), JSONB)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [r.id for r in rows]


# ---------------------------------------------------------------------------
# 1. GET /api/v1/admin/tags
# ---------------------------------------------------------------------------


async def test_admin_list_tags_返回数字id与系统标记():
    """admin 列表必须带**数字 id**（删除端点按数字 id 定位），
    用户端那个 slug 版本驱动不了删除按钮。"""
    token = await _staff_token()
    tag_id = await _make_tag("通用标签")

    async with client(token) as c:
        r = await c.get(ADMIN_TAGS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert "tags" in body
    item = next(t for t in body["tags"] if t["id"] == tag_id)
    assert item["slug"].startswith("通用标签")
    assert item["is_system"] is False
    assert item["subscriber_count"] == 0
    assert isinstance(item["id"], int), "id 必须是数字 —— 删除端点按数字 id 定位"


async def test_admin_list_tags_未登录_401():
    async with client() as c:
        r = await c.get(ADMIN_TAGS)
    assert r.status_code == 401, r.text


async def test_admin_list_tags_operator_也可访问():
    """后端是 require_admin_or_operator —— admin 与 operator 同权。"""
    token = await _staff_token("operator")
    async with client(token) as c:
        r = await c.get(ADMIN_TAGS)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 2. DELETE /api/v1/admin/tags/{tag_id}
# ---------------------------------------------------------------------------


async def test_admin_delete_tag_正常删除():
    token = await _staff_token()
    tag_id = await _make_tag("可删标签")

    async with client(token) as c:
        r = await c.request("DELETE", f"{ADMIN_TAGS}/{tag_id}", json={"reason": "测试清理这个标签"})

    assert r.status_code == 200, r.text
    assert await _tag_exists(tag_id) is False, "标签必须真的没了"


async def test_admin_delete_tag_reason太短_400():
    """reason ≥5 字符是审计要求（与 articles / voices 一致）。"""
    token = await _staff_token()
    tag_id = await _make_tag("原因太短")

    async with client(token) as c:
        r = await c.request("DELETE", f"{ADMIN_TAGS}/{tag_id}", json={"reason": "删"})

    assert r.status_code == 400, r.text
    assert await _tag_exists(tag_id) is True, "校验失败不该把标签删掉"


async def test_admin_delete_tag_系统标签_403():
    """系统标签禁删 —— 蒸馏 LLM 输出对齐依赖内置标签集。"""
    token = await _staff_token()
    tag_id = await _make_tag("系统标签", is_system=True)

    async with client(token) as c:
        r = await c.request("DELETE", f"{ADMIN_TAGS}/{tag_id}", json={"reason": "试试删掉系统标签"})

    assert r.status_code == 403, r.text
    assert await _tag_exists(tag_id) is True


async def test_admin_delete_tag_不存在_404():
    token = await _staff_token()
    async with client(token) as c:
        r = await c.request(
            "DELETE", f"{ADMIN_TAGS}/99999999", json={"reason": "删一个不存在的标签"}
        )
    assert r.status_code == 404, r.text


async def test_admin_delete_tag_未登录_401():
    tag_id = await _make_tag("未登录要删")
    async with client() as c:
        r = await c.request("DELETE", f"{ADMIN_TAGS}/{tag_id}", json={"reason": "未登录也要删除"})
    assert r.status_code == 401, r.text
    assert await _tag_exists(tag_id) is True


async def test_admin_delete_tag_剥掉文章里的幽灵标签名():
    """CP-DELETE-TAG-CLEANUP：删标签时**同事务**把该标签名从已蒸馏文章的 tags 数组剥掉。

    distilled_articles.tags 是 JSONB 文本数组（非 FK），删了 tag 不清理，
    文章详情/订阅流会把「已不存在的标签」渲染给用户。本用例验证：
    1. 含该标签名（混在其它标签里）的文章被剥掉该名，其它标签保留；
    2. 不含该标签名的文章不受影响。
    """
    token = await _staff_token()
    tag_id = await _make_tag("幽灵标签")
    d1 = await _make_distilled_with_tags(["幽灵标签", "科技"])
    d2 = await _make_distilled_with_tags(["商业"])

    async with client(token) as c:
        r = await c.request("DELETE", f"{ADMIN_TAGS}/{tag_id}", json={"reason": "清理幽灵标签名"})

    assert r.status_code == 200, r.text
    assert await _tag_exists(tag_id) is False
    # 名字必须被剥干净：没有任何文章的 tags 还挂着它
    assert await _distilled_tags_contain("幽灵标签") == [], "幽灵标签名必须被剥干净"

    # 其它标签不受影响（剥空的那篇只剩 "科技"）。验证范围锁在本次造的两篇上，
    # 不扫全表 —— 共享测试库里可能有历史遗留的 tags='null'（JSONB 的 JSON null，
    # `IS NOT NULL` 筛不掉，但 ORM 会反序列化成 Python None 触发 tuple(None)）。
    async with AsyncSessionLocal() as s:
        rows = (
            (await s.execute(select(DistilledArticle).where(DistilledArticle.id.in_([d1, d2]))))
            .scalars()
            .all()
        )
        tag_sets = {tuple(r.tags) for r in rows}
    assert ("科技",) in tag_sets, "同文的其它标签必须保留"
    assert ("商业",) in tag_sets, "不含该名的文章必须原样保留"


# ---------------------------------------------------------------------------
# 3. 音色库 CRUD
# ---------------------------------------------------------------------------


def _voice_payload(slug: str) -> dict:
    return {
        "slug": slug,
        "display_name": f"音色-{slug}",
        "ref_audio_url": f"/var/lib/stashbox/ref/{slug}.mp3",
        "ref_text": "这是参考文本，用来克隆音色。",
        "description": "测试用音色",
    }


async def test_admin_list_voices_200():
    token = await _staff_token()
    async with client(token) as c:
        r = await c.get(ADMIN_VOICES)
    assert r.status_code == 200, r.text
    assert "items" in r.json()


async def test_admin_create_voice_200():
    token = await _staff_token()
    slug = f"t_{uuid.uuid4().hex[:8]}"
    try:
        async with client(token) as c:
            r = await c.post(ADMIN_VOICES, json=_voice_payload(slug))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["slug"] == slug
        assert body["display_name"] == f"音色-{slug}"
        await _purge_voice(body["id"])
    finally:
        pass


async def test_admin_create_voice_缺必填字段_422():
    """缺参考文本 → **422**（pydantic 的必填校验，不是 500）。

    参考文本必须与音频内容一致，否则克隆出的音色会念错 —— 所以它不是可选项。
    """
    token = await _staff_token()
    payload = _voice_payload(f"t_{uuid.uuid4().hex[:8]}")
    payload.pop("ref_text")
    async with client(token) as c:
        r = await c.post(ADMIN_VOICES, json=payload)
    assert r.status_code == 422, r.text


async def test_admin_create_voice_语义校验不通过_400():
    """字段在但**内容不合法**（参考文本为空）→ 400 业务错误。

    422 是 pydantic 的「形状不对」，400 是业务层的「这个音色不成立」——
    两码事，测试要分别钉住。
    """
    token = await _staff_token()
    payload = _voice_payload(f"t_{uuid.uuid4().hex[:8]}")
    payload["ref_text"] = ""
    async with client(token) as c:
        r = await c.post(ADMIN_VOICES, json=payload)
    assert r.status_code == 400, r.text


async def test_admin_create_voice_未登录_401():
    async with client() as c:
        r = await c.post(ADMIN_VOICES, json=_voice_payload(f"t_{uuid.uuid4().hex[:8]}"))
    assert r.status_code == 401, r.text


async def test_admin_update_voice_200():
    token = await _staff_token()
    slug = f"t_{uuid.uuid4().hex[:8]}"
    async with client(token) as c:
        created = await c.post(ADMIN_VOICES, json=_voice_payload(slug))
    assert created.status_code == 200, created.text
    voice_id = created.json()["id"]
    try:
        async with client(token) as c:
            r = await c.put(
                f"{ADMIN_VOICES}/{voice_id}",
                json={"display_name": "改过的名字", "description": "改过的描述"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["display_name"] == "改过的名字"
        assert r.json()["description"] == "改过的描述"
    finally:
        await _purge_voice(voice_id)


async def test_admin_delete_voice_必须带reason():
    """删除音色必须带 ≥5 字符 reason —— 删默认音色会让全平台用户静默降级，
    事后审计要查得到是谁删的、为什么删。"""
    token = await _staff_token()
    slug = f"t_{uuid.uuid4().hex[:8]}"
    async with client(token) as c:
        created = await c.post(ADMIN_VOICES, json=_voice_payload(slug))
    assert created.status_code == 200, created.text
    voice_id = created.json()["id"]
    try:
        async with client(token) as c:
            r = await c.request("DELETE", f"{ADMIN_VOICES}/{voice_id}", json={"reason": "删"})
        assert r.status_code == 400, r.text
        # 还在
        async with client(token) as c:
            r2 = await c.get(ADMIN_VOICES)
        assert any(v["id"] == voice_id for v in r2.json()["items"])
    finally:
        await _purge_voice(voice_id)


async def test_admin_delete_voice_正常删除():
    token = await _staff_token()
    slug = f"t_{uuid.uuid4().hex[:8]}"
    async with client(token) as c:
        created = await c.post(ADMIN_VOICES, json=_voice_payload(slug))
    assert created.status_code == 200, created.text
    voice_id = created.json()["id"]

    async with client(token) as c:
        r = await c.request(
            "DELETE", f"{ADMIN_VOICES}/{voice_id}", json={"reason": "测试清理这个音色"}
        )
    assert r.status_code == 200, r.text

    async with client(token) as c:
        r2 = await c.get(ADMIN_VOICES)
    assert not any(v["id"] == voice_id for v in r2.json()["items"]), "音色必须真的没了"


async def test_admin_voice_端点未登录_401():
    for method, path, kwargs in (
        ("get", ADMIN_VOICES, {}),
        ("post", ADMIN_VOICES, {"json": _voice_payload("t_x")}),
        ("put", f"{ADMIN_VOICES}/v_x", {"json": {"display_name": "x"}}),
        ("delete", f"{ADMIN_VOICES}/v_x", {"json": {"reason": "删一个"}}),
    ):
        async with client() as c:
            # httpx 的 delete() 不吃 json=（只有 content=）→ 统一走 request()
            r = await c.request(method.upper(), path, **kwargs)
        assert r.status_code == 401, f"{method.upper()} {path} 未登录应是 401：{r.text}"
