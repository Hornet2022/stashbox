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
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common import cache_service, system_config
from stashbox.backend.common.auth_admin import require_admin_or_operator
from stashbox.backend.common.database import get_db
from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.models import (
    AdminOperationLog,
    Article,
    DistilledArticle,
    Feedback,
    User,
)
from stashbox.backend.app.services.llm import (
    SUPPORTED_PROVIDERS,
    reload,
    resolve_config,
)

log = get_logger("content-service.admin")

router = APIRouter()


class AdminActionRequest(BaseModel):
    """admin 写操作统一 body：reason 必填（审计留痕）。"""

    reason: str


# ─── admin 段原样搬入（@app 改 @router）─────────────────────────────────────


class AdminActionRequest(BaseModel):
    """admin 写操作统一 body：reason 必填（审计留痕）。"""

    reason: str


# ---------------------------------------------------------------------------
# CP3.6-A3 admin 其他端点（v1 §3.6）
# ---------------------------------------------------------------------------
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
    from_: str | None = None,
    to: str | None = None,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """v1 §3.6 审计日志查询（读 admin_operation_logs，CP3.6-A1）。

    查询参数：page / size / actor_id / action_type / from / to
    排序：created_at DESC；过滤：actor_id exact + action_type exact + 时间范围。
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


def _masked_llm_config(config: dict, source: str, updated_at: str | None) -> dict:
    """把完整配置（含明文 api_key）转成可出网的响应体。"""
    api_key = config.get("api_key") or ""
    return {
        "provider": config["provider"],
        "model": config["model"],
        "base_url": config.get("base_url") or None,
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


@router.get("/api/v1/admin/llm/test")
async def admin_llm_test(user: dict = Depends(require_admin_or_operator)):
    """CP7.3 联调真验用：用当前 factory 的 client 发一次 chat()，确认 provider 真换了。

    临时端点 —— 用 ENABLE_LLM_TEST_ENDPOINT=0 关掉（关掉后返回 404）。
    openai client 的 chat() 还是 CP7.1 的 NotImplementedError 占位实现，
    所以 provider=openai 时这里会 ok=false + 报错，但 provider 字段能证明切换生效。
    """
    if os.getenv("ENABLE_LLM_TEST_ENDPOINT", "1").lower() in {"0", "false", "no"}:
        raise NotFound(message="llm test endpoint disabled")

    client = await reload()
    result = {
        "provider": client.provider_name,
        "model": getattr(client, "model", None),
    }
    try:
        result["text"] = await client.chat("CP7.3 hot-reload smoke test：用一句话总结这段话。")
        result["ok"] = True
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return result


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


async def _safe_revenue(db: AsyncSession) -> float:
    """本月已支付订单金额合计（revenue）。

    orders 表在部分部署可能不存在（无独立 migration 约束），缺表/缺列时返回 0
    而非让 stats 端点整体 500。
    """
    try:
        val = await db.scalar(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM orders "
                "WHERE status = 'paid' "
                "AND date_trunc('month', created_at) = date_trunc('month', now())"
            )
        )
        return float(val or 0)
    except Exception:
        return 0.0


# CP9.4: admin stats 缓存（30s TTL read-through cache）
_admin_stats_cache: dict = {}
_admin_stats_expires: dict = {}


def _get_cached_stats():
    """从内存缓存读 stats，TTL 30s。"""
    now = time.time()
    if "stats" in _admin_stats_cache and _admin_stats_expires.get("stats", 0) > now:
        return _admin_stats_cache["stats"]
    return None


def _set_cached_stats(data: dict):
    """写 stats 到内存缓存，TTL 30s。"""
    _admin_stats_cache["stats"] = data
    _admin_stats_expires["stats"] = time.time() + 30


@router.get("/api/v1/admin/stats")
async def admin_stats(
    user: dict = Depends(require_admin_or_operator), db: AsyncSession = Depends(get_db)
):
    # CP9.4 read-through cache
    cached = _get_cached_stats()
    if cached is not None:
        return cached

    total_users = await db.scalar(select(func.count()).select_from(User))
    total_articles = await db.scalar(select(func.count()).select_from(Article))
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

    # CP3.6-A3 新增字段（不破坏现有结构，仅加字段）
    # failed_distillations_24h：articles 近 24h 失败
    failed_24h = await db.scalar(
        select(func.count())
        .select_from(Article)
        .where(
            Article.status == "failed",
            Article.created_at > (func.now() - timedelta(days=1)),
        )
    )
    # active_audio_files：distilled_articles done 且有 audio_url（映射 audio_files ready）
    active_audio = await db.scalar(
        select(func.count())
        .select_from(DistilledArticle)
        .where(
            DistilledArticle.status == "done",
            DistilledArticle.audio_url.isnot(None),
        )
    )
    # revenue：orders 本月已支付（表可能缺失 → 0）
    revenue = await _safe_revenue(db)

    result = {
        "total_users": total_users or 0,
        "total_articles": total_articles or 0,
        "pending": pending or 0,
        "listened": listened or 0,
        "revenue": revenue,
        "active_audio_files": active_audio or 0,
        "failed_distillations_24h": failed_24h or 0,
    }
    _set_cached_stats(result)
    return result


# CP11.0.3 蒸馏 P95 metrics（从 ai-service /metrics 解析）
_DISTILL_P95_CACHE: dict[str, float] = {}
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

    格式：`distill_step_duration_seconds_bucket{step="step1_structure",le="..."} N`
    """
    result = {"by_step": {}, "overall": {"p50": None, "p95": None, "p99": None}}
    # 按 step 分组 bucket
    buckets_by_step: dict[str, list[tuple[float, float]]] = {}
    for m in re.finditer(
        r"distill_step_duration_seconds_bucket\{([^}]+)\}\s+([0-9.e+-]+)",
        metrics_text,
    ):
        label_block = m.group(1)
        cnt = float(m.group(2))
        le_m = re.search(r'le="([^"]+)"', label_block)
        step_m = re.search(r'step="([^"]+)"', label_block)
        if not le_m or not step_m:
            continue
        le = le_m.group(1)
        step = step_m.group(1)
        if le == "+Inf":
            le = 1e18
        else:
            le = float(le)
        buckets_by_step.setdefault(step, []).append((le, cnt))

    # 计算每个 step 的 P50/P95/P99（用线性插值近似）
    for step, buckets in buckets_by_step.items():
        buckets.sort(key=lambda x: x[0])
        total = buckets[-1][1] if buckets else 0
        if total <= 0:
            continue
        p = {}
        for q, label in [(0.5, "p50"), (0.95, "p95"), (0.99, "p99")]:
            target = total * q
            prev_le, prev_cnt = 0.0, 0.0
            for le, cnt in buckets:
                if cnt >= target:
                    # 线性插值
                    if cnt == prev_cnt:
                        p[label] = le
                    else:
                        ratio = (target - prev_cnt) / (cnt - prev_cnt)
                        p[label] = prev_le + ratio * (le - prev_le)
                    break
                prev_le, prev_cnt = le, cnt
            else:
                p[label] = buckets[-1][0]
        result["by_step"][step] = p

    # overall: 跨 step 累加 P95 的简单平均（足够看趋势）
    if result["by_step"]:
        for q in ("p50", "p95", "p99"):
            vals = [s.get(q) for s in result["by_step"].values() if s.get(q) is not None]
            if vals:
                result["overall"][q] = sum(vals) / len(vals)
    return result


@router.get("/api/v1/admin/distill-p95")
async def admin_distill_p95(
    user: dict = Depends(require_admin_or_operator),
):
    """蒸馏 P50/P95/P99 耗时（秒），从 ai-service Prometheus metrics 解析。

    用于 admin-web Dashboard 显示蒸馏性能。
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
            "overall": {"p50": None, "p95": None, "p99": None},
            "error": str(exc),
        }
