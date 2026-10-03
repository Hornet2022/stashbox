"""多策略抓取降级链：HTTP → 无头浏览器 →（预留）代理池。

## 为什么是"链"而不是"给某个 fetcher 加点加固"

剪藏失败的原因分布在三个互不相干的层面，加固单点只能覆盖其中一层：

| 层面 | 症状 | 对策 |
|---|---|---|
| 网络/瞬时 | 连接重置、502、429、CDN 抖动 | 退避重试（`net.py`） |
| 渲染 | 正文由 JS 注入，HTML 里没有 | 无头浏览器（`browser.py`） |
| IP 身份 | 机房 IP 被判定为爬虫 | 代理（预留，未接） |

前两层**不需要采购任何东西**就能上，第三层要买住宅代理 —— 那是产品决定，不是
默认开启。所以这里先交付前两层，把第三层留成显式的注册位。

## 升级与否：错误码决定，不是"失败就升级"

盲目对所有失败重试下一级是错的，两个方向都错：

- **不该升级的升级**（404 文章已删 / UNSUPPORTED 链接不支持）—— 换渲染引擎、
  换 IP 都不会让一篇被删的文章复活，只会让用户从等 3 秒变成等 30 秒。
- **该升级的不升级**（`parser` 报"抽不到正文"）—— 这恰恰是 JS 渲染页的典型症状，
  升到浏览器通道一次就能救回来。

所以升级集合是**按错误码白名单**判定的（`ESCALATABLE_CODES`），不是靠猜。

## 总时限是硬约束

降级链每一级都有自己的超时，三级串起来最坏到分钟级。剪藏是**同步接口**，
用户盯着转圈等。所以整条链共享一个 `Deadline`，每级只拿"剩余预算"和
"本级上限"的较小值；预算耗尽就不再开新的一级，直接把攒到的错误抛出去。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from stashbox.backend.common import fetch_metrics

from .antibot import detect_bot_challenge
from .base import FetcherError, FetcherErrorCode, FetchResult
from .net import Deadline

log = logging.getLogger("stashbox.fetch.pipeline")

#: 值得升级到下一通道的错误码。
#:
#: - `NETWORK`：本级网络层已经重试过仍失败，换通道（尤其换出口 IP）有意义
#: - `RATE_LIMIT`：被限流，同一 IP 继续打只会更糟，必须换通道
#: - `AUTH`：被判定为非官方客户端 —— **这正是无头浏览器通道的主战场**
#: - `PARSE`：抽不到正文 = 典型 JS 渲染页症状，浏览器通道一次命中
ESCALATABLE_CODES: frozenset[FetcherErrorCode] = frozenset(
    {
        FetcherErrorCode.NETWORK,
        FetcherErrorCode.RATE_LIMIT,
        FetcherErrorCode.AUTH,
        FetcherErrorCode.PARSE,
        # 撞了人机验证仍值得试一次浏览器：验证墙本质是**指纹**判定，
        # 而浏览器通道的指纹和 HTTP 通道不同，有些站点只对陌生 UA 出验证码。
        #
        # 代价是有界的（只发生在已经失败的路径上，且受 BROWSER_TIER_CAP 约束），
        # 而收益是"本来要失败的链接被救回"。既然剪藏成功率是第一优先级，先赌这一把。
        #
        # 怎么知道这个赌注划不划算？**指标已经能回答**，不用猜：
        #   fetch_escalations_total{from_tier="http",code="fetcher.bot_challenge"}  高
        #   且 fetch_attempts_total{tier="browser",outcome="success"} 也在涨
        #   -> 升级在赚钱，保持开启；
        #   若前者高而后者长期为 0 -> 升级纯属浪费时间，再把这里挪出白名单。
        FetcherErrorCode.BOT_CHALLENGE,
    }
)

# 刻意**不**升级的：
#: `NOT_FOUND` 文章没了（重试一万次结果一样，只会拖慢用户）
#: `UNSUPPORTED` 链接不支持（换通道也支持不了）
#: `SSRF_BLOCKED` 目标指向内网/回环/链路本地，被安全策略拦下。
#:   这是**终态安全决策**不是瞬时故障：换渲染引擎、换出口 IP 都不该让它通过
#:   （而浏览器通道另有自己的导航拦截，升级过去既白等一次浏览器启动，
#:   又会让"浏览器不可用"这种噪音覆盖掉真正的原因 —— 本轮测试实测踩到过）。
#: `INTERNAL` 这是我们自己的 bug，换通道掩盖 bug 而不是修 bug

#: 浏览器通道的独立上限（秒）。比 Tier 1 短：浏览器更慢，
#: 所以只在"很可能是 JS 渲染问题"时才值得开，用户不该为它多等一分钟。
BROWSER_TIER_CAP = 12.0


@dataclass
class Acquired:
    """一节通道拿到的原始页面。

    除了 HTML 还要带上原始 URL 和站点私有元数据（content-type 等）：
    解析函数要在**渲染后**才知道这些，而这些值只在 HTTP 通道拿得到，
    让解析函数只收 (html, final_url, status) 就得靠闭包往外搬，更容易出错。
    """

    html: str
    final_url: str
    status: int | None
    tier: str
    url: str = ""
    extra: dict = field(default_factory=dict)


#: 通道函数：拿到 Acquired
Acquire = Callable[[], Awaitable[Acquired]]

#: 解析函数：Acquired → FetchResult
Parse = Callable[[Acquired], FetchResult]


@dataclass
class Tier:
    """一节降级通道。"""

    name: str
    acquire: Acquire | None


#: `PARSE` 是**兜底类别**：走到它意味着"我们知道失败，但不知道具体为什么"。
#: 所以它永远不该盖掉一个已经查明原因的错误。
#:
#: 本轮实测的踩坑：百家号在 HTTP 通道被识别为 BOT_CHALLENGE（撞验证码），
#: 浏览器通道落到"这里空空如也"只能报 PARSE。按"取最后一个"上报时，
#: 用户和运维看到的是"没提取出正文"，而真正原因被抹平。
_SPECIFICITY: dict[FetcherErrorCode, int] = {
    FetcherErrorCode.BOT_CHALLENGE: 5,  # 最具体：连"对方要人机验证"都知道了
    FetcherErrorCode.SSRF_BLOCKED: 5,
    FetcherErrorCode.NOT_FOUND: 4,
    FetcherErrorCode.RATE_LIMIT: 4,
    FetcherErrorCode.AUTH: 4,
    FetcherErrorCode.UNSUPPORTED: 3,
    FetcherErrorCode.NETWORK: 2,
    FetcherErrorCode.INTERNAL: 2,
    FetcherErrorCode.PARSE: 1,  # 兜底，最低优先级
}


def _pick_better_error(current: FetcherError | None, new: FetcherError) -> FetcherError:
    """在多个通道各自的失败里，挑信息量最大的那个上报。

    规则：新错误更具体就换，否则保留当前（先到的通常是判定更早、结论更明确的通道）。
    相同具体度时**不换**，保证"先到先得"的行为可预测。
    """
    if current is None:
        return new
    return new if _SPECIFICITY.get(new.code, 0) > _SPECIFICITY.get(current.code, 0) else current


async def fetch_with_escalation(
    *,
    url: str,
    parse: Parse,
    tiers: Sequence[Tier],
    deadline: Deadline,
    source: str,
) -> FetchResult:
    """按顺序跑各通道，第一节成功的直接返回。

    Returns:
        FetchResult，`raw_metadata` 里带 `fetch_tier`（命中哪一节）
        和 `fetch_tiers_tried`（试过哪几节）—— 成功率到底是被什么卡住的，
        只能靠这两个字段在日志里看出来。

    Raises:
        FetcherError：所有通道都失败时抛**最有信息量的那个**错误，
                     而不是简单取最后一个 —— 见 `_pick_better_error`。

    关于"抛哪个错"：本轮实测撞出过一个真问题。百家号文章在 HTTP 通道被明确
    识别为「撞了百度图形验证码」（BOT_CHALLENGE，说清了原因），但浏览器通道
    落到「这里空空如也」的错误页，只能报一个笼统的 PARSE。若按"最后一个"上报，
    用户看到的就是"没提取出正文" —— 而 PARSE 是**兜底类别**（我们不知道原因），
    拿它去覆盖一个已经知道的明确原因，等于把结论抹平。
    """

    last_error: FetcherError | None = None
    tried: list[str] = []
    started = time.monotonic()

    for tier in tiers:
        if tier.acquire is None:
            continue
        if deadline.expired():
            log.info("fetch_budget_exhausted url=%s tried=%s", url, tried)
            break
        tried.append(tier.name)
        try:
            acquired = await tier.acquire()
        except FetcherError as exc:
            if exc.code not in ESCALATABLE_CODES:
                # 确定性的失败：立刻上抛，别让用户等下一级
                log.info(
                    "fetch_terminal_fail url=%s tier=%s code=%s msg=%s",
                    url,
                    tier.name,
                    exc.code.value,
                    exc.message,
                )
                fetch_metrics.record_failure(tier.name, exc.code.value, time.monotonic() - started)
                raise
            last_error = _pick_better_error(last_error, exc)
            fetch_metrics.record_escalation(tier.name, exc.code.value)
            log.info(
                "fetch_escalate url=%s tier=%s code=%s msg=%s",
                url,
                tier.name,
                exc.code.value,
                exc.message,
            )
            continue
        except Exception as exc:  # noqa: BLE001 —— 通道自身炸了（浏览器不可用等）
            # 关键：通道「不可用」**不是一次真实尝试的结论**。
            # 如果这里覆盖 last_error，用户和排障看到的就会是"浏览器不可用"，
            # 而真实原因（比如"目标指向内网被安全策略拦下"）被彻底盖掉 ——
            # 这条路径是本轮测试实测抓出来的真 bug，不是假想。
            # 所以：已经有真实结论时，只记日志不覆盖。
            log.info(
                "fetch_tier_skipped url=%s tier=%s err=%s: %s",
                url,
                tier.name,
                type(exc).__name__,
                exc,
            )
            if last_error is None:
                last_error = FetcherError(
                    code=FetcherErrorCode.NETWORK,
                    message=f"tier {tier.name} unavailable: {type(exc).__name__}: {exc}",
                    source=source,
                )
            continue

        # 拿到 HTML 了，但**不代表它是文章**。先判是不是人机验证页，再进解析器。
        #
        # 顺序很关键：挑战页进解析器会被报成"抽不到正文"，那既误导用户
        # （他不是要登录，是对方要他做人机验证），也让运维在指标里误以为该去查抽取器。
        # 放在 pipeline 而不是各 fetcher 的 parse 里：一处覆盖所有通道和所有站点。
        #
        # ⚠️ 必须和 parse 放在**同一个 try** 里：挑战页判定的升级路径要和解析
        # 失败走同一套规则，否则 BOT_CHALLENGE 会被当成终态直接上抛，
        # 浏览器通道永远没机会试（这是本轮实测发现的接线错误，不是设计）。
        try:
            detect_bot_challenge(acquired.html, final_url=acquired.final_url, source=source)
            result = parse(acquired)
        except FetcherError as exc:
            if exc.code not in ESCALATABLE_CODES:
                log.info(
                    "fetch_parse_terminal url=%s tier=%s code=%s msg=%s",
                    url,
                    tier.name,
                    exc.code.value,
                    exc.message,
                )
                fetch_metrics.record_failure(tier.name, exc.code.value, time.monotonic() - started)
                raise
            last_error = _pick_better_error(last_error, exc)
            fetch_metrics.record_escalation(tier.name, exc.code.value)
            log.info(
                "fetch_parse_escalate url=%s tier=%s code=%s msg=%s",
                url,
                tier.name,
                exc.code.value,
                exc.message,
            )
            continue

        result.raw_metadata["fetch_tier"] = tier.name
        result.raw_metadata["fetch_tiers_tried"] = tried
        fetch_metrics.record_success(tier.name, time.monotonic() - started)
        log.info("fetch_ok url=%s tier=%s tried=%s", url, tier.name, tried)
        return result

    if last_error is not None:
        fetch_metrics.record_failure(
            # 归到"最后一节"而不是"http"：失败发生在哪一节决定了下次该修哪一层
            tried[-1] if tried else "none",
            last_error.code.value,
            time.monotonic() - started,
        )
        raise last_error
    fetch_metrics.record_failure("none", "no_tier_available", time.monotonic() - started)
    raise FetcherError(
        code=FetcherErrorCode.NETWORK,
        message="no fetch tier available (budget exhausted or all tiers disabled)",
        source=source,
    )


__all__ = [
    "BROWSER_TIER_CAP",
    "ESCALATABLE_CODES",
    "Acquired",
    "Tier",
    "fetch_with_escalation",
]
