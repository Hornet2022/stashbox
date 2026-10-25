"""CP-TTS-VOICE：音色库 + 用户音色/语速偏好 的 HTTP 端点。

分两组：

**用户端**（`require_user`，安卓 App 用）
    GET  /api/v1/tts/voices                 音色列表 + 可选语速档位
    GET  /api/v1/users/me/tts-preference    我选的音色 + 我的语速
    PUT  /api/v1/users/me/tts-preference    改（部分更新）

**管理端**（`require_admin_or_operator`，admin-web 用）
    GET    /api/v1/admin/tts/voices                  列表（含已下架）
    POST   /api/v1/admin/tts/voices                  新建
    PUT    /api/v1/admin/tts/voices/{voice_id}       更新
    DELETE /api/v1/admin/tts/voices/{voice_id}       软删
    POST   /api/v1/admin/tts/voices/{voice_id}/preview  试听（真合成一段）
    POST   /api/v1/admin/tts/voices/import-from-config  从当前 TTS 配置导入为音色

⚠️ 语速存服务端只为**多端同步**，实际变速由客户端 ExoPlayer 做，不影响已生成的
音频 —— 所以用户调语速**不会**触发重跑蒸馏（单篇实测约 23 分钟，不能拿来当
「改个设置就等一刻钟」）。音色相反：音色是合成期参数，换音色想作用于存量文章
必须走重生成入口。

⚠️ 上传走 **base64 JSON** 而不是 multipart：admin-web 的 axios 实例硬编码了
`Content-Type: application/json`（`src/api/client.ts:43-45`），FormData 要逐请求
覆盖 header，是这个项目里没有先例的写法。base64 让两端都只 dealing JSON，
代价是体积膨胀 33% —— 参考音频通常 3~15s（约 0.3~1.5MB），可接受。
"""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.common.auth import require_user
from stashbox.backend.common.auth_admin import require_admin_or_operator
from stashbox.backend.common.database import get_db
from stashbox.backend.common.exceptions import InvalidRequest, NotFound
from stashbox.backend.common.logging import get_logger
from stashbox.backend.common.models import AdminOperationLog, Article, DistilledArticle
from clients.ai_client import get_ai_client  # noqa: E402

from stashbox.backend.common.tts_voice_service import (
    MAX_REF_AUDIO_BYTES,
    UNSET,
    ResolvedVoice,
    VoiceError,
    available_speeds,
    create_voice,
    delete_voice,
    get_voice,
    get_user_preference,
    list_voices,
    resolve_voice_for_user,
    set_user_preference,
    update_voice,
    voice_usage_counts,
)

log = get_logger(__name__)


def _uid(user: dict) -> int:
    """与 content-service/main.py 的同名函数同口径：从 sub 解析，失败兜底 0。

    require_user / require_admin_or_operator 返回的 payload 没有 id 字段。
    """
    try:
        return int(user["sub"])
    except (KeyError, ValueError, TypeError):
        return 0


router = APIRouter()


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
class VoiceOut(BaseModel):
    """音色对外形态。

    刻意**不吐 ref_audio_url**：那是内部存储地址，普通用户没有理由知道，
    也不该能拿去直接下载别人的参考音频文件。
    """

    id: str
    slug: str
    display_name: str
    description: Optional[str] = None
    is_default: bool = False


class VoiceListOut(BaseModel):
    voices: list[VoiceOut]
    #: 服务端单一事实源 —— App 不再硬编码档位，改这里就改全端行为
    available_speeds: list[float]
    default_speed: float = 1.0


class TTSPreferenceOut(BaseModel):
    voice_id: Optional[str] = None
    voice_name: Optional[str] = None
    speed: float = 1.0
    available_speeds: list[float] = Field(default_factory=available_speeds)
    #: 实际生效的音色（偏好 → 默认 → 全局配置的逐级回退结果）
    effective_voice_name: Optional[str] = None
    effective_source: str = "global_config"


class TTSPreferenceUpdate(BaseModel):
    """部分更新：不传的字段保持不变。

    voice_id 显式传 null = 「跟随默认音色」；不传该字段 = 不动。
    靠 pydantic 的 model_fields_set 区分这两种情况 —— 这是本项目
    TTSConfigUpdate 已经用过的约定（见 admin_router.py:786）。
    """

    voice_id: Optional[str] = None
    speed: Optional[float] = None


