"""CP-TTS-VOICE：音色库读写 + 用户偏好 + 蒸馏时的音色解析。

**为什么需要这一层**：IndexTTS 是零样本克隆，音色 = (参考音频, 参考文本) 对
（见 `app/services/tts/indextts.py:19`）。在本表出现之前，服务端只有全局那**一份**
`indextts_ref_audio`，所有用户共用一个音色，谁都换不了。

解析顺序（三级，逐级回退）：

    1. 用户在 App 里选的音色（user_tts_preferences.voice_id，指向 active 行）
    2. 全局默认音色（tts_voices.is_default = true）
    3. 全局 TTS 配置里的 indextts_ref_audio / indextts_ref_text

第 3 级是**必须保留的历史兜底**：管理员还没在后台建音色时，蒸馏链路要照常工作，
不能因为「音色库是空的」就全线失败 —— 那是「加了新功能把老功能搞坏」。

第 3 级命中时 `voice_id` 返回 None，调用方据此在 distilled_articles.tts_voice_id
留空（表示「用全局配置，来源不可溯源」），而不是回填一个假音色。

**不缓存音色解析结果**：一次蒸馏 20+ 分钟，解析只发生在开头；相比之下
缓存过期带来的「用户刚换音色却还用旧音色合成」更难排查。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from stashbox.backend.common.database import AsyncSessionLocal
from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.models import (
    MAX_PLAYBACK_SPEED,
    MIN_PLAYBACK_SPEED,
    TTSVoice,
    UserTTSPreference,
)
from stashbox.backend.common.models.tts_voice import DEFAULT_PLAYBACK_SPEEDS

log = get_logger(__name__)


class _Unset:
    """「这个参数没传」的哨兵。

    不用 None，因为 None 对 voice_id 有真实含义（显式清空 = 跟随默认音色）。
    见 `set_user_preference` 的三态说明。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # 调试日志里要能看出来
        return "<UNSET>"


UNSET = _Unset()

# 参考音频大小上限：IndexTTS 零样本克隆的参考音频通常 3~15s，
# 24kHz 单声道 16bit 约 1 分钟 ≈ 2.9MB。给到 10MB 足够宽松，
# 再大基本是误传了整篇文章音频（实测蒸馏产物就有 27MB）。
MAX_REF_AUDIO_BYTES = 10 * 1024 * 1024

# 字段长度上限，**必须与表结构对齐**。
#
# 2026-09-30 自测发现的坑：不在这里校验，超长值会一路捅到 PostgreSQL，
# 触发 `StringDataRightTruncationError: value too long for type character
# varying(64)`，而该异常没被映射成业务错误 → 接口返 **500 Internal Server
# Error**，用户只看到「服务器错误」，完全不知道是自己名字打太长。
# 管理后台的表单当时也一个 maxLength 都没有，很容易踩到。
MAX_SLUG_LEN = 64
MAX_DISPLAY_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 255
MAX_REF_AUDIO_URL_LEN = 512


def _check_len(value: str, limit: int, field: str) -> str:
    if len(value) > limit:
        raise VoiceError(f"{field}超长：{len(value)} 字，上限 {limit} 字")
    return value


class VoiceError(ValueError):
    """音色库 / 偏好的业务错误。调用方转成 4xx。"""


@dataclass(frozen=True)
class ResolvedVoice:
    """蒸馏时要用的音色。

    voice_id 为 None = 回落到全局 TTS 配置（来源不可溯源）。
    """

    ref_audio: str
    ref_text: str
    voice_id: str | None
    display_name: str | None
    source: str  # 'user' | 'default' | 'global_config'


def _new_voice_id() -> str:
    return f"ttsv_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# 音色库 CRUD
# ---------------------------------------------------------------------------
async def list_voices(*, include_inactive: bool = False) -> list[TTSVoice]:
    """列音色。App 侧只传 active；admin 侧传 True 看得到下架的。"""
    async with AsyncSessionLocal() as db:
        stmt = select(TTSVoice).where(TTSVoice.deleted_at.is_(None))
        if not include_inactive:
            stmt = stmt.where(TTSVoice.is_active.is_(True))
        stmt = stmt.order_by(TTSVoice.sort_order.asc(), TTSVoice.created_at.asc())
        rows = (await db.execute(stmt)).scalars().all()
        return list(rows)


async def get_voice(voice_id: str) -> TTSVoice | None:
    async with AsyncSessionLocal() as db:
        stmt = select(TTSVoice).where(TTSVoice.id == voice_id, TTSVoice.deleted_at.is_(None))
        return (await db.execute(stmt)).scalar_one_or_none()


