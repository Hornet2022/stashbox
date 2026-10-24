"""
content-service（端口 8102） - 文章 CRUD + 待听/听过/收藏/跳过 + 标签 + D9 回调。

CP1.5：全部走真实 PostgreSQL（articles 表）。
数据隔离：文章按 user_id 归属，非 owner 访问详情/操作返回 403。
软删除：删除走 updated deleted_at（本服务不直接删除，CP1.6 再加）。

CP1.7：D9 端到端 —— 不要求登录态 → 建文章 → 自动触发 ai-service 蒸馏 →
客户端轮询 status / audio-url 拿音频。

⚠️ P2-1 文件规模警告（2026-09-21 走查）：

  本文件 2388 行单文件，是项目里最大的 hot-spot。结构组成：
    line  92-224   helpers (_uid / _new_article_id / _to_response / _validate_url / ...)
    line  263-606  _create_article + add/list/get/mark-listened/retry（核心用户路径）
    line  608-959  favorites / later-listens / snooze / skip
    line  959-1572 d9 callback / clawbot / tags / admin-stats（admin 段之前）
    line 1572-2388 admin 段（12 端点 + 9 helper）   ← P2-1 建议优先拆这 820 行

  拆分路线（建议作为独立 CP，不建议混在 P0/P1 修复集里）：
    admin_router.py        ← 拆 1572-2388（admin 端点 + helpers）
    favorites_router.py    ← 拆 608-959
    articles_router.py     ← 拆 263-606（含 _create_article 等）
    d9_router.py           ← 拆 959-1572（d9/clawbot/tags/admin-stats 部分）

  拆分时每个新 router 用 APIRouter() 声明，main.py 用 include_router 装上；
  app = FastAPI(...) 仍保留在 main.py（lifespan / middleware / CORS 集中管理）。
"""

import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

# content-service 目录名带连字符，不能当包导入，故把自身目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # noqa: E402

from clients.ai_client import get_ai_client  # noqa: E402
from fetchers import (  # noqa: E402
    FetcherError,
    FetcherErrorCode,
    fetch_error_user_message,
    get_fetcher,
    map_fetcher_error,
)
from fetchers.document import (  # noqa: E402
    DOCUMENT_SOURCE,
    MAX_DOCUMENT_BYTES,
    parse_document,
)
from schemas import (  # noqa: E402
    AddArticleRequest,
    AdminArticleRequest,
    ArticleResponse,
    ArticleStatusResponse,
    AudioUrlResponse,
    ClawBotMessageRequest,
    D9AddRequest,
    D9AddResponse,
    TTSVoiceBrief,
    WechatMpMessageRequest,
)

from stashbox.backend.common import cache_service, quota_service
from stashbox.backend.common.distill_stage import get_stage
from stashbox.backend.common.config import settings
from stashbox.backend.common.tts_voice_service import get_voice_briefs
from stashbox.backend.common.auth import create_access_token, require_user, require_user_optional
from stashbox.backend.common.auth_admin import require_admin_or_operator
from stashbox.backend.common.callback_auth import require_callback_secret
from stashbox.backend.common.database import AsyncSessionLocal, get_db
from stashbox.backend.common.exceptions import (
    BizException,
    Forbidden,
    NotFound,
    register_exception_handlers,
)
from stashbox.backend.common.logging import get_logger, setup_logging
from stashbox.backend.common.middleware import RequestIDMiddleware
from stashbox.backend.common.models import (
    AdminOperationLog,
    Article,
    DistilledArticle,
    Favorite,
    Feedback,
    FeedbackV2,
    LaterListen,
    ListeningStatus,
    Tag,
    TagSubscription,
    User,
)
from stashbox.backend.common.observability import install_health_endpoints
from stashbox.backend.common.analytics import track, track_simple
from stashbox.backend.common.events import EventName


def _uid(user: dict) -> int:
    """§11.15 / CP7.x bugfix: require_user 返回的 payload 没有 id 字段，
    统一从 sub 解析，且防御性 int()"""
    try:
        return int(user["sub"])
    except (KeyError, ValueError, TypeError):
        # 兜底：tag 端点 §11.15 用 user["id"] 会 KeyError
        raise HTTPException(status_code=401, detail="invalid token: missing sub")


setup_logging("content-service")
log = get_logger(__name__)
app = FastAPI(title="stashbox-content-service", version="0.3.0")
register_exception_handlers(app)
app.add_middleware(RequestIDMiddleware)
install_health_endpoints(app)
# CORS（CP7.3.5）：content-service 此前没装 CORS 中间件，浏览器直连被拦，admin-web
# 只能用 vite proxy 绕过 —— 生产部署没有 proxy，这里必须后端真支持。
# 必须最后 add：FastAPI 中间件倒序执行（最后 add 最先 run = 最外层），
# 这样 OPTIONS 预检在最外层就被吃掉，不会落到下游路由匹配。origin 白名单走
# settings.cors_origins（env: CORS_ORIGINS，逗号分隔），不硬编码到代码里。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# P2-1 拆分：12 个 admin 端点（v1 §3.6 + CP7.x llm/test）整体抽到 admin_router.py，
# 用 APIRouter() 注入。include_router 不带 prefix —— admin_router 路径已是完整路径
# （/api/v1/admin/*），保留前缀由 router 内的端点负责。后续 CP 推进 favorites /
# articles / d9 三个 router 拆分时，遵循同样的 include_router 模式。
#
# 为什么按文件路径加载而不是 `from admin_router import router`
# -------------------------------------------------------
# ai-service 和 content-service 各有一个同名 `admin_router.py`，且两边 main
# 都是顶层 import。顶层模块名共享 sys.modules，谁先加载谁占住名字 —— 于是
# 同时跑 tests/ai + tests/gateway 时，gateway 的 conftest 先加载了
# content-service，ai-service/main.py 就会把 **content-service 的 admin 路由**
# 挂进自己的 app（表现为 /api/v1/admin/ab-report 404，鉴权断言也数错）。
# 显式指定唯一模块名把这个共享状态彻底消除。
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "content_service_admin_router", Path(__file__).resolve().parent / "admin_router.py"
)
_mod = _ilu.module_from_spec(_spec)
sys.modules["content_service_admin_router"] = _mod
_spec.loader.exec_module(_mod)
admin_router = _mod.router

# 暴露模块本身：单测要 patch 它命名空间里的 get_ai_client 等依赖，
# 比在测试里 `sys.modules["<名字>"]` 硬编码取更稳（名字变了测试不会 KeyError）。
admin_router_module = _mod

app.include_router(admin_router)


# CP-TTS-VOICE：音色库 + 用户音色/语速偏好端点。
# 同样按文件路径加载（不复用 import），理由见上面 admin_router 那段注释 ——
# 多个服务都有同名模块，直接 import 会撞名。
_spec_tts_voice = _ilu.spec_from_file_location(
    "content_service_tts_voice_router", Path(__file__).resolve().parent / "tts_voice_router.py"
)
_mod_tts_voice = _ilu.module_from_spec(_spec_tts_voice)
sys.modules["content_service_tts_voice_router"] = _mod_tts_voice
_spec_tts_voice.loader.exec_module(_mod_tts_voice)
app.include_router(_mod_tts_voice.router)


class InvalidRequest(BizException):
    """参数 / 身份类错误（HTTP 400，业务码按场景传）。"""

    http_status = 400


ANONYMOUS_USER_ID = 0  # 匿名文章归属（v1 §4.3.1 无 device_id 列，本期用 user_id=0 标记）

#: 调用方**尚未确定真实来源**时用的占位 source。
#:
#: 这些码在 fetcher 跑完之后会被替换成真实来源（github / wechat / pdf / …）——
#: 留着占位码的话，列表里就会出现「用户剪的是 GitHub 仓库，来源显示通用」。
#:
#: · `web`  —— 用户侧剪藏入口的默认值（`AddArticleRequest.source` 默认 "web"）；
#: · `url`  —— admin 侧录入链接的默认值（admin-web 发 `source: 'url'`）。
#:
#: ⚠️ 只放**占位码**，不放真实来源码：wechat/wechat_mp/douyin/pdf 这些是
#: 调用方**已经知道**来源的路径，不能被 fetcher 的判断改写。
_GENERIC_SOURCES = frozenset({"web", "url"})
ANONYMOUS_OPEN_ID = "__anonymous__"
AUDIO_URL_TTL_SEC = 3600
OSS_AUDIO_BASE = "https://stashbox-audio.oss-cn-hangzhou.aliyuncs.com"


def _new_article_id() -> str:
    return f"art_{uuid.uuid4().hex[:24]}"


async def _tts_voice_brief(task: DistilledArticle | None) -> TTSVoiceBrief | None:
    """把蒸馏产物的 `tts_voice_id` 翻成「谁念的」（CP-TTS-VOICE 溯源）。

    走 `get_voice_briefs`（不过滤软删）：管理员下架/删除音色后，**历史音频的
    署名仍要留着**。用 `get_voice`（过滤软删）的话，删完音色所有老文章的来源
    会一起变 null，用户看着自己明明听过的音色凭空消失。

    查不到（id 悬空）也回 None —— 与「迁移前历史文章无从得知」同义，不编造。
    """
    if task is None or not task.tts_voice_id:
        return None
    brief = (await get_voice_briefs([task.tts_voice_id])).get(task.tts_voice_id)
    if brief is None:
        return None
    return TTSVoiceBrief(id=brief.id, name=brief.name, available=brief.available)


def _to_response(
    a: Article,
    task: DistilledArticle | None = None,
    *,
    full_script: bool = False,
    tts_voice: TTSVoiceBrief | None = None,
    stage: str | None = None,
) -> ArticleResponse:
    """CP9.x dev 兜底：本地模式（STORAGE_PROVIDER=local）下 audio_url 用
    PUBLIC_GATEWAY_URL 拼，避免真机客户端连到 localhost（设备本机）。

    生产模式（OSS / 公网）：保持 task.audio_url 原值（已是 OSS 公网/签名 URL）。
    """
    audio_url = task.audio_url if task else None
    if (settings.storage_provider == "local" or settings.enable_local_audio_mount) and audio_url:
        if "localhost" in audio_url or "127.0.0.1" in audio_url or audio_url.startswith("/"):
            from urllib.parse import urlparse

            path = urlparse(audio_url).path
            audio_url = f"{settings.public_gateway_url.rstrip('/')}{path}"
        # CP9.x：扫描磁盘实际存在的扩展名，避免后缀不匹配（见 _resolve_actual_audio_extension）
        audio_url = _resolve_actual_audio_extension(audio_url, settings.local_audio_dir)
    return ArticleResponse(
        id=a.id,
        url=a.url,
        source=a.source,
        title=a.title,
        owner_id=str(a.user_id),
        status=_derive_status(a, task),
        favorite=a.favorite,
        skip=a.skip,
        stage=stage,
        created_at=a.created_at.isoformat() if a.created_at else "",
        audio_url=audio_url,
        task_id=task.id if task else None,
        duration_sec=task.duration_sec if task else None,
        # CP-TIME：蒸馏完成时间（distilled_articles.updated_at 在 done/failed 步骤写入，
        # 客户端用此字段在详情页展示「蒸馏完成于 X 分钟前」）
        distilled_at=task.updated_at.isoformat() if task and task.updated_at else None,
        # CP-TIME：articles.updated_at —— 蒸馏中心失败列表展示「失败于」使用
        updated_at=a.updated_at.isoformat() if a.updated_at else None,
        # CP-TAG-FILTER：透出蒸馏 LLM 自动生成的标签列表（pending 时 None）
        tags=list(task.tags) if task and task.tags else None,
        # CP-DISTILL-TEXT：详情页透出听感改写稿全文；列表页用摘要前缀避免响应膨胀
        script_text=_script_text_for(task, list_mode=not full_script),
        # 2026-10-23：三类再创作内容的分区（关联思考/延伸判断/拓展方向）。
        # 同样**只在详情页填** —— 分区内容有字数，列表每行挂一份会让响应膨胀。
        content_parts=(task.content_parts if task and task.content_parts else None)
        if full_script
        else None,
        # CP-TTS-VOICE 溯源：只有详情页调用方才解析（列表恒 None，见 schema 注释）
        tts_voice=tts_voice,
    )


def _script_text_for(task: DistilledArticle | None, *, list_mode: bool) -> str | None:
    """CP-DISTILL-TEXT：详情返回全文；列表返回前 120 字摘要（卡片副标题用）。"""
    if task is None or not task.script_text:
        return None
    text = task.script_text
    if list_mode:
        head = text.strip().replace("\n", " ")
        return head[:120] + ("…" if len(head) > 120 else "")
    return text


async def _get_owned(article_id: str, user_id: int, db: AsyncSession) -> Article:
    result = await db.execute(select(Article).where(Article.id == article_id))
    art = result.scalar_one_or_none()
    if art is None:
        raise NotFound(message=f"article {article_id} not found")
    if art.user_id != user_id:
        raise Forbidden(message="not the owner of this article")
    return art


# ---------------------------------------------------------------------------
# 订阅授权（2026-10-24 产品拍板：运营录入的内容靠订阅分发）
# ---------------------------------------------------------------------------
#
# 产品决策：「admin 录入的内容……应该在用户 app 的订阅功能中形成闭环，
# **订阅了分类就可以看到，没订阅就看不到**」。
#
# ## 为什么需要这一层
#
# 订阅流（`GET /api/v1/subscriptions/feed`）与订阅推送**本来就不按 owner 过滤**
# —— 它们按**分类**分发，所以运营录入的内容（归属 `user_id=0`）天然会出现在
# 订阅者的流里。
#
# 但**读侧端点只认属主**：详情 / 音频 / 进度 / 听完 / 评分 全走
# `_get_owned`，非属主一律 403 ⇒ **闭环断在最后一步**：
# 订阅流里明明列出了这篇文章，用户点进去却是「无权访问」、音频也拿不到。
#
# ## 授权规则（三个条件都要满足，缺一不可）
#
# 1. 文章**已就绪**（`distilled_articles.status == 'done'` 且有 audio_url）
#    —— 没蒸馏完的没有可听的东西，也不该出现在订阅流里；
# 2. 文章的分类（`distilled_articles.tags`）非空；
# 3. **我确实订阅了**其中某个分类（`tag_subscriptions`）。
#
# ⇒ 这条规则与订阅流的筛选条件**同源**：流里能看到的，这里就能打开；
#    流里看不到的，这里也打不开（403）。不会出现「列表里有、点开没权限」。
#
# ⚠️ **只放行「消费」类操作**：读稿、听音频、记自己的收听进度/评分/收藏。
#    **删除、重试蒸馏、稍后读**这些是**属主的私有操作**，不给订阅者
#    （否则订阅者能删掉别人的文章）。
async def _subscription_grants_access(art: Article, user_id: int, db: AsyncSession) -> bool:
    """订阅者是否可以消费这篇文章（读 / 听 / 记录自己的状态）。"""
    da = await db.scalar(select(DistilledArticle).where(DistilledArticle.article_id == art.id))
    if da is None or da.status != "done" or not da.audio_url:
        return False
    art_tags = da.tags if isinstance(da.tags, list) else []
    if not art_tags:
        return False
    rows = await db.execute(
        select(Tag.name)
        .join(TagSubscription, TagSubscription.tag_id == Tag.id)
        .where(TagSubscription.user_id == user_id)
    )
    subscribed = {r[0] for r in rows.fetchall()}
    return bool(subscribed & set(art_tags))


