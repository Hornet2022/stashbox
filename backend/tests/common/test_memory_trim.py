"""MemoryTrimMiddleware：把分配器碎片还给内核（2026-10-25 生产事故回归）。

## 为什么这是回归测试

生产 content-service 被 cgroup ``memory.high`` 节流**永久冻死**
（进程 D 状态 ⇒ 请求永不返回 + 整个服务不响应 + SIGKILL 都杀不掉）。
顶穿水位线的是**常驻内存**，实测（��进程连抓 4 次同一篇 17 图微信文）：

    抓取后（gc 跑过）  anon  97 → 103 → 104 → 104 MiB   ← 每次剪藏留一层碎片
    malloc_trim 之后   anon  77 →  77 →  77 →  77 MiB   ← 每次都精确回到同一数

⇒ ``gc`` 收不掉（不是活对象），``malloc_trim`` 能全部收回。

## 这些用例锁住什么

1. **装了**：content-service 真的挂了这个中间件（忘装 = 白写）；
2. **该 trim 时 trim**：RSS 超阈值 → 调 trim；
3. **不该 trim 时不 trim**：RSS 没超阈值 → 不调（trim 要遍历整个堆，不免费）；
4. **不搞坏请求**：响应原样返回；请求抛异常时异常照常抛出、trim 照常做；
5. **非 glibc 平台不炸**：macOS / 没有 ``malloc_trim`` 时安静跳过。
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stashbox.backend.common.middleware import memory_trim as mt


@pytest.fixture
def app_with_middleware():
    app = FastAPI()
    app.add_middleware(mt.MemoryTrimMiddleware)

    @app.get("/ok")
    async def _ok():
        return {"ok": True}

    @app.get("/boom")
    async def _boom():
        raise RuntimeError("炸了")

    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# 1. 装没装
# ---------------------------------------------------------------------------
def test_content_service_installed_the_middleware():
    """content-service 必须真的挂上 MemoryTrimMiddleware。

    为什么单独锁：中间件写得再好，没装到 app 上就是零效果 ——
    而这种「文件在、没接线」的疏漏最难在生产上发现（剪藏照样成功，
    只是内存照样爬）。这里直接读 app.user_middleware 断言。
    """
    from stashbox.backend.common.middleware.memory_trim import MemoryTrimMiddleware
    from tests.content.helpers import app as content_app

    installed = [m.cls for m in content_app.user_middleware]
    assert (
        MemoryTrimMiddleware in installed
    ), f"content-service 没挂 MemoryTrimMiddleware（当前挂了 {installed}）"


# ---------------------------------------------------------------------------
# 2. 该 trim 时 trim / 不该 trim 时不 trim
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_trims_when_rss_above_threshold(app_with_middleware, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(mt, "current_rss_mib", lambda: mt.TRIM_ABOVE_MIB + 10)
    monkeypatch.setattr(mt, "trim_allocator", lambda: calls.append(True) or True)

    async with _client(app_with_middleware) as c:
        r = await c.get("/ok")

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls == [True], "RSS 超阈值却没有 trim"


@pytest.mark.asyncio
async def test_does_not_trim_below_threshold(app_with_middleware, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(mt, "current_rss_mib", lambda: mt.TRIM_ABOVE_MIB)
    monkeypatch.setattr(mt, "trim_allocator", lambda: calls.append(True) or True)

    async with _client(app_with_middleware) as c:
        r = await c.get("/ok")

    assert r.status_code == 200
    assert calls == [], "RSS 恰好等于阈值时不该 trim（阈值是「超过」才触发）"


@pytest.mark.asyncio
async def test_trims_on_failure_path_too(app_with_middleware, monkeypatch):
    """请求炸了也要 trim —— 失败路径往往正是大对象最多的那条。"""
    calls: list[bool] = []
    monkeypatch.setattr(mt, "current_rss_mib", lambda: mt.TRIM_ABOVE_MIB + 10)
    monkeypatch.setattr(mt, "trim_allocator", lambda: calls.append(True) or True)

    async with _client(app_with_middleware) as c:
        with pytest.raises(RuntimeError):
            await c.get("/boom")

    assert calls == [True], "请求失败时没有 trim"


# ---------------------------------------------------------------------------
# 3. 非 glibc 平台 / 读不到 RSS 时安静跳过
# ---------------------------------------------------------------------------
def test_trim_allocator_is_noop_without_malloc_trim(monkeypatch):
    """平台没有 malloc_trim（macOS / 非 glibc）⇒ 返回 False，不抛异常。"""
    monkeypatch.setattr(mt, "_get_libc", lambda: None)
    assert mt.trim_allocator() is False


def test_trim_allocator_swallows_errors(monkeypatch):
    """malloc_trim 自己炸了 ⇒ 吞掉（绝不能因为资源回收把请求搞挂）。"""

    class _Boom:
        def __getattr__(self, _name):
            raise OSError("boom")

    monkeypatch.setattr(mt, "_get_libc", lambda: _Boom())
    assert mt.trim_allocator() is False


def test_current_rss_mib_returns_zero_when_unreadable(monkeypatch):
    """读不到 /proc（macOS）⇒ 返回 0 ⇒ 视作「不用 trim」，不崩。"""

    def _raise(*_a, **_k):
        raise FileNotFoundError("/proc/self/statm")

    monkeypatch.setattr("builtins.open", _raise)
    assert mt.current_rss_mib() == 0


# ---------------------------------------------------------------------------
# 4. 常量不许悄悄退化
# ---------------------------------------------------------------------------
def test_trim_threshold_stays_in_sane_band():
    """阈值不许被人随手改成一个没意义的数。

    实测依据（生产）：剪藏刚跑完 RSS ≈145 MiB，trim 后 ≈118 MiB，
    空闲请求 ≈118 MiB ⇒ 128 这条线卡在两者之间。
    改这个数之前先重跑一次生产实测（见 deploy/README.md §9.3.1）。
    """
    assert 64 <= mt.TRIM_ABOVE_MIB <= 256, (
        f"TRIM_ABOVE_MIB={mt.TRIM_ABOVE_MIB} 落在实测区间之外 —— "
        "改它之前先按 deploy/README.md §9.3.1 重测一次"
    )
