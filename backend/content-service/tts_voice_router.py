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

from stashbox.backend.common.auth import require_user
from stashbox.backend.common.auth_admin import require_admin_or_operator
from stashbox.backend.common.exceptions import InvalidRequest, NotFound
from stashbox.backend.common.logging import get_logger
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
)

log = get_logger(__name__)

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
    """管理端多吐几项：参考音频地址、参考文本、上下架、排序。"""

    ref_audio_url: str
    ref_text: str
    is_active: bool = True
    sort_order: int = 0
    updated_at: Optional[str] = None


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


def _to_admin_out(v) -> AdminVoiceOut:
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
    rows = await list_voices(include_inactive=True)
    return {"items": [_to_admin_out(v).model_dump() for v in rows]}


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
async def admin_delete_voice(voice_id: str, user: dict = Depends(require_admin_or_operator)):
    try:
        await delete_voice(voice_id)
    except VoiceError as exc:
        raise NotFound(message=str(exc)) from exc
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
        # 非 IndexTTS provider 不支持按调用覆盖音色
        raise InvalidRequest(
            message=(
                f"当前 TTS provider（{client.provider_name}）不支持按音色试听，"
                "音色库只对 indextts 生效"
            )
        )
    except Exception as exc:
        log.warning("tts_voice_preview_failed", voice_id=voice_id, error=str(exc))
        raise InvalidRequest(message=f"试听合成失败: {exc}") from exc

    from stashbox.backend.app.services.storage import get_storage

    key = f"voices/preview_{uuid.uuid4().hex}.wav"
    url = await get_storage().save(key, audio, content_type="audio/wav")
    return VoicePreviewOut(
        audio_url=url,
        bytes_len=len(audio),
        duration_sec=len(audio) // 32000,
        voice_id=voice_id,
    )


@router.post("/api/v1/admin/tts/voices/import-from-config", response_model=ImportFromConfigOut)
async def admin_import_voice_from_config(
    user: dict = Depends(require_admin_or_operator),
):
    """把当前全局 TTS 配置里的 indextts_ref_audio 收编成音色库第一条。

    存在的理由：迁移 0033 **刻意没有 seed 默认音色** —— 迁移里读 env 会把本机
    绝对路径固化进库，换台机器就是死路径。管理员点一次这个按钮，就能在不敲
    任何路径的前提下完成冷启动。
    """
    from stashbox.backend.app.services.tts import current_config

    cfg = await current_config()
    ref_audio = (cfg.get("indextts_ref_audio") or "").strip()
    ref_text = (cfg.get("indextts_ref_text") or "").strip()
    if not ref_audio:
        raise InvalidRequest(
            message="当前 TTS 配置没有 indextts_ref_audio，无法导入。请先在 TTS 配置页填参考音频"
        )

    existing = await list_voices(include_inactive=True)
    for v in existing:
        if v.ref_audio_url == ref_audio:
            # 已经收编过了 —— 幂等返回，别造重复行
            return ImportFromConfigOut(voice=_to_admin_out(v), created=False)

    try:
        voice = await create_voice(
            slug="default",
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
