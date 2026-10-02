"""CP-TTS-VOICE BUG#12：`POST /distill/start` 重复调用会重复入队。

**bug**：端点对**配额**做了幂等（已有蒸馏行就不重复扣），但对**入队**没有。
`DistillDispatcher.enqueue_distill` 直接 `enqueue_job(...)`，不带任何幂等键，
所以对同一篇文章连续调两次 = 两个独立 Arq job，两者都会写**同一行**
`distilled_articles`。

后果：
  - 白烧一轮 GPU（单篇实测约 23 分钟）；
  - 两个 worker 并发写同一行，A 的音频可能被 B 覆盖，script_text / tags /
    audio_url 来自不同轮次，产物是**混的**；
  - 两次的耗时叠加，一篇可能跑 46 分钟。

**为什么会真的发生**，而不只是"理论上可以重复调"：
  1. 客户端响应丢失 → 重试 → 两次入队（最常见，幂等性问题里排第一的成因）；
  2. 安卓端目前靠 `status == READY` 门禁挡住重复点击，但那是**客户端**保护。
     服务端一旦被任何脚本/第三方/回归测试直接调用就没有这层；
  3. 和 BUG#7 有因果关系：蒸馏完成后详情缓存没失效，用户重进详情页看到的
     仍是 READY，于是「换音色 → 重新生成」入口还在，再点一次就重复入队。

修法：已有蒸馏行且处于 `queued` / `running` 时**不入队**，原样返回既有
task_id（并把 status 回报成 running），让调用方拿到的是同一个任务而不是新任务。
`done` / `failed` 仍允许重跑 —— 那正是「重新生成」和「失败重试」要的行为。

前置：本机 PG + Redis 已起，且已 `alembic upgrade head`（含 0033）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.models import Article, DistilledArticle, User

AI_DIR = Path(__file__).resolve().parents[2] / "ai-service"

#: 这些状态表示「已经有活儿在跑了」，重复入队只会打架
IN_FLIGHT = ("queued", "running")


def _load_ai_main():
    name = "_bug12_ai_main"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, AI_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def owner_and_article():
    """一个用户 + 一篇 pending 文章（还没有蒸馏行）。"""
    async with AsyncSessionLocal() as db:
        user = User(
            open_id="cp_bug12_" + __import__("uuid").uuid4().hex[:20],
            nickname="pytest",
            tier="free",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        uid = int(user.id)
        aid = f"art_{__import__('uuid').uuid4().hex[:24]}"
        db.add(
            Article(
                id=aid,
                user_id=uid,
                url="https://mp.weixin.qq.com/s/bug12",
                source="wechat_mp",
                title="BUG#12 探针",
                status="pending",
                favorite=False,
                skip=False,
            )
        )
        await db.commit()

    yield uid, aid

    async with AsyncSessionLocal() as db:
        await db.execute(delete(DistilledArticle).where(DistilledArticle.article_id == aid))
        await db.execute(delete(Article).where(Article.id == aid))
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()


@pytest.fixture
def dispatcher_spy(monkeypatch):
    """替换全局 dispatcher，记录每次入队的 (task_id, article_id)。

    顺带把配额判定 stub 掉：本目录的 conftest 有个 autouse fixture 把
    `redis.asyncio.Redis` 换成 no-op 桩，而 `has_article_quota` 走 `client.exists`，
    桩没实现好会抛 `'coroutine' object is not callable`，把本用例的断言全带偏。
    这里要验的是**入队幂等**，不是配额，配额由别的用例管。
    """
    module = _load_ai_main()
    calls: list[tuple[str, str]] = []

    class _FakeDispatcher:
        async def enqueue_distill(self, *, task_id, article_id, user_id, url, title=None, **kw):
            calls.append((task_id, article_id))
            return f"job_{len(calls)}"

    async def _no_quota(article_id: str) -> bool:
        return False

    async def _mark_quota(article_id: str) -> None:
        return None

    async def _consume(db, user_id, amount=1):
        # 配额路径会一路走到 cache_service 的 Lua 脚本（register_script），
        # 而本目录的 Redis 是 no-op 桩，会抛 'coroutine' object is not callable。
        # 本用例验的是入队幂等，配额另有专门用例，不在这里掺和。
        #
        # 必须返回**真实形状的 dict**而不是 None：/api/v1/distill/start 不读返回值，
        # 但 /api/v1/articles/{id}/distill 会 `quota["quota_used"]`，
        # 返回 None 会 TypeError 把入队断言一起带偏（看起来像守卫失效，其实是桩的问题）。
        return {"quota_used": amount, "monthly_quota": 99, "remaining": 99 - amount}

    monkeypatch.setattr(module, "get_dispatcher", lambda: _FakeDispatcher())
    monkeypatch.setattr(module.cache_service, "has_article_quota", _no_quota)
    monkeypatch.setattr(module.cache_service, "mark_article_quota", _mark_quota)
    monkeypatch.setattr(module.quota_service, "consume", _consume)

    async def _get_quota(db, user_id):
        # 同上：get_quota 会走 cache_service.set_quota → register_script，
        # 本目录的 Redis 是 no-op 桩会抛 'coroutine' object is not callable。
        # /api/v1/articles/{id}/distill 的响应里要回 quota_used，必须有桩。
        return {"quota_used": 1, "monthly_quota": 99, "remaining": 98}

    monkeypatch.setattr(module.quota_service, "get_quota", _get_quota)
    return calls


@pytest_asyncio.fixture
async def client():
    """ASGI 客户端直打 ai-service，鉴权走真 JWT。"""
    import httpx

    module = _load_ai_main()
    transport = httpx.ASGITransport(app=module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth(uid: int) -> dict:
    from stashbox.backend.common.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(str(uid))}"}


async def _start(client, uid: int, aid: str):
    return await client.post(
        "/api/v1/distill/start",
        headers={**_auth(uid), "Content-Type": "application/json"},
        json={"article_id": aid},
    )


@pytest.mark.asyncio
async def test_连续两次入队只应排一个任务(owner_and_article, client, dispatcher_spy):
    """核心回归：第二次调用必须**不**再入队。"""
    uid, aid = owner_and_article

    r1 = await _start(client, uid, aid)
    assert r1.status_code == 200, r1.text
    r2 = await _start(client, uid, aid)
    assert r2.status_code == 200, r2.text

    assert len(dispatcher_spy) == 1, (
        f"同一篇文章入队了 {len(dispatcher_spy)} 次：{dispatcher_spy} —— "
        f"两个 Arq job 会并发写同一行 distilled_articles，产物会混"
    )


@pytest.mark.asyncio
async def test_重复调用返回同一个task_id(owner_and_article, client, dispatcher_spy):
    """幂等的另一半：调用方拿到的必须是同一个任务，而不是新任务。"""
    uid, aid = owner_and_article

    r1 = await _start(client, uid, aid)
    r2 = await _start(client, uid, aid)

    assert (
        r1.json()["task_id"] == r2.json()["task_id"]
    ), "第二次调用返回了新的 task_id，App 会以为换了任务在跑"


@pytest.mark.asyncio
async def test_已完成的文章仍可重新生成(owner_and_article, client, dispatcher_spy):
    """`done` 状态**必须**还能重入队 —— 那正是「换音色重新生成」的行为。

    这是 BUG#12 修复最容易踩的坑：把「已在跑」判成「已存在」一并挡掉，
    重生成功能就整个死了，而它的 8 条后台用例一条都不跑重生成入口。
    """
    uid, aid = owner_and_article
    await _start(client, uid, aid)
    assert len(dispatcher_spy) == 1

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(DistilledArticle).where(DistilledArticle.article_id == aid))
        ).scalar_one()
        row.status = "done"
        await db.commit()

    r = await _start(client, uid, aid)
    assert r.status_code == 200, r.text
    assert len(dispatcher_spy) == 2, "已完成的文章必须还能重新入队，否则重生成功能死了"


@pytest.mark.asyncio
async def test_失败的文章可以重试(owner_and_article, client, dispatcher_spy):
    """`failed` 同理：失败重试是独立于重生成的能力，别一起挡掉。"""
    uid, aid = owner_and_article
    await _start(client, uid, aid)

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(DistilledArticle).where(DistilledArticle.article_id == aid))
        ).scalar_one()
        row.status = "failed"
        await db.commit()

    r = await _start(client, uid, aid)
    assert r.status_code == 200, r.text
    assert len(dispatcher_spy) == 2


# ---------------------------------------------------------------------------
# 同一条守卫，但打在**另一个端点**上
#
# 上面四个用例验的是 /api/v1/distill/start。生产剪藏链路走的却是
# /api/v1/articles/{id}/distill（content-service 的 trigger_distill 拼的就是它），
# 而「已在跑就不再入队」的守卫当初**只加在前一个端点上**，后者无条件 enqueue。
#
# 实测（2026-10-02，华为 JEF-AN20 剪藏一篇 838 字文章）：安卓在
# POST /api/v1/articles 之后又自己调了一次这个端点（相隔 163ms）→ 同一篇文章
# 入队两个独立 Arq job（payload 完全相同、含同一个 task_id，但 job_id 是新生成的，
# Arq 的去重拦不住）。后果是单篇耗时翻倍并双倍烧 LLM/TTS，第一遍音频整个丢弃：
#   10:53:57 job A 开始 TTS(7段) → 11:04:59 A 完成
#   11:04:59 job B 被取走（入队于 10:53:42，delayed=676.97s）→ 11:05:13 B 重做(8段)
#
# 教训写在这里免得再犯：**幂等要么两个端点都有，要么两个都没有**。
# 「不重复扣配额」不等于「幂等」—— 计费和入队是两条独立的路径。
# ---------------------------------------------------------------------------


async def _start_by_article(client, uid: int, aid: str):
    return await client.post(f"/api/v1/articles/{aid}/distill", headers=_auth(uid))


@pytest.mark.asyncio
async def test_文章级端点连续两次调用只应排一个任务(owner_and_article, client, dispatcher_spy):
    """安卓剪藏会连打两次这个端点，必须只入队一个。"""
    uid, aid = owner_and_article

    r1 = await _start_by_article(client, uid, aid)
    assert r1.status_code == 200, r1.text
    r2 = await _start_by_article(client, uid, aid)
    assert r2.status_code == 200, r2.text

    assert len(dispatcher_spy) == 1, (
        f"文章级端点入队了 {len(dispatcher_spy)} 次：{dispatcher_spy} —— "
        f"重复入队会让单篇蒸馏跑两遍（实测 ~23 分钟 vs ~12），算力与 LLM 费用双倍"
    )


@pytest.mark.asyncio
async def test_文章级端点幂等时返回真实在跑状态(owner_and_article, client, dispatcher_spy):
    """幂等分支要回报**真实**状态，而不是复用非幂等分支的 "started"。

    否则调用方无法区分「刚排上」和「已经在跑了」。
    """
    uid, aid = owner_and_article

    r1 = await _start_by_article(client, uid, aid)
    r2 = await _start_by_article(client, uid, aid)

    assert r1.json()["task_id"] == r2.json()["task_id"]
    assert r2.json()["job_id"] == "", "幂等分支不该凭空编一个 job_id"
    assert r2.json()["status"] in (
        "queued",
        "running",
    ), f"幂等分支应回报真实在跑状态，实际 {r2.json()['status']!r}"
    assert r2.json()["quota_consumed"] is False, "幂等分支不该再报扣了配额"