async def _get_readable(article_id: str, user_id: int, db: AsyncSession) -> Article:
    """读侧：属主**或订阅者**。

    与 [_get_owned] 的唯一区别：非属主时先问「订阅授权」，
    不通过才 403。属主路径的行为**一个字没变**。
    """
    result = await db.execute(select(Article).where(Article.id == article_id))
    art = result.scalar_one_or_none()
    if art is None:
        raise NotFound(message=f"article {article_id} not found")
    if art.user_id != user_id and not await _subscription_grants_access(art, user_id, db):
        raise Forbidden(message="not the owner of this article")
    return art


async def _get_owned_with_task(
    article_id: str, user_id: int, db: AsyncSession
) -> tuple[Article, DistilledArticle | None]:
    """articles LEFT JOIN distilled_articles（pending 时还没有蒸馏任务）。"""
    row = (
        await db.execute(
            select(Article, DistilledArticle)
            .outerjoin(DistilledArticle, DistilledArticle.article_id == Article.id)
            .where(Article.id == article_id)
        )
    ).first()
    if row is None:
        raise NotFound(message=f"article {article_id} not found")
    art, task = row
    if art.user_id != user_id:
        raise Forbidden(message="not the owner of this article")
    return art, task


async def _get_readable_with_task(
    article_id: str, user_id: int, db: AsyncSession
) -> tuple[Article, DistilledArticle | None]:
    """读侧（属主**或订阅者**）：见 [_get_readable] 的说明。"""
    row = (
        await db.execute(
            select(Article, DistilledArticle)
            .outerjoin(DistilledArticle, DistilledArticle.article_id == Article.id)
            .where(Article.id == article_id)
        )
    ).first()
    if row is None:
        raise NotFound(message=f"article {article_id} not found")
    art, task = row
    if art.user_id != user_id and not await _subscription_grants_access(art, user_id, db):
        raise Forbidden(message="not the owner of this article")
    return art, task


def _derive_status(art: Article, task: DistilledArticle | None) -> str:
    """ai-service 只更新 distilled_articles，articles.status 会停在 distilling，故按任务派生。

    ⚠️ 2026-10-21：`task.status == 'failed'` 但 `articles.status == 'distilling'`
    时**不是终态** —— 那是「这次尝试失败了，但 Arq 还会自动重试」的中间态。

    背景：内测实测一篇稿子 LLM 返回空 content → 首次尝试失败 → 产物表先被写成
    failed → 15 秒后 Arq 重试**成功**（1052 字稿 / 250 秒音频 / 语速 4.21 正常）。
    用户截到的正是中间态那张「失败 / 换个源试试？」的页面。

    所以这里以 **articles.status 为准**判「还在进行」：蒸馏任务每次执行
    （含 Arq 重试）都会把 articles.status 置回 distilling（见 distill_task），
    真正失败时才会置成 failed。两者不一致 → 以 articles 为准。

    另：中间态**不**把产物表写成 'running'/'queued' —— 那两个值在
    IN_FLIGHT_DISTILL_STATUSES 里，会挡住用户手动重试，且 Arq 若没真正重试
    就永久卡住（用户既不能重试也不能重生成）。
    """
    if task is None:
        return art.status
    if task.status == "done":
        return "ready"
    if task.status == "failed":
        # ⚠️ `articles.status` 不是终态 → 这次失败**还在重试**，不是最终失败。
        #
        # 两种「还在重试」：
        #  ① Arq 自动重试：distill_task 把 articles.status 置回 'distilling'
        #     （内测实测：一篇稿子 LLM 返回空 content → 首次失败 → 15 秒后
        #      Arq 重试**成功**，用户截到的正是中间态那张「失败」页）
        #  ② 用户手动点「重试蒸馏」：user_retry_distill 把 articles.status
        #     置回 'pending'，但产物表要等 ai-service 回写新任务才更新 ——
        #     在那之前 _derive_status 会继续返回 'failed'，用户点了「重试」
        #     却还看到「失败」，像是按钮没生效（这是**既有行为**，之前没人当 bug）。
        #
        # 所以：**articles 不是终态就是「进行中」**，产物表的 failed 只是
        # 「上一次尝试的结果」。
        return art.status if art.status in ("pending", "distilling") else "failed"
    return art.status


def _validate_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise InvalidRequest(message=f"unsupported url scheme: {url}", code=2001)


# 公众号文本里的 URL：纯文本或 <a href> 包裹，够用即可（不引 lxml / bs4）
_URL_RE = re.compile(r'https?://[^\s<>"\'`]+')
# 消息里 URL 常紧跟中文/英文标点（"看这个 https://x.com/a。"），末尾要 trim
_TRAILING_PUNCT = "。，、；：！？）】》」』…“”‘’.,;:!?)\"'"


def _extract_url(text: str) -> str | None:
    """从公众号文本里抽第一条 URL（末尾标点 trim 掉），没有返回 None。"""
    match = _URL_RE.search(text or "")
    if match is None:
        return None
    url = match.group(0).rstrip(_TRAILING_PUNCT)
    return url or None


