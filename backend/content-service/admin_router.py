"""content-service admin 路由（CP3.6-A1/A2/A3 + CP7.x）。

P2-1 拆分（CP11.x 走查）：
    从原 content-service/main.py 1604-2417 行（含 SectionalBase + AdminActionRequest
    schema + 12 端点 + 9 helper）整体抽出。所有依赖通过 APIRouter() 注入；admin 段
    与主路由之间仅共享 stashbox.backend.common.* 与 app.services.llm.*，
    不依赖 main.py 的局部变量。

路由清单（拆分前 vs 拆分后 0 外部行为差异）：
    POST /api/v1/admin/articles/{article_id}/force-retry
    POST /api/v1/admin/audio/{audio_id}/invalidate
    GET  /api/v1/admin/audit-log
    GET  /api/v1/admin/llm/config
    PUT  /api/v1/admin/llm/config
    GET  /api/v1/admin/llm/test
    GET  /api/v1/admin/export/{users|articles|feedback|audit-log|subscriptions}.csv
    GET  /api/v1/admin/stats
    GET  /api/v1/admin/distill-p95
"""

import csv
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

from stashbox.backend.common.redis_client import get_redis_pool

from clients.ai_client import get_ai_client  # noqa: E402

from stashbox.backend.common import cache_service, system_config
from stashbox.backend.common.auth import create_access_token
from stashbox.backend.common.auth_admin import require_admin_or_operator
from stashbox.backend.common.database import AsyncSessionLocal, get_db
from stashbox.backend.common.exceptions import InvalidRequest, NotFound
from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.models import (
    AdminOperationLog,
    Article,
    DistilledArticle,
    Feedback,
    Tag,
    TagSubscription,
    User,
)
from stashbox.backend.app.services.llm import (
    build_adhoc_client,
    SUPPORTED_PROVIDERS,
    reload,
    resolve_config,
)
from stashbox.backend.app.services.tts import (
    SUPPORTED_PROVIDERS as TTS_SUPPORTED_PROVIDERS,
    reload as tts_reload,
    resolve_config as tts_resolve_config,
)

log = get_logger("content-service.admin")

router = APIRouter()


def _uid(user: dict) -> int | None:
    """从 require_admin_or_operator 注入的 user dict 取 id。

    兼容历史上 admin_router 隐式依赖的全局 _uid（CP TTS-Config：显式补齐，
    不再依赖未声明的外部注入）。
    """
    if not user:
        return None
    if isinstance(user.get("id"), int):
        return user["id"]
    sub = user.get("sub")
    try:
        return int(sub) if sub is not None else None
    except (TypeError, ValueError):
        return None


class AdminActionRequest(BaseModel):
    """admin 写操作统一 body：reason 必填（审计留痕）。"""

    reason: str


# ─── admin 段原样搬入（@app 改 @router）─────────────────────────────────────
# （AdminActionRequest 类定义已上提至本段之前，避免 F811 重复定义）

# ---------------------------------------------------------------------------
# CP3.6-A3 admin 其他端点（v1 §3.6）
# ---------------------------------------------------------------------------


