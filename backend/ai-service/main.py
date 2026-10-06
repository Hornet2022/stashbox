"""
ai-service（端口 8103） - L4 蒸馏 worker（mock 流水线）+ 任务状态机。

CP1.5：蒸馏任务写真实 PostgreSQL（distilled_articles 表），状态机 queued → running → done。
本期 mock：用 asyncio.sleep(2) 模拟每步耗时，不调真实 LLM / TTS（CP3 才接）。
不读 Article 表（CP3 才接 D9 → 蒸馏全链路）；distill/{id} 经 articles 校验归属。
"""

import asyncio
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common import cache_service, quota_service
from stashbox.backend.common.auth import require_user
from stashbox.backend.common.database import AsyncSessionLocal, get_db
from stashbox.backend.common.exceptions import (
    Forbidden,
    InvalidRequest,
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.models import Article, DistilledArticle
from stashbox.backend.common.observability import install_health_endpoints
from stashbox.backend.common.redis_client import get_redis_pool
from stashbox.backend.common.analytics import track_simple
from stashbox.backend.common.events import EventName

# ai-service 目录名带连字符，不能当包导入；且 dispatcher 等平级模块（DistillDispatcher、
# arq_settings 等）需要本目录在 sys.path 上才能 `from dispatcher import ...`。
# 补上这一行是为了与 content-service/main.py:50 的写法对齐。
#
# 没有它的时候：从 `backend/` 跑 pytest tests/observability 会在收集期抛
# `ModuleNotFoundError: No module named 'dispatcher'`（observability 7 个文件里
# 6 个 ERROR）—— 因为那条路径是按文件路径加载 main.py 的，cwd 不是 ai-service/。
# 平时靠 run_dev.sh 从服务目录起服，掩盖了这个问题。
sys.path.insert(0, str(Path(__file__).resolve().parent))  # noqa: E402

from dispatcher import get_dispatcher, shutdown_dispatcher  # noqa: E402

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """CP3.6.1：FastAPI lifespan 替代 deprecated @app.on_event。

    startup 期间启动队列 poller（try/except 包住，不拖垮服务）。
    shutdown 期间关闭 Arq 连接池。
    """
    # ---- startup: 队列 poller ----
    poll_task = None
    try:
        from observability.metrics import DISTILL_QUEUE_SIZE
        from arq_settings import load_arq_config

        cfg = load_arq_config()
        queue_name = cfg.queue_name
        redis_url = cfg.redis_url

        async def _poll_loop():
            client = aioredis.from_url(redis_url)
            while True:
                try:
                    size = await client.zcard(queue_name)
                    DISTILL_QUEUE_SIZE.labels(queue=queue_name).set(size)
                except Exception as exc:
                    log.warning("queue poller 失败（忽略）: %s", exc)
                await asyncio.sleep(30)

        poll_task = asyncio.create_task(_poll_loop())
        log.info("distill queue poller 已启动")
    except Exception as exc:
        log.error("distill queue poller 启动失败（忽略）: %s", exc)
    # CP6.2.2.2b 埋点：SERVICE_START
    try:
        async with AsyncSessionLocal() as session:
            await track_simple(session, EventName.SERVICE_START, 0, "n/a")
            await session.commit()  # track() 只 flush 不 commit
    except Exception:
        pass  # 失败不阻塞 startup

    yield

    # ---- shutdown: 关闭 poller + Arq ----
    if poll_task:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
    await shutdown_dispatcher()
    # CP3.6.2：lifespan shutdown 关闭所有 cached LLM clients（httpx 连接池）
    try:
        from llm import close_all_llm_clients

        await close_all_llm_clients()
    except Exception as exc:
        log.warning("close_all_llm_clients_failed", error=str(exc))
    # CP6.2.2.2b 埋点：SERVICE_STOP
    try:
        async with AsyncSessionLocal() as session:
            await track_simple(session, EventName.SERVICE_STOP, 0, "n/a")
            await session.commit()  # track() 只 flush 不 commit
    except Exception:
        pass


setup_logging("ai-service")
app = FastAPI(title="stashbox-ai-service", version="0.2.0", lifespan=lifespan)
register_exception_handlers(app)
# B2：admin 只读看板端点（few-shot 池 / evaluations / 变体统计 / consents）
#
# 按文件路径加载而不是 `from admin_router import router`：content-service 也有
# 一个同名 admin_router.py，两边都是顶层 import 会共享 sys.modules["admin_router"]。
# 同时跑 tests/ai + tests/gateway 时 gateway 的 conftest 先加载 content-service，
# 这里就会把 content-service 的 admin 路由挂进 ai-service 的 app。
# 唯一模块名把这个共享状态消除（content-service 那边同样处理了）。
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "ai_service_admin_router", Path(__file__).resolve().parent / "admin_router.py"
)
_mod = _ilu.module_from_spec(_spec)
sys.modules["ai_service_admin_router"] = _mod
_spec.loader.exec_module(_mod)
admin_router = _mod.router