def _is_valid_url(url: str) -> bool:
    """URL 合法性：scheme 必须 http/https（顺带挡掉 javascript: 这类 XSS）+ 有 host。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


async def _ensure_anonymous_user(db: AsyncSession) -> None:
    """匿名哨兵用户（id=0）：articles.user_id 有 FK，匿名文章落库前必须存在该行。

    ## 为什么先查再插

    这个函数以前是**无条件** INSERT + COMMIT。两个副作用：

    1. 多一次写往返（哪怕行早就存在）；
    2. **无条件 COMMIT 会把调用方 session 里尚未落库的东西一起提交掉** ——
       而 `_create_article` 现在也会调它（见下），多一次中途 commit
       会让「谁负责提交」的边界变得含糊。

    改成「先查，存在就直接返回」：绝大多数调用是「哨兵早就建好了」，
    这时一次 SELECT 就结束，不碰事务边界。

    ## 为什么 `_create_article` 也要调它（2026-10-23 生产实测）

    生产上 `articles_user_id_fkey` 拦掉了一批 admin 录入：
    **users 表里从来没有过 id=0 这一行**（哨兵是按需建的，匿名剪藏才会建），
    而 admin 端点用 `ANONYMOUS_USER_ID` 归属却没先确保哨兵存在。

    ⇒ 修在**收口处**（`_create_article`）而不是逐个端点打补丁：
    凡是往 articles 写 `user_id=0` 的路径（匿名剪藏 / wechat_mp /
    admin 录入链接 / admin 录入文档）都自动安全，不会再出现
    「新加一个端点忘了建哨兵 → 线上必现外键错误」。
    """
    exists = await db.scalar(select(User.id).where(User.id == ANONYMOUS_USER_ID))
    if exists is not None:
        return
    await db.execute(
        insert(User)
        .values(
            id=ANONYMOUS_USER_ID,
            open_id=ANONYMOUS_OPEN_ID,
            nickname="anonymous",
            tier="free",
            monthly_quota=0,
        )
        .on_conflict_do_nothing()
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "content-service"}


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _to_raw_content(result) -> dict:
    """FetchResult → JSONB-ready dict。

    asdict 只重建 dict/list/tuple，datetime 会原样保留（不会自动转 ISO 字符串），
    直接写 JSONB 会在序列化时炸，故再走一次 json 归一化。
    """
    return json.loads(json.dumps(asdict(result), default=_json_default))


#: 剪藏抓取的总时限预算（秒），覆盖「HTTP 退避重试 + 无头浏览器兜底」整条降级链。
#:
#: 30s 的取法：Tier 1 单次 10s（沿用改造前的值）+ 退避 + Tier 2 浏览器 12s 上限
#: （见 fetchers/pipeline.BROWSER_TIER_CAP）+ 余量。够跑完两级通道，又不至于
#: 让用户对着转圈等到以为应用挂了。可用环境变量覆盖（云端网络差时调大）。
_ENV_CAPTURE_FETCH_BUDGET = "STASHBOX_CAPTURE_FETCH_BUDGET"
_CAPTURE_FETCH_BUDGET_DEFAULT = 30.0


def _capture_fetch_budget() -> float:
    try:
        return max(
            5.0,
            float(os.environ.get(_ENV_CAPTURE_FETCH_BUDGET) or "") or _CAPTURE_FETCH_BUDGET_DEFAULT,
        )
    except ValueError:
        return _CAPTURE_FETCH_BUDGET_DEFAULT


async def _prefetch_for_capture(url: str):
    """剪藏前置抓取：拿到正文才允许建文章。

    2026-10-03 改。**为什么改成硬失败**：

    原来抓取失败被 `_create_article` 里的 `except FetcherError` 整个吞掉，
    文章照样建成 `status=pending` + `raw_content=None`，蒸馏任务照派。
    到了 ai-service，`_load_raw_content` 拿不到正文就返回字面量
    `"[empty article] title=… url=…"`，**这段占位符被当成正文喂给了 LLM**。

    于是一次抓取失败会连锁放大成：
      扣掉 1 次配额 → 派一个注定失败的蒸馏任务 → 白烧 LLM token →
      用户等 12 分钟看到失败，而剪藏接口当时返回的是 200。

    剪藏成功率是听匣的命门，失败必须**当场**、**带可操作原因**返回给用户，
    不能变成一个 12 分钟后才知道结果的黑洞。服务号回调（wechat_mp）
    早就是这个行为，其余入口只是漏改了。

    本函数是纯读操作：不写库、不扣配额、不派任务，失败只抛异常。
    """
    fetcher = get_fetcher(url)
    if fetcher is None:
        # GenericURLFetcher 是 catch-all，走到这里说明 URL 连通用兜底都匹配不上
        # （如 scheme 不对）。原来这里直接 return，文章建成无正文 —— 同样必炸。
        raise BizException(
            code=2001, message=fetch_error_user_message(FetcherErrorCode.UNSUPPORTED)
        )
    try:
        # budget = **整条降级链**的总时限（HTTP 重试 + 无头浏览器兜底共用）。
        #
        # 为什么必须显式给：抓取现在是多通道降级（见 fetchers/pipeline.py），
        # 每级都有自己的超时，串起来最坏能到分钟级。而剪藏是同步接口，用户盯着
        # 一个转圈等 —— 30 秒还没回来，他已经认定应用坏了。所以总时限是硬约束，
        # 由这一层（HTTP 边界）定，而不是让 fetcher 自己随便串。
        return await fetcher.fetch(url, timeout=10.0, budget=_capture_fetch_budget())
    except FetcherError as exc:
        log.info(
            f"capture_fetch_fail: url={url} code={exc.code.value} "
            f"source={exc.source} msg={exc.message}"
        )
        raise map_fetcher_error(exc) from exc
    except Exception as exc:
        # 超时 / SSL / 解析这类连 FetcherError 都没包的意外，同样不能软降级。
        log.warning(f"capture_fetch_unexpected: url={url} err={type(exc).__name__}: {exc}")
        raise BizException(
            code=2002,
            message=fetch_error_user_message(FetcherErrorCode.NETWORK),
            http_status=502,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


async def _create_article(
    url: str,
    user_id: int,
    source: str,
    title: str | None,
    db: AsyncSession,
    raw_content: dict | None = None,  # 默认为 None：其他调用点行为不变
    event: EventName | None = None,  # 建库后要打的埋点（必须落在 commit 之前）
    *,
    fetch_on_create: bool = True,  # True=建库前调 fetcher 抓 title/source/raw_content
    dedup: bool = True,  # True=同用户同 URL 已有未删文章则复用（CP-DUPLICATE-CLIP）
    manual_tags: list[str] | None = None,  # 运营指定的分类（0040；None=用 LLM 猜的）
) -> Article:
    # 2026-10-23：往 articles 写 `user_id=0` 之前，哨兵用户必须存在 ——
    # articles.user_id 有 FK 到 users(id)，而哨兵（id=0）是**按需**建的
    # （只有匿名剪藏路径会建）。生产上 users 表从来没有过 id=0 这一行，
    # 于是 admin 录入端点必现 `articles_user_id_fkey` 错误。
    #
    # 修在这里（**收口处**）而不是逐个端点打补丁：凡是往 articles 写
    # `user_id=0` 的路径都自动安全，不会再有「新端点忘了建哨兵」。
    if user_id == ANONYMOUS_USER_ID:
        await _ensure_anonymous_user(db)

    # CP11.0.7 P1.1：建库前同步抓一下页面，拿到 title/source/raw_content。
    #
    # 2026-10-03：失败不再软降级，改为抛错（见 _prefetch_for_capture 的注释）。
    # 抓不到正文就没有可蒸馏的东西，硬失败比"建一条注定失败的记录"诚实得多，
    # 也把用户等待 12 分钟才知道结果的时间差抹掉了。
    if fetch_on_create and (title is None or source in _GENERIC_SOURCES or raw_content is None):
        fr = await _prefetch_for_capture(url)
        if title is None and fr.title:
            title = fr.title
        if source in _GENERIC_SOURCES and fr.source and fr.source != "unknown":
            source = fr.source
        if raw_content is None:
            # 必须走 _to_raw_content：asdict 不转换 datetime（publish_time），
            # 裸 asdict 写 JSONB 会在 INSERT 时抛 "datetime is not JSON serializable"
            # → flush 失败污染 session → commit/refresh 连环 InvalidRequestError → 500
            # （真机「剪藏文档链接→服务暂不可用」的根因，2026-09-23）。
            raw_content = _to_raw_content(fr)
        log.info(
            "fetch_ok on add",
            extra={"url": url, "fetcher": fr.source, "title_len": len(fr.title or "")},
        )

    # CP-DUPLICATE-CLIP：同一用户重复剪藏同一 URL → 复用已有文章，不再建新行。
    #
    # articles 表对 (user_id, url) 没有任何唯一约束，所以 D9 / submit_article
    # 重复提交同一链接会一路建到底：同一篇文章出现两条、配额扣两次。
    # 实测两次 POST 同一 URL 拿到两个不同 article_id，quota_used 0 → 2。
    #
    # 口径：只查同一用户 + 未删除。不同用户剪藏同一链接各自拥有一份（属主隔离 +
    # 配额按用户计），这是产品语义不是 bug；已软删的允许重新剪藏。
    #
    # dedup=False 的调用点（wechat_mp handler 那类由上游保证唯一性的）行为不变。
    art = None
    if dedup:
        art = await db.scalar(
            select(Article)
            .where(
                Article.user_id == user_id,
                Article.url == url,
                Article.deleted_at.is_(None),
            )
            .order_by(Article.created_at.desc())
            .limit(1)
        )
    if art is not None:
        log.info(
            "duplicate_clip_reuse",
            extra={"url": url, "user_id": user_id, "article_id": art.id},
        )
        # 复用时**若这次指定了分类，就更新它**（0040）。
        #
        # 为什么不能静默丢掉：运营的分类是**分发依据**（订阅了分类才看得到），
        # 丢掉它 = 运营明明选了「财经」，内容却还挂在「科技」下 ——
        # 而运营看到的是「创建成功」，**没有任何报错**。
        # 这正是这一轮在消灭的那类静默失败。
        #
        # ⚠️ 但**已经蒸馏完**的稿子，其 `distilled_articles.tags` 不会跟着变 ——
        # 那要等**重新蒸馏**才会生效。所以这里记一条 warning 把这件事说出来，
        # 而不是让运营以为改了分类就立刻生效了。
        if manual_tags:
            new_tags = [str(t).strip() for t in manual_tags if str(t).strip()]
            if new_tags and new_tags != (art.manual_tags or []):
                art.manual_tags = new_tags
                await db.commit()
                if art.status in ("ready", "listened"):
                    log.warning(
                        "duplicate_clip_manual_tags_updated article=%s tags=%s "
                        "(已发布的稿子需要**重新蒸馏**才会换分类 —— "
                        "distilled_articles.tags 是蒸馏时写的)",
                        art.id,
                        new_tags,
                    )
                else:
                    log.info(
                        "duplicate_clip_manual_tags_updated article=%s tags=%s",
                        art.id,
                        new_tags,
                    )
        return art

    art = Article(
        id=_new_article_id(),
        user_id=user_id,
        url=url,
        source=source,
        title=title,
        status="pending",
        raw_content=raw_content,  # FetchResult 全字段（JSONB，ai-service 蒸馏输入）
        # 运营指定的分类（0040）。
        manual_tags=(list(manual_tags) if manual_tags else None),
        favorite=False,
        skip=False,
    )
    db.add(art)
    if event is not None:
        # track() 只 flush 不 commit，get_db() 收尾只 session.close() —— close() 隐式
        # rollback 会丢掉 flush 出来的 feedback 行，所以埋点必须写在 commit() 之前
        # （与 7ba3221 / 4ab6b4f 同一模式）。
        try:
            await track(db, event, user_id=user_id, article_id=art.id)
        except Exception as exc:
            log.warning(f"{event} 埋点异常（忽略）: article={art.id} err={exc}")
    await db.commit()
    await db.refresh(art)
    await cache_service.invalidate_pending(user_id)  # 待听列表缓存失效
    return art


@app.post("/api/v1/articles/add", response_model=ArticleResponse)
async def add_article(
    response: Response,
    req: AddArticleRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """⚠️ Deprecated（CP11.x 走查 P2-2 标记）。

    与 POST /api/v1/articles 行为不一致：
      - 本端点：不扣配额，CP1.4 风格历史行为
      - 新端点：扣配额、cache_service 标记、ARTICLE_SUBMIT 埋点

    为兼容老版本 iOS/Android 客户端暂时保留入口，响应头加 Deprecation 提示客户端迁移。
    下个主版本（CP12+）移除，并同步下掉 api-gateway/config.py:57 路由。
    """
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "CP12"
    response.headers["Link"] = '</api/v1/articles>; rel="successor-version"'
    art = await _create_article(req.url, _uid(user), req.source, None, db)
    await get_ai_client().trigger_distill(art.id, auth_token=create_access_token(str(art.user_id)))
    return _to_response(art)


@app.post("/api/v1/articles")
async def submit_article(
    req: AddArticleRequest, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """提交链接（v1 §3.1）：扣 1 次配额 → 建文章 → 自动派蒸馏 → 配额/待听缓存失效。

    扣减走 quota_service 乐观锁（含 Redis Lua 原子失效），配额用尽抛 3001。

    CP9.x fix：B1 修复 —— submit_article 必须自动调 trigger_distill，否则安卓/管理后台
    剪藏后文章永远 pending，arq 队列空跑。原 `add_article`（deprecated）调了 trigger_distill，
    新端点漏调，现补上。
    """
    uid = _uid(user)
    # CP-DUPLICATE-CLIP：先查重再扣费，重复提交同一链接不重复计费。
    dup = await _find_existing_clip(req.url, uid, db)
    if dup is not None:
        log.info(
            "submit_duplicate_reuse",
            extra={"url": req.url, "user_id": uid, "article_id": dup.id},
        )
        try:
            await get_ai_client().trigger_distill(
                article_id=dup.id, auth_token=create_access_token(str(uid))
            )
        except Exception as exc:
            log.warning(f"auto_distill_trigger_failed (dup): article={dup.id} error={exc}")
        return _to_response(dup)

    # 2026-10-03：**先抓后扣**。
    #
    # 原来是「先扣配额 → 再建库（内部抓取）」，而抓取失败是软降级的，于是
    # 一次网络抖动 = 扣掉 1 次配额 + 建一条无正文的文章 + 派一个注定失败的
    # 蒸馏任务，而接口返回 200。用户什么都没得到，还少了配额。
    #
    # 现在先做一次纯读的抓取：失败直接 2001/2002 返回，既不建库也不计费。
    # 抓到了再扣配额建库 —— 这时建库只差一次 DB insert。
    fr = await _prefetch_for_capture(req.url)

    # 2026-10-23：GitHub 走**专属 fetcher** 时，来源要用**真实来源**。
    #
    # 为什么只改这一个：`articles.source` 落的是**请求体里传的** source
    # （默认 "web"），而真正解析它的是 fetcher（`raw_content["source"]`）。
    # 剪藏一个 GitHub 仓库若存 "web"，列表里就显示「通用」—— 用户分不清
    # 自己剪的是网页还是代码库，admin 的来源筛选也跟着失真。
    #
    # ⚠️ 只对**新增的**专属来源改，不动既有映射（wechat/wechat_mp/douyin/pdf
    # 的既有口径保持不变）—— 那几个的历史数据里两种写法都存在，
    # 现在统一会让老数据的来源显示突变。
    source = "github" if fr.source == "github" else req.source

    # 后半程（去重 → 扣配额 → 建库 → 派蒸馏 → 失效缓存）与文档剪藏**共用一份**
    # 实现，见 _clip_after_fetch 的说明。
    return await _clip_after_fetch(
        url=req.url,
        uid=uid,
        source=source,
        title=req.title or fr.title or None,
        raw_content=_to_raw_content(fr),
        db=db,
    )


async def _read_document_upload(
    file: UploadFile | None,
    text: str | None,
) -> tuple[str, str, bytes]:
    """上传载荷 → (filename, content_type, data)。

    **用户侧与 admin 侧共用同一份判据** —— 这两条路径的行为必须逐字一致：
    同一份内容「用户能传、运营传不了」是个查不到原因的问题（两边各写一份
    判据时，改了一边另一边就漂了）。

    · `file` 与 `text` **二选一**，同时给或都不给都是 400。
    · 文件 ≤ MAX_DOCUMENT_BYTES（与 nginx `client_max_body_size 10m` 同口径）。
    · 粘贴文本按 UTF-8 存成字节，**走与文件完全相同的解析路径**。

    Raises:
        InvalidRequest: 入参不合法 / 超限 / 空文件。
    """
    has_file = file is not None and file.filename is not None
    has_text = text is not None and text.strip() != ""
    if not has_file and not has_text:
        raise InvalidRequest(message="请上传一个文档文件，或直接粘贴文本内容")
    if has_file and has_text:
        raise InvalidRequest(message="文档文件与文本内容二选一，不要同时提交")

    if has_file:
        filename = file.filename or "upload"
        content_type = file.content_type or ""
        data = await file.read()
        if len(data) > MAX_DOCUMENT_BYTES:
            raise InvalidRequest(
                message=(
                    f"文档太大了（{len(data) // 1024 // 1024}MB），"
                    f"上限 {MAX_DOCUMENT_BYTES // 1024 // 1024}MB"
                )
            )
        if len(data) == 0:
            raise InvalidRequest(message="文件是空的")
    else:
        filename = "pasted.md"
        content_type = "text/markdown"
        data = text.encode("utf-8")  # type: ignore[union-attr]
        if len(data) > MAX_DOCUMENT_BYTES:
            raise InvalidRequest(
                message=(
                    f"文本太长了（{len(data) // 1024 // 1024}MB），"
                    f"上限 {MAX_DOCUMENT_BYTES // 1024 // 1024}MB"
                )
            )

    return filename, content_type, data


@app.post("/api/v1/articles/document")
async def submit_document(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    title: str | None = Form(default=None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """**文档剪藏**：直接提交一份文档（而不是链接）。

    ## 为什么单独一个端点而不是给 `POST /api/v1/articles` 加字段

    那条链路的轴是 **URL**（`get_fetcher(url)` → 抓 → 建库）。文档没有 URL，
    硬塞进去就要在链路中间插一个「这次不抓，用上传的正文」的分叉 ——
    而分叉一旦插在链路中间，后面每一环都要记得「这次没有 fetcher 结果」。

    这里改成**在链路之前**分叉：正文从哪来（抓 / 解析）由入口决定，
    之后的每一步（去重 → 扣配额 → 建库 → 派蒸馏 → 失效缓存）完全共用。

    ## 与链接剪藏的语义对齐

    · **先解析后扣费**：解析不出正文 → 2002 返回，**不建库也不计费**
      （与链接剪藏「先抓后扣」同口径，2026-10-03 定的）。
    · **去重**：`url` 用**内容 hash**（`doc://{sha256[:16]}`）→ 同一份文档
      重复上传复用同一篇，不重复扣配额。
    · **正文形状不变**：`raw_content` 仍是 FetchResult 同形
      ⇒ 蒸馏、订阅、TTS 全链路**零改动**。

    ## 参数

    · `file`：文档文件（二进制）。与 `text` **二选一**。
    · `text`：直接粘贴的文本（Markdown / 纯文本）。与 `file` 二选一。
      走这个参数时不需要 multipart 的 file 部分，但请求仍是 multipart
      （`text` 是 Form 字段）—— 客户端实现更简单。

    ## 大小控制

    文件 ≤ [MAX_DOCUMENT_BYTES]（与 nginx 的 `client_max_body_size 10m` 同口径）。
    **端点与网关必须一致**，否则用户看到的是「413」这种没有信息量的报错。
    """
    uid = _uid(user)

    # -- 入参校验与读取（判据与 admin 侧共用一份，见 _read_document_upload）--
    filename, content_type, data = await _read_document_upload(file, text)

    # -- 解析（失败 → 不建库不计费）----------------------------------
    try:
        fr = parse_document(
            filename=filename,
            data=data,
            content_type=content_type,
        )
    except FetcherError as exc:
        # 复用抓取侧的错误映射（业务码 2001/2002 + 按 source 分流的话术）
        raise map_fetcher_error(exc) from exc

    # 内容 hash 当 url → 同一份文档重复上传天然去重
    doc_url = fr.url
    final_title = (title or "").strip() or fr.title

    result = await _clip_after_fetch(
        url=doc_url,
        uid=uid,
        source=DOCUMENT_SOURCE,
        title=final_title,
        raw_content=_to_raw_content(fr),
        db=db,
    )
    result["filename"] = filename
    result["content_type"] = content_type
    result["text_length"] = len(fr.content_text)
    return result


async def _clip_after_fetch(
    *,
    url: str,
    uid: int,
    source: str,
    title: str | None,
    raw_content: dict,
    db: AsyncSession,
) -> dict:
    """**剪藏拿到正文之后**的公共后半程（建库 → 扣配额 → 派蒸馏 → 失效缓存）。

    ## 为什么要抽出来

    链接剪藏（`POST /api/v1/articles`）与文档剪藏（`POST /api/v1/articles/document`）
    的**后半程完全一样**：去重 → 扣配额 → 建库 → 派蒸馏 → 失效缓存。
    差别只在「正文从哪来」。

    这两段一旦各写一份，就会各自漂移 —— 而漂移的方向恰恰是**扣费与幂等**：
    文档路径漏了「先抓后扣」就会变成「解析失败也扣配额」，
    漏了 `mark_article_quota` 就会二次扣费。

    ⚠️ 扣配额在**建库之前**，但**解析在扣配额之前**（见调用方）——
    顺序是「先拿到正文 → 再扣钱 → 再建库」，与链接剪藏同口径
    （2026-10-03 定的：抓不到正文就不建库不计费）。
    """
    # 去重：同 user + 同 url（文档路径的 url 是内容 hash）已有未删文章 → 复用
    dup = await _find_existing_clip(url, uid, db)
    if dup is not None:
        log.info(
            "clip_duplicate_reuse",
            extra={"url": url, "user_id": uid, "article_id": dup.id},
        )
        try:
            await get_ai_client().trigger_distill(
                article_id=dup.id, auth_token=create_access_token(str(uid))
            )
        except Exception as exc:
            log.warning("auto_distill_trigger_failed (dup): article=%s error=%s", dup.id, exc)
        return {
            "article_id": dup.id,
            "url": dup.url,
            "status": dup.status,
            # 复用旧文章：没有新的 task_id（trigger_distill 内部会判断
            # 「已在跑就不再入队」），返回 None 表示「没有新 task_id」而非失败。
            "task_id": None,
            "quota_used": 0,
            "monthly_quota": 0,
            "remaining": 0,
            "reused": True,
        }

    quota = await quota_service.consume(db, uid)  # 用尽抛 QuotaExceededError(3001)
    art = await _create_article(
        url,
        uid,
        source,
        title,
        db,
        raw_content=raw_content,
        event=EventName.ARTICLE_SUBMIT,
        fetch_on_create=False,  # 正文已经拿到了，别再抓一次
    )
    await cache_service.mark_article_quota(art.id)  # 打标：该文章已扣过配额

    # 自动派蒸馏：失败仅 log 不破请求（ai-service 不可达时文章仍 pending，等下次重试）
    trigger_result = None
    try:
        trigger_result = await get_ai_client().trigger_distill(
            article_id=art.id,
            auth_token=create_access_token(str(art.user_id)),
        )
        if trigger_result:
            log.info(
                "auto_distill_triggered",
                extra={"article_id": art.id, "task_id": trigger_result.get("task_id")},
            )
    except Exception as exc:
        log.warning(
            "auto_distill_trigger_failed",
            extra={"article_id": art.id, "error": str(exc)},
        )

    return {
        "article_id": art.id,
        "url": art.url,
        "status": art.status,
        "task_id": (trigger_result or {}).get("task_id"),
        "quota_used": quota["quota_used"],
        "monthly_quota": quota["monthly_quota"],
        "remaining": quota["monthly_quota"] - quota["quota_used"],
        "reused": False,
    }


@app.post("/api/v1/admin/articles")
async def admin_create_article(
    req: AdminArticleRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """运营手动录入文章（admin 全局视图用）。

    为什么必须有这个端点，而不是让后台复用 ``POST /api/v1/articles``：
    那个是**用户侧剪藏**入口，用 admin token 打过去会连带三件运营不想要的事：

      1. ``quota_service.consume(db, uid)`` —— 扣的是**运营账号自己的**配额，
         运营只是录一篇文章，配额却按用户剪藏的价扣；
      2. ``_create_article(url, _uid(user), ...)`` —— 文章 owner 变成运营账号，
         于是这条本该是「全站公共内容」的文章变成运营名下的私有文章，
         在用户侧列表里只有运营自己看得到；
      3. 语义上把「运营造内容」和「用户剪藏」混成同一类数据，后续按 owner 统计全失真。

    本端点：归属记 ``ANONYMOUS_USER_ID(0)``（与匿名剪藏同一约定）、
    **不扣配额**、写 admin 审计日志、照常派蒸馏。
    """
    _validate_url(req.url)
    art = await _create_article(
        req.url,
        ANONYMOUS_USER_ID,
        req.source,
        req.title,
        db,
        event=EventName.ARTICLE_SUBMIT,
        manual_tags=req.tags,
    )
    # 记审计：这条是 admin 写操作，不能只靠 AuditMiddleware（它只挂在网关上，
    # 直连 content-service:8102 绕过网关时不会有记录 —— 见 plist 绑定收紧的说明）。
    db.add(
        AdminOperationLog(
            admin_tier=str(user.get("tier", "admin")),
            action="ADMIN_CREATE_ARTICLE",
            target_type="article",
            target_id=str(art.id),
            reason="admin 手动录入文章",
            method="POST",
            path="/api/v1/admin/articles",
            request_body={"url": req.url, "source": req.source, "title": req.title},
        )
    )
    await db.commit()

    task_id = None
    try:
        trigger_result = await get_ai_client().trigger_distill(
            article_id=art.id,
            auth_token=create_access_token(str(art.user_id)),
        )
        task_id = (trigger_result or {}).get("task_id")
    except Exception as exc:  # 蒸馏派发失败不该让录入回滚
        log.warning(
            "admin_create_article_distill_failed", extra={"article_id": art.id, "error": str(exc)}
        )

    return {
        "article_id": art.id,
        "url": art.url,
        "status": art.status,
        "task_id": task_id,
    }


@app.post("/api/v1/admin/articles/document")
async def admin_create_article_document(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    title: str | None = Form(default=None),
    # 运营指定的分类（multipart 里重复字段：tags=科技&tags=财经）
    tags: list[str] = Form(default_factory=list),
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """**运营侧**录入文档（admin 全局视图用）。

    ## 为什么不复用用户侧的 `POST /api/v1/articles/document`

    那个端点是**用户剪藏**入口，用 admin token 打过去会连带三件运营不想要的事
    （与 `POST /api/v1/admin/articles` 同理由）：扣**运营账号自己的**配额、
    文章 owner 变成运营本人（于是「全站公共内容」变成运营名下的私有文章）、
    语义上把「运营造内容」和「用户剪藏」混成同一类数据。

    本端点：归属记 `ANONYMOUS_USER_ID(0)`、**不扣配额**、写 admin 审计日志、
    照常派蒸馏。

    ## 与用户侧端点的差异只有三处

    · 归属：`ANONYMOUS_USER_ID` 而不是操作者；
    · 配额：不扣；
    · 鉴权：`require_admin_or_operator`。

    **正文解析用的是同一份 `parse_document`** —— 判据（大小上限、空文件、
    正文下限、格式分派）全部共用，避免「用户能传运营传不了」。

    ## 与 admin 链接录入端点的差异

    链接录入 `POST /api/v1/admin/articles` 只收 URL（**运营自己贴链接**），
    那个端点不带正文 —— 抓取发生在建库前（`_create_article` 的
    `fetch_on_create`）。本端点是**运营直接上传正文**，不经过抓取，
    所以 `fetch_on_create=False` + 显式传 `raw_content`。
    """
    filename, content_type, data = await _read_document_upload(file, text)

    # -- 解析（失败 → 不建库）----------------------------------------
    try:
        fr = parse_document(
            filename=filename,
            data=data,
            content_type=content_type,
        )
    except FetcherError as exc:
        raise map_fetcher_error(exc) from exc

    final_title = (title or "").strip() or fr.title
    # 内容 hash 当 url → 同一份文档重复录入复用同一篇（与用户侧同口径）
    art = await _create_article(
        fr.url,
        ANONYMOUS_USER_ID,
        DOCUMENT_SOURCE,
        final_title,
        db,
        raw_content=_to_raw_content(fr),
        manual_tags=tags,
        event=EventName.ARTICLE_SUBMIT,
        fetch_on_create=False,  # 正文已经在上传里了，不该再去抓一次
    )

    db.add(
        AdminOperationLog(
            admin_tier=str(user.get("tier", "admin")),
            action="ADMIN_CREATE_ARTICLE",
            target_type="article",
            target_id=str(art.id),
            reason="admin 手动录入文档",
            method="POST",
            path="/api/v1/admin/articles/document",
            request_body={
                "filename": filename,
                "content_type": content_type,
                "title": final_title,
                "text_length": len(fr.content_text),
            },
        )
    )
    await db.commit()

    task_id = None
    try:
        trigger_result = await get_ai_client().trigger_distill(
            article_id=art.id,
            auth_token=create_access_token(str(art.user_id)),
        )
        task_id = (trigger_result or {}).get("task_id")
    except Exception as exc:  # 蒸馏派发失败不该让录入回滚
        log.warning(
            "admin_create_article_distill_failed", extra={"article_id": art.id, "error": str(exc)}
        )

    return {
        "article_id": art.id,
        "url": art.url,
        "status": art.status,
        "task_id": task_id,
        "filename": filename,
        "content_type": content_type,
        "text_length": len(fr.content_text),
    }


# --- 列表端点共享的两个约束（2026-10 P0）----------------------------------
#
# 1) 不要 select 出 raw_content。
#    articles.raw_content 是抓取后的原始正文 JSONB —— fetchers/parser.py 把
#    content_text 限 50KB / content_html 限 200KB，单行可达 ~250KB。而
#    `_to_response` **一次都不读它**（只用 id/url/source/title/user_id/
#    status/favorite/skip/created_at/updated_at + task 那侧字段）。
#    列表 select 整行 ORM 实体 = 把 250KB × N 从 PG 搬进进程再原样丢掉：
#      - 20 条就是 ~5MB/次请求，纯搬运开销；
#      - pending / listened 两个端点此前**还没有 LIMIT**，行数随用户历史线性
#        增长，老用户一次请求就能把 content-service 内存打爆。
#
#    `defer` 只改 SELECT 的列清单，返回的还是同一个 ORM 对象，_to_response 无需改。
#    ⚠️ 反面风险：将来若给 _to_response 加上读 raw_content 的分支，defer 过的
#    端点会在访问那一刻触发 lazy load → async 上下文抛 MissingGreenlet（500，
#    不是 None）。所以 defer **只能**用在逐个核对过「不读该列」的端点上，
#    不要图省事整仓套上去。
_LISTING_DEFER = defer(Article.raw_content)

# 2) 无分页列表端点的行数上限。
#    取 200 是「足够覆盖任何真实用户的未处理列表」与「即使忘了 defer 也不会
#    打死进程」之间的折中，不是拍脑袋的产品决策 —— 见下面截断时的告警。
_LIST_PAGE_CAP = 200


def _truncate_for_list(rows: list, cap: int, *, endpoint: str, uid: int) -> list:
    """把结果截到 cap，**并在真的截断时打一条 warning**。

    为什么要告警：静默截断是最难查的一类问题 —— 用户少了内容、日志一片干净、
    监控全绿。宁可吵一点。有这条日志，看到「少了几篇」时能立刻确认是截断
    而不是查询出错。
    """
    if len(rows) <= cap:
        return rows
    log.warning(
        "list_truncated",
        endpoint=endpoint,
        user_id=uid,
        returned=cap,
        fetched=len(rows),
        cap=cap,
    )
    return rows[:cap]


@app.get("/api/v1/articles")
async def list_articles(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
    tag: Optional[str] = None,  # CP-TAG-FILTER：按 Tag.slug 过滤，None=全量
):
    """列出当前用户的 articles 列表（按 created_at desc 排序，分页）。

    admin-web Articles 页调用此端点。
    ?tag=xxx 时只列已蒸馏过且 tags 包含该 slug 的文章（pending/distilling 不带 tags）。

    CP-NOTE：articles.deleted_at 在本仓是"装饰字段" —— 删文章走硬删除（行 DELETE），
    deleted_at 永远为 NULL。list 端保留 .is_(None) 过滤是为 alembic/ORM 历史兼容，
    不参与语义。
    """
    uid = _uid(user)

    base_filter = [Article.user_id == uid, Article.deleted_at.is_(None)]

    if tag:
        # CP-TAG-FILTER：通过 Tag.slug → tag.id 找到匹配项，再 JOIN 蒸馏表按 JSONB 包含筛
        tag_row = await db.scalar(select(Tag).where(Tag.slug == tag))
        if not tag_row:
            # 不存在的 slug → 空集合（不报错，admin-web / 安卓筛选 UI 一致语义）
            return {"items": [], "total": 0, "tag": tag, "tag_id": None}
        tag_id = tag_row.id
        tag_name = tag_row.name
        # 用 EXISTS 子查询过滤 article 仅保留 tags 包含此 tag 的（按中文 name 匹配）
        # —— 蒸馏 LLM 输出的是 name，"科技"中文；slug 用于传参。
        tag_filter_clause = Article.id.in_(
            select(DistilledArticle.article_id).where(
                DistilledArticle.tags.is_not(None),
                DistilledArticle.tags.contains([tag_name]),  # JSONB 包含数组 → 元素匹配
            )
        )
        base_filter.append(tag_filter_clause)

    total = await db.scalar(select(func.count()).select_from(Article).where(*base_filter))

    result = await db.execute(
        select(Article, DistilledArticle)
        .outerjoin(DistilledArticle, DistilledArticle.article_id == Article.id)
        .options(_LISTING_DEFER)  # 见 _LISTING_DEFER：_to_response 不读 raw_content
        .where(*base_filter)
        .order_by(Article.created_at.desc(), Article.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()

    # CP-TAG-FILTER：埋点（admin-web / 安卓 UI 筛选真实发起到 TAG_FILTER）
    if tag and rows:
        try:
            await track(
                db,
                EventName.TAG_FILTER,
                user_id=uid,
                article_id="n/a",  # 过滤事件无单一关联文章
                metadata={"tag_slug": tag, "tag_id": tag_id, "result_count": total or 0},
            )
            await db.commit()
        except Exception as exc:
            log.warning(f"TAG_FILTER 埋点异常（忽略）: slug={tag} err={exc}")

    return {
        "items": [_to_response(art, task).model_dump() for art, task in rows],
        "total": total or 0,
        "tag": tag,
        "tag_id": tag_id if tag and tag_row else None,
    }


@app.get("/api/v1/articles/pending")
async def list_pending(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    uid = _uid(user)
    cached = await cache_service.get_pending(uid)
    if cached is not None:
        return {"articles": cached, "count": len(cached), "cached": True}

    result = await db.execute(
        select(Article, DistilledArticle)
        .outerjoin(DistilledArticle, DistilledArticle.article_id == Article.id)
        .options(_LISTING_DEFER)  # 见 _LISTING_DEFER
        .where(
            Article.user_id == uid,
            Article.status.in_(["pending", "distilling", "ready"]),
            Article.skip.is_(False),
            Article.deleted_at.is_(None),
        )
        .order_by(Article.created_at.desc())
        .limit(_LIST_PAGE_CAP + 1)  # +1 用于判别是否触发截断
    )
    rows = _truncate_for_list(result.all(), _LIST_PAGE_CAP, endpoint="list_pending", uid=uid)
    items = [_to_response(art, task).model_dump() for art, task in rows]
    await cache_service.set_pending(uid, items)  # 回填（ttl 60s）
    return {"articles": items, "count": len(items), "cached": False}


@app.get("/api/v1/articles/listened")
async def list_listened(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)):
    uid = _uid(user)
    result = await db.execute(
        select(Article, DistilledArticle)
        .outerjoin(DistilledArticle, DistilledArticle.article_id == Article.id)
        .options(_LISTING_DEFER)  # 见 _LISTING_DEFER
        .where(
            Article.user_id == uid,
            Article.status == "listened",
            Article.deleted_at.is_(None),
        )
        .order_by(Article.created_at.desc())  # 截断时必须给出确定顺序，否则被丢的是随机行
        .limit(_LIST_PAGE_CAP + 1)  # +1 用于判别是否触发截断
    )
    rows = _truncate_for_list(result.all(), _LIST_PAGE_CAP, endpoint="list_listened", uid=uid)
    items = [_to_response(art, task).model_dump() for art, task in rows]
    return {"articles": items, "count": len(items)}


@app.get("/api/v1/articles/{article_id}", response_model=ArticleResponse)
async def get_article(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """文章详情（含蒸馏全文）。

    ⚠️ **缓存必须在属主校验之后才有资格被信任**（2026-10 修的越权读）：

    原来是「先查缓存、命中就直接 return」，把 `_get_owned_with_task` 整段跳过了。
    而 `article:detail:{id}` 这个 key 不带 user 维度，于是任何登录用户只要知道
    别人的 article_id，就能在缓存存活期（300s）内直接读到**完整 payload** ——
    标题、原文链接、蒸馏全文 —— 本该是 403。

    旁边所有兄弟端点（`/status`、`/audio-url`）都是先 `_get_owned_with_task` 再干活，
    只有这里把缓存放在了前面，所以这不是设计取舍，是个离群点。

    现在缓存命中要带 `user_id` 一起判（`cache_service.get_article` 里做，见其说明）：
    属主对得上才直接返回，对不上就当没命中、落到下面正常的属主校验拿 403。
    属主仍然享受「不查库」的快路径，越权者拿不到任何东西。
    """
    uid = _uid(user)
    cached = await cache_service.get_article(article_id, user_id=uid)
    if cached:
        # ⚠️ **stage 不能从缓存里返回** —— 它是**易变**的（蒸馏进行中每十几秒
        # 变一次），而详情缓存有 300s TTL。缓存里的 stage 最多会滞后 5 分钟，
        # 用户看到「蒸馏中」却没有任何阶段信息，正是这个 bug 的表现。
        #
        # 所以 stage 每次现读 Redis（~0.1ms），不参与缓存。
        cached["stage"] = await get_stage(article_id)
        return cached

    art, task = await _get_readable_with_task(article_id, uid, db)
    # 订阅授权：属主或订阅者都可读（见 _get_readable 的说明）
    # CP-DISTILL-TEXT：详情页返回蒸馏稿全文（列表页只要 120 字摘要）
    payload = _to_response(
        art,
        task,
        full_script=True,
        tts_voice=await _tts_voice_brief(task),
    ).model_dump()
    await cache_service.set_article(article_id, payload, owner_id=art.user_id)  # ttl 300s
    # stage 现读、不入缓存（与上面缓存命中分支同一口径）
    payload["stage"] = await get_stage(article_id)
    return payload


@app.delete("/api/v1/articles/{article_id}")
async def delete_article(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """CP-DELETE：用户删除自己的文章（硬删除）。

    语义：
    - 仅 owner 可删；非 owner → 404（删除是破坏性操作，按「越权 404」决策不泄露
      资源存在性，与 GET 详情的 403 有意不同）
    - 级联清理：蒸馏结果 + 音频文件 + 收藏/稍后听/收听进度；feedback_v2 断引用保留
    - 配额不返还（蒸馏成本已发生）
    - 删除中的蒸馏任务不做取消（worker 写回时找不到行自然失败，可接受）
    """
    from article_purge import finish_purge, purge_article

    uid = _uid(user)
    result = await db.execute(select(Article).where(Article.id == article_id))
    art = result.scalar_one_or_none()
    if art is None or art.user_id != uid:
        raise NotFound(message=f"article {article_id} not found")
    owner_user_id = art.user_id
    audio_keys: list[str] = []

    await purge_article(db, article_id, audio_keys=audio_keys)

    try:
        await track_simple(db, "article_delete", uid, article_id)
    except Exception as exc:
        log.warning(f"ARTICLE_DELETE 埋点异常（忽略）: article={article_id} err={exc}")

    await db.commit()
    await finish_purge(article_id, owner_user_id, audio_keys=audio_keys)
    return {"ok": True, "id": article_id, "deleted": True}


@app.post("/api/v1/articles/{article_id}/mark-listened")
async def mark_listened(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    art = await _get_owned(article_id, _uid(user), db)
    art.status = "listened"

    # track() 只 flush 不 commit，get_db() 收尾只 close —— 埋点必须在 commit() 之前写，
    # 否则 flush 出来的 feedback 行被 close() 的隐式 rollback 丢掉（CP7.3.4）。
    try:
        await track(
            db,
            EventName.AUDIO_COMPLETE,
            user_id=_uid(user),
            article_id=article_id,
        )
    except Exception as exc:
        # track() 内部已兜底，这里是双保险：埋点失败不能拖垮业务
        log.warning(f"AUDIO_COMPLETE 埋点异常（忽略）: article={article_id} err={exc}")

    await db.commit()
    # 两个缓存都要失效（2026-10 补：这两条写路径原先一个都没失效）
    #
    # 待听列表按 `status.in_(["pending","distilling","ready"])` 过滤，
    # 而本端点把状态改成 `listened` —— 这篇文章本该从待听里消失。
    # 只失效详情缓存不够：用户 60 秒内打开过待听页，点完「听完了」列表里那一条
    # 还赖着不走（`pending` 缓存 ttl 60s），再点一次还是同一个结果，像按钮坏了。
    await cache_service.invalidate_article(article_id)
    await cache_service.invalidate_pending(_uid(user))
    return {"id": article_id, "status": "listened"}


# ---------------------------------------------------------------------------
# CP5.2 用户端 distill 失败重试（v1 §11.5）
# ---------------------------------------------------------------------------


async def push_retry_message(user_id: int, article_id: str) -> None:
    """v1 §11.5 CP5.2 '换源重试' 推送卡片。

    复用 CP5.4b push 队列（write_notification），不接极光推送（红线）。
    本期卡片类型 = retry_card，data 字段含 article_id + suggested_alternative。
    注意：PushNotification 模型无 type/data 字段，简化 title+body 直接展示。
    """
    from stashbox.backend.common.models.push_notification import PushNotification

    async with AsyncSessionLocal() as session:
        notif = PushNotification(
            user_id=user_id,
            article_id=article_id,
            title="换个来源重试？",
            body="这篇原文被拒收，要不要换一个源？",
            # 2026-10-21：status 严格表示**投递状态**，新建一律 pending，
            # 由投递 worker 真的去发之后再改。默认值 'sent' 等于
            # "创建即宣称已送达"，而当时没有任何投递动作。
            status="pending",
        )
        session.add(notif)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            # 推送失败不破请求
            pass


@app.post("/api/v1/articles/{article_id}/retry")
async def user_retry_distill(
    article_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """v1 §11.5 CP5.2 用户端蒸馏失败重试。

    行为：
      1. 校验 article 存在（不存在 404）
      2. 校验 article 属于当前 user（user_id 不匹配 403）
      3. 校验状态 = failed（其他状态 409 conflict）
      4. 状态置 pending，retry_count += 1
      5. 触发 ai-service 蒸馏（不可达时仅置 pending，worker 自动重试）
      6. 触发推送"换源重试"卡片（CP5.4b push 队列已有，写消息）
    失败回滚事务。
    """
    art = await db.get(Article, article_id)
    if art is None:
        raise NotFound(message=f"article {article_id} not found")

    if art.user_id != _uid(user):
        raise Forbidden(message="not your article")

    if art.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"article status is {art.status}, only 'failed' can retry",
        )

    art.status = "pending"
    art.retry_count = (art.retry_count or 0) + 1

    # 响应字段先取局部变量：track() 失败会 rollback，rollback 会 expire ORM 对象
    retry_count = art.retry_count
    art_user_id = art.user_id

    # CP5.2 埋点（必须在 commit 之前：track() 只 flush，否则随 close() 隐式 rollback 丢失）
    try:
        await track(
            db,
            EventName.ARTICLE_RETRY_REQUESTED,
            user_id=_uid(user),
            article_id=article_id,
        )
    except Exception as exc:
        log.warning(f"ARTICLE_RETRY_REQUESTED 埋点异常（忽略）: article={article_id} err={exc}")

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 触发蒸馏
    queued = await get_ai_client().trigger_distill(
        article_id, auth_token=create_access_token(str(art_user_id))
    )

    # 写推送"换源重试"卡片（CP5.4b push 队列）
    await push_retry_message(art_user_id, article_id)

    # 缓存失效（2026-10 补：原先这条写路径一个都没失效）
    #
    # 状态从 failed 变回 pending，待听列表的过滤条件（pending/distilling/ready）
    # 随之从「不包含」变成「包含」—— 重试成功的文章应该重新出现在待听里，
    # 不失效的话用户得等最多 60s 才知道。详情缓存同理（ttl 300s，
    # 里面还带着改之前的 status）。
    await cache_service.invalidate_article(article_id)
    await cache_service.invalidate_pending(art_user_id)

    return {
        "article_id": article_id,
        "status": "pending",
        "retry_count": retry_count,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "distill_triggered": queued is not None,
    }


# ---------------------------------------------------------------------------
# CP5.5 文章反馈闭环（v1 §3.1 / §4.3.4）：4 端点写 feedback 表
# ---------------------------------------------------------------------------
SKIP_REASONS = ("too_long", "boring", "low_quality", "other")
"""CP8.6: 推荐 enum（不强校验）。客户端 UI 可下拉选 4 项之一；服务端现在接受
任意字符串（≤ 64 char），详见 /skip 端点 docstring。保留元组仅用于：① 文档 ②
客户端 enum 来源 ③ 后续埋点分类统计。"""


class SkipRequest(BaseModel):
    """skip 原因（v1 §3.1，CP8.6 放宽为自由文本）。

    CP8.6 之前字段为 Literal["too_long","boring","low_quality","other"] —— 任何其他值
    （如客户端自定义 "not_interested"）都会被拒。现改为 str：服务端只校验「非空」
    + 「≤ 64 char」（feedback.reason 列宽上限）。
    """

    reason: str | None = None


class ListenCompleteRequest(BaseModel):
    """听完上报：duration_sec 可选（客户端播放时长，用于断点续听分析）。"""

    duration_sec: int | None = None


class RateRequest(BaseModel):
    """评分：1-5 星 + 可选评论。越界走业务 400（非 422）。"""

    rating: int | None = None
    comment: str | None = None


async def _write_feedback(
    db: AsyncSession,
    user_id: int,
    article_id: str,
    type_: str,
    *,
    rating: int | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
) -> Feedback:
    """写 feedback 行（v1 §4.3.4）。

    只 add 不 commit —— 调用方把 feedback 写和 article 字段更新放同一事务提交。
    """
    fb = Feedback(
        user_id=user_id,
        article_id=article_id,
        type=type_,
        rating=rating,
        reason=reason,
        metadata_=metadata if metadata is not None else {},
    )
    db.add(fb)
    return fb


@app.post("/api/v1/articles/{article_id}/favorite")
async def favorite(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """「收藏一下」快轨道（v1 §3.1 单数）。

    CP8.6 Bug 3 文档化决策：与 /api/v1/favorites（复数，folder + note 双轨）
    并存，不合并。详见下方 __doc_decision_favorite_vs_favorites__ 注释。

    行为：
      - articles.favorite = True
      - feedback(type="favorite") —— 同一事务
      - 写库后 invalidate article detail 缓存（CP8.6 Bug 1）
      - 幂等：重复点 favorite 不重复写 feedback 行（2026-10 修）

    幂等按**目标状态**判重，不按事件次数：只有「当前已经是收藏态、且已存在
    favorite 埋点行」才直接返回。取消收藏后再收藏仍会写新行 —— 那是一次真实
    的状态回转，不该被吃掉。

    这里曾长期标注「TODO：客户端应避免双击」，把幂等性推给了调用方。实际后果
    不是报错：`feedback` 表只有索引、没有唯一约束（见 common/models/feedback.py），
    双击不会抛 IntegrityError，而是**静默写入重复埋点行**，收藏类事件统计被灌水。
    原注释描述的故障模式本身就不成立。现已对齐复数 /favorites 的预查写法。
    """
    uid = _uid(user)
    art = await _get_readable(article_id, uid, db)
    # 订阅授权：属主或订阅者都可个人收藏（见 _get_readable 的说明）

    if art.favorite:
        existing = await db.scalar(
            select(Feedback)
            .where(
                Feedback.user_id == uid,
                Feedback.article_id == article_id,
                Feedback.type == "favorite",
            )
            .order_by(Feedback.id.desc())
            .limit(1)
        )
        if existing is not None:
            await cache_service.invalidate_article(article_id)
            return {"id": article_id, "favorite": True, "feedback_id": existing.id}

    art.favorite = True
    fb = await _write_feedback(db, uid, article_id, "favorite")
    await db.commit()
    await db.refresh(fb)  # commit 后 id/created_at 需回读（expire_on_commit）
    await cache_service.invalidate_article(article_id)  # CP8.6 Bug 1: 失效 stale 缓存
    return {"id": article_id, "favorite": True, "feedback_id": fb.id}


@app.post("/api/v1/articles/{article_id}/unfavorite")
async def unfavorite_article(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """取消收藏（CP9.3）：articles.favorite = False + feedback(type=unfavorite)。

    幂等：文章本来就没收藏时直接返回 ok。
    """
    uid = _uid(user)
    art = await _get_readable(article_id, uid, db)
    # 订阅授权：属主或订阅者都可个人收藏（见 _get_readable 的说明）
    art.favorite = False
    fb = await _write_feedback(db, uid, article_id, "unfavorite")
    await db.commit()
    await db.refresh(fb)
    await cache_service.invalidate_article(article_id)
    return {"id": article_id, "favorite": False, "feedback_id": fb.id}


# CP8.6 Bug 3 — `/favorite`（单数）vs `/favorites`（复数）双轨设计决策
# ============================================================================
# 决策时间:  CP8.6
# 决策人:    Hornet（产品）+ 后端（实现）
# 状态:      两套并存，不替 Hornet 拍板统一（待 v2 再评估）
#
# 单数 `POST /api/v1/articles/{id}/favorite`（本函数上方）:
#   用途:    「❤️ 收藏一下」快操作
#   写入:    articles.favorite = True  +  feedback(type="favorite")
#   是否幂等: 否（双击会重复写 feedback 行 —— 客户端 UI 应防抖）
#   场景:    列表 / 详情页的一键收藏按钮
#   字段:    无 folder / 无 note
#
# 复数 `POST /api/v1/articles/{id}/favorites`（下方 add_favorite）:
#   用途:    「收藏到文件夹」管理操作
#   写入:    favorites 表（user_id + article_id + folder + note，UNIQUE 约束）
#   是否幂等: 是（同 user + article + folder 重复加返 already_favorited）
#   场景:    收藏夹管理页 / 拖拽到 folder
#   字段:    folder（默认 "default"） + note（可选）
#
# 后续清理时机（v2 再评估）:
#   - 合并到一张表（favorites 扩展加一个特殊 folder='__quick__'）
#   - 或者废弃单数，统一用复数（前端要改 UI）
#   - 关键指标：用户实际用了哪个、各自的点击率
# ============================================================================


# ---------------------------------------------------------------------------
# CP5.5 收藏 + 稍后听（folder+note 双轨——不动 articles.favorite / feedback 表）
# ---------------------------------------------------------------------------


@app.get("/api/v1/favorites")
async def list_favorites(
    folder: str | None = None,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """列出我的收藏（可按 folder 过滤）。P1-4：LEFT JOIN articles 取 title 便于客户端展示。"""
    uid = _uid(user)
    q = (
        select(Favorite, Article.title)
        .outerjoin(Article, Article.id == Favorite.article_id)
        .where(Favorite.user_id == uid)
    )
    if folder:
        q = q.where(Favorite.folder == folder)
    q = q.order_by(Favorite.created_at.desc())
    result = await db.execute(q)
    rows = result.all()
    return {
        "favorites": [
            {
                "id": f.id,
                "article_id": f.article_id,
                "article_title": title,  # 可能为 None（article 已删除等），客户端降级显示 article_id
                "folder": f.folder,
                "note": f.note,
                "created_at": f.created_at.isoformat(),
            }
            for f, title in rows
        ]
    }


@app.get("/api/v1/favorites/folders")
async def list_favorite_folders(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """列出我的所有 folder（去重 + 计数）。"""
    uid = _uid(user)
    result = await db.execute(
        select(Favorite.folder, func.count(Favorite.id))
        .where(Favorite.user_id == uid)
        .group_by(Favorite.folder)
        .order_by(Favorite.folder)
    )
    rows = result.all()
    return {"folders": [{"folder": folder, "count": count} for folder, count in rows]}


@app.post("/api/v1/articles/{article_id}/favorites")
async def add_favorite(
    article_id: str,
    body: dict,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """加收藏（带 folder + note，复数轨道）。

    CP8.6 Bug 3 文档化决策：与单数 POST /articles/{id}/favorite 并存，详见上方
    `__doc_decision_favorite_vs_favorites__` 注释。本端点是「收藏到文件夹」管理
    操作，写 favorites 表（UNIQUE 约束保证幂等）。
    """
    uid = _uid(user)
    folder = body.get("folder", "default")
    note = body.get("note")

    # 文章必须存在**且属于当前用户**
    #
    # 2026-10-22：原来只查存在性。别人文章的 id 不可猜（art_ + uuid4 前 24 位），
    # 所以这不是越权读 —— 写进去的行挂在**调用者**名下。但结果是调用者的收藏列表里
    # 会出现**打不开（403）**的条目：写的时候说"存在"，读的时候被属主校验拦掉。
    # 同仓库的兄弟端点（mark-listened 等）都用 _get_owned，口径要一致。
    await _get_readable(article_id, uid, db)
    # 订阅授权：属主或订阅者都可个人收藏（见 _get_readable 的说明）

    # 检查是否已存在
    existing = await db.scalar(
        select(Favorite).where(
            Favorite.user_id == uid,
            Favorite.article_id == article_id,
            Favorite.folder == folder,
        )
    )
    if existing:
        # 幂等分支必须回全字段：`folder` 在正常分支有，缺了它安卓
        # `AddFavoriteResponse.folder: String`（无默认值）反序列化直接
        # MissingFieldException 崩掉 —— kotlinx 的 ignoreUnknownKeys 只忽略
        # **多余**键，不补**缺失的必填**键，所以这层兜不住。
        # 触发路径很日常：连点两次心形、列表数据过期后再点一次。
        return {
            "ok": True,
            "already_favorited": True,
            "id": existing.id,
            "folder": existing.folder,
        }

    fav = Favorite(user_id=uid, article_id=article_id, folder=folder, note=note)
    db.add(fav)
    await db.flush()  # 先拿 id（响应字段），但事务不结束，埋点同事务一起提交
    fav_id = fav.id

    # 埋点（必须在 commit 之前：track() 只 flush，否则随 close() 隐式 rollback 丢失）
    try:
        await track(
            db,
            EventName.FAVORITE_ADD,
            user_id=uid,
            article_id=article_id,
            metadata={"folder": folder},
        )
    except Exception as exc:
        # track() 内部已兜底，这里是双保险：埋点失败不能拖垮业务
        log.warning(f"FAVORITE_ADD 埋点异常（忽略）: article={article_id} err={exc}")

    await db.commit()
    await cache_service.invalidate_article(article_id)

    return {"ok": True, "id": fav_id, "folder": folder}


@app.patch("/api/v1/favorites/{favorite_id}")
async def update_favorite(
    favorite_id: int,
    body: dict,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """改 folder / note。"""
    uid = _uid(user)
    fav = await db.get(Favorite, favorite_id)
    if not fav or fav.user_id != uid:
        raise NotFound(message="favorite 不存在")

    if "folder" in body:
        fav.folder = body["folder"]
    if "note" in body:
        fav.note = body["note"]
    await db.commit()
    await cache_service.invalidate_article(fav.article_id)

    return {"ok": True, "id": fav.id}


@app.delete("/api/v1/favorites/{favorite_id}")
async def delete_favorite(
    favorite_id: int,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """删收藏。"""
    uid = _uid(user)
    fav = await db.get(Favorite, favorite_id)
    if not fav or fav.user_id != uid:
        raise NotFound(message="favorite 不存在")

    article_id = fav.article_id  # 响应/埋点字段先取局部变量
    await db.delete(fav)

    # 埋点（必须在 commit 之前：track() 只 flush，否则随 close() 隐式 rollback 丢失）
    try:
        await track(
            db,
            EventName.FAVORITE_REMOVE,
            user_id=uid,
            article_id=article_id,
        )
    except Exception as exc:
        log.warning(f"FAVORITE_REMOVE 埋点异常（忽略）: article={article_id} err={exc}")

    await db.commit()
    await cache_service.invalidate_article(article_id)

    return {"ok": True}


@app.get("/api/v1/later-listens")
async def list_later_listens(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """我的稍后听列表。P1-4：LEFT JOIN articles 取 title 便于客户端展示。"""
    uid = _uid(user)
    result = await db.execute(
        select(LaterListen, Article.title)
        .outerjoin(Article, Article.id == LaterListen.article_id)
        .where(LaterListen.user_id == uid)
        .order_by(LaterListen.created_at.desc())
    )
    rows = result.all()
    return {
        "later_listens": [
            {
                "id": i.id,
                "article_id": i.article_id,
                "article_title": title,  # 可能为 None（article 已删除等），客户端降级显示 article_id
                "snooze_until": i.snooze_until.isoformat() if i.snooze_until else None,
                "created_at": i.created_at.isoformat(),
            }
            for i, title in rows
        ]
    }


@app.post("/api/v1/articles/{article_id}/snooze")
async def snooze_article(
    article_id: str,
    body: dict,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """标记稍后听。"""
    uid = _uid(user)
    # 同加收藏：校验存在**且属于当前用户**（原来只查存在性，
    # 会让别人的文章出现在自己的「稍后听」里，点进去又是 403）。
    await _get_readable(article_id, uid, db)
    # 订阅授权：属主或订阅者都可个人稍后听（见 _get_readable 的说明）

    snooze_until = body.get("snooze_until")
    if snooze_until:
        dt = datetime.fromisoformat(snooze_until.replace("Z", "+00:00"))
        snooze_until = dt.astimezone(timezone.utc).replace(tzinfo=None)

    existing = await db.scalar(
        select(LaterListen).where(
            LaterListen.user_id == uid,
            LaterListen.article_id == article_id,
        )
    )
    if existing:
        existing.snooze_until = snooze_until
        item_id = existing.id  # 局部变量前置，防 commit 后 expire

        # 埋点（必须在 commit 之前：track() 只 flush，否则随 close() 隐式 rollback 丢失）
        try:
            await track(
                db,
                EventName.ARTICLE_SNOOZE,
                user_id=uid,
                article_id=article_id,
                metadata={"updated": True},  # 与新建分支区分
            )
        except Exception as exc:
            log.warning(f"ARTICLE_SNOOZE 埋点异常（忽略）: article={article_id} err={exc}")

        await db.commit()
        await cache_service.invalidate_article(article_id)
        return {"ok": True, "id": item_id, "updated": True}

    item = LaterListen(user_id=uid, article_id=article_id, snooze_until=snooze_until)
    db.add(item)
    await db.flush()  # 先拿 id（响应字段），但事务不结束，埋点同事务一起提交
    item_id = item.id

    # 埋点（必须在 commit 之前：track() 只 flush，否则随 close() 隐式 rollback 丢失）
    try:
        await track(
            db,
            EventName.ARTICLE_SNOOZE,
            user_id=uid,
            article_id=article_id,
        )
    except Exception as exc:
        log.warning(f"ARTICLE_SNOOZE 埋点异常（忽略）: article={article_id} err={exc}")

    await db.commit()
    await cache_service.invalidate_article(article_id)

    return {"ok": True, "id": item_id}


@app.delete("/api/v1/articles/{article_id}/snooze")
async def unsnooze_article(
    article_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """取消稍后听。"""
    uid = _uid(user)
    existing = await db.scalar(
        select(LaterListen).where(
            LaterListen.user_id == uid,
            LaterListen.article_id == article_id,
        )
    )
    if not existing:
        return {"ok": True, "was_snoozed": False}

    await db.delete(existing)
    await db.commit()
    await cache_service.invalidate_article(article_id)
    return {"ok": True}


@app.post("/api/v1/articles/{article_id}/skip")
async def skip(
    article_id: str,
    req: SkipRequest = SkipRequest(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """跳过（v1 §3.1）：articles.skip=True + feedback(type=skip, reason=...)，同一事务。

    CP8.6 Bug 2 修复：reason 从「4 选 1 Literal」放宽为「自由文本」。
    - 空字符串 / 缺失 → 业务 400（reason is required）
    - 超过 64 字符 → 业务 400（feedback.reason 列宽 64）
    - 其他任意字符串（含 "not_interested"、"too_short"、中文等）→ 200 OK
    - SKIP_REASONS 仍保留作为推荐 enum（用于客户端 UI 分类统计），不强制。
    """
    if not req.reason or not req.reason.strip():
        raise InvalidRequest(message="reason is required", code=4001)
    reason = req.reason.strip()
    if len(reason) > 64:
        raise InvalidRequest(message="reason too long (max 64 chars)", code=4001)

    uid = _uid(user)
    art = await _get_owned(article_id, uid, db)
    art.skip = True
    fb = await _write_feedback(db, uid, article_id, "skip", reason=reason)
    await db.commit()
    await db.refresh(fb)
    await cache_service.invalidate_article(article_id)
    return {"id": article_id, "skip": True, "feedback_id": fb.id, "reason": reason}


@app.post("/api/v1/articles/{article_id}/listen-complete")
async def listen_complete(
    article_id: str,
    req: ListenCompleteRequest = ListenCompleteRequest(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """听完上报（v1 §3.1 mark-listened 的反馈闭环版）：写 feedback(type=listen_complete)。

    articles 表无 listened_at 列（v1 §4.3.1 未建，本期红线不动 alembic），
    故 listened_at 取 feedback.created_at —— 同一事务里 DB 侧 NOW()，语义等价。
    """
    uid = _uid(user)
    await _get_readable(article_id, uid, db)
    # 订阅授权：属主或订阅者都可个人收听状态（见 _get_readable 的说明）
    meta = {"duration_sec": req.duration_sec} if req.duration_sec is not None else {}
    fb = await _write_feedback(db, uid, article_id, "listen_complete", metadata=meta)
    await db.commit()
    await db.refresh(fb)
    await cache_service.invalidate_article(article_id)
    return {
        "id": article_id,
        "listened_at": fb.created_at.isoformat() if fb.created_at else None,
        "feedback_id": fb.id,
    }


@app.post("/api/v1/articles/{article_id}/rate")
async def rate(
    article_id: str,
    req: RateRequest = RateRequest(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """评分（1-5 星）：写 feedback(type=rate, rating=..., metadata={comment})。

    刻意**不**改 articles.quality_score —— 避免与推荐算法循环依赖，留 CP5.6 离线计算。
    """
    if req.rating is None:
        raise InvalidRequest(message="rating is required", code=4001)
    if not 1 <= req.rating <= 5:
        raise InvalidRequest(message="rating must be between 1 and 5", code=4001)

    uid = _uid(user)
    await _get_readable(article_id, uid, db)
    # 订阅授权：属主或订阅者都可个人评分（见 _get_readable 的说明）
    meta = {"comment": req.comment} if req.comment else {}
    fb = await _write_feedback(db, uid, article_id, "rate", rating=req.rating, metadata=meta)
    await db.commit()
    await db.refresh(fb)
    await cache_service.invalidate_article(article_id)
    return {"id": article_id, "rating": req.rating, "feedback_id": fb.id}


# ---------------------------------------------------------------------------
# CP5.5-A3 反馈分类 + 评分（feedback_v2 双轨）
# ---------------------------------------------------------------------------
FEEDBACK_CATEGORIES = ("bug", "feature", "content", "audio_quality", "other")


class FeedbackV2CreateRequest(BaseModel):
    article_id: str | None = None
    category: str
    rating: int | None = None
    content: str
    contact: str | None = None
    device_info: dict | None = None


@app.post("/api/v1/feedback-v2")
async def create_feedback_v2(
    body: FeedbackV2CreateRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """提交反馈（分类 + 可选评分）。"""
    uid = _uid(user)

    # 校验 category
    if body.category not in FEEDBACK_CATEGORIES:
        raise InvalidRequest(message=f"category 必须是 {FEEDBACK_CATEGORIES} 之一")

    # 校验 rating
    if body.rating is not None and not (1 <= body.rating <= 5):
        raise InvalidRequest(message="rating 必须在 1-5 之间")

    # content 非空
    if not body.content.strip():
        raise InvalidRequest(message="content 必填")

    # article_id 可选，但若填了必须存在
    if body.article_id:
        art = await db.get(Article, body.article_id)
        if not art:
            raise NotFound(message=f"article 不存在: {body.article_id}")

    fb = FeedbackV2(
        user_id=uid,
        article_id=body.article_id,
        category=body.category,
        rating=body.rating,
        content=body.content.strip(),
        contact=body.contact,
        device_info=body.device_info,
    )
    db.add(fb)
    await db.flush()  # get id without ending transaction
    fb_id = fb.id
    fb_category = fb.category

    # 埋点（仅当有 article_id 时，feedback 表 article_id 为 NOT NULL FK）
    if body.article_id:
        await track(
            db,
            EventName.FEEDBACK_V2_SUBMIT,
            user_id=uid,
            article_id=body.article_id,
            metadata={
                "category": body.category,
                "rating": body.rating,
                "has_contact": bool(body.contact),
            },
        )

    await db.commit()
    if body.article_id:
        await cache_service.invalidate_article(body.article_id)

    return {"ok": True, "id": fb_id, "category": fb_category}


@app.get("/api/v1/feedback-v2")
async def list_my_feedback_v2(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    category: str | None = None,
    limit: int = 50,
):
    """列出我提交的反馈。"""
    uid = _uid(user)
    q = select(FeedbackV2).where(FeedbackV2.user_id == uid)
    if category:
        q = q.where(FeedbackV2.category == category)
    q = q.order_by(FeedbackV2.created_at.desc()).limit(limit)
    result = await db.execute(q)
    items = result.scalars().all()
    return {
        "feedbacks": [
            {
                "id": f.id,
                "article_id": f.article_id,
                "category": f.category,
                "rating": f.rating,
                "content": f.content,
                "created_at": f.created_at.isoformat(),
            }
            for f in items
        ]
    }


async def _find_existing_clip(url: str, user_id: int, db: AsyncSession) -> Article | None:
    """同用户 + 同 URL + 未删除 的已有文章（CP-DUPLICATE-CLIP）。

    单独抽出来是因为**扣费必须发生在判断之后**：
    d9_add_article / submit_article 原实现是「先 consume 扣费 → 再建文章」，
    重复剪藏同一链接时虽然不再建新行，配额却已经白扣了一次。
    """
    return await db.scalar(
        select(Article)
        .where(
            Article.user_id == user_id,
            Article.url == url,
            Article.deleted_at.is_(None),
        )
        .order_by(Article.created_at.desc())
        .limit(1)
    )


@app.post("/api/v1/callback/d9-add-article", response_model=D9AddResponse)
async def d9_add_article(
    req: D9AddRequest,
    user: dict | None = Depends(require_user_optional),
    device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
    db: AsyncSession = Depends(get_db),
):
    """D9 入口（v1 §3.5）：微信「更多打开方式」→ 听匣，不要求登录态。

    1. 解析 caller：已登录用 user_id，未登录用 device_id（两者都没有 → 4001）
    2. 配额预扣（仅已登录；匿名不计费）
    3. 建 articles 行（status=pending）
    4. 触发 ai-service 蒸馏（失败只 log，不影响 D9 返回）
    """
    if user is None and not device_id:
        raise InvalidRequest(message="device_id required for anonymous D9", code=4001)
    _validate_url(req.url)

    # 2026-10-03：同 submit_article，**先抓后扣**。
    # D9 是微信里「更多打开方式」进来的路径，用户看到的是系统级的分享返回，
    # 抓不到正文却返回成功 = 微信里显示"已打开"、听匣里 12 分钟后失败。
    # 抓不到就当场报错，别建一条空文章也别扣配额。
    fr = await _prefetch_for_capture(req.url)

    if user is not None:
        uid = _uid(user)
        # CP-DUPLICATE-CLIP：先查重再扣费。重复剪藏同一链接复用已有文章，
        # 配额不动 —— 否则用户重复分享一次就被多扣一次。
        dup = await _find_existing_clip(req.url, uid, db)
        if dup is not None:
            log.info(
                "d9_duplicate_reuse",
                extra={"url": req.url, "user_id": uid, "article_id": dup.id},
            )
            task = await get_ai_client().trigger_distill(
                dup.id, auth_token=create_access_token(str(uid))
            )
            return D9AddResponse(
                article_id=dup.id,
                task_id=(task or {}).get("task_id"),
                status="distilling" if task else dup.status,
                device_id=device_id,
            )
        await quota_service.consume(db, uid)  # 用尽抛 QuotaExceededError(3001)
    else:
        uid = ANONYMOUS_USER_ID
        await _ensure_anonymous_user(db)

    art = await _create_article(
        req.url,
        uid,
        req.source,
        req.title or fr.title or None,
        db,
        raw_content=_to_raw_content(fr),
        fetch_on_create=False,  # 上面已抓过
    )
    # 打标：该文章已扣过配额（匿名不计费也算），避免 ai-service 蒸馏时重复扣
    await cache_service.mark_article_quota(art.id)

    task = await get_ai_client().trigger_distill(art.id, auth_token=create_access_token(str(uid)))
    return D9AddResponse(
        article_id=art.id,
        task_id=(task or {}).get("task_id"),
        status="distilling" if task else art.status,
        device_id=device_id,
    )


@app.get("/api/v1/articles/{article_id}/status", response_model=ArticleStatusResponse)
async def article_status(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """文章 + 蒸馏任务聚合状态（v1 §11.4 CP1.7）：客户端轮询这个端点等 ready。"""
    art, task = await _get_readable_with_task(article_id, _uid(user), db)
    # 订阅授权：属主或订阅者都可读（见 _get_readable 的说明）
    return ArticleStatusResponse(
        article_id=art.id,
        status=_derive_status(art, task),
        # 蒸馏当前阶段（fetch/rewrite/tts/concat）—— App 轮询这个端点时
        # 拿它显示「正在合成语音…」，而不是干等笼统的「蒸馏中」。
        # TTS 是最慢的一步（上游 RTF≈0.3，1000 字稿要合成 4 分钟音频），
        # 没有阶段信息用户会以为卡死。
        stage=await get_stage(article_id),
        task_id=task.id if task else None,
        task_status=task.status if task else None,
        error=art.error,
        audio_url=task.audio_url if task else None,
        audio_duration_sec=task.duration_sec if task else None,
        tags=task.tags if task else None,
        quality_score=task.quality_score if task else None,
        created_at=art.created_at.isoformat() if art.created_at else "",
        updated_at=art.updated_at.isoformat() if art.updated_at else "",
        tts_voice=await _tts_voice_brief(task),
    )


@app.get("/api/v1/articles/{article_id}/audio-url", response_model=AudioUrlResponse)
async def article_audio_url(
    article_id: str, user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """取音频播放地址：仅 status=ready 可用，其余 404。

    CP-AUDIO-URL-STATIC：`audio_url` 若是**永久静态直链**（bucket 公共读 /
    OSS_PUBLIC_BASE_URL —— 内容归属 app 账户，必须长期可播、不能擅自失效），
    原样返回且 `expires_at=None`，客户端据此不再做「过期前刷新」。

    只有当库里存的 URL **本身已带签名参数**时，才沿用阿里云 OSS 的
    过期语义（`?Expires=...&OSSAccessKeyId=...`）。历史上的 mock 签名
    （`Signature=mock`）仅作为签名 URL 的占位形式保留，不再无条件拼给静态直链。

    本地 dev 模式（STORAGE_PROVIDER=local / ENABLE_LOCAL_AUDIO_MOUNT=1）：
    audio_url 用 PUBLIC_GATEWAY_URL 拼出，避开 localhost —— 真机客户端
    连 gateway 时 localhost 指向设备自己，会 404。

    CP9.x 修正：服务端 audio_url 后缀（.mp3/.m4a/.wav）必须匹配磁盘上的真实文件后缀。
    否则 ExoPlayer 按后缀选 MP3/M4A/PCM 解码器会失败。
    兜底策略：本地模式下扫描 LOCAL_AUDIO_DIR 下 /audio/{article_id}.{ext} 实际存在的扩展名。
    """
    art, task = await _get_readable_with_task(article_id, _uid(user), db)
    # 订阅授权：属主或订阅者都可读（见 _get_readable 的说明）
    if _derive_status(art, task) != "ready":
        raise NotFound(message=f"audio not ready for article {article_id}")

    expires_ts = int(time.time()) + AUDIO_URL_TTL_SEC
    # CP9.x dev：本地模式用 PUBLIC_GATEWAY_URL 当 host，path 沿用 task.audio_url 的 path
    if settings.storage_provider == "local" or settings.enable_local_audio_mount:
        task_url = task.audio_url if task else None
        if task_url:
            # task_url 形如 http://localhost:8100/audio/audio/{art.id}.mp3 → 用 PUBLIC_GATEWAY_URL 替换 host
            # 或者 https://stashbox-audio.oss... → 跳过本地模式走 OSS
            if "localhost" in task_url or "127.0.0.1" in task_url or task_url.startswith("/"):
                from urllib.parse import urlparse

                path = urlparse(task_url).path
                base = f"{settings.public_gateway_url.rstrip('/')}{path}"
            else:
                base = task_url
        else:
            # 没 task.audio_url → 用本地音频路径兜底
            base = f"{settings.public_gateway_url.rstrip('/')}/audio/audio/{art.id}.mp3"
        # 关键修正：扫描磁盘上实际存在的扩展名，覆盖 URL 后缀
        base = _resolve_actual_audio_extension(base, settings.local_audio_dir)
    else:
        base = (task.audio_url if task else None) or f"{OSS_AUDIO_BASE}/{art.id}.m4a"
    # CP6.2.1 埋点：audio_play_start。本端点无业务写操作，没有现成 commit —— track()
    # 只 flush，必须由这里显式 commit() 把 feedback 行落库，否则随 close() 丢失。
    try:
        await track_simple(db, EventName.AUDIO_PLAY_START, _uid(user), article_id)
        await db.commit()
    except Exception as exc:
        log.warning(f"AUDIO_PLAY_START 埋点异常（忽略）: article={article_id} err={exc}")
    # CP-AUDIO-URL-STATIC（实测修复）：
    # 这里的 `?Expires=...&OSSAccessKeyId=mock&Signature=mock` 是阿里云 OSS
    # 时代的遗留（签名本期 mock，见 docstring）。现在 audio_url 多数是**永久静态
    # 直链**（bucket 公共读 + OSS_PUBLIC_BASE_URL，内容归属 app 账户、要求长期
    # 可播、不能擅自失效），对它做两件错事：
    #   1. 拼上无意义的 mock 签名参数（SeaweedFS 靠"忽略未知参数"才没报错）；
    #   2. 回报一个假的 `expires_at`，让客户端以为会过期 → 每次恢复播放/切档
    #      都白跑一次 audio-url 接口（App 侧 isExpiringSoon() 会命中）。
    #
    # 现在：**只有当 URL 本来就带签名参数时**才沿用签名并回报 expires_at；
    # 干净的静态直链原样返回、expires_at=None，客户端据此知道"不会过期"。
    from urllib.parse import parse_qs, urlparse as _urlparse

    _is_signed = bool(parse_qs(_urlparse(base).query))
    # 已经是签名 URL → 原样返回（签名自带的过期语义照旧）
    # 干净的静态直链   → 原样返回，expires_at=None（它不会过期）
    _url = base
    _expires_at = None
    if _is_signed:  # pragma: no cover - 预留：接真签名（阿里云 RAM）后的分支
        _expires_at = datetime.fromtimestamp(expires_ts, timezone.utc).isoformat()

    return AudioUrlResponse(
        article_id=art.id,
        audio_url=_url,
        expires_at=_expires_at,
        duration_sec=(task.duration_sec if task else None) or 0,
    )


def _resolve_actual_audio_extension(base_url: str, audio_dir: str) -> str:
    """扫描 LOCAL_AUDIO_DIR 下 /audio/{article_id}.* 实际存在的音频文件。

    CP9.x：服务端 audio_url 后缀与磁盘文件后缀不一致时（例如 task_url 是
    .mp3 但本地只存了 .wav —— placeholder 文件是 wav 因为 Python wave 模块），客户端
    ExoPlayer 按后缀选 decoder 会失败。改用磁盘扫描兜底。

    注意 LocalStorage 落盘用 key=f"audio/{article_id}.{hash8}.{ext}"
    （内容寻址，见 app/services/storage/base.py:versioned_audio_key），
    所以文件路径是 audio_dir/audio/{article_id}.{hash8}.{ext} ——
    扫描两个候选位置（直接 + audio 子目录）。

    优先级：.m4a > .mp3 > .wav（m4a 优先因为生产 OSS 通常返 m4a）
    同后缀有多个版本时取**最新**（mtime 最大）—— 内容寻址意味着重新生成会
    留下旧文件名的版本，「最新」才是当前该播的那份。

    ⚠️ 只认**主档**（`{article_id}.{hash8}.{ext}`），不认多码率变体
    （`{article_id}.{bitrate}k.{hash8}.{ext}`）—— 变体是给客户端按网速选的
    备选，兜底 URL 不该指向它。
    """
    import re
    from urllib.parse import urlparse
    from pathlib import Path

    parsed = urlparse(base_url)
    path = Path(parsed.path)  # e.g. /audio/audio/art_xxx.mp3
    stem = path.stem  # art_xxx
    parent = path.parent  # /audio/audio

    audio_dir_path = Path(audio_dir)
    # 候选位置：audio_dir 直接 + audio_dir/audio 子目录（LocalStorage key 模板）
    candidates_root = [audio_dir_path / stem, audio_dir_path / "audio" / stem]
    for ext in (".m4a", ".mp3", ".wav", ".ogg", ".aac"):
        for root in candidates_root:
            # ① 历史命名（无 hash）：{article_id}.{ext}
            candidate = root.with_suffix(ext)
            if candidate.exists():
                # 替换 base_url 的后缀为真实存在的后缀
                new_path = parent / f"{stem}{ext}"
                new_parsed = parsed._replace(path=str(new_path))
                return new_parsed.geturl()
            # ② 内容寻址命名（当前生产形态）：{article_id}.{hash8}.{ext}
            #    正则排除多码率变体（{article_id}.{bitrate}k.{hash8}.{ext}）
            pattern = re.compile(rf"^{re.escape(root.name)}\.[0-9a-f]{{8}}{re.escape(ext)}$")
            found = [
                f
                for f in root.parent.glob(f"{root.name}.*{ext}")
                if f.is_file() and pattern.match(f.name)
            ]
            if found:
                newest = max(found, key=lambda f: f.stat().st_mtime)
                new_path = parent / newest.name
                new_parsed = parsed._replace(path=str(new_path))
                return new_parsed.geturl()
    # 没找到实际文件：保持原 URL（让客户端 404，便于调试）
    return base_url


# CP11.0.1 Android 断点续听
class ProgressUpdateRequest(BaseModel):
    position_sec: int
    total_sec: int | None = None


@app.post("/api/v1/articles/{article_id}/progress")
async def update_progress(
    article_id: str,
    req: ProgressUpdateRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """上报/更新收听进度（断点续听）。"""
    uid = _uid(user)

    # 检查 article 是否存在**且属于当前用户**
    #
    # 2026-10-22：这里原来只查存在性，注释却写着「（归属校验）」——
    # 注释声称的归属校验并不存在。已改成真的 _get_owned。
    await _get_readable(article_id, uid, db)
    # 订阅授权：属主或订阅者都可个人播放进度（见 _get_readable 的说明）

    # upsert
    existing = await db.scalar(
        select(ListeningStatus).where(
            ListeningStatus.user_id == uid,
            ListeningStatus.article_id == article_id,
        )
    )
    if existing:
        existing.position_sec = req.position_sec
        if req.total_sec is not None:
            existing.total_sec = req.total_sec
    else:
        existing = ListeningStatus(
            user_id=uid,
            article_id=article_id,
            position_sec=req.position_sec,
            total_sec=req.total_sec,
        )
        db.add(existing)

    await db.commit()
    return {"ok": True, "article_id": article_id, "position_sec": req.position_sec}


@app.get("/api/v1/articles/{article_id}/progress")
async def get_progress(
    article_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """获取收听进度（断点续听）。无记录返回 200 + null。"""
    uid = _uid(user)

    record = await db.scalar(
        select(ListeningStatus).where(
            ListeningStatus.user_id == uid,
            ListeningStatus.article_id == article_id,
        )
    )
    if not record:
        return {"article_id": article_id, "position_sec": None, "total_sec": None}

    return {
        "article_id": article_id,
        "position_sec": record.position_sec,
        "total_sec": record.total_sec,
    }


@app.post("/api/v1/callback/clawbot-message")
async def clawbot_message(
    req: ClawBotMessageRequest,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """ClawBot 入口：接收消息，解析出 URL 则建文章（口径与 submit_article 一致）。

    ## 2026-10-22 修：三个洞一起

    原来的实现只有三行：

        art = None
        if "http" in req.text:
            art = await _create_article(req.text, _uid(user), "clawbot", None, db)
        return {"received": True, "article": _to_response(art) if art else None}

    1. **不扣配额**：登录用户可以无限建文章。articles 表对 (user_id, url)
       没有唯一约束，换个 URL 就能一直建 → 用户配额纹丝不动，列表里堆满
       永远 pending 的条目（因为也不派蒸馏）。
    2. **不派蒸馏**：建出来的文章永远是 pending，用户点了播放没反应。
    3. **把整段 text 当 URL 用**：`"http" in req.text` 只判断有没有 http，
       真的传「看看这个 https://xxx」进去，url 存的是**整句话** →
       抓取必然失败。
    → 现在与 `submit_article` 同款：抽 URL → 校验 → **先抓后扣** →
      扣配额 → 建文章 → 打「已扣费」标记 → 派蒸馏。
      配额是这条路径唯一的天然上限，洞就被堵上了。

    ## 为什么是「先抓后扣」

    抓不到正文就没有可蒸馏的东西 —— 那就不该扣钱。`submit_article` 与
    `d9_add_article` 都是这个口径，这里必须一样，否则同一个动作有三种计费。
    """
    uid = _uid(user)

    url = _extract_url(req.text)
    if url is None:
        # 没有 URL：这不是错误（可能就是一句闲聊），照收。
        return {"received": True, "article": None}
    if not _is_valid_url(url):
        raise InvalidRequest(message="url invalid", code=2001)

    # 先抓后扣：抓不到就不建文章也不计费（与 submit_article 同款）。
    fr = await _prefetch_for_capture(url)

    # 扣配额（与 submit_article / d9_add_article 同口径；用尽抛 3001）。
    quota = await quota_service.consume(db, uid)

    art = await _create_article(
        url,
        uid,
        "clawbot",
        fr.title or None,
        db,
        raw_content=_to_raw_content(fr),
        fetch_on_create=False,  # 上面已经抓过了，别再抓一次
    )
    await cache_service.mark_article_quota(art.id)  # 打标：该文章已扣过配额

    # 派蒸馏（失败仅 log 不破请求：文章留在 pending，等用户手动重试）。
    trigger_result = None
    try:
        trigger_result = await get_ai_client().trigger_distill(
            article_id=art.id,
            auth_token=create_access_token(str(art.user_id)),
        )
    except Exception as exc:
        log.warning(
            "auto_distill_trigger_failed (clawbot)",
            extra={"article_id": art.id, "error": str(exc)},
        )

    return {
        "received": True,
        "article": _to_response(art),
        "task_id": (trigger_result or {}).get("task_id"),
        "quota_used": quota["quota_used"],
        "monthly_quota": quota["monthly_quota"],
        "remaining": quota["monthly_quota"] - quota["quota_used"],
    }


@app.post("/api/v1/callback/wechat-mp-message")
async def wechat_mp_message(
    req: WechatMpMessageRequest,
    callback_secret: str = Depends(require_callback_secret),
    db: AsyncSession = Depends(get_db),
):
    """微信公众号服务号回调（v1 §11.2 CP2.5）。

    接收用户发给服务号的 URL → 路由 fetcher 抓正文 → 建文章 → 触发蒸馏。

    - **鉴权走共享密钥**（`X-Callback-Secret`，见 common/callback_auth.py）：
      服务号回调没有用户 JWT，而这条路径**不扣配额**，所以它必须是全站最严的入口之一。
      原实现只挂 `Depends(get_db)`，即无任何鉴权 —— 任意匿名请求 `text` 塞个 URL 就能
      建文章 + 触发蒸馏，直接烧 LLM/TTS。注释里「公众号已认证用户身份」从未在代码中落地。
    - 不扣配额（匿名入口，等客户端登录后再扣）
    - 失败抛 BizException(2001=URL 不支持 / 2002=抓取失败)
    """
    url = _extract_url(req.text)
    if url is None:
        raise InvalidRequest(message="no url found", code=2001)
    if not _is_valid_url(url):
        raise InvalidRequest(message=f"url invalid: {url}", code=2001)

    fetcher = get_fetcher(url)
    if fetcher is None:  # 理论不会发生（generic_url 兜底），留着防回归
        raise InvalidRequest(message="no fetcher matched", code=2001)

    try:
        result = await fetcher.fetch(url)
    except FetcherError as exc:
        # CP6.2.2.2a: ARTICLE_CAPTURE_FAILED / ARTICLE_UNSUPPORTED 埋点
        if exc.code == FetcherErrorCode.UNSUPPORTED:
            await track(
                db,
                EventName.ARTICLE_UNSUPPORTED,
                user_id=ANONYMOUS_USER_ID,
                article_id="n/a",
                metadata={"error": exc.message},
            )
        else:
            await track(
                db,
                EventName.ARTICLE_CAPTURE_FAILED,
                user_id=ANONYMOUS_USER_ID,
                article_id="n/a",
                metadata={"error": exc.code.value if hasattr(exc.code, "value") else str(exc.code)},
            )
        # track() 只 flush 不 commit，而这里随后要 raise（get_db 会 rollback）——
        # 显式 commit() 把 feedback 行先落库，否则埋点随 rollback 一起丢（CP7.4-prereq）。
        try:
            await db.commit()
        except Exception as commit_exc:
            log.warning(f"失败路径埋点 commit 失败（忽略）: err={commit_exc}")
        raise map_fetcher_error(exc) from exc

    await _ensure_anonymous_user(db)
    art = await _create_article(
        url,
        ANONYMOUS_USER_ID,
        "wechat_mp",
        result.title or None,
        db,
        raw_content=_to_raw_content(result),  # FetchResult → JSONB，免二次抓取
    )
    task = await get_ai_client().trigger_distill(
        art.id, auth_token=create_access_token(str(ANONYMOUS_USER_ID))
    )
    return {
        "received": True,
        "article_id": art.id,
        "task_id": (task or {}).get("task_id"),
        "title": art.title,
        "source": art.source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "content_text_length": len(result.content_text),
        "has_media": len(result.media_urls) > 0,
    }


@app.get("/api/v1/tags")
async def list_tags(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """CP5.3a：从 DB 读标签（不再用 mock）
    P1-1：LEFT JOIN tag_subscriptions 取每 tag 的"我是否已订阅"，客户端 Switch 即时正确显示
    CP-TAG-FILTER：article_count 一次 group by 算完（零 N+1），用户能看到该标签下已蒸馏的文章数
    """
    uid = _uid(user)
    # CP-TAG-FILTER：article_count 统计「该 user 已蒸馏且打了此标签」的文章数。
    # ⚠️ @> 条件必须放在 DistilledArticle 的 OUTER JOIN 的 **ON 子句** 里，
    # 而不是外层 WHERE：放 WHERE 会把 LEFT JOIN 退化成 INNER JOIN ——
    # 用户没有任何已蒸馏文章的标签（新用户尤甚）会被整行过滤掉，
    # 订阅页拿到 {"tags":[]} 直接空白（真机反馈，2026-09-23 修复）。
    rows = (
        await db.execute(
            select(
                Tag,
                TagSubscription.tag_id.is_not(None),
                func.count(DistilledArticle.article_id),
            )
            .outerjoin(
                TagSubscription,
                (TagSubscription.tag_id == Tag.id) & (TagSubscription.user_id == uid),
            )
            .outerjoin(
                DistilledArticle,
                (
                    DistilledArticle.article_id.in_(
                        select(Article.id).where(
                            Article.user_id == uid,
                            Article.deleted_at.is_(None),
                        )
                    )
                )
                & DistilledArticle.tags.is_not(None)
                # JSONB @> 数组语义："tags 数组包含 [name]"
                # —— 因为 :name 是子查询外 Tag.name 字符串值（不是 ORM 属性），
                # 不能直接在 Python 端 [Tag.name] bind —— SQLAlchemy 会把它当成列引用传。
                & DistilledArticle.tags.op("@>")(func.cast(func.json_build_array(Tag.name), JSONB)),
            )
            .group_by(Tag.id, TagSubscription.tag_id)
            .order_by(Tag.category, Tag.name)
        )
    ).all()
    return {
        "tags": [
            {
                "id": tag.slug,  # 用 slug 作为 id（与 mock 兼容）
                "name": tag.name,
                "category": tag.category,
                "subscribed": bool(is_subscribed),
                # CP-TAG-FILTER：该 user 在此标签下已蒸馏的文章数（订阅页跳文章流用）
                "article_count": int(cnt),
            }
            for tag, is_subscribed, cnt in rows
        ]
    }


class TagCreateRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=64)
    category: str = Field("subject", max_length=32)


@app.post("/api/v1/tags")
async def create_tag(
    req: TagCreateRequest,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """admin/operator 创自定义标签（CP5.3b）。

    CP-TAG-FILTER：检查 slug 冲突（已有）+ name 冲突（新增），避免同名歧义。
    """
    # slug 冲突（已有）
    existing_slug = await db.scalar(select(Tag).where(Tag.slug == req.slug))
    if existing_slug:
        raise HTTPException(status_code=409, detail=f"tag slug 已存在: {req.slug}")
    # CP-TAG-FILTER：name 冲突（避免两个不同 slug 显示同样中文名，订阅推送/列表显示歧义）
    existing_name = await db.scalar(select(Tag).where(Tag.name == req.name))
    if existing_name:
        raise HTTPException(
            status_code=409,
            detail=f"tag name 已存在（slug={existing_name.slug}）: {req.name}",
        )

    tag = Tag(
        slug=req.slug,
        name=req.name,
        category=req.category,
        is_system=False,
        creator_id=_uid(user),
    )
    db.add(tag)

    # track() 只 flush 不 commit，get_db() 收尾只 close 不 commit —— 埋点必须在
    # commit() 之前写，否则 flush 的 feedback 行会被 close() 的隐式 rollback 丢掉。
    # 埋点失败时 track() 内部会 rollback（expire 掉 ORM 对象），故先取响应字段
    tag_slug, tag_name, tag_category = tag.slug, tag.name, tag.category

    try:
        await track(
            db,
            EventName.TAG_CREATE,
            user_id=_uid(user),
            article_id="n/a",  # 标签事件无关联文章，但 feedback.article_id NOT NULL
            metadata={"tag_slug": tag_slug, "category": tag_category},
        )
    except Exception as exc:
        # track() 内部已兜底，这里是双保险：埋点失败不能拖垮业务
        log.warning(f"TAG_CREATE 埋点异常（忽略）: slug={tag_slug} err={exc}")

    await db.commit()
    # tag 影响用户看到的文章列表（按 tag 筛选），失效用户的待听列表缓存
    await cache_service.invalidate_pending(_uid(user))

    return {
        "id": tag_slug,
        "name": tag_name,
        "category": tag_category,
    }


@app.post("/api/v1/tags/{tag_id_or_slug}/subscribe")
async def subscribe_tag(
    tag_id_or_slug: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """用户订阅标签（CP5.3b）。tag_id_or_slug 接受 slug 或数字 id。"""
    if tag_id_or_slug.isdigit():
        tag = await db.get(Tag, int(tag_id_or_slug))
    else:
        tag = await db.scalar(select(Tag).where(Tag.slug == tag_id_or_slug))
    if not tag:
        raise NotFound(message=f"tag 不存在: {tag_id_or_slug}")

    existing = await db.scalar(
        select(TagSubscription).where(
            TagSubscription.user_id == _uid(user),
            TagSubscription.tag_id == tag.id,
        )
    )
    if existing:
        return {"ok": True, "already_subscribed": True}

    await cache_service.invalidate_pending(_uid(user))

    # 埋点失败时 track() 内部会 rollback（expire 掉 ORM 对象），故先取响应字段
    tag_id, tag_slug = tag.id, tag.slug

    sub = TagSubscription(user_id=_uid(user), tag_id=tag_id)
    db.add(sub)

    # track() 只 flush 不 commit —— 必须在 commit() 之前，否则埋点随 close() 回滚丢失
    try:
        await track(
            db,
            EventName.TAG_SUBSCRIBE,
            user_id=_uid(user),
            article_id="n/a",  # 标签事件无关联文章，但 feedback.article_id NOT NULL
            metadata={"tag_slug": tag_slug},
        )
    except Exception as exc:
        log.warning(f"TAG_SUBSCRIBE 埋点异常（忽略）: slug={tag_slug} err={exc}")

    await db.commit()

    return {"ok": True, "tag_id": tag_id, "tag_slug": tag_slug}


@app.post("/api/v1/tags/{tag_id_or_slug}/unsubscribe")
async def unsubscribe_tag(
    tag_id_or_slug: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """用户取消订阅标签（CP5.3b）。"""
    if tag_id_or_slug.isdigit():
        tag = await db.get(Tag, int(tag_id_or_slug))
    else:
        tag = await db.scalar(select(Tag).where(Tag.slug == tag_id_or_slug))
    if not tag:
        raise NotFound(message=f"tag 不存在: {tag_id_or_slug}")

    # 埋点失败时 track() 内部会 rollback（expire 掉 ORM 对象），故先取响应字段
    tag_id, tag_slug = tag.id, tag.slug

    result = await db.execute(
        delete(TagSubscription).where(
            TagSubscription.user_id == _uid(user),
            TagSubscription.tag_id == tag_id,
        )
    )
    if result.rowcount == 0:
        # 无订阅可删，直接返回；delete 未 commit 也无需回滚
        return {"ok": True, "already_unsubscribed": True}

    await cache_service.invalidate_pending(_uid(user))

    # track() 只 flush 不 commit —— 必须在 commit() 之前，否则埋点随 close() 回滚丢失
    try:
        await track(
            db,
            EventName.TAG_UNSUBSCRIBE,
            user_id=_uid(user),
            article_id="n/a",  # 标签事件无关联文章，但 feedback.article_id NOT NULL
            metadata={"tag_slug": tag_slug},
        )
    except Exception as exc:
        log.warning(f"TAG_UNSUBSCRIBE 埋点异常（忽略）: slug={tag_slug} err={exc}")

    await db.commit()

    return {"ok": True, "tag_id": tag_id, "tag_slug": tag_slug}


# ---------------------------------------------------------------------------
# 订阅内容流（2026-10-23）
# ---------------------------------------------------------------------------
#
# ## 为什么要有这个端点
#
# 订阅这件事此前**只有「写」，没有「读回」**：
#   · 写：`POST /api/v1/tags/{id}/subscribe` ✅ 有
#   · 读：「我订阅的标签下有哪些新内容」❌ **一个端点都没有**
#
# 而 `GET /api/v1/articles?tag=` 又被属主过滤限死（只返回**自己**的文章）——
# 那是文章列表的语义（防越权），不该拿它来承载「订阅带来的是**别人**的内容」。
#
# 结果就是：用户订阅了一个标签 → 订阅行写进库了 → **再没有任何地方能看到它**。
# 加上 agent 路径从不写 `distilled_articles.tags`（推送的唯一输入），
# 整条链路上「订阅」= 一个**永远没有回报的开关**。
#
# ## 口径
#
# · 只返回**已就绪**（status=done 且有 audio_url）的内容 —— 还没合成出来的
#   推给用户没意义（点开是空的）。
# · 包含**别人**蒸馏的文章 —— 这正是订阅的意义。
# · 自己的文章也包含（订阅自己的内容不奇怪，且方便确认订阅是否生效）。
#
# ## 鉴权
#
# 走 `require_user`（和订阅端点同一套）。**不要**在这里放宽详情页的属主校验 ——
# 那是防越权的红线；订阅者看摘要可以，点全文的可见性口径要单独出方案。


@app.get("/api/v1/subscriptions/feed")
async def subscription_feed(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """我订阅的标签下产生的文章（订阅内容流）。

    这是订阅闭环的**读回**一环：订阅 → 有内容 → 这里能看到。
    """
    uid = _uid(user)

    # 1) 我订阅的标签 → 名字（da.tags 存的是 name，推送也按 name 精确匹配）
    rows = await db.execute(
        select(Tag.name)
        .join(TagSubscription, TagSubscription.tag_id == Tag.id)
        .where(TagSubscription.user_id == uid)
    )
    tag_names = [r[0] for r in rows.fetchall()]
    if not tag_names:
        # 没订阅 → 空列表（不是 404：这是合法状态）
        return {"items": [], "total": 0, "tags": []}

    # 2) 这些标签下、已就绪的蒸馏结果
    #    `jsonb_exists_any(tags, text[])` = 「tags 数组里存在任一给定标签」。
    #
    # ⚠️ 右侧**必须显式 cast 成 text[]**：直接传 Python list 会被 SQLAlchemy
    # 渲染成 `jsonb ?| $2::JSONB` → Postgres 报
    # `operator does not exist: jsonb ?| jsonb`（实测踩过）。
    tag_array = cast(tag_names, ARRAY(String))
    tag_filter = func.jsonb_exists_any(DistilledArticle.tags, tag_array)

    count_stmt = (
        select(func.count())
        .select_from(DistilledArticle)
        .where(
            DistilledArticle.status == "done",
            DistilledArticle.deleted_at.is_(None),
            DistilledArticle.audio_url.isnot(None),
            tag_filter,
        )
    )
    total = (await db.execute(count_stmt)).scalar() or 0

    list_stmt = (
        select(DistilledArticle, Article)
        .join(Article, Article.id == DistilledArticle.article_id)
        .where(
            DistilledArticle.status == "done",
            DistilledArticle.deleted_at.is_(None),
            DistilledArticle.audio_url.isnot(None),
            tag_filter,
        )
        .order_by(DistilledArticle.created_at.desc(), DistilledArticle.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = await db.execute(list_stmt)

    items = []
    for da, art in rows.all():
        items.append(
            {
                "article_id": art.id,
                "title": art.title or "未命名",
                "source": art.source,
                "tags": da.tags if isinstance(da.tags, list) else [],
                "audio_url": da.audio_url,
                "duration_sec": da.duration_sec,
                "created_at": da.created_at.isoformat() if da.created_at else None,
                # 安卓 NavHost 注册的 deep link scheme（见 MainActivity 的 navDeepLink）
                "deeplink": f"stashbox://detail/{art.id}",
            }
        )

    return {"items": items, "total": int(total), "tags": tag_names}


# [已搁置 2026-10] tag_filter 埋点（原 CP5.3b）。
# 当时留给下一个人是因为 v1 §11.5 没写清 filter 的触发位置。现在正式搁置：
# 按 tag 筛选发生在前端本地（列表页 chip），后端 GET /api/v1/articles 收到的
# tag 参数是运营后台的筛选，不存在「用户点了某个 tag」这个可供归因的独立事件。
# 真要做，需要先定义清楚埋的是哪一次交互，再补一条 alembic 无关的 analytics 事件。
# 在那之前留一条 TODO 只会让 grep TODO 的人以为有活没干完。

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8102)