class AdminVoiceOut(VoiceOut):
    """管理端多吐几项：参考音频地址、参考文本、上下架、排序、**有没有人在用**。"""

    ref_audio_url: str
    ref_text: str
    is_active: bool = True
    sort_order: int = 0
    updated_at: Optional[str] = None
    #: 被多少用户选中（`user_tts_preferences` 计数）。
    #:
    #: 为什么要有它：音色库页原来只能看到「这个音色存在」，看不到「**有人用吗**」。
    #: 生产实测 1 条音色、2 行用户偏好但 `voice_id` 全为 NULL ——
    #: 管理员完全看不出「用户其实没在选音色」。
    selected_users: int = 0
    #: 是否**真的**会用于新剪藏的文章：默认音色，或有用户选了它。
    #:
    #: ⚠️ 这条是「**新建音色默认不生效**」那个坑的显式化：
    #: 新建时 `is_default` 默认 false、也没有用户选它 ⇒ 解析链回落到默认音色
    #: ⇒ 这个音色建完**根本不会被用上**，而界面毫无提示。
    #: 现在管理端能算出这个标记，列表里直接标「未启用」。
    is_effective: bool = False


class VoiceCreateIn(BaseModel):
    slug: str
    display_name: str
    #: 二选一：直接给路径/URL，或 base64 上传
    ref_audio_url: Optional[str] = None
    ref_audio_b64: Optional[str] = None
    ref_text: str
    description: Optional[str] = None
    is_default: bool = False
    is_active: bool = True
    sort_order: int = 0


class VoiceUpdateIn(BaseModel):
    display_name: Optional[str] = None
    ref_audio_url: Optional[str] = None
    ref_audio_b64: Optional[str] = None
    ref_text: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    sort_order: Optional[int] = None


class VoicePreviewIn(BaseModel):
    text: str = "这是一段试听文本，用来确认这个音色的效果。"


class VoiceDeleteIn(BaseModel):
    """删除音色的原因（写入审计日志）。

    与 tag / article 的破坏性操作同口径：必填、≥5 字符、落 admin_operation_logs。
    """

    reason: str = Field(..., min_length=1, description="删除原因，会写入审计日志")


class VoicePreviewOut(BaseModel):
    audio_url: str
    bytes_len: int
    duration_sec: int
    voice_id: str


class ImportFromConfigOut(BaseModel):
    voice: AdminVoiceOut
    created: bool = True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _to_out(v) -> VoiceOut:
    return VoiceOut(
        id=v.id,
        slug=v.slug,
        display_name=v.display_name,
        description=v.description,
        is_default=v.is_default,
    )


def _to_admin_out(v, selected_users: int = 0) -> AdminVoiceOut:
    return AdminVoiceOut(
        id=v.id,
        slug=v.slug,
        display_name=v.display_name,
        description=v.description,
        is_default=v.is_default,
        ref_audio_url=v.ref_audio_url,
        ref_text=v.ref_text,
        is_active=v.is_active,
        sort_order=v.sort_order,
        updated_at=v.updated_at.isoformat() if v.updated_at else None,
        selected_users=selected_users,
        # 「生效」= 默认音色 **或** 有用户选了它。两者都没有 ⇒ 建了但不会被用上。
        is_effective=bool(v.is_default) or selected_users > 0,
    )


async def _resolve_ref_audio_url(ref_audio_url: Optional[str], ref_audio_b64: Optional[str]) -> str:
    """两条输入路径归一：直接给地址，或 base64 上传后落存储。

    两者都没给 → InvalidRequest（让 create_voice 的必填校验统一报）。
    """
    if ref_audio_b64:
        return await _save_uploaded_audio(ref_audio_b64)
    if ref_audio_url:
        return ref_audio_url.strip()
    raise InvalidRequest(message="需要提供 ref_audio_url 或 ref_audio_b64 之一")


