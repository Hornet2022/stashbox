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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

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
        FetcherError：所有通道都失败时抛**最后一节**的错误（信息量最大：
                     最后一节通常是成本最高、结论最强的那次尝试）
    """
    last_error: FetcherError | None = None
    tried: list[str] = []

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
                raise
            last_error = exc
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

        # 拿到 HTML 了，但解析仍可能失败（比如渲染后仍抽不到正文）——
        # 那属于解析结论，同样按升级规则处理。
        try:
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
                raise
            last_error = exc
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
        log.info("fetch_ok url=%s tier=%s tried=%s", url, tier.name, tried)
        return result

    if last_error is not None:
        raise last_error
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