# CP9.x：列所有用户文章（admin 全局视图，不按 JWT sub 过滤）
# CP-TAG-FILTER：admin 可按 tag=slug 过滤（只看所有用户在该标签下的文章）
@router.get("/api/v1/admin/articles")
async def admin_list_articles(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    tag: Optional[str] = None,
):
    """列出所有用户的 articles（admin 全局视图）。

    与 /api/v1/articles 的区别：本端点**不按 JWT sub 过滤**，admin 可看全量。
    CP-TAG-FILTER：admin tag 过滤按 Tag.slug → Tag.name 在 DISTILLED_ARTICLES.tags 中匹配
    """
    base_filter = []
    if status:
        base_filter.append(Article.status == status)
    if tag:
        tag_row = await db.scalar(select(Tag).where(Tag.slug == tag))
        if not tag_row:
            return {"items": [], "total": 0, "tag": tag, "tag_id": None}
        base_filter.append(
            Article.id.in_(
                select(DistilledArticle.article_id).where(
                    DistilledArticle.tags.is_not(None)
                    & DistilledArticle.tags.op("@>")(
                        func.cast(func.json_build_array(tag_row.name), JSONB)
                    )
                )
            )
        )
    q = select(Article).order_by(Article.created_at.desc())
    if base_filter:
        q = q.where(*base_filter)
    if base_filter:
        total = await db.scalar(select(func.count()).select_from(Article).where(*base_filter))
    else:
        total = await db.scalar(select(func.count()).select_from(Article))
    rows = (await db.execute(q.limit(limit).offset(offset))).scalars().all()
    items = []
    for r in rows:
        items.append(
            {
                "id": r.id,
                "user_id": int(r.user_id) if r.user_id else 0,
                "url": r.url,
                "source": r.source,
                "title": r.title,
                "status": r.status,
                "audio_url": r.audio_url,
                "favorite": r.favorite,
                "skip": r.skip,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
        )
    return {
        "items": items,
        "total": total or 0,
        "limit": limit,
        "offset": offset,
        "tag": tag,
        "tag_id": tag_row.id if tag and tag_row else None,
    }


@router.get("/api/v1/admin/articles/{article_id}")
async def admin_get_article(
    article_id: str,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """admin 单篇文章详情（含 task_id + status + audio_url）。"""
    art = await db.get(Article, article_id)
    if art is None:
        raise NotFound(message=f"article {article_id} not found")

    distilled = (
        await db.execute(select(DistilledArticle).where(DistilledArticle.article_id == article_id))
    ).scalar_one_or_none()

    return {
        "article": {
            "id": art.id,
            "user_id": int(art.user_id) if art.user_id else 0,
            "url": art.url,
            "source": art.source,
            "title": art.title,
            "status": art.status,
            "audio_url": art.audio_url,
            "favorite": art.favorite,
            "skip": art.skip,
            "error": art.error,
            "retry_count": art.retry_count,
            "raw_content_summary": (
                (art.raw_content or {}).get("content_text", "")[:200]
                if isinstance(art.raw_content, dict)
                else None
            ),
            "created_at": art.created_at.isoformat() if art.created_at else None,
            "updated_at": art.updated_at.isoformat() if art.updated_at else None,
        },
        "distill": (
            {
                "id": distilled.id,
                "status": distilled.status,
                "audio_url": distilled.audio_url,
                "duration_sec": distilled.duration_sec,
                "tags": distilled.tags,
                "quality_score": distilled.quality_score,
                "script_text": (distilled.script_text or "")[:300],
                "updated_at": distilled.updated_at.isoformat() if distilled.updated_at else None,
            }
            if distilled
            else None
        ),
    }


@router.post("/api/v1/admin/articles/{article_id}/force-retry")
async def admin_force_retry(
    article_id: str,
    req: AdminActionRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 强制重试蒸馏。

    行为：
      1. reason ≥5 字符校验（commit 前，失败不落库）
      2. 校验 article 存在（不存在 404）
      3. 状态置 pending（article 表无 retry_count 列，以 status=pending 表达"待重试"，
         由后续 ai-service / worker 重新蒸馏）
      4. 同事务写 admin_operation_logs 一条（A1 已建表）
      5. 提交后触发 ai-service 蒸馏；不可达时仅置 pending，由 worker 自动重试
    失败回滚事务。
    """
    if len(req.reason.strip()) < 5:
        raise InvalidRequest(message="reason 至少 5 个字符")

    art = await db.get(Article, article_id)
    if art is None:
        raise NotFound(message=f"article {article_id} not found")

    art.status = "pending"

    log_row = AdminOperationLog(
        admin_id=_uid(user),
        admin_tier=user.get("tier", "unknown"),
        action="force_retry",
        target_type="article",
        target_id=article_id,
        reason=req.reason,
        method="POST",
        path=f"/api/v1/admin/articles/{article_id}/force-retry",
        request_body={"reason": req.reason},
        response_status=200,
    )
    db.add(log_row)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await cache_service.invalidate_article(article_id)

    # 触发蒸馏（ai-service 不可达返回 None，不破请求；status=pending 让 worker 自动重试）
    queued = await get_ai_client().trigger_distill(
        article_id, auth_token=create_access_token(str(art.user_id))
    )
    return {
        "article_id": article_id,
        "status": "pending",
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "distill_triggered": queued is not None,
    }


@router.delete("/api/v1/admin/articles/{article_id}")
async def admin_delete_article(
    article_id: str,
    req: AdminActionRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """CP-DELETE：admin 删除任意文章（硬删除，含音频文件）。

    与用户端删除共用 article_purge.purge_article（FK 按序清理）。
    reason ≥5 字符必填（审计），同事务写 admin_operation_logs。
    """
    from article_purge import finish_purge, purge_article

    if len(req.reason.strip()) < 5:
        raise InvalidRequest(message="reason 至少 5 个字符")

    art = await db.get(Article, article_id)
    if art is None:
        raise NotFound(message=f"article {article_id} not found")
    owner_user_id = art.user_id

    await purge_article(db, article_id)

    log_row = AdminOperationLog(
        admin_id=_uid(user),
        admin_tier=user.get("tier", "unknown"),
        action="delete_article",
        target_type="article",
        target_id=article_id,
        reason=req.reason,
        method="DELETE",
        path=f"/api/v1/admin/articles/{article_id}",
        request_body={"reason": req.reason},
        response_status=200,
    )
    db.add(log_row)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await finish_purge(article_id, owner_user_id)
    return {"article_id": article_id, "deleted": True}


@router.post("/api/v1/admin/audio/{audio_id}/invalidate")
async def admin_audio_invalidate(
    audio_id: str,
    req: AdminActionRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 音频文件作废。

    本仓库无独立 audio_files 表，音频实体即 distilled_articles（含 audio_url）。
    行为：
      1. reason ≥5 字符校验
      2. 校验 distilled_article 存在（不存在 404）
      3. 状态置 invalidated
      4. 同事务写 admin_operation_logs 一条
    幂等：重复调用保持 invalidated 状态，仍记录操作日志。
    失败回滚事务。
    """
    if len(req.reason.strip()) < 5:
        raise InvalidRequest(message="reason 至少 5 个字符")

    audio = await db.get(DistilledArticle, audio_id)
    if audio is None:
        raise NotFound(message=f"audio {audio_id} not found")

    audio.status = "invalidated"

    log_row = AdminOperationLog(
        admin_id=_uid(user),
        admin_tier=user.get("tier", "unknown"),
        action="audio_invalidate",
        target_type="audio",
        target_id=audio_id,
        reason=req.reason,
        method="POST",
        path=f"/api/v1/admin/audio/{audio_id}/invalidate",
        request_body={"reason": req.reason},
        response_status=200,
    )
    db.add(log_row)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await cache_service.invalidate_article(audio.article_id)

    return {"audio_id": audio_id, "status": "invalidated"}


@router.get("/api/v1/admin/audit-log")
async def admin_audit_log(
    page: int = 1,
    size: int = 20,
    actor_id: str | None = None,
    action_type: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 审计日志查询（读 admin_operation_logs，CP3.6-A1）。

    查询参数：page / size / actor_id / action_type / from / to
    排序：created_at DESC；过滤：actor_id exact + action_type exact + 时间范围。

    ⚠️ ``from_`` 必须带 ``alias="from"``：``from`` 是 Python 关键字，FastAPI 默认
    直接拿形参名当 query 名，于是形参叫 from_ 时前端发 ``from`` 会被当**未知参数静默丢弃**。
    后果是「结束时间 to 生效、开始时间 from 不生效」—— 筛出来的结果里混着范围外的记录，
    却看起来像筛过了。审计场景下这比完全不筛更危险。
    """
    page = max(page, 1)
    size = max(min(size, 100), 1)
    offset = (page - 1) * size

    # CP9.5 修复：from_/to 字符串转 datetime(原代码 SQLAlchemy 字符串 vs timestamp 比较 500)
    from datetime import datetime as _dt

    from_dt = None
    to_dt = None
    if from_ is not None:
        try:
            from_dt = _dt.fromisoformat(from_)
        except (ValueError, TypeError):
            from_dt = None
    if to is not None:
        try:
            to_dt = _dt.fromisoformat(to)
        except (ValueError, TypeError):
            to_dt = None

    query = select(AdminOperationLog)
    if actor_id is not None:
        try:
            query = query.where(AdminOperationLog.admin_id == int(actor_id))
        except ValueError:
            pass  # 非数字 actor_id 不匹配任何行，返回空
    if action_type is not None:
        query = query.where(AdminOperationLog.action == action_type)
    if from_dt is not None:
        query = query.where(AdminOperationLog.created_at >= from_dt)
    if to_dt is not None:
        query = query.where(AdminOperationLog.created_at <= to_dt)

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        (
            await db.execute(
                query.order_by(AdminOperationLog.created_at.desc()).offset(offset).limit(size)
            )
        )
        .scalars()
        .all()
    )

    items = [
        {
            "id": r.id,
            "actor_id": r.admin_id,
            "action_type": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "payload": r.request_body,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]
    return {"total": total or 0, "items": items}


# ---------------------------------------------------------------------------
# CP7.3 admin LLM 配置（admin-web 配置页后端）
#
# 落库：system_config 表 key="llm"（见 common/system_config.py）。
# 热生效：PUT 写完立刻 DEL 缓存 + `await reload()` 重建 factory 里的 client。
# api_key 只往外吐 set/last4，明文不出现在任何响应里。
# ---------------------------------------------------------------------------
class LLMConfigUpdate(BaseModel):
    provider: str
    model: str | None = None
    api_key: str | None = None  # 空/不传 = 不动已存的那把 key
    # 不传 = 不动已存的 base_url；显式 null / "" = 清空（回落到 env）
    base_url: str | None = None


# 通用字段缺失时，按 provider 回落到它专属的 env 字段。
# 与 app/services/llm/__init__.py 的 build_client 读法保持一致。
_PROVIDER_MODEL_KEY = {"openai": "openai_llm_model", "qwen_vl": "qwen_vl_model"}
_PROVIDER_BASE_KEY = {"openai": "openai_llm_base_url", "qwen_vl": "qwen_vl_base_url"}
_PROVIDER_KEY = {"openai": "openai_llm_api_key", "qwen_vl": "qwen_vl_api_key"}


def _masked_llm_config(config: dict, source: str, updated_at: str | None) -> dict:
    """把完整配置（含明文 api_key）转成可出网的响应体。

    2026-10-02 修 KeyError: 'model'。

    原来这里是 ``config["model"]`` 硬索引，但 ``_env_config()`` 只产出
    ``openai_llm_model`` / ``qwen_vl_model`` 这类 **provider 专属** 字段，
    通用 ``model`` 键只有当 system_config 表里配过才会有。而该表在新部署下
    是空的 → 回落 env → 没有 ``model`` → KeyError → 整个 GET 500。

    后果不是"少显示一个字段"：**运营在后台打不开 LLM 配置页**，
    既看不到当前用的哪个模型，也没法改。改法是按 provider 回落到专属字段，
    与 build_client 的读法一致。
    """
    provider = str(config.get("provider") or "mock").lower()
    api_key = config.get("api_key") or config.get(_PROVIDER_KEY.get(provider, ""), "")
    return {
        "provider": provider,
        "model": config.get("model")
        or config.get(_PROVIDER_MODEL_KEY.get(provider, ""), "")
        or None,
        "base_url": config.get("base_url")
        or config.get(_PROVIDER_BASE_KEY.get(provider, ""), "")
        or None,
        "api_key_set": bool(api_key),
        "api_key_last4": api_key[-4:] if api_key else None,
        "source": source,  # db = 表里配了；env = 回落环境变量/默认值
        "updated_at": updated_at,
    }


@router.get("/api/v1/admin/llm/config")
async def admin_llm_config_get(user: dict = Depends(require_admin_or_operator)):
    """当前生效的 LLM 配置（DB > env > 默认值）。"""
    stored, updated_at = await system_config.get_config_row(system_config.KEY_LLM)
    config = resolve_config(stored)
    return _masked_llm_config(config, "db" if stored else "env", system_config.as_iso(updated_at))


@router.put("/api/v1/admin/llm/config")
async def admin_llm_config_put(
    req: LLMConfigUpdate,
    user: dict = Depends(require_admin_or_operator),
):
    """改 LLM 配置 → 落 system_config + 立即 reload factory（热生效）。"""
    provider = req.provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise InvalidRequest(
            message=f"provider 必须是 {list(SUPPORTED_PROVIDERS)} 之一，当前 {provider!r}"
            "（deepseek / glm 的 client 还没实现，配了也会回落到 mock）",
        )

    stored = dict(await system_config.get_config(system_config.KEY_LLM) or {})
    stored["provider"] = provider
    if req.model:
        stored["model"] = req.model
    if req.api_key:
        stored["api_key"] = req.api_key
    if "base_url" in req.model_fields_set:  # 显式传了才动（null / "" = 清空）
        stored["base_url"] = (req.base_url or "").strip() or None

    row = await system_config.set_config(system_config.KEY_LLM, stored, updated_by=_uid(user))
    client = await reload()  # 改完即生效，不用重启进程
    log.info(
        "admin_llm_config_updated",
        admin_id=_uid(user),
        provider=provider,
        model=stored.get("model"),
        client=client.provider_name,
    )
    return _masked_llm_config(
        resolve_config(row["value"]),
        "db",
        row["updated_at"].isoformat() if row["updated_at"] else None,
    )


# ---------------------------------------------------------------------------
# CP-LLM-TEST-ERR：/admin/llm/test 错误分类
#
# 之前直接把 f"{type(exc).__name__}: {exc}"[:300] 吐给前端，401/403/404/超时/网络错
# 全混在一起，用户看不出是 API key 错、模型不存在、欠费还是网络问题。
# 这里归一到 error_kind + status_code + 给一段「能直接照着做」的中文 hint；
# 同时去脱敏（httpx 异常里偶尔会带上 Authorization header）。
# ---------------------------------------------------------------------------

# 错误分类。前端按 kind 渲染不同 toast 引导：
#   auth      API key 无效 / 过期 / 权限不足（401）
#   forbidden 账号受限：欠费 / 无该模型权限 / 区域不允许（403）
#   notfound  模型不存在 / base_url 路径错（404）
#   badreq    参数非法（400/422）—— 一般是模型名拼错、temperature 越界等
#   ratelimit 触发限流（429）；hint 里有 retry-after 秒数（如果有）
#   timeout   第三方服务在 timeout 内未响应
#   connect   无法连到 base_url（DNS / 端口不通 / 跨网段）
#   network   其他传输错误（SSL、TLS、连接重置等）
#   internal  内部代码错误（KeyError、JSONDecode、未实现等），不该被运维看到原样
LLM_TEST_ERROR_KINDS = frozenset(
    {
        "auth",
        "forbidden",
        "notfound",
        "badreq",
        "ratelimit",
        "timeout",
        "connect",
        "network",
        "internal",
    }
)

# 默认引导文案（按场景给"下一步干啥"）。error_kind -> 一句话中文。
# 注意这是 hint（提示用户），不是原始异常的复述 —— 原始异常放 detail 字段。
_LLM_ERROR_HINTS: dict[str, str] = {
    "auth": "API key 无效或已过期，请检查后重新保存配置",
    "forbidden": "账号被限制使用该模型（可能欠费、无模型权限或区域受限），请到供应商后台核查",
    "notfound": "模型不存在或 Base URL 路径错误，请核对「模型名」和「Base URL」",
    "badreq": "请求参数不合法（通常是模型名格式不对或参数越界），请检查配置",
    "ratelimit": "请求过于频繁，请稍后再试",
    "timeout": "第三方服务未在 timeout 内响应，可能正在排队或服务卡死，请稍后重试",
    "connect": "无法连接到 LLM 服务端，请检查 Base URL（含端口、协议）是否可访问",
    "network": "网络传输异常（SSL/连接重置等），请检查网络环境或代理设置",
    "internal": "服务端处理异常（响应格式非预期），请联系开发排查并附上 detail",
}


def _classify_llm_error(exc: BaseException) -> dict[str, Any]:
    """把任意异常压成 {kind, status_code, hint, detail}。

    - kind: 上方 LLM_TEST_ERROR_KINDS 之一，前端用它选 toast 文案
    - status_code: 第三方服务实际返回的 HTTP 状态码（如果有）
    - hint: 给操作员看的「下一步干啥」中文提示
    - detail: 原始异常字符串（脱敏后），技术排查用

    特别注意 httpx.ConnectError / TimeoutException / HTTPStatusError 的区分；
    同时脱敏 Authorization / Bearer token，避免 API key 跟着错误回显出去。
    """
    # 嵌套异常：openai / qwen client 在 3 次 retry 后抛
    #   RuntimeError("... after 3 attempts: <last_err>")
    # last_err 通常是 HTTPStatusError 或 TimeoutException，但被 str() 包了一层；
    # 我们先取最里层的 cause 来分类。
    inner = exc
    while inner.__cause__ is not None and inner.__cause__ is not inner:
        inner = inner.__cause__
    # httpx.HTTPStatusError: 看 status_code；httpx.TimeoutException: timeout；
    # httpx.ConnectError / NetworkError: connect / network。
    status_code: int | None = None
    retry_after: str | None = None
    kind = "internal"
    if isinstance(inner, httpx.HTTPStatusError):
        status_code = inner.response.status_code
        retry_after = inner.response.headers.get("retry-after")
        sc = inner.response.status_code
        if sc == 401:
            kind = "auth"
        elif sc == 403:
            kind = "forbidden"
        elif sc == 404:
            kind = "notfound"
        elif sc in (400, 422):
            kind = "badreq"
        elif sc == 429:
            kind = "ratelimit"
        elif sc >= 500:
            kind = "internal"  # 服务端 5xx 当成"内部"（不是用户配置问题）
        else:
            kind = "badreq"
    elif isinstance(inner, httpx.TimeoutException):
        kind = "timeout"
    elif isinstance(inner, httpx.ConnectError):
        kind = "connect"
    elif isinstance(inner, httpx.NetworkError):
        kind = "network"
    elif isinstance(inner, httpx.HTTPError):
        # 兜底 httpx 家族异常
        kind = "network"

    hint = _LLM_ERROR_HINTS.get(kind, _LLM_ERROR_HINTS["internal"])
    if kind == "ratelimit" and retry_after:
        hint = f"{hint}（Retry-After: {retry_after}s）"

    # 原始异常字符串（脱敏：去掉 Authorization / Bearer xxx）
    raw = f"{type(inner).__name__}: {inner}"
    # httpx 的 Request URL 形式：for url 'https://...' —— 把 URL 里 query 中的 key
    # （罕见但有）也一起去掉。简单起见，把 Authorization: Bearer xxxx 整段删。
    detail = re.sub(r"Authorization:\s*Bearer\s+\S+", "Authorization: Bearer ***", raw)
    # 防御：万一 URL 里塞了 api_key 参数也清掉
    detail = re.sub(r"(api_key|access_token)=[^&\s]+", r"\1=***", detail)
    # 限长保护：500 字，避免特殊响应体塞爆日志和前端
    if len(detail) > 500:
        detail = detail[:500] + "…"

    return {
        "kind": kind,
        "status_code": status_code,
        "hint": hint,
        "detail": detail,
    }


class LLMTestRequest(BaseModel):
    """「测试调用」要验的那份配置 —— 运营**刚填进表单**的值，不落库。"""

    provider: str
    model: str
    api_key: str = ""
    base_url: str | None = None


@router.post("/api/v1/admin/llm/test")
async def admin_llm_test_with_config(
    req: LLMTestRequest,
    user: dict = Depends(require_admin_or_operator),
):
    """按**请求里的配置**发一次 chat()，不读已保存的、不写库。

    为什么需要它：GET 版本走 `reload()` 拿的是**已保存**的配置。运营改了
    base_url / model 点「测试调用」，看到的是旧配置的绿灯 —— 填错的地址能通过
    测试、保存成功，然后在生产推理时才炸。整条链路上没有任何一处提示测的
    不是你填的东西，而那正是这个页面存在的唯一理由。

    GET 版本保留（老前端 / 脚本仍可用），前端不再用它。
    """
    if os.getenv("ENABLE_LLM_TEST_ENDPOINT", "1").lower() in {"0", "false", "no"}:
        raise NotFound(message="llm test endpoint disabled")

    provider = req.provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise InvalidRequest(
            message=f"provider 必须是 {list(SUPPORTED_PROVIDERS)} 之一，当前 {provider!r}"
        )

    # app/services/llm 用 provider 前缀式键名，这里从请求体映射过去
    cfg: dict[str, Any] = {
        "provider": provider,
        f"{provider}_llm_model": req.model,
        f"{provider}_llm_api_key": req.api_key,
        f"{provider}_llm_base_url": req.base_url,
    }
    if provider == "qwen_vl":
        # qwen_vl 的 provider 名带 _vl，键名是 qwen_vl_llm_*（与 build_client 一致）
        cfg["qwen_vl_llm_model"] = req.model
        cfg["qwen_vl_llm_api_key"] = req.api_key
        cfg["qwen_vl_llm_base_url"] = req.base_url

    result: dict[str, Any] = {"provider": provider, "model": req.model}
    client = None
    try:
        client = build_adhoc_client(cfg)
    except Exception as exc:  # noqa: BLE001 —— provider/参数非法也要给可读分类
        cls = _classify_llm_error(exc)
        result.update(
            ok=False,
            error=cls["hint"],
            error_kind=cls["kind"],
            status_code=cls["status_code"],
            hint=cls["hint"],
            detail=cls["detail"],
        )
        return result

    try:
        text = await client.chat("CP7.3 hot-reload smoke test：用一句话总结这段话。")
        result.update(
            ok=True,
            text=text,
            error=None,
            error_kind=None,
            status_code=None,
            hint=None,
            detail=None,
        )
    except Exception as exc:
        cls = _classify_llm_error(exc)
        result.update(
            ok=False,
            error=cls["hint"],
            error_kind=cls["kind"],
            status_code=cls["status_code"],
            hint=cls["hint"],
            detail=cls["detail"],
        )
    finally:
        # 一次性 client：close() 会真关底层 httpx 池，测试端点不该留下连接
        try:
            await client.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不该覆盖测试结果
            log.warning("adhoc llm client close failed", exc_info=True)
    return result


@router.get("/api/v1/admin/llm/test")
async def admin_llm_test(user: dict = Depends(require_admin_or_operator)):
    """CP7.3 联调真验用：用当前 factory 的 client 发一次 chat()，确认 provider 真换了。

    临时端点 —— 用 ENABLE_LLM_TEST_ENDPOINT=0 关掉（关掉后返回 404）。
    openai client 的 chat() 是 CP7.3.4 真实现；provider=openai 也会真发请求，
    所以这里的 ok / error_kind 是这次"调通与否"的真实信号。

    响应字段：
        ok             bool 是否成功拿到 chat() 返回
        provider       当前生效的 provider 名
        model          当前生效的模型名
        text           成功时的 LLM 返回文本（成功才有）
        error_kind     失败时的分类（auth/forbidden/notfound/badreq/ratelimit/
                       timeout/connect/network/internal，前端按 kind 渲染 toast）
        status_code    第三方服务返回的 HTTP 状态码（如果是 HTTP 类错误；否则 null）
        hint           给操作员的「下一步干啥」中文提示
        detail         原始异常的脱敏字符串，技术排查用（不再 300 字截断）
    """
    if os.getenv("ENABLE_LLM_TEST_ENDPOINT", "1").lower() in {"0", "false", "no"}:
        raise NotFound(message="llm test endpoint disabled")

    client = await reload()
    result: dict[str, Any] = {
        "provider": client.provider_name,
        "model": getattr(client, "model", None),
    }
    try:
        text = await client.chat("CP7.3 hot-reload smoke test：用一句话总结这段话。")
        result["ok"] = True
        result["text"] = text
        # 成功也回 null 字段，保证前端 schema 一致（兼容字段 error 也置 null，
        # 避免老前端区分"无 error 键"与"error=null"时出 bug）
        result["error"] = None
        result["error_kind"] = None
        result["status_code"] = None
        result["hint"] = None
        result["detail"] = None
    except Exception as exc:
        cls = _classify_llm_error(exc)
        result["ok"] = False
        result["error"] = cls["hint"]  # 兼容老前端字段（直接展示这一行 hint）
        result["error_kind"] = cls["kind"]
        result["status_code"] = cls["status_code"]
        result["hint"] = cls["hint"]
        result["detail"] = cls["detail"]
        # 失败时 text 不返回（避免和 error 含义冲突）
    return result


# ---------------------------------------------------------------------------
# CP TTS-Config：admin TTS 配置（admin-web 配置页后端）
#
# 落库：system_config 表 key="tts"（见 common/system_config.py）。
# 热生效：PUT 写完立刻 DEL 缓存 + `await tts_reload()` 重建 factory 里的 client。
# api_key 只往外吐 set/last4，明文不出现在任何响应里。
# ---------------------------------------------------------------------------
class TTSConfigUpdate(BaseModel):
    """PUT 请求体。空/不传的字段视作「不动」，null/"" 显式清空回落到 env。"""

    provider: str
    # edge
    edge_voice: str | None = None
    # openai 协议（火山方舟/OpenAI/Azure）
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str | None = None
    openai_voice: str | None = None
    # doubao
    doubao_api_key: str | None = None
    doubao_token: str | None = None
    doubao_app_id: str | None = None
    doubao_voice: str | None = None
    doubao_resource_id: str | None = None
    # local（macOS say + ffmpeg）
    local_voice: str | None = None
    ffmpeg_bin: str | None = None
    # indextts（oMLX /v1/audio/speech + ref_audio 零样本克隆）
    indextts_base_url: str | None = None
    indextts_model: str | None = None
    indextts_ref_audio: str | None = None
    indextts_ref_text: str | None = None


def _masked_tts_config(config: dict, source: str, updated_at: str | None) -> dict:
    """把完整配置（含明文 api_key）转成可出网的响应体。"""
    api_key = config.get("openai_api_key") or config.get("doubao_api_key") or ""
    return {
        "provider": config.get("provider") or "mock",
        "edge_voice": config.get("edge_voice"),
        "openai_base_url": config.get("openai_base_url"),
        "openai_model": config.get("openai_model"),
        "openai_voice": config.get("openai_voice"),
        "doubao_voice": config.get("doubao_voice"),
        "doubao_resource_id": config.get("doubao_resource_id"),
        "local_voice": config.get("local_voice"),
        "ffmpeg_bin": config.get("ffmpeg_bin"),
        "indextts_base_url": config.get("indextts_base_url"),
        "indextts_model": config.get("indextts_model"),
        "indextts_ref_audio": config.get("indextts_ref_audio"),
        "indextts_ref_text": config.get("indextts_ref_text"),
        "api_key_set": bool(api_key),
        "api_key_last4": api_key[-4:] if api_key else None,
        "source": source,
        "updated_at": updated_at,
    }


@router.get("/api/v1/admin/tts/config")
async def admin_tts_config_get(user: dict = Depends(require_admin_or_operator)):
    """当前生效的 TTS 配置（DB > env > 默认值）。"""
    stored, updated_at = await system_config.get_config_row(system_config.KEY_TTS)
    config = tts_resolve_config(stored)
    return _masked_tts_config(config, "db" if stored else "env", system_config.as_iso(updated_at))


@router.put("/api/v1/admin/tts/config")
async def admin_tts_config_put(
    req: TTSConfigUpdate,
    user: dict = Depends(require_admin_or_operator),
):
    """改 TTS 配置 → 落 system_config + 立即 reload factory（热生效）。"""
    provider = req.provider.strip().lower()
    if provider not in TTS_SUPPORTED_PROVIDERS:
        raise InvalidRequest(
            message=(f"provider 必须是 {list(TTS_SUPPORTED_PROVIDERS)} 之一，当前 {provider!r}")
        )

    stored = dict(await system_config.get_config(system_config.KEY_TTS) or {})
    stored["provider"] = provider
    # 用 model_fields_set 判断「显式传了」：null/""=清空回 env；不传=不动
    fields = req.model_fields_set
    for fname in (
        "edge_voice",
        "openai_base_url",
        "openai_model",
        "openai_voice",
        "doubao_voice",
        "doubao_resource_id",
        "local_voice",
        "ffmpeg_bin",
        "indextts_base_url",
        "indextts_model",
        "indextts_ref_audio",
        "indextts_ref_text",
    ):
        if fname in fields:
            v = getattr(req, fname)
            stored[fname] = (v or "").strip() or None
    # api_key/token 不传=不动；显式传空串=不动（避免误清空已有 key）
    if "openai_api_key" in fields and req.openai_api_key:
        stored["openai_api_key"] = req.openai_api_key
    if "doubao_api_key" in fields and req.doubao_api_key:
        stored["doubao_api_key"] = req.doubao_api_key
    if "doubao_token" in fields and req.doubao_token:
        stored["doubao_token"] = req.doubao_token
    if "doubao_app_id" in fields:
        stored["doubao_app_id"] = (req.doubao_app_id or "").strip() or None

    row = await system_config.set_config(system_config.KEY_TTS, stored, updated_by=_uid(user))
    client = await tts_reload()  # 改完即生效，不用重启进程
    log.info(
        "admin_tts_config_updated",
        admin_id=_uid(user),
        provider=provider,
        client=client.provider_name,
    )
    return _masked_tts_config(
        tts_resolve_config(row["value"]),
        "db",
        row["updated_at"].isoformat() if row["updated_at"] else None,
    )


@router.post("/api/v1/admin/tts/test")
async def admin_tts_test_with_config(
    req: TTSConfigUpdate,
    user: dict = Depends(require_admin_or_operator),
):
    """按**请求里的配置**合一次音，不读已保存的、不写库。

    与 /api/v1/admin/llm/test 的 POST 版同因：GET 版走 `tts_reload()` 拿的是
    **已保存**的配置，运营改了 provider / base_url / key 点「测试调用」，
    看到的是旧配置的绿灯 —— 填错的地址能通过测试、保存后才在生产合成时炸。

    合并语义与 PUT 完全一致（复用同一段字段覆盖逻辑），区别只在最后一步：
    PUT 落库 + reload，这里只 `tts_resolve_config` 出一个内存态 client，
    测完即关，不入 factory 的缓存。
    """
    if os.getenv("ENABLE_LLM_TEST_ENDPOINT", "1").lower() in {"0", "false", "no"}:
        raise NotFound(message="tts test endpoint disabled")

    provider = req.provider.strip().lower()
    if provider not in TTS_SUPPORTED_PROVIDERS:
        raise InvalidRequest(
            message=f"provider 必须是 {list(TTS_SUPPORTED_PROVIDERS)} 之一，当前 {provider!r}"
        )

    from stashbox.backend.app.services.tts import build_client

    stored = dict(await system_config.get_config(system_config.KEY_TTS) or {})
    stored["provider"] = provider
    fields = req.model_fields_set
    for fname in (
        "edge_voice",
        "openai_base_url",
        "openai_model",
        "openai_voice",
        "doubao_voice",
        "doubao_resource_id",
        "local_voice",
        "ffmpeg_bin",
        "indextts_base_url",
        "indextts_model",
        "indextts_ref_audio",
        "indextts_ref_text",
    ):
        if fname in fields:
            v = getattr(req, fname)
            stored[fname] = (v or "").strip() or None
    if "openai_api_key" in fields and req.openai_api_key:
        stored["openai_api_key"] = req.openai_api_key
    if "doubao_api_key" in fields and req.doubao_api_key:
        stored["doubao_api_key"] = req.doubao_api_key
    if "doubao_token" in fields and req.doubao_token:
        stored["doubao_token"] = req.doubao_token
    if "doubao_app_id" in fields:
        stored["doubao_app_id"] = (req.doubao_app_id or "").strip() or None

    effective = tts_resolve_config(stored)
    result: dict[str, Any] = {
        "provider": provider,
        "voice": effective.get(f"{provider}_voice")
        if provider != "edge"
        else effective.get("edge_voice"),
    }
    client = None
    try:
        client = build_client(effective)
        b = await client.synthesize("听匣 TTS 连通性测试。", voice=result["voice"])
        result.update(
            ok=bool(b),
            bytes_len=len(b or b""),
            error=None,
            error_kind=None,
            status_code=None,
            hint=None,
            detail=None,
        )
    except Exception as exc:  # noqa: BLE001 —— provider 非法 / 网络 / 鉴权都要可读分类
        cls = _classify_llm_error(exc)
        result.update(
            ok=False,
            error=cls["hint"],
            error_kind=cls["kind"],
            status_code=cls["status_code"],
            hint=cls["hint"],
            detail=cls["detail"],
        )
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 —— 关闭失败不该覆盖测试结果
                log.warning("adhoc tts client close failed", exc_info=True)
    return result


@router.get("/api/v1/admin/tts/test")
async def admin_tts_test(user: dict = Depends(require_admin_or_operator)):
    """联调真验：用当前 factory 的 client 发一次 synthesize()，确认 provider 真换了。

    mock provider 始终返回固定静音 mp3 字节；其它 provider 真正调底层服务。
    临时端点 —— 用 ENABLE_TTS_TEST_ENDPOINT=0 关掉（关掉后返回 404）。
    """
    if os.getenv("ENABLE_TTS_TEST_ENDPOINT", "1").lower() in {"0", "false", "no"}:
        raise NotFound(message="tts test endpoint disabled")

    client = await tts_reload()
    result: dict[str, Any] = {
        "provider": client.provider_name,
        "voice": getattr(client, "voice", None),
    }
    try:
        # 短文，30 字以内；mock 也跑得通（返回静音 mp3）
        b = await client.synthesize(
            "TTS 烟雾测试。",
            voice=getattr(client, "voice", None),
        )
        result["bytes_len"] = len(b or b"")
        result["ok"] = bool(b)
        # 成功也回 null 字段，保证前端 schema 一致
        result["error"] = None
        result["error_kind"] = None
        result["status_code"] = None
        result["hint"] = None
        result["detail"] = None
    except Exception as exc:
        cls = _classify_tts_error(exc, client.provider_name)
        result["ok"] = False
        result["bytes_len"] = None
        # 兼容老字段：error 直接展示 hint 文案
        result["error"] = cls["hint"]
        result["error_kind"] = cls["kind"]
        result["status_code"] = cls["status_code"]
        result["hint"] = cls["hint"]
        result["detail"] = cls["detail"]
    return result


# ---------------------------------------------------------------------------
# CP-TTS-TEST-ERR：/admin/tts/test 错误分类
#
# 之前和 LLM 一样直接吐 `f"{type(exc).__name__}: {exc}"[:300]`，5 个 provider
# （mock/edge/openai/doubao/local/indextts）错误全混在一起：
#   - openai / doubao / indextts 把 httpx 异常包成 RuntimeError(...)：
#     `RuntimeError("OpenAITTS 错误 401: {...}")`、
#     `IndexTTSError("参考音频文件不存在: ...")`，
#     单纯 isinstance(httpx.HTTPStatusError) 判不出来。
#   - edge 是 `RuntimeError("edge-tts 未安装")` / `RuntimeError("Edge TTS 返回空 bytes")`，
#     无 HTTP 概念。
#   - local 是子进程失败：`RuntimeError("say 合成失败...")` / `RuntimeError("ffmpeg 转码失败...")`。
#
# 分类策略：先看 cause 链（httpx.TimeoutException / ConnectError / NetworkError），
# 兜底再正则匹配 RuntimeError 消息字符串提取 HTTP 状态码 / provider 专属关键词。
# ---------------------------------------------------------------------------

# TTS 错误分类。复用 LLM 大部分 kind，新增 4 个 TTS 专属：
#   empty            返回 200 但 audio bytes 为空（4 个 provider 都会抛"返回空音频/bytes"）
#   missing_dep      缺 Python 包（如 edge-tts 没装）
#   subprocess       本地子进程失败（say / ffmpeg）
#   business_code    火山引擎业务错误码（code=4xxxxxxx，HTTP 是 200 但业务失败）
TTS_TEST_ERROR_KINDS = frozenset(
    {
        "auth",
        "forbidden",
        "notfound",
        "badreq",
        "ratelimit",
        "timeout",
        "connect",
        "network",
        "empty",
        "missing_dep",
        "subprocess",
        "business_code",
        "internal",
    }
)

# 错误分类 → 中文引导文案。error_kind → "下一步干啥"。
_TTS_ERROR_HINTS: dict[str, str] = {
    "auth": "API Key 无效或缺失，请检查后重新保存配置",
    "forbidden": "账号被限制（可能欠费、无该模型权限或区域受限），请到供应商后台核查",
    "notfound": "资源不存在 —— 模型/音色 ID 错、Base URL 路径错或参考音频文件找不到",
    "badreq": "请求参数非法（通常是模型名/音色 ID 格式不对），请检查配置",
    "ratelimit": "请求过于频繁，请稍后再试",
    "timeout": "TTS 服务在 timeout 内未响应，可能正在排队或服务卡死，请稍后重试",
    "connect": "无法连接到 TTS 服务端，请检查 Base URL（含端口、协议）和网络",
    "network": "网络传输异常（SSL/连接重置等），请检查网络环境或代理设置",
    "empty": "TTS 服务返回了 200 但音频为空（可能音色 ID 不对或服务端异常），请检查配置或重试",
    "missing_dep": "依赖 Python 包未安装，请按提示 pip install 后重启 ai-service",
    "subprocess": "本地子进程失败 —— 检查 ffmpeg 是否在路径中、`say` 是否可用、或磁盘剩余空间",
    "business_code": "供应商业务错误码（HTTP 200 但业务失败），通常是无权限/欠费/参数错，请到供应商后台核查",
    "internal": "服务端处理异常（响应格式非预期），请联系开发排查并附上 detail",
}

# 从 RuntimeError 字符串里抠出 HTTP 状态码的正则（兜底用）。
# 覆盖 4 个 provider 的 4 种写法：
#   OpenAITTS 错误 401: {...}
#   Doubao TTS HTTP 401: ...
#   IndexTTS HTTP 401: ...
#   Client error '401 Unauthorized' for url '...'
_TTS_HTTP_CODE_RE = re.compile(r"(?:HTTP\s+|\u9519\u8bef\s+|Client error ')(\d{3})")

# 火山引擎业务错误码（HTTP 200 但 code 非 0 / 20000000）：
#   Doubao TTS 错误: code=4500000 message="..."
# 火山引擎 code 是 7 位数字（4xxxxxxx / 20000000），用 \d{5,7} 兼容历史 5 位格式。
_TTS_TSCODE_RE = re.compile(r"code=(\d{5,7})")


def _classify_tts_error(exc: BaseException, provider: str) -> dict[str, Any]:
    """把任意 TTS 异常压成 {kind, status_code, hint, detail}。

    分类优先级：
      1) cause 链里有 HTTPStatusError → 按状态码分（auth/forbidden/...）
      2) cause 链里有 TimeoutException → timeout
      3) cause 链里有 ConnectError → connect
      4) cause 链里有 NetworkError → network
      5) 字符串匹配 provider 专属关键词（未安装/返回空/子进程错/参考音频/业务码）
      6) 字符串里抠 HTTP 状态码按数字分
      7) 兜底 internal

    注意：openai/doubao/indextts 把 httpx 异常包了 RuntimeError，
    所以优先级 1-4 通常需要在 __cause__ 链上找；edge/local 没有 HTTP 概念，
    走优先级 5。
    """
    # 把 cause 链展开成列表（去掉自引用）
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        chain.append(cur)
        seen.add(id(cur))
        cur = cur.__cause__

    # 沿 cause 链找 HTTPStatusError（拿状态码）
    status_code: int | None = None
    retry_after: str | None = None
    for e in chain:
        if isinstance(e, httpx.HTTPStatusError):
            status_code = e.response.status_code
            retry_after = e.response.headers.get("retry-after")
            break

    kind = "internal"
    if any(isinstance(e, httpx.HTTPStatusError) for e in chain):
        # 优先级 1：按 HTTPStatusError 的状态码分
        sc = status_code or 0
        if sc == 401:
            kind = "auth"
        elif sc == 403:
            kind = "forbidden"
        elif sc == 404:
            kind = "notfound"
        elif sc in (400, 422):
            kind = "badreq"
        elif sc == 429:
            kind = "ratelimit"
        elif sc >= 500:
            kind = "internal"
        else:
            kind = "badreq"
    elif any(isinstance(e, httpx.TimeoutException) for e in chain):
        kind = "timeout"
    elif any(isinstance(e, httpx.ConnectError) for e in chain):
        kind = "connect"
    elif any(isinstance(e, httpx.NetworkError) for e in chain):
        kind = "network"
    else:
        # 优先级 5：字符串匹配（兜底 —— openai/doubao/indextts 都用 RuntimeError 把
        # 真实 HTTP 状态码埋进字符串里，正则提不出来就走 internal）
        msg = str(exc) or ""
        msg_lc = msg.lower()
        # missing_dep
        if "未安装" in msg or "未装" in msg:
            kind = "missing_dep"
        # subprocess（local provider 专属）
        elif "say 合成失败" in msg or "ffmpeg 转码失败" in msg:
            kind = "subprocess"
        # empty
        elif "返回空音频" in msg or "返回空 bytes" in msg:
            kind = "empty"
        # notfound（indextts 参考音频路径错 / 模型不存在）
        elif (
            "参考音频文件不存在" in msg
            or "参考音频太小" in msg
            or (provider == "indextts" and "参考音频" in msg)
        ):
            kind = "notfound"
        # auth（缺凭证）：openai 写"需要 api_key"、doubao 写"需要 Coding Plan 专属 API Key"
        # 用正则覆盖"需要" + 中间 ≤40 字 + "api_key|API Key|凭证|API_KEY"
        elif re.search(r"需要.{0,40}?(api[_ ]?key|API Key|API_KEY|凭证)", msg, re.DOTALL):
            kind = "auth"
        # business_code（火山引擎业务码）
        elif "doubao tts 错误: code=" in msg_lc:
            kind = "business_code"
        elif status_code is None:
            # 优先级 6：字符串里抠 HTTP 状态码
            m = _TTS_HTTP_CODE_RE.search(msg)
            if m:
                sc = int(m.group(1))
                status_code = sc
                if sc == 401:
                    kind = "auth"
                elif sc == 403:
                    kind = "forbidden"
                elif sc == 404:
                    kind = "notfound"
                elif sc in (400, 422):
                    kind = "badreq"
                elif sc == 429:
                    kind = "ratelimit"
                elif sc >= 500:
                    kind = "internal"
                else:
                    kind = "badreq"

    hint = _TTS_ERROR_HINTS.get(kind, _TTS_ERROR_HINTS["internal"])
    if kind == "ratelimit" and retry_after:
        hint = f"{hint}（Retry-After: {retry_after}s）"
    # business_code 把码值带进 hint，方便操作员一眼看到
    if kind == "business_code":
        code_match = _TTS_TSCODE_RE.search(str(exc))
        if code_match:
            hint = f"{hint}（code={code_match.group(1)}）"

    # 脱敏：去 Authorization / Bearer / api_key query 参数 / X-Api-Key / env 变量
    raw = f"{type(exc).__name__}: {exc}"
    detail = re.sub(r"Authorization:\s*Bearer\s+\S+", "Authorization: Bearer ***", raw)
    detail = re.sub(r"(api_key|access_token)=[^&\s]+", r"\1=***", detail)
    detail = re.sub(r"X-Api-Key:\s*\S+", "X-Api-Key: ***", detail)
    detail = re.sub(
        r"(DOUBAO_TTS_API_KEY|DOUBAO_TTS_TOKEN|OPENAI_TTS_API_KEY)=\S+",
        r"\1=***",
        detail,
    )
    if len(detail) > 500:
        detail = detail[:500] + "…"

    return {
        "kind": kind,
        "status_code": status_code,
        "hint": hint,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# CP5.6 admin CSV 数据导出（5 端点，v1 §11.5）
#
# 实现约束（本任务红线）：
#   - 标准库 csv + io.StringIO 生成，不引第三方（pandas / openpyxl）
#   - StreamingResponse 逐批下发，避免大表一次性进内存
#   - 不接 OSS / S3、不加 gzip、不加 limit（admin 全量）
#   - 鉴权统一 require_admin_or_operator；每次导出写一条 admin_operation_logs
# ---------------------------------------------------------------------------
_CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
_EXPORT_LOG_ACTION = "ADMIN_EXPORT"


def _csv_filename(name: str) -> str:
    """users -> users-2026-09-17.csv（UTC 日期）。"""
    return f"{name}-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv"


def _csv_cell(value):
    """单元格归一化：None -> ""，datetime -> ISO8601，dict/list -> JSON 文本。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _csv_chunk(rows: list[list]) -> str:
    """把一批行渲染成 CSV 文本（行尾 CRLF，符合 RFC 4180）。"""
    buf = io.StringIO()
    csv.writer(buf).writerows([[_csv_cell(c) for c in row] for row in rows])
    return buf.getvalue()


