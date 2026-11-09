"""静态音频目录 + **过期 key 重定向**（2026-11-09）。

## 问题

音频 key 是内容寻址的（`{article_id}.{hash8}.{ext}`），
重新蒸馏 ⇒ key 变 ⇒ **旧文件被删** ⇒ 客户端缓存里的旧 URL 变成 404。

实测（2026-11-09 内测）：用户听一篇被重新蒸馏过的文章 ⇒ 服务端 404
⇒ 客户端把它归类成「瞬时网络类」⇒ 界面显示「网络不稳，正在重连…」，
并在同一个永远不存在的文件上反复自动重连（用户说「手机网络正常」是对的）。

## 做法

文件不存在时 ⇒ 查该文章**当前**的音频 key ⇒ 若与请求的不同 ⇒ **302 重定向**过去。

⇒ 旧 URL 永远拿得到**当前**音频：既不 404，也不会播到旧音频；
⇒ 已装的旧客户端**不需要发版**就能恢复。

## 为什么继承 StaticFiles 而不是另写一条路由

另写路由会**遮蔽**静态挂载 ⇒ 得自己实现 Range 支持（客户端在用 Range 续传，
实测有 206 响应）。继承 StaticFiles 则 Range / ETag / 条件请求全都原样保留，
只在 404 这一个分支上动手。

## 为什么解析器是**注入**的

本模块不碰数据库（DB 查询在 `resolve.py` + 网关接线处）⇒
单测可以在不建库的情况下把每条分支测到。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

import structlog
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# 解析器：挂载内路径 → 重定向目标（None = 不重定向，走原本的 404）
StaleResolver = Callable[[str], Awaitable[str | None]]


class AudioFilesWithStaleRedirect(StaticFiles):
    """静态文件服务；文件缺失且 key 已过期时，重定向到当前版本。"""

    def __init__(self, *args, stale_resolver: StaleResolver | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        # 没给解析器 ⇒ 只当普通静态目录（本地开发 / 测试）
        self._stale_resolver = stale_resolver

    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if self._stale_resolver is None:
                raise
            target = await self._stale_resolver(path)
            if not target:
                raise
            structlog.get_logger("api-gateway").info(
                "audio_stale_key_redirect",
                requested=path,
                target=target,
            )
            return RedirectResponse(target, status_code=302)


def build_stale_resolver(
    lookup: Callable[[str], Awaitable[str | None]],
    *,
    parse: Callable[[str], object | None],
    is_stale: Callable[[str, str | None], bool],
    mount_prefix: str,
) -> StaleResolver:
    """把「解析 key → 查当前 key → 判定是否过期 → 拼公开路径」拼成一个解析器。

    参数都从外面注入（而不是在这里 import 具体实现）⇒
    本模块与 `stale_key.py` / `resolve.py` 之间没有循环依赖，也让单测好写。

    兜底原则：**解析器自己出错 ⇒ 当作"没有过期 key"** ⇒ 照旧 404。
    静态文件服务不该因为查库失败而 500。
    """

    async def _resolve(path: str) -> str | None:
        try:
            parsed = parse(path)
            if parsed is None:
                return None
            current = await lookup(parsed.article_id)  # type: ignore[attr-defined]
            if not is_stale(parsed.key, current):  # type: ignore[attr-defined]
                return None
            return f"{mount_prefix}/{current}"
        except Exception as exc:  # noqa: BLE001 —— 兜底：解析失败按"没有"处理
            structlog.get_logger("api-gateway").warning(
                "audio_stale_lookup_failed",
                path=path,
                error=str(exc),
            )
            return None

    return _resolve


def default_audio_dir() -> str:
    """音频静态目录（与网关读同一个环境变量）。"""
    return os.getenv("LOCAL_AUDIO_DIR", "/tmp/audio")
