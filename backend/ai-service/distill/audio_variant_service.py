"""CP7.3.0：多码率音频生成（§5 决策 2 路线 B = 按需转码）。

设计（docs/听感产品化方案_v1.md §2.5 / §5 决策 2）：
- 主音频（step3/4 TTS 合成，~128k）是唯一"全保真"产物，蒸馏时已上传 storage。
- 低码率变体（96k / 64k）**不预生成**，在客户端请求 variants 或播放时按需转码：
  1. 查 article_audio_variants 表 —— 命中直接返回（幂等）
  2. 未命中 → storage.fetch 主音频 → ffmpeg 转码 → storage.save → 写 DB 行
- 转码失败 / ffmpeg 缺失 / fetch 不支持（OSS 未实现）→ 只返回主音频档，
  客户端退化播原 URL。**永不抛错破播放主流程。**

注意：memory 架构约束「OSS 只被 Content 写」针对生产写链路；本服务的变体写
走抽象 Storage（当前 STORAGE_PROVIDER=local），生产切 OSS 前该约束需 Hermes
复核（任务包已标注）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stashbox.backend.app.services.storage import get_storage
from stashbox.backend.common.models import ArticleAudioVariant, DistilledArticle

if TYPE_CHECKING:
    from stashbox.backend.app.services.storage.base import Storage

log = structlog.get_logger("distill.audio_variant")

# ---------------------------------------------------------------------------
# 常量（§5 决策 2：3 档码率；主档 = TTS 产物，转码档 = 按需生成）
# ---------------------------------------------------------------------------

MAIN_BITRATE = 128  # 主音频档（不转码，直接指向 distilled_articles.audio_url）
TRANSCODE_BITRATES = (96, 64)  # 按需转码档（kbps）
ALL_BITRATES = (MAIN_BITRATE,) + TRANSCODE_BITRATES

# 单任务转码超时（5 分钟音频 ≪ 此值；防 ffmpeg 卡死拖垮请求）
TRANSCODE_TIMEOUT_SEC = 60

# 主音频合理上限 20MB（防御性：超限不拉回转码）
MAX_MAIN_BYTES = 20 * 1024 * 1024


def _find_bin(name: str) -> str | None:
    """ffmpeg/ffprobe 路径探测（与 steps.py 同款：homebrew 不在 PATH）。"""
    for cand in (
        shutil.which(name),
        "/opt/homebrew/bin/ffmpeg" if name == "ffmpeg" else "/opt/homebrew/bin/ffprobe",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ):
        if cand and Path(cand).exists():
            return cand
    return None


def _variant_key(article_id: str, bitrate: int, fmt: str = "m4a") -> str:
    """storage key 规范：audio/{article_id}.{bitrate}k.{fmt}（主音频是 audio/{id}.m4a）。"""
    return f"audio/{article_id}.{bitrate}k.{fmt}"


async def probe_duration_sec(audio_bytes: bytes) -> Optional[int]:
    """ffprobe 探测时长（秒，向上取整）；失败 → None。"""
    ffprobe = _find_bin("ffprobe")
    if not ffprobe:
        return None
    tmp: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tf:
            tf.write(audio_bytes)
            tmp = tf.name
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        raw = stdout.decode().strip()
        if proc.returncode == 0 and raw:
            return max(1, int(float(raw) + 0.5))
    except Exception as e:  # noqa: BLE001 —— 探测失败不抛，调用方用 da.duration_sec 兜底
        log.warning("ffprobe_duration_failed", error=str(e))
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    return None


async def transcode_bytes(main_bytes: bytes, bitrate_kbps: int) -> bytes:
    """ffmpeg 转码到目标码率（mono AAC/m4a）。失败抛 RuntimeError（调用方兜底）。

    输出到临时文件而非 pipe：m4a 的 moov atom 需要 seek 回写，pipe 会产碎片化文件。
    """
    ffmpeg = _find_bin("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found（homebrew 路径已探测）")

    src: Optional[str] = None
    dst: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as ts:
            ts.write(main_bytes)
            src = ts.name
        fd, dst = tempfile.mkstemp(suffix=f".{bitrate_kbps}k.m4a")
        os.close(fd)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-i",
            src,
            "-vn",
            "-ac",
            "1",  # mono（ORM 默认 mono=True，省带宽）
            "-b:a",
            f"{bitrate_kbps}k",
            "-movflags",
            "+faststart",
            dst,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=TRANSCODE_TIMEOUT_SEC)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 转码失败 rc={proc.returncode}: {stderr[-300:]!r}")
        out = Path(dst).read_bytes()
        if not out:
            raise RuntimeError("ffmpeg 输出为空")
        return out
    finally:
        if src:
            Path(src).unlink(missing_ok=True)
        if dst:
            Path(dst).unlink(missing_ok=True)


class AudioVariantService:
    """CP7.3.0 多码率变体服务（依赖可注入，便于单测 mock storage/session）。"""

    def __init__(
        self,
        storage: "Storage | None" = None,
        session_factory=None,
    ):
        self._storage = storage
        self._session_factory = session_factory

    @property
    def storage(self) -> "Storage":
        if self._storage is None:
            self._storage = get_storage()
        return self._storage

    # -- 查询 ----------------------------------------------------------------

    async def list_variants(
        self,
        db: AsyncSession,
        distilled_article: DistilledArticle,
        *,
        generate_missing: bool = True,
    ) -> list[dict]:
        """返回 3 档码率可用性列表（缺档时按需转码补齐）。

        永不抛错：转码失败 → 该档 available=False（客户端播主档）。
        """
        results: list[dict] = []

        # 已入库的变体（含软删过滤）
        existing_rows = await db.scalars(
            select(ArticleAudioVariant).where(
                ArticleAudioVariant.distilled_article_id == distilled_article.id,
                ArticleAudioVariant.deleted_at.is_(None),
            )
        )
        existing = {r.bitrate: r for r in existing_rows}

        for bitrate in ALL_BITRATES:
            if bitrate == MAIN_BITRATE:
                results.append(
                    {
                        "bitrate": MAIN_BITRATE,
                        "available": bool(distilled_article.audio_url),
                        "url": distilled_article.audio_url,
                        "file_size_bytes": None,  # 主档大小不单独存（可 HEAD 拿）
                        "is_main": True,
                    }
                )
                continue

            row = existing.get(bitrate)
            if row is None and generate_missing:
                row = await self.ensure_variant(db, distilled_article, bitrate)
            results.append(
                {
                    "bitrate": bitrate,
                    "available": row is not None,
                    "url": None if row is None else await self._url_for_row(row),
                    "file_size_bytes": None if row is None else row.file_size_bytes,
                    "is_main": False,
                }
            )
        return results

    async def _url_for_row(self, row: ArticleAudioVariant) -> str:
        """变体 URL：优先已存 oss_key 重签（local storage 拼法稳定）。"""
        try:
            base = getattr(self.storage, "public_url_base", None)
            if base:
                return f"{base}/{row.oss_key}"
        except Exception:  # noqa: BLE001
            pass
        return row.oss_key

    # -- 生成（幂等） ----------------------------------------------------------

    async def ensure_variant(
        self,
        db: AsyncSession,
        distilled_article: DistilledArticle,
        bitrate: int,
    ) -> Optional[ArticleAudioVariant]:
        """确保某码率变体存在：查表命中即返；未命中转码 + 上传 + 写行。

        任何失败 → None（log warning，不破调用方）。并发双转码窗口由
        unique(distilled_article_id, bitrate) 兜底：第二次 insert 撞约束 →
        rollback → 重查命中。
        """
        if bitrate not in TRANSCODE_BITRATES:
            log.warning("variant_bitrate_not_transcodable", bitrate=bitrate)
            return None
        if not distilled_article.audio_url:
            return None  # 主音频都没有（mock 路径）→ 无源可转

        # 1. 命中直接返回
        row = await db.scalar(
            select(ArticleAudioVariant).where(
                ArticleAudioVariant.distilled_article_id == distilled_article.id,
                ArticleAudioVariant.bitrate == bitrate,
                ArticleAudioVariant.deleted_at.is_(None),
            )
        )
        if row is not None:
            return row

        # 2. 拉主音频
        main_key = self.storage.key_from_url(distilled_article.audio_url)
        try:
            main_bytes = await self.storage.fetch(main_key)
        except Exception as e:  # noqa: BLE001 —— OSS 未实现 fetch / 文件缺失
            log.warning(
                "variant_fetch_main_failed",
                key=main_key,
                error=str(e),
            )
            return None
        if len(main_bytes) > MAX_MAIN_BYTES:
            log.warning("variant_main_too_large", size=len(main_bytes))
            return None

        # 3. 转码
        try:
            out_bytes = await transcode_bytes(main_bytes, bitrate)
        except Exception as e:  # noqa: BLE001
            log.warning("variant_transcode_failed", bitrate=bitrate, error=str(e))
            return None

        # 4. 上传 + 写行
        key = _variant_key(distilled_article.article_id, bitrate)
        try:
            await self.storage.save(key, out_bytes, content_type="audio/mp4")
        except Exception as e:  # noqa: BLE001
            log.warning("variant_save_failed", key=key, error=str(e))
            return None

        duration = await probe_duration_sec(out_bytes)
        new_row = ArticleAudioVariant(
            id=f"avar_{uuid.uuid4().hex[:24]}",
            distilled_article_id=distilled_article.id,
            bitrate=bitrate,
            file_size_bytes=len(out_bytes),
            oss_key=key,
            format="m4a",
            duration_sec=duration or distilled_article.duration_sec or 0,
            sample_rate=24000,
            mono=True,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        try:
            async with db.begin_nested():  # SAVEPOINT：撞唯一约束不脏整条 session
                db.add(new_row)
                await db.flush()
        except Exception as e:  # noqa: BLE001 —— 并发双转码：输家重查
            log.info("variant_insert_conflict_requery", error=str(e))
            row = await db.scalar(
                select(ArticleAudioVariant).where(
                    ArticleAudioVariant.distilled_article_id == distilled_article.id,
                    ArticleAudioVariant.bitrate == bitrate,
                )
            )
            return row
        await db.commit()
        log.info(
            "variant_generated",
            distilled_article_id=distilled_article.id,
            bitrate=bitrate,
            size=len(out_bytes),
        )
        return new_row


# 模块级单例（FastAPI 端点用；测试可 new 自己的）
_service: AudioVariantService | None = None


def get_variant_service() -> AudioVariantService:
    global _service
    if _service is None:
        _service = AudioVariantService()
    return _service