async def _save_uploaded_audio(raw_b64: str) -> str:
    """base64 wav → 存储 → 返回可读 URL。

    复用蒸馏产物那套 Storage 抽象（`app/services/storage/`），所以音色音频
    和成稿音频落在同一个 bucket，不需要为音色单独配一套凭证。
    """
    payload = raw_b64.split(",", 1)[-1] if "," in raw_b64 else raw_b64
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidRequest(message=f"参考音频不是合法 base64: {exc}") from exc

    if len(data) < 1000:
        raise InvalidRequest(
            message=f"参考音频太小（{len(data)} 字节），至少需要 1KB 才可能是有效 wav"
        )
    if len(data) > MAX_REF_AUDIO_BYTES:
        raise InvalidRequest(
            message=(
                f"参考音频过大（{len(data) // 1024 // 1024}MB），"
                f"上限 {MAX_REF_AUDIO_BYTES // 1024 // 1024}MB。"
                "零样本克隆的参考音频通常 3~15 秒，请裁剪后再传"
            )
        )
    if data[:4] != b"RIFF":
        raise InvalidRequest(message="参考音频必须是 wav（RIFF 头）")

    from stashbox.backend.app.services.storage import get_storage

    key = f"voices/{uuid.uuid4().hex}.wav"
    try:
        url = await get_storage().save(key, data, content_type="audio/wav")
    except Exception as exc:
        log.warning("tts_voice_upload_failed", key=key, error=str(exc))
        raise InvalidRequest(message=f"参考音频上传失败: {exc}") from exc
    log.info("tts_voice_uploaded", key=key, bytes=len(data))
    return url


# ---------------------------------------------------------------------------
# 用户端
# ---------------------------------------------------------------------------
@router.get("/api/v1/tts/voices", response_model=VoiceListOut)
async def list_available_voices(user: dict = Depends(require_user)):
    """可选购音色列表（只含已上架的）。

    连同 `available_speeds` 一起给：语速档位是服务端单一事实源，
    App 不该硬编码 —— 后台改了这里就改了全端行为。
    """
    voices = await list_voices(include_inactive=False)
    return VoiceListOut(
        voices=[_to_out(v) for v in voices],
        available_speeds=available_speeds(),
        default_speed=1.0,
    )


@router.get("/api/v1/users/me/tts-preference", response_model=TTSPreferenceOut)
async def get_my_tts_preference(user: dict = Depends(require_user)):
    """我选的音色 + 我的语速。

    同时回 `effective_voice_name` / `effective_source`：用户可能从没设过偏好，
    这时实际生效的是全局默认音色或全局配置 —— App 应该显示**实际生效的那个**，
    否则用户会看到「未选择」但听到的是某个音色，典型的假闭环。

    ⚠️ **已下架 / 已软删的音色要当没选**（2026-09-30 自测发现的假闭环）：
    管理员把用户选中的音色下架后，`resolve_voice_for_user` 会跳过它回落到
    默认/全局配置，但本端点原先照样回它的 `display_name` ——
    App 于是显示「待下架音色」，可实际生效的根本不是它，而且它也不在
    `GET /api/v1/tts/voices` 的可选列表里，Sheet 中没有对应行可点。
    用户看到的是一个自己选不了、也没在生效的音色，音频还和显示的不一样。
    现在 `voice_id` / `voice_name` 一起置空，如实回落到 `effective_*`。
    """
    user_id = int(user["sub"])
    pref = await get_user_preference(user_id)
    resolved: ResolvedVoice = await resolve_voice_for_user(user_id)

    voice_id: Optional[str] = None
    voice_name: Optional[str] = None
    if pref is not None and pref.voice_id:
        voice = await get_voice(pref.voice_id)
        if voice is not None and voice.is_active:
            voice_id = voice.id
            voice_name = voice.display_name
        # 查不到 / 已下架 → 保持 None，等同「跟随默认」，与解析链的行为一致

    return TTSPreferenceOut(
        voice_id=voice_id,
        voice_name=voice_name,
        speed=float(pref.speed) if pref is not None else 1.0,
        available_speeds=available_speeds(),
        effective_voice_name=resolved.display_name,
        effective_source=resolved.source,
    )