app.include_router(admin_router)
app.add_middleware(RequestIDMiddleware)
install_health_endpoints(app)


# CP11.0.3 Prometheus metrics endpoint（暴露 step duration / success / failure / queue size）
@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# mock 4 步蒸馏流水线（本期不落库每一步，仅用其耗时模拟）
_STEPS = [
    ("step1", "多模态理解（Qwen2.5-VL mock）", 0.25),
    ("step2", "听感改写（Claude Sonnet mock）", 0.50),
    ("step3", "TTS 合成（豆包 TTS mock）", 0.75),
    ("step4", "音频拼接（FFmpeg mock）", 1.00),
]


def _new_task_id() -> str:
    return f"dst_{uuid.uuid4().hex[:24]}"


#: CP-TTS-VOICE BUG#12：这些状态说明**已经有活儿在跑了**，此时重复入队只会打架。
#: 放在模块级而不是端点内，是为了和 `tests/ai/test_distill_start_idempotent.py`
#: 里的 `IN_FLIGHT` 对齐 —— 判据散落两处迟早会漂。
IN_FLIGHT_DISTILL_STATUSES = ("queued", "running")


def _requeue_or_create(
    db: AsyncSession, existed_da: DistilledArticle | None, article_id: str
) -> str:
    """重跑蒸馏时复用已有行，或新建一行，返回 task_id。

    CP-DISTILL-NONDESTRUCTIVE：`distilled_articles.article_id` 上有唯一约束
    （distilled_articles_article_id_key），二次蒸馏不能再 INSERT，否则撞
    UniqueViolationError → PendingRollbackError → 500，所以必须复用原行。

    但复用时**不清空产物字段**。原实现在这里把 script_text / audio_url /
    duration_sec / quality_score / tags 一律置 None，代价是：
      - 正在收听的用户音频立刻失效
      - 已完成的产物元数据不可恢复（本轮验收就误伤了一篇真机在用文章，
        script_text / tags / quality_score 全丢，只能靠重跑 TTS 找回）
    现在只改 status，保留旧产物，等 worker 跑成功时由它覆盖。
    这样"重跑"退化为"刷新"，失败也不会把用户已有的东西打掉。
    """
    if existed_da is not None:
        existed_da.status = "queued"
        return existed_da.id
    task_id = _new_task_id()
    db.add(
        DistilledArticle(
            id=task_id,
            article_id=article_id,
            status="queued",
            audio_url=None,
            script_text=None,
        )
    )
    return task_id


