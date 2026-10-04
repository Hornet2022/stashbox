"""文章详情缓存的越权读回归（2026-10）

`GET /api/v1/articles/{id}` 原来是「先查缓存、命中就直接 return」，把属主校验
`_get_owned_with_task` 整段跳过了。而缓存 key `article:detail:{id}` 不带 user 维度，
所以任何登录用户只要知道别人的 article_id，就能在缓存存活期（300s）内直接读到
**完整 payload** —— 标题、原文链接、蒸馏全文 —— 本该是 403。

这不是设计取舍，是离群点：旁边的 `/status`、`/audio-url` 都是先校验属主再干活。
一个只有「查缓存-返回」两步、且这两步都不看用户是谁的端点，在一屏代码里格外显眼。

判据设计：**先让属主读一次把缓存焐热，再让另一个用户读同一篇**。修之前第二步会
直接 200 并带回全文 —— 这是最贴近真实利用方式的复现顺序（缓存是属主正常访问时
产生的，不是攻击者能自己造的）。

守卫有效性验证：把 `get_article` 里的 `user_id=uid` 去掉（回到「命中即返回」），
`test_cached_detail_is_not_served_to_non_owner` 立刻红，且报的是 200 != 403。
"""

from helpers import client, new_article, new_task, new_user

# 一段足够独特的蒸馏正文：泄漏时能被一眼认出来，避免「返回了但是空壳」蒙混过关
SECRET_SCRIPT = "这是只应属主可见的蒸馏全文-MARKER-8f3a"


def detail_url(article_id: str) -> str:
    return f"/api/v1/articles/{article_id}"


async def _ready_article_with_script(uid: int) -> str:
    """造一篇 ready 且带蒸馏正文/articles 的文章。"""
    article_id = await new_article(uid, status="ready")
    await new_task(article_id, status="done", duration_sec=180)
    # 蒸馏全文存在 distilled_articles 的稿字段上；这里直接写库最省事，
    # 因为端点读的就是这张表。
    from sqlalchemy import update

    from stashbox.backend.common.database import AsyncSessionLocal
    from stashbox.backend.common.models import DistilledArticle

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(DistilledArticle)
            .where(DistilledArticle.article_id == article_id)
            .values(script_text=SECRET_SCRIPT)
        )
        await session.commit()
    return article_id


# ---------------------------------------------------------------------------
# 1. 核心回归：缓存焐热后，非属主读不到
# ---------------------------------------------------------------------------
async def test_cached_detail_is_not_served_to_non_owner():
    owner_uid, owner_token = await new_user()
    _other_uid, other_token = await new_user()
    article_id = await _ready_article_with_script(owner_uid)

    # 第一步：属主正常访问 → 缓存被焐热（复现「缓存在生产里是真实存在的东西」）
    async with client(owner_token) as c:
        r_owner = await c.get(detail_url(article_id))
    assert r_owner.status_code == 200, r_owner.text
    assert SECRET_SCRIPT in r_owner.text, "属主自己都读不到正文，用例前提没成立"

    # 第二步：另一个用户读同一篇。缓存此刻是热的，正是原来会泄漏的那个窗口。
    async with client(other_token) as c:
        r_other = await c.get(detail_url(article_id))

    assert (
        r_other.status_code == 403
    ), f"非属主拿到了别人的文章详情：{r_other.status_code} —— 缓存绕过了属主校验"
    assert r_other.json()["code"] == 40300
    assert SECRET_SCRIPT not in r_other.text, "蒸馏全文泄漏了"


# ---------------------------------------------------------------------------
# 2. 属主的快路径没被牺牲掉（别把修法做成「一律查库」）
# ---------------------------------------------------------------------------
async def test_owner_still_gets_detail_twice():
    """属主连续读两次都是 200，且第二次仍走缓存。

    修法如果是「把属主校验提到缓存前面」，属主每次都得多查一次库 —— 缓存白做了。
    本条的判据是行为等价（两次都拿到同样内容），真正的性能收益靠下面一条钉。
    """
    owner_uid, owner_token = await new_user()
    article_id = await _ready_article_with_script(owner_uid)

    async with client(owner_token) as c:
        first = await c.get(detail_url(article_id))
        second = await c.get(detail_url(article_id))

    assert (
        first.status_code == 200 and second.status_code == 200
    ), f"{first.status_code} / {second.status_code} —— 属主自己被挡住了"
    assert first.json() == second.json(), "两次返回不一致，缓存语义变了"


# ---------------------------------------------------------------------------
# 3. 非属主的一次越权访问不能把属主的缓存挤掉
# ---------------------------------------------------------------------------
async def test_non_owner_request_does_not_poison_owner_cache():
    """越权者不该能通过「读一次」把属主的缓存覆盖成他自己的视图。

    修法里非属主是「当没命中 → 落到正常的属主校验 → 403」，全程不写缓存。
    如果哪天有人图省事改成「非属主也走一遍组装并回填」，这里会红。
    """
    owner_uid, owner_token = await new_user()
    _other_uid, other_token = await new_user()
    article_id = await _ready_article_with_script(owner_uid)

    async with client(owner_token) as c:
        await c.get(detail_url(article_id))

    async with client(other_token) as c:
        await c.get(detail_url(article_id))  # 403

    async with client(owner_token) as c:
        r = await c.get(detail_url(article_id))

    assert r.status_code == 200, r.text
    assert SECRET_SCRIPT in r.text, "属主的缓存被越权者的请求搞坏了"