@router.put("/api/v1/users/me/tts-preference", response_model=TTSPreferenceOut)
async def update_my_tts_preference(req: TTSPreferenceUpdate, user: dict = Depends(require_user)):
    """改音色 / 语速。

    **不会触发重跑蒸馏**：语速是播放端行为；音色只对之后新剪藏的文章生效，
    存量文章要换音色得走重生成入口（见 re-distill 端点）。
    """
    user_id = int(user["sub"])
    fields = req.model_fields_set
    if "voice_id" not in fields and "speed" not in fields:
        raise InvalidRequest(message="至少要传 voice_id 或 speed 之一")

    try:
        await set_user_preference(
            user_id,
            # UNSET 哨兵：区分「没传 voice_id（不动）」和「传了 null（跟随默认）」。
            # 用 model_fields_set 判断字段在不在，用值本身表达三态 ——
            # 少了这层区分，用户点「跟随默认音色」会静默不生效。
            voice_id=req.voice_id if "voice_id" in fields else UNSET,
            speed=req.speed if "speed" in fields else None,
        )
    except VoiceError as exc:
        raise InvalidRequest(message=str(exc)) from exc

    return await get_my_tts_preference(user)


# ---------------------------------------------------------------------------
# 管理端
# ---------------------------------------------------------------------------
@router.get("/api/v1/admin/tts/voices")
async def admin_list_voices(user: dict = Depends(require_admin_or_operator)):
    """音色列表（含已下架）。

    额外带 `selected_users`（被多少用户选中）与 `is_effective`
    （是否**真的**会用于新剪藏）—— 见 `AdminVoiceOut` 的注释：
    没有这两个字段时，「建了但没生效」与「没人选过音色」在界面上完全看不出来。
    """
    rows = await list_voices(include_inactive=True)
    usage = await voice_usage_counts()
    return {"items": [_to_admin_out(v, usage.get(v.id, 0)).model_dump() for v in rows]}


@router.post("/api/v1/admin/tts/voices")
async def admin_create_voice(req: VoiceCreateIn, user: dict = Depends(require_admin_or_operator)):
    ref_audio = await _resolve_ref_audio_url(req.ref_audio_url, req.ref_audio_b64)
    try:
        voice = await create_voice(
            slug=req.slug,
            display_name=req.display_name,
            ref_audio=ref_audio,
            ref_text=req.ref_text,
            description=req.description,
            is_default=req.is_default,
            is_active=req.is_active,
            sort_order=req.sort_order,
        )
    except VoiceError as exc:
        raise InvalidRequest(message=str(exc)) from exc
    return _to_admin_out(voice).model_dump()


@router.put("/api/v1/admin/tts/voices/{voice_id}")
async def admin_update_voice(
    voice_id: str, req: VoiceUpdateIn, user: dict = Depends(require_admin_or_operator)
):
    fields = req.model_fields_set
    ref_audio = None
    if "ref_audio_b64" in fields and req.ref_audio_b64:
        ref_audio = await _save_uploaded_audio(req.ref_audio_b64)
    elif "ref_audio_url" in fields and req.ref_audio_url:
        ref_audio = req.ref_audio_url.strip()

    try:
        voice = await update_voice(
            voice_id,
            display_name=req.display_name if "display_name" in fields else None,
            ref_audio=ref_audio,
            ref_text=req.ref_text if "ref_text" in fields else None,
            description=req.description if "description" in fields else None,
            is_active=req.is_active if "is_active" in fields else None,
            is_default=req.is_default if "is_default" in fields else None,
            sort_order=req.sort_order if "sort_order" in fields else None,
        )
    except VoiceError as exc:
        raise InvalidRequest(message=str(exc)) from exc
    return _to_admin_out(voice).model_dump()


