"""LLM 失败分类（2026-11-09 事故引入）。

## 为什么要单独一个模块

**2026-11-09 生产事故**：上游 `tertu.cn` 整机不可达（TCP 连不上），
4 篇内测文章的蒸馏全部在 rewrite 步骤失败。日志里写的是：

    error=TimeoutError()
    agent distill failed at rewrite (timeout): openai chat failed after 3 attempts: TimeoutError()

**"timeout" 是错的**：真实原因是**连不上**（ConnectTimeout），不是**响应慢**。
差别在于处置方式 —— 连不上 ⇒ 应该熔断、应该立刻失败、应该告诉用户"服务暂时不可用"；
响应慢 ⇒ 上游还活着，该降级该重试。

## 根因：异常链被丢了

`ai-service/llm/openai.py` 的 `_post` 里套了一层总墙钟死线：

    async with asyncio.timeout(self.timeout):
        resp = await self._client.post(...)

`asyncio.timeout` 到点抛的是**裸 `TimeoutError`**，httpx 的真实异常
（`httpx.ConnectTimeout`）只在 `__cause__` 里。而上层分类
（`agent/runner.py`）当时只读 `str(e)` —— 拿到的是 `"TimeoutError()"`，
于是"连不上"被一律判成"超时"。

⇒ 分类必须**沿异常链找根因**，不能只看 `str(e)`。

## 分类口径

| kind | 含义 | 典型异常 |
|------|------|----------|
| `unreachable` | 连不上上游（DNS/TCP/连接被拒） | `httpx.ConnectError` / `httpx.ConnectTimeout` |
| `timeout` | 连上了但没在时限内读完/写完 | `httpx.ReadTimeout` / `WriteTimeout` / `PoolTimeout` |
| `network` | 其它传输层错误（连接被重置等） | `httpx.TransportError` |
| `unknown` | 认不出来 | 其它 |

注意 `httpx.ConnectTimeout` 是 `httpx.TimeoutException` 的子类，
而 `httpx.ReadTimeout` 也是 —— **必须先判 ConnectTimeout**，
否则"连不上"会再次被吞成"超时"。
"""

from __future__ import annotations

import asyncio

import httpx

# 分类常量（跨模块共用，避免各处写字符串字面量）
KIND_UNREACHABLE = "unreachable"
KIND_TIMEOUT = "timeout"
KIND_NETWORK = "network"
KIND_UNKNOWN = "unknown"

_MAX_CAUSE_DEPTH = 6


def iter_exception_chain(exc: BaseException | None):
    """沿 `__cause__` / `__context__` 走一遍异常链（去重、限深）。

    为什么两个都要看：
    - `__cause__`：显式 `raise X from Y` 或 `asyncio.timeout` 的超时上下文；
    - `__context__`：隐式 "during handling of the above exception"。

    真实根因在哪一个里取决于抛法，两个都看才不漏。
    """
    seen: set[int] = set()
    depth = 0
    cur: BaseException | None = exc
    while cur is not None and depth < _MAX_CAUSE_DEPTH and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        depth += 1
        nxt = cur.__cause__ or cur.__context__
        if nxt is cur:
            break
        cur = nxt


def classify_transport_error(exc: BaseException | None) -> str:
    """把一个传输层异常分类成 kind（见模块文档的口径表）。

    认不出就返回 `unknown` —— **不要**猜成 timeout：
    猜错会让"上游已死"被当成"上游慢"，于是不熔断、继续空转 30 分钟
    （这正是 2026-11-09 事故的形态）。
    """
    if exc is None:
        return KIND_UNKNOWN

    # 熔断是**我们自己**抛的，优先级最高：它已经带 kind 了。
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str) and kind:
        return kind

    has_timeout = False
    for item in iter_exception_chain(exc):
        # 🔴 顺序要紧：ConnectTimeout 是 TimeoutException 的子类，
        #    先判 ConnectError/ConnectTimeout，再判 ReadTimeout。
        if isinstance(item, (httpx.ConnectError, httpx.ConnectTimeout)):
            return KIND_UNREACHABLE
        if isinstance(item, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
            has_timeout = True
            continue
        if isinstance(item, httpx.TransportError):
            if has_timeout:
                continue
            return KIND_NETWORK
        if isinstance(item, (asyncio.TimeoutError, TimeoutError)):
            has_timeout = True

    if has_timeout:
        return KIND_TIMEOUT
    return KIND_UNKNOWN


def is_unreachable(exc: BaseException | None) -> bool:
    """上游是不是"连不上"（熔断只对这种开闸）。

    只对 `unreachable` 熔断的理由：响应慢（timeout）说明上游**还活着**，
    开闸反而会砍掉本可以成功的请求；而连不上时每一次请求都是必然失败，
    空转只会拖长用户的等待。
    """
    return classify_transport_error(exc) == KIND_UNREACHABLE
