"""CP-TTS-VOICE：音色库 + 用户音色/语速偏好（闭环数据底座）。

**为什么音色要单独建表**：IndexTTS 是零样本克隆
（`app/services/tts/indextts.py:19`：「voice 参数无意义 —— 音色由参考音频决定」），
所以本项目的「音色」天然就是 **(参考音频, 参考文本) 对**，不是 provider 那种
填个 voice 名字就行的字符串。参考文本必须与音频内容一致，否则克隆出的音色
会念错或含糊 —— 这也是 `ref_text` 设成 NOT NULL 而不是可选的原因。

解析顺序（见 `common/tts_voice_service.py:resolve_voice_for_user`）：

    用户偏好 voice_id → is_default 的音色 → 全局 indextts_ref_audio 配置

最后一级是历史兜底：本表出现之前，所有人共用全局那一份参考音频。保留它意味着
管理员还没在后台建音色时，蒸馏链路照常工作，不会因为「音色库是空的」而全盘失败。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# 可选播放语速档位（服务端单一事实源，App 不再硬编码）
# 0.75/1.0/1.25/1.5/2.0 与既有 FullScreenPlayerScreen 的五档对齐，
# 改动这里 = 改全端行为，不需要发 App 版本。
DEFAULT_PLAYBACK_SPEEDS: tuple[float, ...] = (0.75, 1.0, 1.25, 1.5, 2.0)

MIN_PLAYBACK_SPEED = 0.5
MAX_PLAYBACK_SPEED = 3.0


class TTSVoice(Base):
    """一个预设音色 = 一份参考音频 + 参考文本 + 展示名。

    - `slug`：稳定业务标识，App 侧可缓存；改名不影响它
    - `is_default`：**至多一条为真**（DB 层用部分唯一索引兜住，
      见迁移 0033 `idx_tts_voices_single_default`）。用户没选音色时回落它，
      多条为真会导致随机命中
    - `is_active`：软下架。已选中的音色下架后，用户偏好行经 FK SET NULL
      变 NULL，自动回落默认音色，而不是让蒸馏直接报错
    """

    __tablename__ = "tts_voices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="ttsv_<uuid24>")
    slug: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="稳定业务标识"
    )
    display_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="展示名，如「婷婷」"
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 存 URL 还是本地路径都允许：IndexTTSClient 两种都能读
    ref_audio_url: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="参考音频 URL/路径"
    )
    ref_text: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="参考文本，须与音频内容一致"
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[str] = mapped_column(
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    updated_at: Mapped[str] = mapped_column(
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    deleted_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=False), nullable=True)

    __table_args__ = (
        Index("idx_tts_voices_active", "is_active", "sort_order"),
        # 部分唯一索引：至多一条 is_default。**必须与迁移 0033 同名同条件**，
        # 否则 alembic check 会把它报成「模型里没有这个索引」而判为漂移。
        Index(
            "idx_tts_voices_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default AND deleted_at IS NULL"),
        ),
    )


class UserTTSPreference(Base):
    """每用户一行：选哪个音色 + 播放语速。

    `speed` 存服务端只为**多端同步**；实际变速由客户端 ExoPlayer 做，
    不影响已生成的音频 —— 所以调语速不需要重跑蒸馏（实测单篇约 23 分钟）。

    `voice_id` 可空 = 跟随全局默认音色。指向的音色被删/下架时 FK SET NULL，
    也是「跟随默认」—— 语义自洽，不需要额外的迁移逻辑。
    """

    __tablename__ = "user_tts_preferences"

    # 既是 PK 又是 FK → 天然「一个用户至多一行」，无需额外唯一约束
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK users.id ON DELETE CASCADE",
    )
    voice_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("tts_voices.id", ondelete="SET NULL"), nullable=True
    )
    speed: Mapped[Decimal] = mapped_column(
        Numeric(precision=3, scale=2), nullable=False, default=Decimal("1.00")
    )

    created_at: Mapped[str] = mapped_column(
        TIMESTAMP(timezone=False), nullable=False, server_default="now()"
    )
    # onupdate 必带：否则改语速/音色后 updated_at 永远停在插入时间，
    # 「上次改于什么时候」这类展示会说谎（自测时真机改完发现时间戳没动）
    updated_at: Mapped[str] = mapped_column(
        TIMESTAMP(timezone=False),
        nullable=False,
        server_default="now()",
        onupdate=func.now(),
    )

    __table_args__ = (Index("idx_user_tts_prefs_voice", "voice_id"),)