def _stream_csv(filename: str, header: list[str], fetch_rows=None) -> StreamingResponse:
    """组 StreamingResponse：BOM + header + 逐行下发。

    ``fetch_rows(session)`` 返回 async 迭代器；用**独立 session**（而非请求级
    ``Depends(get_db)``）在生成器内执行，避免请求级 session 在响应体流式发送
    期间被依赖注入提前 close。``fetch_rows=None`` 时只回 header（表缺失降级）。
    """

    async def _gen():
        yield "\ufeff"  # UTF-8 BOM：Excel 直接打开中文列名/内容不乱码
        yield _csv_chunk([header])
        if fetch_rows is None:
            return
        async with AsyncSessionLocal() as session:
            async for row in fetch_rows(session):
                yield _csv_chunk([row])

    return StreamingResponse(
        _gen(),
        media_type=_CSV_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _write_export_log(
    db: AsyncSession, user: dict, path: str, filename: str, row_count: int
) -> None:
    """每次导出写一条 admin_operation_logs（响应开始前提交，客户端中断也留痕）。"""
    db.add(
        AdminOperationLog(
            admin_id=_uid(user),
            admin_tier=user.get("tier", "unknown"),
            action=_EXPORT_LOG_ACTION,
            target_type="export",
            target_id=filename,
            reason=f"admin CSV 导出 {filename}",
            method="GET",
            path=path,
            request_body={"filename": filename, "row_count": row_count},
            response_status=200,
        )
    )
    await db.commit()


async def _table_exists(db: AsyncSession, name: str) -> bool:
    """表是否存在。v1 §4 部分表尚未落 migration，缺失时导出降级为 header-only。"""
    return bool(
        await db.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name)"
            ),
            {"name": name},
        )
    )


