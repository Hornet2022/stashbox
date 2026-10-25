"""MemoryTrimMiddleware：请求结束后把 glibc 分配器里的碎片还给操作系统。

## 为什么需要它（2026-10-25 生产事故实测）

content-service 被 cgroup 的 ``memory.high`` **节流**永久冻死：进程进 D 状态
（``wchan=mem_cgroup_handle_over_high``），请求永不返回，**整个服务**都不响应
（连列表查询也超时），且 D 状态 ``SIGKILL`` 都杀不掉 ⇒ 不会自愈、不会告警。

而顶穿水位线的**不是某一次请求的瞬时开销，是常驻内存**。
实测（同进程连抓 4 次同一篇 17 图的微信文，每次都是完整抓取+识图）：

    抓取后（gc 跑过）  anon  97 → 103 → 104 → 104 MiB
    malloc_trim 之后   anon  77 →  77 →  77 →  77 MiB

⇒ 每次剪藏留下的是**分配器碎片**（大对象释放后 arena 没还给内核）：

* ``gc.collect()`` 收不掉 —— 它不是活对象，是 glibc 手里已经 free 的页；
* ``malloc_trim(0)`` 能**全部**收回来 —— 收完之后每次都精确回到同一个数。

⇒ 装这个中间件：像剪藏这种「大对象来回倒」的重活干完，把碎片还给内核，
常驻内存就不会随剪藏次数爬升 ⇒ 不会顶到水位线。

## 为什么放在中间件而不是剪藏那一处

碎片是**所有**重活留下的，不只剪藏；而「什么时候能还」只有一个统一的时机 ——
**请求结束**（此时这次请求的大对象已经没人引用了）。放在中间件里 ⇒ 一份实现，
所有端点都受益，将来加别的重活路径也不会漏。

## 为什么要设阈值

``malloc_trim`` 会遍历整个堆做 ``madvise``，不是免费的。空闲请求没必要做。
实测（生产）：剪藏刚跑完 RSS ≈ 145 MiB，trim 之后回落到 ≈ 118 MiB；
普通列表请求一直在 ≈ 118 MiB 上下 ⇒ 阈值取 128 MiB：
能把「剪藏后的碎片」挑出来，又不会让普通请求白 trim。

## 平台

``malloc_trim`` 是 glibc 特有的。macOS / 非 glibc 上 :func:`trim_allocator`
直接返回 ``False``（不做任何事），开发机与 CI 不会因此报错。
"""

import asyncio
import ctypes
import os

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

_log = structlog.get_logger("memory_trim")

#: RSS 超过这个值（MiB）才 trim。低于它说明没有可回收的碎片，白 trim 只是浪费 CPU。
#:
#: 实测依据（2026-10-25 生产）：剪藏刚跑完 ≈145 MiB → trim 后 ≈118 MiB；
#: 空闲/列表请求稳定在 ≈118 MiB。⇒ 128 这条线正好卡在两者之间。
TRIM_ABOVE_MIB = 128

_libc = None
_libc_resolved = False


def _get_libc():
    """拿到 libc 并确认它有 ``malloc_trim``；没有就返回 None（非 glibc 平台）。"""
    global _libc, _libc_resolved
    if _libc_resolved:
        return _libc
    _libc_resolved = True
    if os.name != "posix":
        _libc = None
        return None
    try:
        lib = ctypes.CDLL("libc.so.6")
        lib.malloc_trim  # noqa: B018 —— 存在性探测：没有这个符号会抛 AttributeError
        _libc = lib
    except (OSError, AttributeError):
        _libc = None
    return _libc


def current_rss_mib() -> int:
    """当前进程的 RSS（MiB）。读不到（macOS 没有 /proc）就返回 0。"""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
    except (OSError, IndexError, ValueError):
        return 0
    return pages * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)


def trim_allocator() -> bool:
    """把 glibc 分配器里已释放的内存还给内核。返回是否真的做了。

    失败一律吞掉 —— 这是「锦上添花的资源回收」，绝不能因为它把请求搞挂。
    """
    lib = _get_libc()
    if lib is None:
        return False
    try:
        lib.malloc_trim(0)
        return True
    except Exception as exc:  # noqa: BLE001
        _log.warning("malloc_trim_failed", err=type(exc).__name__)
        return False


class MemoryTrimMiddleware(BaseHTTPMiddleware):
    """请求结束后按阈值把分配器碎片还给内核。"""

    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # 请求炸了也要还碎片 —— 失败路径往往正是大对象最多的那条
            await self._maybe_trim(path=request.url.path)
            raise
        await self._maybe_trim(path=request.url.path)
        return response

    async def _maybe_trim(self, *, path: str) -> None:
        before = current_rss_mib()
        if before <= TRIM_ABOVE_MIB:
            return
        # malloc_trim 是同步的（要遍历整个堆 + madvise）⇒ 丢线程里跑，
        # 别把事件循环卡住。
        after = await asyncio.to_thread(self._trim_sync)
        if after is not None:
            _log.info("memory_trimmed", path=path, rss_before_mib=before, rss_after_mib=after)

    @staticmethod
    def _trim_sync() -> int | None:
        """在线程里跑 trim；返回 trim 后的 RSS（MiB），没做就返回 None。"""
        if not trim_allocator():
            return None
        return current_rss_mib()


__all__ = [
    "TRIM_ABOVE_MIB",
    "MemoryTrimMiddleware",
    "current_rss_mib",
    "trim_allocator",
]