async def _run_pipeline(task_id: str, simulate_failure: bool = False) -> None:
    """mock 4 步蒸馏流水线，结果写回 distilled_articles。

    CP1.6：simulate_failure=True 时走失败分支 → failed + 退还配额。
    真实蒸馏失败（CP3 接 LLM/TTS 后）走同一条退还路径。

    CP3.5-pre-3：端点已改用 Arq（见 dispatcher.enqueue_distill / tasks.distill_task），
    本函数保留作 sync fallback（紧急回滚可临时切回 BackgroundTasks，单测也直接用它）。
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DistilledArticle).where(DistilledArticle.id == task_id)
        )
        da = result.scalar_one_or_none()
        if da is None:
            return
        da.status = "running"
        await session.commit()

        for _step_key, _step_name, _progress in _STEPS:
            await asyncio.sleep(2)

        if simulate_failure:
            da.status = "failed"
            await session.commit()
            # 蒸馏失败 → 退还配额（quota_used-1, quota_version+1）+ 缓存失效
            result = await session.execute(select(Article).where(Article.id == da.article_id))
            art = result.scalar_one_or_none()
            if art is not None:
                await quota_service.refund(session, int(art.user_id))
                await cache_service.clear_article_quota(da.article_id)  # 退还后允许重扣
            return

        da.status = "done"
        da.audio_url = f"https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com/{da.article_id}.m4a"
        da.duration_sec = 300
        da.tags = ["科技", "商业"]
        da.quality_score = 8.5
        # CP10: 写回 articles.status=ready + audio_url
        art_result = await session.execute(select(Article).where(Article.id == da.article_id))
        art = art_result.scalar_one_or_none()
        if art is not None:
            art.status = "ready"
            art.audio_url = da.audio_url
            await session.flush()
        await session.commit()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class DistillStartRequest(BaseModel):
    article_id: str
    # url / title 可选且**被忽略** —— 端点一律取库里的值（见 distill_start
    # docstring：传什么蒸馏什么等于允许把 A 的产物写到 B 上）。
    # 这两个字段保留只为兼容老调用方；曾经是必填，App 的「用新音色重新生成」
    # 按钮只该传 article_id，被迫编个 url 出来反而容易传错。
    url: str | None = None
    title: str | None = None


class DistillStartResponse(BaseModel):
    task_id: str
    article_id: str
    status: str
    job_id: str = ""  # CP3.5-pre-3：Arq job_id（BackgroundTasks 时代没有）
    # CP-TTS-VOICE：本次会用哪个音色合成。让 App 能在「重新生成」确认弹窗里
    # 告诉用户「将用『男声』重新生成」，而不是让用户点了之后不知道会变成谁。
    voice_id: str | None = None
    voice_name: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """遗留别名（CP1.6 在用）：CP6.4 起新增 /healthz /readyz，本端点保持不动。"""
    redis_status = "ok"
    client = None
    try:
        client = aioredis.Redis(connection_pool=get_redis_pool())
        await client.ping()
    except Exception:
        redis_status = "error"
    finally:
        if client is not None:
            await client.aclose()
    return {"status": "ok", "service": "ai-service", "redis": redis_status}


@app.post("/api/v1/distill/start", response_model=DistillStartResponse)
async def distill_start(
    req: DistillStartRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """按 article_id 触发蒸馏。

    CP-DISTILL-START-HARDEN（本轮修复的三个问题，都是实测出来的）：

    1) **越权**。原实现只信请求体里的 article_id/url，从不查 Article，
       任何人拿到别人的 article_id 就能对其重跑蒸馏。实测：以 user 9277 的
       token 对 user 1 名下的 art_wxSOP_verify_0001 调本端点返回 200。
       现在查库校验归属，非本人 → 403（与 /articles/{id}/distill 同口径）。

    2) **不扣配额**。本端点从头到尾没碰过 quota_service，而
       /articles/{id}/distill 扣。同一件事两条路径、两种计费口径。

    3) **破坏性复用**。文章已有蒸馏行时复用原行，并把 script_text / audio_url /
       duration_sec / quality_score / tags 一律置空。实测把一篇 status=done、
       有音频的真机在用文章打成了 status=queued，产物元数据全部丢失，且无法找回。
       改为**保留旧产物**：重跑期间用户仍能听旧音频，worker 跑成功才覆盖。

    url / title 一律取库里的值，不用请求体传的——传什么蒸馏什么，等于允许
    调用方把 A 文章的产物写到 B 上。

    CP-TTS-VOICE BUG#12：入队侧补幂等 —— 已有蒸馏行且处于 `queued` / `running`
    时不再入队，原样返回既有 task_id（见下方注释与
    `tests/ai/test_distill_start_idempotent.py`）。

    CP-TTS-VOICE：**本端点同时是「用当前音色重新生成」的入口**，不另开新端点。
    理由是本项目已经吃过亏 —— 见上面第 2) 条：「同一件事两条路径、两种计费口径」。
    再加一个 `/re-distill` 只会重演：它要么不扣费（和这里不一致），要么重复扣费
    （用户点两次就少两次配额）。音色是合成期参数，worker 里
    `resolve_voice_for_user()` 会重新解析用户当前选的音色，所以重跑自然生效。
    """
    uid = int(user["sub"])

    # 提前解析一次音色回给 App（worker 里会再解析一次；这里纯粹为了回显，
    # 两次之间用户改了偏好的话以 worker 那次为准，不追求强一致）。
    # import 提到函数开头：下面的幂等分支要直接 return 用到它。
    from stashbox.backend.common.tts_voice_service import resolve_voice_for_user

    art = await db.scalar(select(Article).where(Article.id == req.article_id))
    if art is None:
        raise NotFound(message=f"article {req.article_id} not found")
    if art.user_id != uid:
        raise Forbidden(message="not the owner of this article")
    url = art.url
    title = art.title

    # 配额：与 /articles/{id}/distill 同口径 —— 已有蒸馏行 **或** content-service
    # 打过「已扣配额」标，都视为已扣过。缺第二个判据会重复扣费（CP-AI-CHARGE-DUP）。
    existed_da = await db.scalar(
        select(DistilledArticle).where(DistilledArticle.article_id == req.article_id)
    )

    # CP-TTS-VOICE BUG#12：**已在跑就不再入队**，原样返回既有任务。
    #
    # 端点此前只对**配额**做了幂等（已有蒸馏行就不重复扣），对**入队**没有 ——
    # `enqueue_distill` 直接 `enqueue_job(...)` 不带任何幂等键，于是对同一篇文章
    # 连调两次 = 两个独立 Arq job，两者都写**同一行** distilled_articles。后果：
    # 白烧一轮 GPU（单篇实测约 23 分钟）、两个 worker 并发写同一行导致产物是混的、
    # 耗时叠加到 46 分钟。
    #
    # 真的会发生的路径不是"理论上能重复调"：客户端响应丢失后重试（幂等性问题里
    # 排第一的成因）、以及和 BUG#7 的因果链 —— 蒸馏完成后详情缓存没失效，用户重进
    # 详情页看到的仍是 READY，「换音色重新生成」入口还在，再点一次就重复入队。
    #
    # 只挡 `queued` / `running`；`done` / `failed` 仍放行 —— 那正是「换音色重新生成」
    # 和「失败重试」依赖的行为，一起挡掉会让重生成功能整个死掉
    # （见 tests/ai/test_distill_start_idempotent.py 的两个回归用例）。
    #
    # ⚠️ 已知取舍：worker 崩溃会让行永远停在 `queued`，此时用户既不能重试也不能重生成，
    # 只能靠运维捞。这是**刻意不**加超时兜底的原因 —— 超时窗口要按「最慢一轮蒸馏
    # 耗时」定（本项目约 23 分钟），定短了会误杀正常长任务，真要做需要单独评估。
    if existed_da is not None and existed_da.status in IN_FLIGHT_DISTILL_STATUSES:
        voice = await resolve_voice_for_user(uid)
        return DistillStartResponse(
            task_id=existed_da.id,
            article_id=req.article_id,
            # 回报**真实**状态而不是写死 running：调用方要能区分「刚排上」和「已在跑」。
            status=existed_da.status,
            # 幂等分支不产生新 job，也就没有 job_id 可回；留空而不是编一个。
            job_id="",
            voice_id=voice.voice_id,
            voice_name=voice.display_name,
        )

    # 扣费权用 `claim_article_quota`（SET NX）原子认领，而不是
    # 「EXISTS 看看 → 扣 → SET 标记」：后者是非原子的 check-then-act，
    # 两个并发请求都会看到「没扣过」而各扣一次。认领在前、扣费失败则还回去，
    # 详见 cache_service.claim_article_quota 的说明。
    if existed_da is None and await cache_service.claim_article_quota(req.article_id):
        try:
            await quota_service.consume(db, uid)  # 用尽抛 3001
        except Exception:
            # 扣费失败（配额用尽 3001 / DB 故障）就把认领还回去，否则「已扣费」
            # 标记会挡住这篇文章之后所有重试 —— 用户就再也不会被扣，等于白送。
            await cache_service.clear_article_quota(req.article_id)
            raise

    task_id = _requeue_or_create(db, existed_da, req.article_id)
    art.status = "distilling"
    await db.commit()

    job_id = await get_dispatcher().enqueue_distill(
        task_id=task_id,
        article_id=req.article_id,
        user_id=uid,
        url=url,
        title=title,
        # 本端点与下面 /articles/{id}/distill 的计费口径**不一样**，所以这里是
        # 恒 True 而那边是 `not is_system_content`：
        #   - claim_article_quota 抢到 → 此刻 consume，真扣了；
        #   - 没抢到 / existed_da 存在 → 扣在 content-service 剪藏那步或更早的
        #     一次调用上，同样是「已扣」；
        #   - uid==0 → consume 抛 3001，请求根本走不到这行。
        # 显式写出来，免得后来者照抄隔壁端点的动态表达式。
        quota_charged=True,
    )
    # 提前解析一次音色回给 App（worker 里会再解析一次；这里纯粹为了回显，
    # 两次之间用户改了偏好的话以 worker 那次为准，不追求强一致）
    voice = await resolve_voice_for_user(uid)
    return DistillStartResponse(
        task_id=task_id,
        article_id=req.article_id,
        status="queued",
        job_id=job_id,
        voice_id=voice.voice_id,
        voice_name=voice.display_name,
    )


@app.post("/api/v1/articles/{article_id}/distill")
async def distill_article(
    article_id: str,
    simulate_failure: bool = False,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """开始蒸馏（v1 §3.2）：已在该文章上做过完整蒸馏则直接复用（按 article 幂等）；

    否则扣一次配额 → 建任务 → 入 Arq 队列。

    - CP1.7.4 幂等修复：复用判定从「已扣过配额」改为「已有完整蒸馏产物」
      （不再把已在 content-service 抓取阶段扣过的配额误算入蒸馏阶段）
    - simulate_failure=True 用于验证「蒸馏失败 → 退还」
    - CP3.5-pre-3：任务不再在请求线程里跑（BackgroundTasks），而是塞进 Arq 队列由
      独立 worker 进程消费；端点签名不变，只多返一个 job_id
    """
    uid = int(user["sub"])
    art_result = await db.execute(select(Article).where(Article.id == article_id))
    art = art_result.scalar_one_or_none()
    if art is None:
        raise NotFound(message=f"article {article_id} not found")
    if art.user_id != uid:
        raise Forbidden(message="not the owner of this article")

    # 关键修复：art 刚从 DB 载入，url/title 已在内存中。
    # 紧邻此处捕获，赶在下方 existed 计数、quota.consume、track_simple 等
    # 任何一次 DB 操作使 ORM 对象过期之前。否则访问 art.url 会触发 lazy
    # 重查，在 await 上下文抛 MissingGreenlet → 500（安卓端「服务不可用」）。
    url = art.url
    title = art.title

    # 已有蒸馏产物行（复用旧行，CP-DISTILL-DUP）
    existed_da = await db.scalar(
        select(DistilledArticle).where(DistilledArticle.article_id == article_id)
    )

    # 幂等：已扣过配额就不再扣。判据有两个，缺一不可：
    #   1) 该文章已有蒸馏产物行（existed_da）—— 说明之前扣过
    #   2) content-service 提交时打的 Redis 标（mark_article_quota）—— 说明
    #      抓取/剪藏阶段已经扣过，蒸馏阶段不该再扣
    #
    # CP-AI-CHARGE-DUP 修复：此前只查 existed_da，而 content-service 的
    # d9_add_article / submit_article 都是「先 consume 扣一次 → 打 Redis 标 →
    # 调本端点触发蒸馏」，此刻 distilled_articles 还没有行 → existed_da is None
    # → **又扣一次**。实测剪藏 1 篇文章 quota_used 0 → 2，免费档 5 篇实际只能
    # 剪藏 2.5 篇。has_article_quota() 写了却全项目零调用，就是漏在这一步。
    #
    # 注：CP1.7.4 的注释写的是「复用判定从『已扣过配额』改为『已有完整蒸馏产物』，
    # 不再把 content-service 抓取阶段扣过的配额误算入蒸馏阶段」—— 注释描述的方向
    # 与代码实际行为相反，代码正是把抓取阶段扣的那次又算了一遍。
    marked = await cache_service.has_article_quota(article_id)
    already_charged = existed_da is not None or marked
    quota_used = None

    # 入队幂等：**已在跑就不再入队**（CP-TTS-VOICE BUG#12 的守卫原本只加在
    # /api/v1/distill/start 上，本端点漏了）。
    #
    # 实测（2026-10-02，华为真机剪藏一篇 838 字文章）：安卓在 POST /api/v1/articles
    # 之后又自己调了一次本端点（相隔 163ms），于是**同一篇文章入队两个独立 Arq job**。
    # 两个 job 的 payload 完全相同（含同一个 task_id），但 Arq 拿 job_id 去重、
    # 这里每次都新生成 job_id，所以拦不住。后果：
    #   10:53:57 job A 开始 TTS（7 段，1182 字）
    #   11:04:58 A 完成 → arq_distill_completed
    #   11:04:59 job B 被取走（入队于 10:53:42，队列里 delayed=676.97s）
    #   11:05:13 B 重新 LLM 改写 + 全量 TTS（8 段，1271 字，文字还不一样）
    # → 单篇耗时翻倍（~23 分钟 vs ~12），本机算力 + LLM 费用双倍，第一遍音频整个丢弃。
    #
    # 之前那句「端点对已存在 task 的文章是 idempotent 的（不重复扣配额），安全」
    # 只对了一半：配额有守卫（`already_charged`），**入队没有**。两个都要有才算幂等。
    #
    # 只挡 queued / running；done / failed 放行 —— 那是「换音色重新生成」和
    # 「失败重试」依赖的行为，一起挡掉会让重生成功能死掉（同 distill_start 的取舍）。
    if existed_da is not None and existed_da.status in IN_FLIGHT_DISTILL_STATUSES:
        return {
            "article_id": article_id,
            "task_id": existed_da.id,
            # 幂等分支不产生新 job，也就没有 job_id 可回；留空而不是编一个。
            "job_id": "",
            # 回报**真实**状态而不是写死 "started"：调用方要能区分「刚排上」和「已在跑」。
            "status": existed_da.status,
            "quota_consumed": False,
            "quota_used": (await quota_service.get_quota(db, uid))["quota_used"],
        }

    # 系统/公共内容不计入用户配额。
    #
    # user_id=0 是 content-service 的 ANONYMOUS_USER_ID，承载两类不该计费的内容：
    #   1. 匿名 D9 剪藏（d9_add_article，注释明写「匿名不计费」）
    #   2. 运营在后台手动录入的文章（POST /api/v1/admin/articles）
    # 而 users 表里 id=0 那行的 monthly_quota=0，于是 quota_service._apply 的
    # `used + delta > monthly`（0+1 > 0）必然成立 → QuotaExceededError(3001)。
    # 结果：这两类文章**永远派不出蒸馏任务**，卡在 pending 没人推。
    # 这里显式跳过计量，而不是去改 users 表 —— 把"不收费"编码在计费点上，
    # 比依赖某个哨兵行的数值更不容易被下次改配置时踩坏。
    is_system_content = uid == 0
    if not already_charged and not is_system_content:
        # 原子认领扣费权（`marked` 那次 EXISTS 只当预判，真正的仲裁在这里，
        # 两者判据是同一个 key）。并发下只有一个赢家会扣费。
        if await cache_service.claim_article_quota(article_id):
            try:
                quota = await quota_service.consume(db, uid)  # 用尽抛 3001
                quota_used = quota["quota_used"]
            except Exception:
                # 扣费失败就把认领还回去：否则「已扣费」标记会让这篇之后
                # 永远不再被扣，等于白送一次蒸馏。见 cache_service.claim_article_quota
                await cache_service.clear_article_quota(article_id)
                raise

    # CP-DISTILL-DUP 修复：article_id 有唯一约束（distilled_articles_article_id_key），
    # 二次蒸馏（failed 重试 / ready 后重蒸）不能再 INSERT —— 之前直接撞
    # UniqueViolationError → PendingRollbackError → 500。改为复用原行。
    # CP-DISTILL-NONDESTRUCTIVE：复用时保留旧产物，不再清空（见 _requeue_or_create）。
    task_id = _requeue_or_create(db, existed_da, article_id)
    art.status = "distilling"
    # CP6.2.1 埋点：distill_start
    # track() 只 flush 不 commit —— 必须在 commit() 之前，否则埋点随 close() 回滚丢失
    try:
        await track_simple(db, EventName.DISTILL_START, uid, article_id)
    except Exception as exc:
        log.warning(f"DISTILL_START 埋点异常（忽略）: article={article_id} err={exc}")
    await db.commit()

    job_id = await get_dispatcher().enqueue_distill(
        task_id=task_id,
        article_id=article_id,
        user_id=uid,
        url=url,
        title=title,
        simulate_failure=simulate_failure,
        # 系统内容（匿名剪藏 / 后台手动录入）上面**跳过**了计费，失败时不能退 ——
        # users.id=0 那行 quota_used 本就是 0，退款会在 quota_service._apply 里撞
        # 「used + delta < 0」抛 3003，被 _refund_quota_once 吞成一条
        # quota_refund_failed 告警：钱没白赚，但把「退款坏了」和「本来就没扣」
        # 两种完全不同的故障混在同一条日志里。
        quota_charged=not is_system_content,
    )
    # 埋点已在 commit() 之前完成（DISTILL_START）

    if quota_used is None:
        quota_used = (await quota_service.get_quota(db, uid))["quota_used"]
    return {
        "article_id": article_id,
        "task_id": task_id,
        "job_id": job_id,
        "status": "started",
        "quota_consumed": not already_charged,
        "quota_used": quota_used,
    }


@app.get("/api/v1/distill/{task_id}")
async def distill_status(
    task_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(DistilledArticle).where(DistilledArticle.id == task_id))
    da = result.scalar_one_or_none()
    if da is None:
        raise NotFound(message=f"task {task_id} not found")

    # 经 articles 表校验归属
    art_result = await db.execute(select(Article).where(Article.id == da.article_id))
    article = art_result.scalar_one_or_none()
    if article is None or article.user_id != int(user["sub"]):
        raise Forbidden(message="not the owner of this task")

    return {
        "task_id": da.id,
        "article_id": da.article_id,
        # 状态机：内部 7 态（queued / step1-4 / done / failed）→ 外部 5 态枚举
        # Android (DistillStatus.kt) 与 admin-web 都消费外部 5 态。
        # 映射详见 _external_distill_status()。
        "status": _external_distill_status(da.status),
        "audio_url": da.audio_url,
        "duration_sec": da.duration_sec,
        "tags": da.tags,
        "quality_score": da.quality_score,
        "created_at": da.created_at.isoformat() if da.created_at else None,
        "updated_at": da.updated_at.isoformat() if da.updated_at else None,
    }


# 蒸馏内部状态 → 跨端契约状态（CP3.5-pre-2 Android 强校验枚举需要）
_DISTILL_STATUS_MAP: dict[str, str] = {
    "queued": "pending",
    "step1_structuring": "distilling",
    "step2_rewriting": "distilling",
    "step3_ttsing": "distilling",
    "step4_concatenating": "distilling",
    "done": "ready",
    "failed": "failed",
}


def _external_distill_status(internal: str) -> str:
    """把内部状态机字符串映射成跨端契约状态（5 态枚举）。

    未知值（DB 被人工改、状态机错误等）→ 降级为 `distilling`，避免 Android 强校验枚举崩溃。
    """
    return _DISTILL_STATUS_MAP.get(internal or "", "distilling")


# ---------------------------------------------------------------------------
# CP7.3.0：多码率音频变体（§5 决策 2 路线 B = 按需转码）
# ---------------------------------------------------------------------------


async def _get_owned_distilled_article(
    db: AsyncSession, task_id: str, user: dict
) -> DistilledArticle:
    """读蒸馏结果并校验归属（与 distill_status 同款，NotFound/Forbidden 语义一致）。"""
    da = (
        await db.execute(select(DistilledArticle).where(DistilledArticle.id == task_id))
    ).scalar_one_or_none()
    if da is None:
        raise NotFound(message=f"task {task_id} not found")
    art = (
        await db.execute(select(Article).where(Article.id == da.article_id))
    ).scalar_one_or_none()
    if art is None or art.user_id != int(user["sub"]):
        raise Forbidden(message="not the owner of this task")
    return da


@app.get("/api/v1/distill/{task_id}/variants")
async def distill_variants(
    task_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """3 档码率可用性（128k 主档 + 96k/64k 按需转码）。永不 5xx：缺档 available=false。"""
    from distill.audio_variant_service import get_variant_service

    da = await _get_owned_distilled_article(db, task_id, user)
    variants = await get_variant_service().list_variants(db, da)
    return {"task_id": task_id, "article_id": da.article_id, "variants": variants}


@app.post("/api/v1/distill/{task_id}/variants/{bitrate}/warm")
async def distill_variant_warm(
    task_id: str,
    bitrate: int,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """预生成某码率变体（通勤 Wi-Fi 预热用；重复调用幂等）。"""
    from distill.audio_variant_service import TRANSCODE_BITRATES, get_variant_service

    if bitrate not in TRANSCODE_BITRATES:
        raise InvalidRequest(
            message=f"bitrate must be one of {list(TRANSCODE_BITRATES)}, got {bitrate}"
        )
    da = await _get_owned_distilled_article(db, task_id, user)
    row = await get_variant_service().ensure_variant(db, da, bitrate)
    return {
        "task_id": task_id,
        "bitrate": bitrate,
        "generated": row is not None,
        "oss_key": None if row is None else row.oss_key,
        "file_size_bytes": None if row is None else row.file_size_bytes,
    }


# ---------------------------------------------------------------------------
# B1 / G1（CP3.7.0 配套端点）：4 维听感评分提交 → distillation_evaluations
# ---------------------------------------------------------------------------


class EvaluationCreateRequest(BaseModel):
    """4 维评分（1-5，各维可 null=用户跳过该项）+ 总评必填。

    校验口径对齐 /rate：手动校验 → 业务 400（code 4001），不走 422。
    """

    hook_score: int | None = None
    section_score: int | None = None
    outro_score: int | None = None
    rhythm_score: int | None = None
    overall_score: int | None = None
    comment: str | None = None
    skip_reason: str | None = None


@app.post("/api/v1/distill/{task_id}/evaluation")
async def create_evaluation(
    task_id: str,
    req: EvaluationCreateRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """4 维评分提交（Android 评分 UI 的写入口，缺口 G1）。

    薄端点：校验 → 归属 → distill.evaluation_service.submit_user_evaluation
    （写表 + few-shot 入池 + 画像联动）→ commit。
    """
    from distill.evaluation_service import (
        submit_user_evaluation,
        validate_evaluation_payload,
    )

    validate_evaluation_payload(req)
    da = await _get_owned_distilled_article(db, task_id, user)
    result = await submit_user_evaluation(
        db,
        da,
        int(user["sub"]),
        hook_score=req.hook_score,
        section_score=req.section_score,
        outro_score=req.outro_score,
        rhythm_score=req.rhythm_score,
        overall_score=req.overall_score,
        comment=req.comment,
        skip_reason=req.skip_reason,
    )
    await db.commit()
    return result


@app.get("/api/v1/distill/{task_id}/evaluation")
async def get_my_evaluation(
    task_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """读回**当前用户**对这篇的最新一条听感评分（评分闭环的读侧）。

    为什么必须有这个端点
    -------------------
    `POST .../evaluation` 只写不读，而 `distillation_evaluations` 长期只有
    `/api/v1/admin/evaluations` 一个读入口（admin 专用）。于是移动端提交完
    只能关弹窗，界面上不留任何"已评分"痕迹；重进文章不知道评过没有，接口
    又不幂等，再点一次就多写一行 —— 这就是"提交评分没有闭环"。

    语义：
    - 只返回 `user_id = 当前用户` 且 `auto_flag=false` 的记录
      （自动/评测员评分不属于"我的评分"，不该污染用户视图）
    - 取**最新一条**（同一篇允许重复提交，客户端也可能重试）
    - 没评过 → 200 + `null` 字段，**不返回 404**
      （404 在这里语义歧义：既可能"没评过"，也可能是"这篇不存在/不是你的"；
      这两种情况调用方要做的决策完全不同。前者要弹评分，后者要报错）
    - 归属校验仍走 `_get_owned_distilled_article`，非 owner 一律 403
    """
    from sqlalchemy import select as _select

    from stashbox.backend.common.models import DistillationEvaluation

    da = await _get_owned_distilled_article(db, task_id, user)
    ev = (
        await db.execute(
            _select(DistillationEvaluation)
            .where(
                DistillationEvaluation.task_id == da.id,
                DistillationEvaluation.user_id == int(user["sub"]),
                DistillationEvaluation.auto_flag.is_(False),
            )
            .order_by(DistillationEvaluation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if ev is None:
        return {
            "id": None,
            "task_id": da.id,
            "hook_score": None,
            "section_score": None,
            "outro_score": None,
            "rhythm_score": None,
            "overall_score": None,
            "comment": None,
            "skip_reason": None,
            "created_at": None,
        }
    return {
        "id": ev.id,
        "task_id": ev.task_id,
        "hook_score": ev.hook_score,
        "section_score": ev.section_score,
        "outro_score": ev.outro_score,
        "rhythm_score": ev.rhythm_score,
        "overall_score": ev.overall_score,
        "comment": ev.comment,
        "skip_reason": ev.skip_reason,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8103)