@router.get("/api/v1/admin/export/users.csv")
async def admin_export_users_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 users 全量（v1 §4.2.1 字段 + 月配额 / 已用配额）。

    display_name / role 是 v1 §3.6 admin web 的字段名（对应 users.nickname / users.tier）；
    last_active_at 本仓库 users 表未建该列（v1 §4.2.1 未列），列位保留但恒为空，
    以免 admin web 表头随实现漂移。
    """
    path = "/api/v1/admin/export/users.csv"
    filename = _csv_filename("users")
    header = [
        "id",
        "email",
        "display_name",
        "role",
        "tier",
        "status",
        "monthly_quota",
        "used_quota",
        "last_active_at",
        "created_at",
    ]
    total = await db.scalar(select(func.count()).select_from(User)) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        result = await session.stream(select(User).order_by(User.id))
        async for u in result.scalars():
            yield [
                u.id,
                u.email,
                u.nickname,
                u.tier,
                u.tier,
                "active",
                u.monthly_quota,
                u.quota_used,
                None,
                u.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@router.get("/api/v1/admin/export/tags.csv")
async def admin_export_tags_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 tags 全量（2026-10-02 补）。

    为什么补：admin-web 的标签管理页一直在调这个导出（``src/api/admin/csv.ts``
    的 ExportKind 含 'tags'），但**后端从来没实现过**，而 admin 段又不走网关的
    前缀 fallback —— 点了「导出标签」必然 404。更糟的是前端用
    ``window.location.href`` 直链下载、绕过了 axios 拦截器，用户看到的是一个
    下载下来的 404 JSON 页面，且**没有任何报错提示**。
    """
    path = "/api/v1/admin/export/tags.csv"
    filename = _csv_filename("tags")
    header = [
        "id",
        "slug",
        "name",
        "category",
        "is_system",
        "creator_id",
        "created_at",
    ]
    total = await db.scalar(select(func.count()).select_from(Tag)) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        result = await session.stream(select(Tag).order_by(Tag.id))
        async for t in result.scalars():
            yield [
                t.id,
                t.slug,
                t.name,
                t.category,
                t.is_system,
                t.creator_id,
                t.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@router.get("/api/v1/admin/export/articles.csv")
async def admin_export_articles_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 articles 全量 + distilled_articles 标签 / 质量分（LEFT JOIN）。

    listened_at：articles 表无该列（v1 §4.3.1 未建），沿用 CP5.5 口径取
    feedback(type='listen_complete') 的 created_at 最大值。
    """
    path = "/api/v1/admin/export/articles.csv"
    filename = _csv_filename("articles")
    header = [
        "id",
        "user_id",
        "title",
        "source",
        "url",
        "status",
        "tags",
        "quality_score",
        "listened_at",
        "created_at",
    ]
    total = await db.scalar(select(func.count()).select_from(Article)) or 0
    await _write_export_log(db, user, path, filename, total)

    listened_at = (
        select(func.max(Feedback.created_at))
        .where(Feedback.article_id == Article.id, Feedback.type == "listen_complete")
        .correlate(Article)
        .scalar_subquery()
    )

    async def fetch(session: AsyncSession):
        stmt = (
            select(Article, DistilledArticle.tags, DistilledArticle.quality_score, listened_at)
            .outerjoin(DistilledArticle, DistilledArticle.article_id == Article.id)
            .order_by(Article.created_at, Article.id)
        )
        result = await session.stream(stmt)
        async for row in result:
            art, tags, score, listened = row
            yield [
                art.id,
                art.user_id,
                art.title,
                art.source,
                art.url,
                art.status,
                "|".join(str(t) for t in tags) if tags else "",
                score,
                listened,
                art.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@router.get("/api/v1/admin/export/feedback.csv")
async def admin_export_feedback_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 feedback 全量（v1 §4.3.4 全字段）。"""
    path = "/api/v1/admin/export/feedback.csv"
    filename = _csv_filename("feedback")
    header = ["id", "user_id", "article_id", "type", "rating", "reason", "metadata", "created_at"]
    total = await db.scalar(select(func.count()).select_from(Feedback)) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        result = await session.stream(select(Feedback).order_by(Feedback.id))
        async for f in result.scalars():
            yield [
                f.id,
                f.user_id,
                f.article_id,
                f.type,
                f.rating,
                f.reason,
                f.metadata_,
                f.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@router.get("/api/v1/admin/export/audit-log.csv")
async def admin_export_audit_log_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 admin_operation_logs 全量（v1 §3.6 5 原则 2 审计留痕）。

    排序与 GET /api/v1/admin/audit-log 一致（created_at DESC）。
    row_count 在写本次导出日志**之前**统计，故不含本次这条。
    """
    path = "/api/v1/admin/export/audit-log.csv"
    filename = _csv_filename("audit-log")
    header = [
        "id",
        "actor_id",
        "action_type",
        "target_type",
        "target_id",
        "payload",
        "created_at",
    ]
    total = await db.scalar(select(func.count()).select_from(AdminOperationLog)) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        stmt = select(AdminOperationLog).order_by(
            AdminOperationLog.created_at.desc(), AdminOperationLog.id.desc()
        )
        result = await session.stream(stmt)
        async for r in result.scalars():
            yield [
                r.id,
                r.admin_id,
                r.action,
                r.target_type,
                r.target_id,
                r.request_body,
                r.created_at,
            ]

    return _stream_csv(filename, header, fetch)


@router.get("/api/v1/admin/export/subscriptions.csv")
async def admin_export_subscriptions_csv(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """导出 subscriptions 全量（v1 §4.6.1）。

    [known issue] 本仓库 subscriptions 表尚未实现（无 ORM 模型 / 无 migration，
    本地 alembic 停在 0004），故表缺失时降级为「只回 header」的合法 CSV 而非 500
    —— 与 admin stats 对同样未实现的 orders 表的处理口径一致（见 _safe_revenue）。
    表落地后本端点无需改代码即自动生效。
    """
    path = "/api/v1/admin/export/subscriptions.csv"
    filename = _csv_filename("subscriptions")
    header = ["id", "user_id", "tier", "started_at", "expires_at", "status", "auto_renew"]

    if not await _table_exists(db, "subscriptions"):
        await _write_export_log(db, user, path, filename, 0)
        return _stream_csv(filename, header)

    total = await db.scalar(text("SELECT count(*) FROM subscriptions")) or 0
    await _write_export_log(db, user, path, filename, total)

    async def fetch(session: AsyncSession):
        # v1 §4.6.1 建表列名是 start_at / expire_at，导出表头按 admin web 契约用 started_at / expires_at
        result = await session.stream(
            text(
                "SELECT id, user_id, tier, start_at AS started_at, expire_at AS expires_at, "
                "status, auto_renew FROM subscriptions ORDER BY id"
            )
        )
        async for r in result:
            yield [
                r.id,
                r.user_id,
                r.tier,
                r.started_at,
                r.expires_at,
                r.status,
                r.auto_renew,
            ]

    return _stream_csv(filename, header, fetch)


async def _safe_revenue(db: AsyncSession) -> tuple[float, bool]:
    """本月已支付订单金额合计（revenue）。

    orders 表在部分部署可能不存在（无独立 migration 约束），缺表/缺列时返回 0
    而非让 stats 端点整体 500。第二返回值表示数据是否可获取（False = 表缺失），
    前端用 revenue_available=false 触发 tooltip 提示 admin「数据源缺失」。

    重要：PG 在 SQL 失败时会将当前事务置为 abort 状态；except 必须显式 rollback
    重置事务，否则调用方后续所有 SQL 都会 InFailedSQLTransactionError。
    """
    try:
        val = await db.scalar(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM orders "
                "WHERE status = 'paid' "
                "AND date_trunc('month', created_at) = date_trunc('month', now())"
            )
        )
        return float(val or 0), True
    except Exception:
        # 重置事务状态（PG 失败后会把整个事务打废，后续 SQL 必须显式 rollback）
        try:
            await db.rollback()
        except Exception:
            pass
        return 0.0, False


# CP-STATS-REDIS：admin stats 缓存从进程内 dict 升级到 Redis。
# - 多 uvicorn worker 共享缓存（之前每 worker 各算一遍 → 重复 4-8 倍 DB 查询）
# - 进程重启缓存不丢（之前 dict 清零 → 30s 内集中击穿 DB）
# - key 加 :v3 后缀：新版字段（comparison/trends/by_source/...）上线时清掉老 key 自动重建
ADMIN_STATS_KEY = "admin:stats:v3"
ADMIN_STATS_TTL = 30  # 秒


def _redis_client() -> Redis:
    return Redis(connection_pool=get_redis_pool())


async def _get_cached_stats() -> dict | None:
    pool = get_redis_pool()
    r = Redis(connection_pool=pool)
    try:
        raw = await r.get(ADMIN_STATS_KEY)
    finally:
        await r.aclose()
    return json.loads(raw) if raw else None


async def _set_cached_stats(data: dict) -> None:
    pool = get_redis_pool()
    r = Redis(connection_pool=pool)
    try:
        await r.set(ADMIN_STATS_KEY, json.dumps(data, default=str), ex=ADMIN_STATS_TTL)
    finally:
        await r.aclose()


async def _invalidate_stats_cache() -> None:
    """手动清缓存：当前端点还没写端点触发（admin 改配置后看 stats 立即生效的需求暂未提）。
    这里保留入口，方便后续接 admin 操作 hook 调用。
    """
    pool = get_redis_pool()
    r = Redis(connection_pool=pool)
    try:
        await r.delete(ADMIN_STATS_KEY)
    finally:
        await r.aclose()


def _pct_delta(now_val: int, prev_val: int) -> float | None:
    """环比百分比（now vs prev）；prev=0 时 None（避免除零）；返回 0.15 表示 +15%。"""
    if prev_val is None or prev_val <= 0:
        return None
    return round((now_val - prev_val) / prev_val, 4)


# 告警阈值常量：admin 在 .env 里能覆盖就更好；先写死常量 + 注释 why
DISTILL_FAILURE_24H_ALERT_THRESHOLD = 5  # 24h 蒸馏失败超过这个数 → warning


@router.get("/api/v1/admin/stats")
async def admin_stats(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    """管理后台总览统计数据。

    字段（按"真实统计要求"重做，CP-STATS-REWORK）：
      总量
        total_users / total_articles / pending / listened
      真实失败口径
        failed_distillations_24h  来自 DistilledArticle.status='failed' AND updated_at > 24h
        failed_articles_24h        来自 Article.status='failed' AND deleted_at IS NULL AND created_at > 24h
      蒸馏成功率
        distill_success_rate       done_count / (done + failed) DistilledArticle 全量
      音频
        active_audio_files         DistilledArticle.status='done' AND audio_url IS NOT NULL
      营收
        revenue                    本月已支付订单金额合计
        revenue_available          True=数据可获取；False=orders 表缺失（不要误以为 0）
      来源分布
        by_source                  {wechat: n, douyin: n, ...}（article.source 聚合）
      趋势
        trends.articles_created_7d  [{date: 2026-01-01, count: 5}, ...]（按 day 分桶）
        trends.users_created_7d     同上（users 表）
        trends.distill_completed_7d 同上（DistilledArticle.status='done' AND updated_at）
      环比
        comparison.new_articles_24h     {today, yesterday, delta_pct}
        comparison.new_users_24h        同上
        comparison.distill_completed_24h 同上
      告警
        warning                    None 或 {code, message}（failed > N 时填）
      元
        generated_at               ISO8601 时间戳（前端可显示"X 秒前更新"）

    缓存：Redis v3 key，TTL 30s。多 worker 共享，进程重启不丢。
    """
    # Redis read-through cache
    cached = await _get_cached_stats()
    if cached is not None:
        return cached

    now = func.now()
    day_ago = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)
    seven_days_ago = now - timedelta(days=7)

    # --- 总量（修 #1：total_articles 排除 deleted_at） ---
    total_users = await db.scalar(select(func.count()).select_from(User))
    total_articles = await db.scalar(
        select(func.count()).select_from(Article).where(Article.deleted_at.is_(None))
    )
    pending = await db.scalar(
        select(func.count())
        .select_from(Article)
        .where(Article.status == "pending", Article.deleted_at.is_(None))
    )
    listened = await db.scalar(
        select(func.count())
        .select_from(Article)
        .where(Article.status == "listened", Article.deleted_at.is_(None))
    )

    # --- 真实失败口径（修 #2） ---
    # failed_distillations_24h: 蒸馏步骤失败（DistilledArticle.status='failed'），
    # 用 updated_at 不用 created_at —— 失败重试时 created_at 是首次创建时间。
    failed_distillations_24h = await db.scalar(
        select(func.count())
        .select_from(DistilledArticle)
        .where(
            DistilledArticle.status == "failed",
            DistilledArticle.updated_at > day_ago,
        )
    )
    # failed_articles_24h: 文章侧失败（URL 抓不到 / 内容异常 / 配额耗尽）。
    # 排除 deleted_at 非空的（用户已删的 article 失败不影响运维观察）。
    failed_articles_24h = await db.scalar(
        select(func.count())
        .select_from(Article)
        .where(
            Article.status == "failed",
            Article.deleted_at.is_(None),
            Article.created_at > day_ago,
        )
    )

    # --- 音频 ---
    active_audio = await db.scalar(
        select(func.count())
        .select_from(DistilledArticle)
        .where(
            DistilledArticle.status == "done",
            DistilledArticle.audio_url.isnot(None),
        )
    )

    # --- 蒸馏成功率（#6）---
    done_total = (
        await db.scalar(
            select(func.count())
            .select_from(DistilledArticle)
            .where(DistilledArticle.status == "done")
        )
        or 0
    )
    failed_total = (
        await db.scalar(
            select(func.count())
            .select_from(DistilledArticle)
            .where(DistilledArticle.status == "failed")
        )
        or 0
    )
    total_attempted = done_total + failed_total
    distill_success_rate = round(done_total / total_attempted, 4) if total_attempted else 1.0

    # --- 营收（修 #3：表缺时不静默返 0，加 availability 标志）---
    revenue_value, revenue_available = await _safe_revenue(db)

    # --- 按 source 拆分（#5）---
    by_source_rows = (
        await db.execute(
            select(Article.source, func.count().label("n"))
            .where(Article.deleted_at.is_(None))
            .group_by(Article.source)
        )
    ).all()
    by_source = {row.source: int(row.n) for row in by_source_rows}

    # --- 趋势（#7）：最近 7 天按 day 分桶 ---
    # 用 PG 的 date_trunc('day', ts)；过滤 deleted_at 排除用户删除噪音
    articles_trend_rows = (
        await db.execute(
            select(
                func.date_trunc("day", Article.created_at).label("day"),
                func.count().label("n"),
            )
            .where(Article.deleted_at.is_(None), Article.created_at > seven_days_ago)
            .group_by("day")
            .order_by("day")
        )
    ).all()
    users_trend_rows = (
        await db.execute(
            select(
                func.date_trunc("day", User.created_at).label("day"),
                func.count().label("n"),
            )
            .where(User.created_at > seven_days_ago)
            .group_by("day")
            .order_by("day")
        )
    ).all()
    distill_done_trend_rows = (
        await db.execute(
            select(
                func.date_trunc("day", DistilledArticle.updated_at).label("day"),
                func.count().label("n"),
            )
            .where(
                DistilledArticle.status == "done",
                DistilledArticle.updated_at > seven_days_ago,
            )
            .group_by("day")
            .order_by("day")
        )
    ).all()

    def _to_daily(rows) -> list[dict[str, Any]]:
        return [{"date": row.day.date().isoformat(), "count": int(row.n)} for row in rows]

    trends = {
        "articles_created_7d": _to_daily(articles_trend_rows),
        "users_created_7d": _to_daily(users_trend_rows),
        "distill_completed_7d": _to_daily(distill_done_trend_rows),
    }

    # --- 环比（#4）---
    # "今天" = now-1d ~ now；"昨天" = now-2d ~ now-1d
    today_articles = (
        await db.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.deleted_at.is_(None), Article.created_at > day_ago)
        )
        or 0
    )
    yesterday_articles = (
        await db.scalar(
            select(func.count())
            .select_from(Article)
            .where(
                Article.deleted_at.is_(None),
                Article.created_at > two_days_ago,
                Article.created_at <= day_ago,
            )
        )
        or 0
    )
    today_users = (
        await db.scalar(select(func.count()).select_from(User).where(User.created_at > day_ago))
        or 0
    )
    yesterday_users = (
        await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.created_at > two_days_ago, User.created_at <= day_ago)
        )
        or 0
    )
    today_distill_done = (
        await db.scalar(
            select(func.count())
            .select_from(DistilledArticle)
            .where(
                DistilledArticle.status == "done",
                DistilledArticle.updated_at > day_ago,
            )
        )
        or 0
    )
    yesterday_distill_done = (
        await db.scalar(
            select(func.count())
            .select_from(DistilledArticle)
            .where(
                DistilledArticle.status == "done",
                DistilledArticle.updated_at > two_days_ago,
                DistilledArticle.updated_at <= day_ago,
            )
        )
        or 0
    )

    comparison = {
        "new_articles_24h": {
            "today": int(today_articles),
            "yesterday": int(yesterday_articles),
            "delta_pct": _pct_delta(int(today_articles), int(yesterday_articles)),
        },
        "new_users_24h": {
            "today": int(today_users),
            "yesterday": int(yesterday_users),
            "delta_pct": _pct_delta(int(today_users), int(yesterday_users)),
        },
        "distill_completed_24h": {
            "today": int(today_distill_done),
            "yesterday": int(yesterday_distill_done),
            "delta_pct": _pct_delta(int(today_distill_done), int(yesterday_distill_done)),
        },
    }

    # --- 告警（#10）---
    warning = None
    if failed_distillations_24h and failed_distillations_24h > DISTILL_FAILURE_24H_ALERT_THRESHOLD:
        warning = {
            "code": "high_distill_failure",
            "message": (
                f"近 24h 蒸馏失败 {failed_distillations_24h} 条 "
                f"（阈值 {DISTILL_FAILURE_24H_ALERT_THRESHOLD}），"
                "建议检查 ai-service 日志 / 第三方 LLM（TTS）服务可用性"
            ),
            "threshold": DISTILL_FAILURE_24H_ALERT_THRESHOLD,
            "actual": int(failed_distillations_24h),
        }

    result = {
        "total_users": int(total_users or 0),
        "total_articles": int(total_articles or 0),
        "pending": int(pending or 0),
        "listened": int(listened or 0),
        "active_audio_files": int(active_audio or 0),
        "failed_distillations_24h": int(failed_distillations_24h or 0),
        "failed_articles_24h": int(failed_articles_24h or 0),
        "distill_success_rate": distill_success_rate,
        "revenue": revenue_value,
        "revenue_available": revenue_available,
        "by_source": by_source,
        "trends": trends,
        "comparison": comparison,
        "warning": warning,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    await _set_cached_stats(result)
    return result


# CP11.0.3 蒸馏 P95 metrics（从 ai-service /metrics 解析）
_DISTILL_P95_CACHE: dict[str, Any] = {}
_DISTILL_P95_CACHE_TS: float = 0.0
_DISTILL_P95_CACHE_TTL = 30.0  # 30 秒缓存


async def _fetch_ai_metrics() -> str:
    """从 ai-service /metrics + arq worker /metrics 拉 Prometheus 文本，合并。

    Worker 进程的 Prometheus registry 与 FastAPI 进程隔离，需独立抓。
    """
    import httpx

    fastapi_url = os.environ.get("AI_SERVICE_URL", "http://localhost:8103")
    worker_url = os.environ.get("AI_WORKER_METRICS_URL", "http://localhost:8104")
    async with httpx.AsyncClient(timeout=4.0) as c:
        parts = []
        try:
            r = await c.get(f"{fastapi_url}/metrics")
            parts.append(r.text)
        except Exception:
            pass
        try:
            r2 = await c.get(f"{worker_url}/metrics")
            parts.append(r2.text)
        except Exception:
            pass
    return "\n".join(parts)


def _parse_distill_p95(metrics_text: str) -> dict:
    """从 Prometheus 文本解析 distill_step_duration_seconds 的 P50/P95/P99。

    CP-DISTILL-PROM-SDK：用 prometheus_client.parser 替换之前的正则手写解析：
      - SDK 自动处理 _bucket/_count/_sum 三种 sample，结构化 labels
      - 鲁棒性：label 顺序、+Inf、空 metrics 都不再让正则崩
      - 0 依赖：prometheus_client 已在 poetry 依赖里（producer 端 ai-service 在用）

    输入：/metrics 文本（含 ai-service FastAPI 进程 + arq worker 进程的合并）
    输出：
      {
        "by_step": {
          "step1_structure": {
            "p50": ..., "p95": ..., "p99": ...,   # 落在 +Inf 里则为 None
            "count": int,        # 样本数 —— 分位数基于几个样本，必须让人看得见
            "mean": float,       # _sum/_count，精确均值
            "upper_bound": float, # 直方图最大**有限**桶上界
          }, ...
        },
        "overall": {同上}
      }

    为什么分位数可能是 None
    ----------------------
    桶上界是有限的。任何落进 `+Inf` 桶的样本，其真实值直方图并不知道 ——
    `+Inf` 只是一个「比最大的有限桶还大」的哨兵，没有数值。
    早先这里把 +Inf 当成 1e18 参与线性插值，于是 P50 落在 +Inf 时算出
    `600 + ratio * (1e18 - 600)`，页面直接显示 `400000000000000320.00 秒`。
    宁可留 None 让运营看见「超量程」，也不编一个像样但假的数字。
    """
    from prometheus_client.parser import text_string_to_metric_families

    result: dict[str, Any] = {
        "by_step": {},
        "overall": {
            "p50": None,
            "p95": None,
            "p99": None,
            "count": 0,
            "mean": None,
            "upper_bound": None,
        },
    }
    # 按 step 分组 bucket：{step: [(le, cumulative_count), ...]}
    buckets_by_step: dict[str, list[tuple[float, float]]] = {}
    # {step: (sum, count)} —— 均值是直方图算不出来的唯一精确量
    sum_count_by_step: dict[str, tuple[float, float]] = {}
    try:
        families = list(text_string_to_metric_families(metrics_text))
    except Exception:
        # 解析失败返回空（前端 Dashboard 卡片显示"暂无数据"，不 500）
        return result

    for family in families:
        # family.name 不带 _bucket/_count/_sum 后缀
        if family.name != "distill_step_duration_seconds":
            continue
        for sample in family.samples:
            step = sample.labels.get("step")
            if step is None:
                continue
            if sample.name.endswith("_bucket"):
                le_label = sample.labels.get("le")
                if le_label is None:
                    continue
                try:
                    # +Inf 不再折成 1e18 —— 折了就会在下面的插值里造出假数字。
                    # 单独收进 inf_buckets 标记，让分位数知道自己不可解。
                    le = float("inf") if le_label == "+Inf" else float(le_label)
                    cnt = float(sample.value)
                except (TypeError, ValueError):
                    continue
                buckets_by_step.setdefault(step, []).append((le, cnt))
            elif sample.name.endswith("_sum"):
                try:
                    s = sum_count_by_step.setdefault(step, (0.0, 0.0))
                    sum_count_by_step[step] = (s[0] + float(sample.value), s[1])
                except (TypeError, ValueError):
                    continue
            elif sample.name.endswith("_count"):
                try:
                    s = sum_count_by_step.setdefault(step, (0.0, 0.0))
                    sum_count_by_step[step] = (s[0], s[1] + float(sample.value))
                except (TypeError, ValueError):
                    continue

    # 计算每个 step 的 P50/P95/P99（用线性插值近似）
    for step, buckets in buckets_by_step.items():
        finite = sorted((le, cnt) for le, cnt in buckets if le != float("inf"))
        total = buckets[-1][1] if buckets else 0
        total = max(total, finite[-1][1] if finite else 0)
        if total <= 0:
            continue
        upper_bound = finite[-1][0] if finite else None
        p: dict[str, Any] = {
            "count": int(total),
            "upper_bound": upper_bound,
            "mean": None,
        }
        total_sum, total_count = sum_count_by_step.get(step, (0.0, 0.0))
        if total_count > 0:
            p["mean"] = total_sum / total_count
        for q, label in [(0.5, "p50"), (0.95, "p95"), (0.99, "p99")]:
            p[label] = _histogram_quantile(finite, total, total * q)
        result["by_step"][step] = p

    # overall：端到端 = 4 个 step 串行相加。
    # 早先这里算的是「各步同名分位数的平均」—— 把 P50 和 P99 混在一起求平均，
    # 得到的数没有任何统计含义。同名相加才是端到端分位数的合理近似；
    # mean 用各步均值之和，由期望的线性性可知就是精确的 E[端到端]。
    # 任一步骤不可解时整体同样不可解，返回 None 而不是半个真半个假。
    steps = list(result["by_step"].values())
    if steps:
        overall = result["overall"]
        overall["count"] = min(s["count"] for s in steps)
        for q in ("p50", "p95", "p99"):
            vals = [s.get(q) for s in steps]
            overall[q] = sum(vals) if all(v is not None for v in vals) else None
        means = [s.get("mean") for s in steps]
        overall["mean"] = sum(means) if all(m is not None for m in means) else None
        bounds = [s.get("upper_bound") for s in steps]
        overall["upper_bound"] = sum(bounds) if all(b is not None for b in bounds) else None
    return result


def _histogram_quantile(
    finite_buckets: list[tuple[float, float]], total: float, target: float
) -> Optional[float]:
    """在**有限**桶里做线性插值求分位数；不可解时返回 None。

    刻意不接受 +Inf 桶：target 落进 +Inf 说明这个分位数超出直方图量程，
    直方图本身没有足够信息给出真值。此时返回 None，由调用方决定怎么呈现。
    """
    if not finite_buckets or total <= 0:
        return None
    prev_le, prev_cnt = 0.0, 0.0
    for le, cnt in finite_buckets:
        if cnt >= target:
            if cnt == prev_cnt:
                return le
            ratio = (target - prev_cnt) / (cnt - prev_cnt)
            return prev_le + ratio * (le - prev_le)
        prev_le, prev_cnt = le, cnt
    # 连最大的有限桶都没到 target —— 全部样本在 +Inf 里，不可解
    return None


@router.get("/api/v1/admin/tags")
async def admin_list_tags(
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """CP-DELETE：admin 标签列表（含数字 id / is_system / 订阅数）。

    用户端 GET /api/v1/tags 的 id 是 slug 且无 is_system，无法驱动删除按钮
    （删除端点按数字 tag_id + 系统标签保护），故给 admin-web 单独一份。
    """
    rows = (
        await db.execute(
            select(Tag, func.count(TagSubscription.id))
            .outerjoin(TagSubscription, TagSubscription.tag_id == Tag.id)
            .group_by(Tag.id)
            .order_by(Tag.category, Tag.name)
        )
    ).all()
    return {
        "tags": [
            {
                "id": tag.id,
                "slug": tag.slug,
                "name": tag.name,
                "category": tag.category,
                "is_system": tag.is_system,
                "subscriber_count": int(cnt),
                "created_at": tag.created_at.isoformat() if tag.created_at else None,
            }
            for tag, cnt in rows
        ]
    }


@router.delete("/api/v1/admin/tags/{tag_id}")
async def admin_delete_tag(
    tag_id: int,
    req: AdminActionRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """CP-DELETE：admin 删除标签。

    保护：is_system 系统标签禁删（403）——蒸馏 LLM 输出对齐依赖内置标签集。
    级联：tag_subscriptions（FK ondelete=CASCADE）、push_notifications.tag_slug
    （FK ondelete=SET NULL）由 DB 自动处理，无需应用层清理。
    distilled_articles.tags 为 JSONB 文本数组（非 FK），历史标签值保留不动。
    reason ≥5 字符必填（审计），同事务写 admin_operation_logs。
    """
    if len(req.reason.strip()) < 5:
        raise InvalidRequest(message="reason 至少 5 个字符")

    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise NotFound(message=f"tag {tag_id} not found")
    if tag.is_system:
        raise HTTPException(status_code=403, detail="系统标签不可删除")

    tag_name = tag.name
    await db.delete(tag)

    log_row = AdminOperationLog(
        admin_id=_uid(user),
        admin_tier=user.get("tier", "unknown"),
        action="delete_tag",
        target_type="tag",
        target_id=str(tag_id),
        reason=req.reason,
        method="DELETE",
        path=f"/api/v1/admin/tags/{tag_id}",
        request_body={"reason": req.reason},
        response_status=200,
    )
    db.add(log_row)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {"tag_id": tag_id, "name": tag_name, "deleted": True}


class DistillStepStats(BaseModel):
    """单个阶段的分位数。

    p50/p95/p99 为 null 表示**超出监控量程**（样本全落在直方图 +Inf 桶），
    与 count == 0（没跑过）含义完全不同：前者要调桶上界或改监控配置，
    后者是没数据。UI 必须区分这两种「没有值」。

    count / mean / upper_bound 是刻意补的：
      count      —— 分位数基于几个样本算出来的，决定它可不可信（3 个样本
                    算出的 P95 基本只是噪声）；
      mean       —— _sum/_count 的精确均值，分位数不可解时它是唯一还
                    准确的量（实测本机 step3_tts 均值 744.85s，而桶上界
                    当时只到 600s，P50/P95/P99 全不可解）；
      upper_bound—— 直方图最大有限桶上界，用来解释「为什么算不出来」。
    """

    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    count: int = 0
    mean: Optional[float] = None
    upper_bound: Optional[float] = None


class DistillP95Response(BaseModel):
    """GET /api/v1/admin/distill-p95 的响应。

    之前这个端点**没有 response_model**，于是 OpenAPI 里 responses 是空的
    （端点在 schema 里，但响应结构未描述）。后果是改契约时没有任何东西能
    校验：这一版加了 count/mean/upper_bound，落盘的 schema 毫无反应，
    只能靠人肉发现。这里补上模型，让契约显式且可核对。
    """

    cached: bool = False
    by_step: dict[str, DistillStepStats] = {}
    overall: DistillStepStats = DistillStepStats()
    error: Optional[str] = None


@router.get("/api/v1/admin/distill-p95", response_model=DistillP95Response)
async def admin_distill_p95(
    user: dict = Depends(require_admin_or_operator),
):
    """蒸馏 P50/P95/P99 耗时（秒），从 ai-service Prometheus metrics 解析。

    用于 admin-web Dashboard 显示蒸馏性能。

    overall 是 4 个阶段**同名分位数相加**的端到端近似（mean 则是各阶段均值
    之和，由期望的线性性可知是精确的 E[端到端]）。任一阶段不可解时整体同样
    返回 null —— 不能拿 3 步的数和冒充整体。
    """
    global _DISTILL_P95_CACHE, _DISTILL_P95_CACHE_TS
    import time as _t

    now = _t.time()
    if _DISTILL_P95_CACHE and (now - _DISTILL_P95_CACHE_TS) < _DISTILL_P95_CACHE_TTL:
        return {"cached": True, **_DISTILL_P95_CACHE}

    try:
        metrics_text = await _fetch_ai_metrics()
        parsed = _parse_distill_p95(metrics_text)
        _DISTILL_P95_CACHE = parsed
        _DISTILL_P95_CACHE_TS = now
        return {"cached": False, **parsed}
    except Exception as exc:
        log.warning(f"distill-p95 fetch failed: {exc}")
        return {
            "cached": False,
            "by_step": {},
            "overall": {
                "p50": None,
                "p95": None,
                "p99": None,
                "count": 0,
                "mean": None,
                "upper_bound": None,
            },
            "error": str(exc),
        }