@router.delete("/api/v1/admin/tts/voices/{voice_id}")
async def admin_delete_voice(
    voice_id: str,
    req: VoiceDeleteIn,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """删除音色。

    reason ≥5 字符必填并写审计日志，与 tag / article 的破坏性操作口径一致。

    此前这个端点不收任何 body、也不写 admin_operation_logs：删除一个**默认音色**
    会让全平台用户静默降级到全局 TTS 配置，而事后审计日志里查不到是谁删的、
    为什么删。tags / articles / quota 都有 reason，只有音色库是例外。
    """
    if len(req.reason.strip()) < 5:
        raise InvalidRequest(message="reason 至少 5 个字符")

    try:
        await delete_voice(voice_id)
    except VoiceError as exc:
        raise NotFound(message=str(exc)) from exc

    log_row = AdminOperationLog(
        admin_id=_uid(user),
        admin_tier=user.get("tier", "unknown"),
        action="delete_voice",
        target_type="tts_voice",
        target_id=voice_id,
        reason=req.reason,
        method="DELETE",
        path=f"/api/v1/admin/tts/voices/{voice_id}",
        request_body={"reason": req.reason},
        response_status=200,
    )
    db.add(log_row)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 —— 审计写失败不掩盖已完成的删除
        log.warning("delete_voice 审计写入失败 voice_id=%s err=%s", voice_id, exc)
        await db.rollback()

    return {"deleted": voice_id}


@router.post("/api/v1/admin/tts/voices/{voice_id}/preview", response_model=VoicePreviewOut)
async def admin_preview_voice(
    voice_id: str,
    req: VoicePreviewIn,
    user: dict = Depends(require_admin_or_operator),
):
    """试听：拿这个音色的 ref_audio 真合成一段。

    真调 oMLX 而不是假装成功 —— 音色库里最容易翻车的是「参考音频和参考文本
    对不上」「音频根本不是有效 wav」，这两个只有真合成一次才会暴露。

    ⚠️ 2026-10-24：openai provider 现在也支持按调用覆盖 ref_audio/ref_text
    （零样本克隆），所以这条路径**不再只对 indextts 生效** ——
    之前这里对 openai 会 TypeError → 400「音色库只对 indextts 生效」。
    """
    voice = await get_voice(voice_id)
    if voice is None:
        raise NotFound(message=f"音色不存在: {voice_id}")

    from stashbox.backend.app.services.tts import reload as tts_reload

    client = await tts_reload()
    try:
        audio = await client.synthesize(
            req.text,
            voice=getattr(client, "voice", None),
            ref_audio=voice.ref_audio_url,
            ref_text=voice.ref_text,
        )
    except TypeError:
        # provider 的 synthesize() 没有 ref_audio/ref_text 形参
        raise InvalidRequest(
            message=(f"当前 TTS provider（{client.provider_name}）不支持按音色试听")
        )
    except Exception as exc:
        log.warning("tts_voice_preview_failed", voice_id=voice_id, error=str(exc))
        raise InvalidRequest(message=f"试听合成失败: {exc}") from exc

    from stashbox.backend.app.services.storage import get_storage

    # ⚠️ 扩展名/时长要跟**实际产物**对上：
    #   · provider 默认出 mp3，硬写成 .wav 会让客户端按 wav 解析
    #   · 时长用 `len(audio) // 32000`（假设 32kbps CBR）对不上
    #     → 生产实际是 55kbps，时长被低估 1.7 倍；
    #     短试听（几秒）更是直接算出 0。
    from stashbox.backend.app.services.tts.audio_duration import audio_duration_sec

    is_wav = audio[:4] == b"RIFF"
    ext = "wav" if is_wav else "mp3"
    content_type = "audio/wav" if is_wav else "audio/mpeg"
    key = f"voices/preview_{uuid.uuid4().hex}.{ext}"
    url = await get_storage().save(key, audio, content_type=content_type)
    return VoicePreviewOut(
        audio_url=url,
        bytes_len=len(audio),
        duration_sec=audio_duration_sec(audio) or 0,
        voice_id=voice_id,
    )


@router.post("/api/v1/admin/tts/voices/import-from-config", response_model=ImportFromConfigOut)
async def admin_import_voice_from_config(
    user: dict = Depends(require_admin_or_operator),
):
    """把当前全局 TTS 配置里的参考音频收编成音色库第一条。

    存在的理由：迁移 0033 **刻意没有 seed 默认音色** —— 迁移里读 env 会把本机
    绝对路径固化进库，换台机器就是死路径。管理员点一次这个按钮，就能在不敲
    任何路径的前提下完成冷启动。

    ⚠️ 2026-10-25：原先这里**只认** `indextts_ref_audio`，而生产的 provider 是
    **openai**（托管端点）、配置里根本没有这个键 ⇒ 这个按钮在生产上**必然 400**
    （死按钮）。现在两个 provider 的键都读，并给出可操作的提示。
    """
    from stashbox.backend.app.services.tts import current_config

    cfg = await current_config()
    # ⚠️ 托管端点（openai）用的是 openai_ref_audio/openai_ref_text，
    # 本地零样本克隆（indextts）用的是 indextts_ref_audio/indextts_ref_text。
    ref_audio = (cfg.get("indextts_ref_audio") or "").strip() or (
        cfg.get("openai_ref_audio") or ""
    ).strip()
    ref_text = (cfg.get("indextts_ref_text") or "").strip() or (
        cfg.get("openai_ref_text") or ""
    ).strip()
    if not ref_audio:
        raise InvalidRequest(
            message=(
                "当前 TTS 配置没有参考音频，无法导入。"
                "请先在「TTS 设置」页填「参考音频地址」与「参考文本」"
                "（托管端点用 openai_*，本地零样本克隆用 indextts_*），再点一次导入。"
            )
        )
    if not ref_text:
        raise InvalidRequest(
            message="当前 TTS 配置没有参考文本，无法导入。参考文本必须与参考音频内容逐字一致。"
        )

    existing = await list_voices(include_inactive=True)
    for v in existing:
        if v.ref_audio_url == ref_audio:
            # 已经收编过了 —— 幂等返回，别造重复行
            return ImportFromConfigOut(voice=_to_admin_out(v), created=False)

    # ⚠️ slug 不能写死 "default"：第二次导入（参考音频换了、或第一次导的是别的源）
    # 会撞「音色标识 'default' 已存在」⇒ **导入功能第二次就废了**。
    # 这里取一个没被占用的 slug。
    taken = {v.slug for v in existing}
    slug = "default"
    if slug in taken:
        slug = f"default-{uuid.uuid4().hex[:6]}"

    try:
        voice = await create_voice(
            slug=slug,
            display_name="默认音色",
            ref_audio=ref_audio,
            ref_text=ref_text,
            description="由当前 TTS 全局配置导入",
            is_default=True,
            sort_order=0,
        )
    except VoiceError as exc:
        raise InvalidRequest(message=str(exc)) from exc
    return ImportFromConfigOut(voice=_to_admin_out(voice), created=True)


# ---------------------------------------------------------------------------
# 存量文章换音色（补闭环的最后一环）
# ---------------------------------------------------------------------------
#
# ## 为什么需要这个端点
#
# 音色是**合成期参数**：换了默认音色，已落盘的 32 段音频**不会变**。
# 而重跑脚本 `backend/scripts/redistill_voices.py` 的默认筛选是
# 「`tts_voice_id` 为空」⇒ 生产上 32 篇全非空 ⇒ 脚本默认跑 **0 篇**。
# ⇒ 结果就是「后台能换音色，但没有任何一条路能把存量换成新音色」。
#
# 这个端点把最后一环补上：把目标音色**设为默认** + 把存量文章重新入队蒸馏。
#
# ## 为什么要「设为默认」而不是「这篇文章单独指定音色」
#
# 蒸馏链路解析音色的顺序是「用户偏好 → 默认音色 → 全局配置」
# （`common/tts_voice_service.py::resolve_voice_for_user`），
# **没有**「按文章指定音色」这一级。
# ⇒ 要让存量文章换成某个音色，唯一自洽的做法就是让它成为默认音色
# （这也正是管理员「换默认音色」这个动作本身的含义）。
#
# ⚠️ 代价要说清楚：设为默认会**改变之后所有新剪藏**的音色。
# 这是「换默认音色」的固有语义，不是本端点引入的。


class VoiceRedistillIn(BaseModel):
    """换音色并重渲存量文章。"""

    reason: str = Field(..., min_length=1, description="重渲原因，会写入审计日志")
    #: 只重渲这几篇；不传 = 全部「当前音色 ≠ 目标音色」且已有音频的文章。
    article_ids: Optional[list[str]] = None
    #: 单次最多重渲多少篇（蒸馏是分钟级的，别让一个请求挂住）。
    limit: int = 50


class VoiceRedistillItem(BaseModel):
    article_id: str
    current_voice_id: Optional[str] = None
    queued: bool = False


class VoiceRedistillOut(BaseModel):
    voice_id: str
    #: 命中的存量文章数（不含 limit 截断）
    matched: int = 0
    #: 实际触发重渲的篇数
    queued: int = 0
    items: list[VoiceRedistillItem] = Field(default_factory=list)


@router.post("/api/v1/admin/tts/voices/{voice_id}/re-distill", response_model=VoiceRedistillOut)
async def admin_voice_redistill(
    voice_id: str,
    req: VoiceRedistillIn,
    user: dict = Depends(require_admin_or_operator),
    db: AsyncSession = Depends(get_db),
):
    """把目标音色设为默认，并重渲存量文章。

    行为：
      1. reason ≥5 字符校验（与其他破坏性/批量操作同口径）
      2. 音色必须存在且在架
      3. **设为默认音色**（`is_default`，至多一条 —— DB 有部分唯一索引兜底）
      4. 找出「当前音色 ≠ 目标音色」且已有音频的存量文章，逐篇重新入队蒸馏
      5. 同事务写 `admin_operation_logs`（action=voice_redistill）

    ⚠️ 蒸馏是**分钟级**的（单篇约 3~6 分钟），所以这里只负责入队，
    不等结果；进度看文章状态。
    """
    if len(req.reason.strip()) < 5:
        raise InvalidRequest(message="reason 至少 5 个字符")

    voice = await get_voice(voice_id)
    if voice is None:
        raise NotFound(message=f"音色不存在: {voice_id}")
    if not voice.is_active:
        raise InvalidRequest(message=f"音色「{voice.display_name}」已下架，不能设为默认")

    # 1) 设为默认（先做，后面入队失败也不会留下「没换音色却重渲了」的错位）
    try:
        await update_voice(voice_id, is_default=True)
    except VoiceError as exc:
        raise InvalidRequest(message=str(exc)) from exc

    # 2) 找存量文章
    stmt = select(
        DistilledArticle.id,
        DistilledArticle.article_id,
        DistilledArticle.tts_voice_id,
    ).where(DistilledArticle.status == "done")
    if req.article_ids:
        stmt = stmt.where(DistilledArticle.article_id.in_(req.article_ids))
    else:
        stmt = stmt.where(
            (DistilledArticle.tts_voice_id.is_(None)) | (DistilledArticle.tts_voice_id != voice_id)
        )
    rows = (await db.execute(stmt)).all()
    matched = len(rows)
    limit = max(1, min(int(req.limit or 50), 200))
    rows = rows[:limit]

    # 3) 逐篇重新入队
    from stashbox.backend.common import cache_service
    from stashbox.backend.common.auth import create_access_token

    items: list[VoiceRedistillItem] = []
    queued = 0
    for _did, article_id, current_voice_id in rows:
        art = await db.get(Article, article_id)
        if art is None:
            items.append(VoiceRedistillItem(article_id=article_id, queued=False))
            continue
        art.status = "pending"
        try:
            await db.commit()
        except Exception:  # noqa: BLE001 —— 单篇失败不打断整批
            await db.rollback()
            items.append(VoiceRedistillItem(article_id=article_id, queued=False))
            continue
        await cache_service.invalidate_article(article_id)
        triggered = await get_ai_client().trigger_distill(
            article_id, auth_token=create_access_token(str(art.user_id))
        )
        if triggered is not None:
            queued += 1
        items.append(
            VoiceRedistillItem(
                article_id=article_id,
                current_voice_id=current_voice_id,
                queued=triggered is not None,
            )
        )

    # 4) 审计
    log_row = AdminOperationLog(
        admin_id=_uid(user),
        admin_tier=user.get("tier", "unknown"),
        action="voice_redistill",
        target_type="tts_voice",
        target_id=voice_id,
        reason=req.reason,
        method="POST",
        path=f"/api/v1/admin/tts/voices/{voice_id}/re-distill",
        request_body={
            "reason": req.reason,
            "article_ids": req.article_ids,
            "limit": req.limit,
            "matched": matched,
        },
        response_status=200,
    )
    db.add(log_row)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 —— 审计写失败不掩盖已完成的入队
        log.warning("voice_redistill 审计写入失败 voice_id=%s err=%s", voice_id, exc)
        await db.rollback()

    return VoiceRedistillOut(voice_id=voice_id, matched=matched, queued=queued, items=items)