async def get_voice_by_slug(slug: str) -> TTSVoice | None:
    async with AsyncSessionLocal() as db:
        stmt = select(TTSVoice).where(TTSVoice.slug == slug, TTSVoice.deleted_at.is_(None))
        return (await db.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True)
class VoiceBrief:
    """「这篇音频是谁念的」的对外形态（CP-TTS-VOICE 溯源）。

    `available=False` 表示该音色已下架或已软删。**仍要回名字**：
    这段音频确实是用它合成的，藏掉名字等于让历史音频变成「来源不明」，
    比标一个「已下架」更糟（用户会以为是自己记错了）。
    """

    id: str
    name: str
    available: bool


async def get_voice_briefs(voice_ids) -> dict[str, VoiceBrief]:
    """按 id 批量取音色**溯源**信息，**不过滤软删**。

    与 [get_voice] 的区别是刻意的：那个是「现在能不能选」，这个是「当时是谁合成的」。
    混用会让管理员删掉音色后，历史文章的来源显示凭空消失。

    入参为空直接返回空字典，避免为常见路径（大量文章用全局配置，voice_id 为 None）
    白跑一次 `IN ()` 查询。
    """
    ids = {v for v in voice_ids if v}
    if not ids:
        return {}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(TTSVoice).where(TTSVoice.id.in_(ids)))).scalars().all()
        return {
            v.id: VoiceBrief(
                id=v.id,
                name=v.display_name,
                available=bool(v.is_active and v.deleted_at is None),
            )
            for v in rows
        }


async def create_voice(
    *,
    slug: str,
    display_name: str,
    ref_audio: str,
    ref_text: str,
    description: str | None = None,
    is_default: bool = False,
    is_active: bool = True,
    sort_order: int = 0,
) -> TTSVoice:
    slug = (slug or "").strip().lower()
    display_name = (display_name or "").strip()
    ref_audio = (ref_audio or "").strip()
    ref_text = (ref_text or "").strip()

    if not slug:
        raise VoiceError("slug 不能为空")
    if not display_name:
        raise VoiceError("音色名称不能为空")
    if not ref_audio:
        raise VoiceError("参考音频不能为空（填本地路径或 URL）")
    if not ref_text:
        # 不是可选项：参考文本必须与音频内容一致，否则克隆出的音色会念错
        raise VoiceError("参考文本不能为空，且必须与参考音频内容一致")

    # 长度校验必须在入库前 —— 超长值捅到 PG 会抛 StringDataRightTruncationError，
    # 那个异常没被映射成业务错误，用户只会看到 500（见 MAX_SLUG_LEN 处的说明）
    _check_len(slug, MAX_SLUG_LEN, "音色标识")
    _check_len(display_name, MAX_DISPLAY_NAME_LEN, "音色名称")
    _check_len(ref_audio, MAX_REF_AUDIO_URL_LEN, "参考音频地址")
    if description:
        _check_len(description.strip(), MAX_DESCRIPTION_LEN, "描述")

    async with AsyncSessionLocal() as db:
        if (await db.execute(select(TTSVoice).where(TTSVoice.slug == slug))).scalar_one_or_none():
            raise VoiceError(f"音色标识 {slug!r} 已存在")

        voice = TTSVoice(
            id=_new_voice_id(),
            slug=slug,
            display_name=display_name,
            description=(description or "").strip() or None,
            ref_audio_url=ref_audio,
            ref_text=ref_text,
            is_default=False,  # 下面统一走 _apply_default，保证「至多一条」
            is_active=is_active,
            sort_order=sort_order,
        )
        db.add(voice)
        if is_default:
            await _apply_default(db, voice)
        await db.commit()
        await db.refresh(voice)
        return voice


