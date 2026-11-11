"""剪藏**后台抓取**（2026-11-11）。

## 为什么把抓取挪到后台

内测反馈（uid 23）：剪藏时反复弹「网络连接失败」，关掉 app 重开又正常。
取证结论（`backend/docs/剪藏网络失败复盘_2026-11-11.md`）：

  · 那 4 次剪藏**服务端全部 200**，文章全部建好、worker 正常合成；
  · 客户端当日 85 个请求全部 2xx，抓取失败 0 条；
  · ⇒ 失败发生在**客户端本地**（没收到/没解析到响应），服务端零感知。

根因是**剪藏接口是同步长请求**：服务端当场抓取（HTTP 退避重试 + 浏览器兜底，
整条降级链最坏 ~40s），客户端一直等着这个响应 —— 期间任何客户端侧抖动
（弱网切换、后台调度、连接池复用）都会变成「**服务端成功、客户端报错**」。
重试又会把这条链再走一遍，于是用户看到的是「反复失败」。

⇒ 剪藏接口改成**快速返回 + 后台抓取**：
接口只做「建一条 pending 行」就返回（毫秒级），抓取在后台跑。
客户端不再有「长连接等抓取」这个失败面 —— 它等的是自己的短请求。

## 三条不能破的语义

1. **先抓后扣**（2026-10-03 定）：抓不到正文 ⇒ **不扣配额**。
   配额在**后台抓取成功之后**才扣；抓取失败的文章 `status=failed` 且**不计费**。
2. **重复剪藏不重复计费**：早查重命中**已在抓**的文章 ⇒ 直接返回同一篇，
   不再驱动第二次抓取；命中**已失败**的文章 ⇒ **重新驱动抓取**（而不是永远返回失败）。
3. **缺席必须点名**：抓取失败要在文章上写**面向用户的中文原因**（`articles.error`），
   不能变成「文章永远 pending、用户不知道发生了什么」。

## 为什么不新建跨服务队列

抓取代码（`fetchers/`）本来就在 content-service 进程里，队列化只会把
「一个进程内的函数调用」变成「一次跨服务 RPC + 一张队列表 + 一个消费者」，
而收益（跨机扩展）当前并不存在。进程内 asyncio 后台任务 + Redis 在跑标记
+ lifespan 定时兜底，改动面最小，且失败路径完全在本进程内可观测。

⚠️ 兜底定时器是**必需**的，不是锦上添花：进程重启 / OOM kill 会让在跑的任务
凭空消失，那篇文章会**永远**停在 pending（用户看到「一直处理中」）。
`sweep_stale_captures` 就是给这种孤儿收尾的。

## 依赖注入的三个钩子（为什么不用 import）

`fetch` / `to_raw` / `distill` 由调用方（`main.py`）传入，而不是在这里 import：

  · **避免循环依赖**：`main.py` 要 import 本模块；本模块若 import `main`
    就成环（content-service 的模块名在生产是 `main`、在测试是
    `_cp17_content_main`，靠 importlib 加载，循环 import 会炸）。
  · **可测**：单测要替换抓取与蒸馏调用；钩子是最直接的替换点，
    且替换的是**行为**（真跑一遍编排）而不是查源码字符串。
  · **单一实现**：钩子由 `main.py` 这一个所有者提供，语义仍只有一份。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

import redis.asyncio as redis
from sqlalchemy import extract, func, select

from stashbox.backend.common import cache_service, quota_service
from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.exceptions import BizException
from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.models import NO_RAW_CONTENT, Article
from stashbox.backend.common.quota_service import QuotaExceededError
from stashbox.backend.common.redis_client import get_redis_pool

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# 可调参数（env 可覆盖）
# ---------------------------------------------------------------------------

#: 抓取超时（秒）：超过这个时间还没抓到正文，文章按「抓取超时」收尾。
#:
#: 取 300s 的依据：抓取预算本身是 30s（`STASHBOX_CAPTURE_FETCH_BUDGET`，
#: 覆盖 HTTP 退避重试 + 浏览器兜底整条降级链），留 10 倍余量给
#: 「网络黑洞 + 退避 sleep」的最坏组合。
_ENV_FETCH_TIMEOUT = "STASHBOX_CAPTURE_FETCH_TIMEOUT"
FETCH_TIMEOUT_SECONDS = int(os.environ.get(_ENV_FETCH_TIMEOUT) or 300)

#: 在跑标记的 TTL（秒）：必须 **大于** 抓取超时，否则一次超长的抓取会被
#: 兜底定时器误判成「孤儿」并标记失败，而抓取其实还在跑。
_ENV_INFLIGHT_TTL = "STASHBOX_CAPTURE_INFLIGHT_TTL"
INFLIGHT_TTL_SECONDS = int(os.environ.get(_ENV_INFLIGHT_TTL) or 900)

#: 兜底定时器的扫描间隔（秒）。
ENV_SWEEP_INTERVAL = "STASHBOX_CAPTURE_SWEEP_INTERVAL"
SWEEP_INTERVAL_SECONDS = int(os.environ.get(ENV_SWEEP_INTERVAL) or 60)

#: 面向用户的中文原因（失败时写进 `articles.error`）。
MSG_FETCH_TIMEOUT = "抓取超时，请重试"
MSG_FETCH_FAILED = "抓取失败，请稍后重试"
MSG_QUOTA_EXHAUSTED = "本月免费额度已用完，剪藏未生效（未扣费）"


# ---------------------------------------------------------------------------
# 纯函数（可测，无副作用）
# ---------------------------------------------------------------------------


def decide_existing_clip_action(
    *,
    status: str | None,
    has_content: bool,
    in_flight: bool,
) -> str:
    """早查重命中旧文章时，该怎么办。返回 ``reuse`` / ``await`` / ``refetch``。

    ## 三个动作分别意味着什么

    · ``reuse`` —— 已经有正文了：沿用旧行为（复用文章，必要时重派蒸馏）。
    · ``await`` —— 没正文，但**正在抓**：不能重复驱动（会抓两次、扣两次）。
    · ``refetch`` —— 没正文，且**没有人在抓**：重新驱动抓取。

    ## 为什么「失败的文章」要 refetch 而不是 reuse

    2026-11-11 之前的直觉是「失败了就复用失败的那篇」—— 那会让用户**永远**
    剪藏不进去：重试一次得到同一个失败，重试十次还是同一个失败。
    抓取失败多半是**网络抖动**（临时性），重新驱动才有意义。

    ## 为什么 `has_content` 优先于其它判据

    有正文 ⇒ 抓取这一步已经完成了，后面只剩蒸馏 ⇒ 不该再驱动抓取。
    """
    if has_content:
        return "reuse"
    if in_flight:
        return "await"
    return "refetch"


def is_fetch_stale(
    age_seconds: float | None,
    *,
    timeout_seconds: int = FETCH_TIMEOUT_SECONDS,
) -> bool:
    """这条「等抓取」的文章是不是**孤儿**（该收尾了）。

    ⚠️ **年龄必须由 DB 时钟算**（`now() - updated_at`），不能用 Python 时钟减
    `created_at`：`created_at` 落库用的是 PG 的 `now()`（**server timezone**，
    本地开发机是 UTC+8、生产容器多半是 UTC），而 Python 侧是 UTC ——
    两边口径不一致时算出来的年龄能差 8 小时（实测：本地库里 created_at 是
    18:12、UTC 是 10:12 ⇒ 「2 小时前的孤儿」被算成「还差 6 小时」）
    ⇒ 兜底定时器**永远扫不到任何东西** ⇒ 孤儿文章永远 pending。

    ⇒ 所以这里只做**阈值比较**（纯函数、可测），年龄由调用方从 DB 取。
    """
    if age_seconds is None:
        return True
    return age_seconds > timeout_seconds


def capture_failure_message(exc: BaseException) -> str:
    """把抓取异常翻译成**面向用户的中文原因**。

    · `BizException` 的 message 已经是面向用户的文案（`fetch_error_user_message`
      产出的那套话术），直接用。
    · 其它异常（超时 / SSL / 解析炸了）**不把技术细节泄给用户** ——
      技术串进日志，文章上只写通用文案。
    """
    if isinstance(exc, BizException):
        msg = (exc.message or "").strip()
        if msg:
            return msg
    return MSG_FETCH_FAILED


# ---------------------------------------------------------------------------
# 在跑标记（Redis）
# ---------------------------------------------------------------------------


def inflight_key(article_id: str) -> str:
    """在跑标记的 Redis key。"""
    return f"capture:inflight:{article_id}"


def _client() -> redis.Redis:
    return redis.Redis(connection_pool=get_redis_pool())


async def mark_inflight(article_id: str) -> bool:
    """标记「这篇正在抓」。返回是否标记成功。

    ⚠️ Redis 不可用时**不抛异常**：标记只是**防重复驱动**的优化，
    真正的兜底是「文章行上的 raw_content 是否为空」+ 兜底定时器。
    宁可重复抓一次（幂等：第二次会发现 raw_content 已有值而跳过），
    也不能让 Redis 挂了就整条剪藏链不可用。
    """
    client = _client()
    try:
        await client.set(inflight_key(article_id), "1", ex=INFLIGHT_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("capture_inflight_mark_failed: article=%s err=%s", article_id, exc)
        return False
    finally:
        await client.aclose()


async def clear_inflight(article_id: str) -> None:
    """清掉在跑标记（抓取结束，无论成败）。"""
    client = _client()
    try:
        await client.delete(inflight_key(article_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("capture_inflight_clear_failed: article=%s err=%s", article_id, exc)
    finally:
        await client.aclose()


async def is_inflight(article_id: str) -> bool:
    """是否正在抓。Redis 不可用时按「不在跑」处理（宁可重抓一次）。"""
    client = _client()
    try:
        return bool(await client.exists(inflight_key(article_id)))
    except Exception as exc:  # noqa: BLE001
        log.warning("capture_inflight_check_failed: article=%s err=%s", article_id, exc)
        return False
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 后台抓取任务
# ---------------------------------------------------------------------------

FetchHook = Callable[[str], Awaitable[Any]]
ToRawHook = Callable[[Any], dict]
DistillHook = Callable[[str, str], Awaitable[Any]]


async def run_capture_fetch(
    article_id: str,
    *,
    fetch: FetchHook,
    to_raw: ToRawHook,
    distill: DistillHook,
) -> str:
    """后台抓取 + 建内容 + 扣配额 + 派蒸馏。返回 ``ok`` / ``failed`` / ``skipped``。

    ## 顺序为什么是「抓 → 扣 → 写内容 → 派蒸馏」

    * **先抓后扣**（2026-10-03）：抓不到 ⇒ 不计费。
    * 扣了配额才写内容 ⇒ 不会出现「有正文但没计费」的免费漏洞。
    * 派蒸馏在最后 ⇒ 蒸馏任务一定能看到正文与配额标记。

    ## 幂等

    `raw_content` 已有值 ⇒ 直接 ``skipped``（别的路径已经抓过）。
    """
    try:
        return await _run_capture_fetch_inner(article_id, fetch=fetch, to_raw=to_raw, distill=distill)
    finally:
        # ⚠️ 在跑标记**必须清掉**（成功/失败/跳过都一样）。
        # 漏清的后果很具体：这篇文章会被永久当成「正在抓」⇒
        # ① 用户再提交同一链接时判定成 `await` ⇒ 永远不会重新驱动抓取；
        # ② 兜底定时器也会跳过它 ⇒ 失败的文章永远停在 failed/pending。
        await clear_inflight(article_id)


async def _run_capture_fetch_inner(
    article_id: str,
    *,
    fetch: FetchHook,
    to_raw: ToRawHook,
    distill: DistillHook,
) -> str:
    async with AsyncSessionLocal() as session:
        art = await session.get(Article, article_id)
        if art is None:
            log.warning("capture_fetch_skip: article %s not found", article_id)
            return "skipped"
        if art.raw_content is not None:
            # 已经抓到过（并发重复驱动 / 别的入口先抓到）⇒ 不重复抓、不重复扣。
            log.info("capture_fetch_skip: article %s already has content", article_id)
            return "skipped"

        try:
            fr = await fetch(art.url)
        except Exception as exc:  # noqa: BLE001
            msg = capture_failure_message(exc)
            log.warning(
                "capture_fetch_failed: article=%s err=%s msg=%s",
                article_id,
                f"{type(exc).__name__}: {exc}",
                msg,
            )
            art.status = "failed"
            art.error = msg
            await session.commit()
            await cache_service.invalidate_article(article_id)
            await cache_service.invalidate_pending(art.user_id)
            return "failed"

        # 配额：抓到了才扣（先抓后扣）。用尽 ⇒ 文章失败但**不扣费**。
        try:
            quota = await quota_service.consume(session, art.user_id)
        except QuotaExceededError:
            log.info("capture_quota_exhausted: article=%s user=%s", article_id, art.user_id)
            art.status = "failed"
            art.error = MSG_QUOTA_EXHAUSTED
            await session.commit()
            await cache_service.invalidate_article(article_id)
            await cache_service.invalidate_pending(art.user_id)
            return "failed"

        art.raw_content = to_raw(fr)
        title = getattr(fr, "title", None)
        if title:
            art.title = title
        # 2026-10-23：GitHub 走专属 fetcher 时，来源要用真实来源（与同步路径同口径）。
        if getattr(fr, "source", None) == "github":
            art.source = "github"
        # 兜底定时器可能已经把这条标成 failed（抓取超时）—— 抓到就是成功，改回 pending。
        art.status = "pending"
        art.error = None
        await session.commit()

        # 打标：该文章已扣过配额（避免 ai-service 蒸馏时二次扣）
        await cache_service.mark_article_quota(article_id)
        await cache_service.invalidate_article(article_id)
        await cache_service.invalidate_pending(art.user_id)

        # 派蒸馏：失败仅 log 不影响抓取结果（ai-service 不可达时文章停在 pending，
        # 等蒸馏中心重派；这与改造前的行为一致）。
        try:
            await distill(article_id, create_access_token(str(art.user_id)))
        except Exception as exc:  # noqa: BLE001
            log.warning("capture_distill_trigger_failed: article=%s err=%s", article_id, exc)

        log.info(
            "capture_fetch_ok: article=%s user=%s quota_used=%s remaining=%s",
            article_id,
            art.user_id,
            quota.get("quota_used"),
            quota.get("remaining"),
        )
        return "ok"


# ---------------------------------------------------------------------------
# 兜底：孤儿 pending 收尾
# ---------------------------------------------------------------------------


async def sweep_stale_captures() -> int:
    """把「等抓取」且**没有人在跑**的超时文章标记为 failed。返回处理条数。

    ## 为什么需要它

    进程重启 / OOM kill / 机器重启 ⇒ 在跑的后台任务凭空消失 ⇒ 文章永远 pending。
    用户看到的是「一直处理中」，永远等不到结果，也永远不会失败。

    ## 判定条件（四条都要满足）

    1. `status == 'pending'`（只处理「等抓取」这一段；等蒸馏的 pending 有正文，不动）
    2. `raw_content IS NULL`（**没有正文** = 抓取这一步还没完成）
    3. `created_at` 早于「现在 − 抓取超时」
    4. **没有在跑标记**（有人在跑就不能动 —— 否则会把正在抓的文章误杀）

    ⚠️ 条件 2 是关键：`status='pending'` 有两种含义（等抓取 / 等蒸馏），
    只看 status 会把「刚抓完、正在蒸馏」的文章误判成孤儿。
    """
    async with AsyncSessionLocal() as session:
        # ⚠️ 时间判定**只有一处实现**（`is_fetch_stale`）：这里不在 Python 侧
        # 算时间差，而是让 **DB 用自己的时钟**算年龄（`now() - updated_at`），
        # Python 只做阈值比较（见 is_fetch_stale 的注释：两边时钟口径必须一致）。
        #
        # 用 `updated_at` 而不是 `created_at`：updated_at 在任何 UPDATE 时刷新
        # ⇒ 它约等于「这次抓取是什么时候开始的」。
        # `created_at` 对「两天前创建、今天重新驱动抓取」的文章会算出 2 天
        # ⇒ 兜底定时器会在抓取刚开始时就把它当孤儿杀掉。
        age_expr = extract(
            "epoch", func.now() - func.coalesce(Article.updated_at, Article.created_at)
        ).label("age_sec")
        rows = (
            (
                await session.execute(
                    select(Article, age_expr).where(
                        Article.status == "pending",
                        NO_RAW_CONTENT,
                        Article.deleted_at.is_(None),
                    )
                )
            )
            .all()
        )
        touched_users: set[int] = set()
        count = 0
        for art, age_sec in rows:
            if not is_fetch_stale(float(age_sec) if age_sec is not None else None):
                continue
            if await is_inflight(art.id):
                continue
            art.status = "failed"
            art.error = MSG_FETCH_TIMEOUT
            touched_users.add(art.user_id)
            count += 1
            log.warning(
                "capture_fetch_timeout_swept: article=%s user=%s created_at=%s",
                art.id,
                art.user_id,
                art.created_at,
            )
        if count:
            await session.commit()
            for art, _age_sec in rows:
                await cache_service.invalidate_article(art.id)
            for user_id in touched_users:
                await cache_service.invalidate_pending(user_id)
        return count


async def stale_capture_loop(interval_seconds: int = SWEEP_INTERVAL_SECONDS) -> None:
    """兜底定时器的主循环（由 lifespan 起，进程退出时取消）。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            swept = await sweep_stale_captures()
            if swept:
                log.warning("capture_sweep_marked_failed: count=%s", swept)
        except Exception as exc:  # noqa: BLE001
            # 定时器自己不能死 —— 死了就再没有兜底了。
            log.error("capture_sweep_error: %s: %s", type(exc).__name__, exc)