async def update_voice(
    voice_id: str,
    *,
    display_name: str | None = None,
    ref_audio: str | None = None,
    ref_text: str | None = None,
    description: str | None = None,
    is_active: bool | None = None,
    is_default: bool | None = None,
    sort_order: int | None = None,
) -> TTSVoice:
    async with AsyncSessionLocal() as db:
        voice = (
            await db.execute(
                select(TTSVoice).where(TTSVoice.id == voice_id, TTSVoice.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if voice is None:
            raise VoiceError(f"音色不存在: {voice_id}")

        if display_name is not None:
            name = display_name.strip()
            if not name:
                raise VoiceError("音色名称不能为空")
            _check_len(name, MAX_DISPLAY_NAME_LEN, "音色名称")
            voice.display_name = name
        if ref_audio is not None:
            audio = ref_audio.strip()
            if not audio:
                raise VoiceError("参考音频不能为空")
            _check_len(audio, MAX_REF_AUDIO_URL_LEN, "参考音频地址")
            voice.ref_audio_url = audio
        if ref_text is not None:
            text = ref_text.strip()
            if not text:
                raise VoiceError("参考文本不能为空")
            voice.ref_text = text
        if description is not None:
            voice.description = description.strip() or None
        if is_active is not None:
            voice.is_active = is_active
        if sort_order is not None:
            voice.sort_order = sort_order
        if is_default is not None:
            if is_default:
                await _apply_default(db, voice)
            elif voice.is_default:
                # 取消默认：允许出现「没有默认音色」的状态，
                # 解析链会退到全局配置，不会挂
                voice.is_default = False

        await db.commit()
        await db.refresh(voice)
        return voice


async def delete_voice(voice_id: str) -> None:
    """软删。

    引用它的用户偏好行靠 FK `ON DELETE SET NULL` 不会级联删 —— 但软删不动 FK，
    所以这里显式把引用置空，让用户立刻回落默认音色，而不是下次打开 App
    看到一个指向已删音色的选中态。
    """
    async with AsyncSessionLocal() as db:
        voice = (
            await db.execute(
                select(TTSVoice).where(TTSVoice.id == voice_id, TTSVoice.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if voice is None:
            raise VoiceError(f"音色不存在: {voice_id}")
        voice.deleted_at = _now()
        voice.is_active = False
        await db.execute(
            update(UserTTSPreference)
            .where(UserTTSPreference.voice_id == voice_id)
            .values(voice_id=None)
        )
        await db.commit()


async def _apply_default(db, voice: TTSVoice) -> None:
    """把 voice 设为唯一默认。必须在调用方的事务里执行。

    ⚠️ 这里**不能**再 `select` 一次 voice：create_voice 里调用本函数时新行
    还没 flush，SELECT 查不到自己，会误报「音色不存在」（实测 import 按钮
    直接 400）。两个调用方手上都已经持有 ORM 对象，直接改它的字段即可 ——
    顺带也少一次查询。
    """
    await db.execute(update(TTSVoice).values(is_default=False))
    voice.is_default = True


# ---------------------------------------------------------------------------
# 用户偏好
# ---------------------------------------------------------------------------
def _validate_speed(speed: float) -> Decimal:
    try:
        val = Decimal(str(round(float(speed), 2)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VoiceError(f"语速值非法: {speed!r}") from exc
    if not (Decimal(str(MIN_PLAYBACK_SPEED)) <= val <= Decimal(str(MAX_PLAYBACK_SPEED))):
        raise VoiceError(f"语速必须在 {MIN_PLAYBACK_SPEED}~{MAX_PLAYBACK_SPEED} 之间，收到 {val}")
    return val


async def get_user_preference(user_id: int) -> UserTTSPreference | None:
    async with AsyncSessionLocal() as db:
        stmt = select(UserTTSPreference).where(UserTTSPreference.user_id == user_id)
        return (await db.execute(stmt)).scalar_one_or_none()


async def set_user_preference(
    user_id: int, *, voice_id: Any = UNSET, speed: float | None = None
) -> UserTTSPreference:
    """upsert 用户偏好（部分更新）。

    `voice_id` 的三态靠 `UNSET` 哨兵区分，缺一不可：

        不传（UNSET）      → 不动音色
        传 None           → 显式清空 = 跟随默认音色
        传 "ttsv_xxx"     → 切到该音色

    区分不了就会出现两种静默 bug：显式传 null 清不掉（用户点了「跟随默认」
    但下次进来还是旧音色），或者没传也被清掉（只改语速顺手把音色丢了）。

    `speed` 传 None = 不动，与 `voice_id` 传 UNSET 同义；语速没有「显式置空」
    的语义（空值不合法），所以用 None 表达不动是安全的。

    ⚠️ **必须用 `ON CONFLICT DO UPDATE` 原子 upsert，不能读-改-写**（2026-09-30
    自由拓展自测发现的 BUG#5）：`user_tts_preferences` 的 PK 就是 user_id，所以
    「首次建偏好」这一步天然是竞态 —— 两个并发请求都会 SELECT 到 None，然后都走
    INSERT，其中一个撞 `duplicate key value violates unique constraint
    "user_tts_preferences_pkey"`，未映射的 IntegrityError 直接变成 **500**。
    实测 6 个并发首写，稳定有 4~5 个 500。

    这不是纯理论：新用户第一次进设置页恰恰是手势最密的时候（点语速 + 点音色可能
    连着来），而「首次写」只在那一刻发生，所以低频却很难复现。改用单条 upsert 后
    读-改-写窗口一并消失，冲突方变成「后写覆盖前写」，符合直觉。
    """
    if voice_id is not UNSET and voice_id is not None:
        voice = await get_voice(voice_id)
        if voice is None:
            raise VoiceError(f"音色不存在: {voice_id}")
        if not voice.is_active:
            # 让 App 拿到 4xx 而不是静默存一个选不上的音色
            raise VoiceError(f"音色已下架: {voice.display_name}")

    validated = _validate_speed(speed) if speed is not None else None

    # DO UPDATE 只写本次真正要改的列 —— 不传的字段不进 set_，天然按列隔离，
    # 并发改音色和改语速不会互相覆盖。
    updates: dict[str, Any] = {}
    if voice_id is not UNSET:
        updates["voice_id"] = voice_id
    if validated is not None:
        updates["speed"] = validated
    if not updates:
        raise VoiceError("至少要更新 voice_id 或 speed 之一")
    updates["updated_at"] = func.now()

    stmt = insert(UserTTSPreference).values(
        user_id=user_id,
        # 插入路径要给全 NOT NULL 列：没传的字段落到这里的默认值
        voice_id=None if voice_id is UNSET else voice_id,
        speed=validated if validated is not None else Decimal("1.00"),
    )

    async with AsyncSessionLocal() as db:
        # 单条语句完成「存在就改、不存在就插」，RETURNING 把结果一起带回来。
        # 不在 commit 后再 SELECT 一次：那样虽然也对，但会让人误以为这里是
        # 读-改-写（回归测试就是照「INSERT 之前不能有 SELECT」来断言的）。
        row = (
            await db.execute(
                stmt.on_conflict_do_update(index_elements=["user_id"], set_=updates).returning(
                    UserTTSPreference.user_id,
                    UserTTSPreference.voice_id,
                    UserTTSPreference.speed,
                    UserTTSPreference.created_at,
                    UserTTSPreference.updated_at,
                )
            )
        ).one()
        await db.commit()

    # 用 transient 实例而不是 ORM 实体：commit 后实体是 expired 状态，
    # 返回出去在调用方读属性时会炸 detached instance
    return UserTTSPreference(
        user_id=row.user_id,
        voice_id=row.voice_id,
        speed=row.speed,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _now():
    from datetime import datetime

    return datetime.utcnow()


# ---------------------------------------------------------------------------
# 蒸馏时的音色解析
# ---------------------------------------------------------------------------
async def resolve_voice_for_user(user_id: int | None) -> ResolvedVoice:
    """解析这个用户蒸馏时该用哪个音色。

    三级回退见模块 docstring。**永不抛异常** —— 解析失败要退到全局配置，
    不能因为「用户偏好行指向一个坏音色」就让整个蒸馏任务失败。
    """
    # 1) 用户显式选的
    if user_id is not None:
        try:
            pref = await get_user_preference(user_id)
        except Exception as exc:  # 偏好表读失败不该拖垮蒸馏
            log.warning("tts_voice_pref_read_failed", user_id=user_id, error=str(exc))
            pref = None
        if pref is not None and pref.voice_id:
            voice = await get_voice(pref.voice_id)
            # 软删 / 下架的音色不算数 → 继续往下回退
            if voice is not None and voice.is_active:
                return ResolvedVoice(
                    ref_audio=voice.ref_audio_url,
                    ref_text=voice.ref_text,
                    voice_id=voice.id,
                    display_name=voice.display_name,
                    source="user",
                )
            log.info(
                "tts_voice_user_pref_invalid_fallback",
                user_id=user_id,
                voice_id=pref.voice_id,
            )

    # 2) 全局默认音色
    try:
        async with AsyncSessionLocal() as db:
            stmt = (
                select(TTSVoice)
                .where(
                    TTSVoice.is_default.is_(True),
                    TTSVoice.is_active.is_(True),
                    TTSVoice.deleted_at.is_(None),
                )
                .limit(1)
            )
            default = (await db.execute(stmt)).scalar_one_or_none()
    except Exception as exc:
        log.warning("tts_voice_default_read_failed", error=str(exc))
        default = None
    if default is not None:
        return ResolvedVoice(
            ref_audio=default.ref_audio_url,
            ref_text=default.ref_text,
            voice_id=default.id,
            display_name=default.display_name,
            source="default",
        )

    # 3) 全局 TTS 配置（历史兜底）
    from stashbox.backend.app.services.tts import current_config

    cfg = await current_config()
    return ResolvedVoice(
        ref_audio=cfg.get("indextts_ref_audio") or "",
        ref_text=cfg.get("indextts_ref_text") or "",
        voice_id=None,
        display_name=None,
        source="global_config",
    )


def available_speeds() -> list[float]:
    """App 可选语速档位。服务端单一事实源，App 不再硬编码。"""
    return list(DEFAULT_PLAYBACK_SPEEDS)
