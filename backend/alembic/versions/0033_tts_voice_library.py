"""CP-TTS-VOICE：音色库 + 用户音色/语速偏好（2026-09-30）

背景：安卓端要「音色和语速可配可调」，但三端现状是断的 ——

  1. 音色在服务端根本**没有概念**。`app/services/tts/indextts.py:19` 写明
     「voice 参数无意义 —— 音色由参考音频决定（克隆）」，IndexTTS 是零样本克隆，
     音色 = 那**唯一一份**全局 `indextts_ref_audio`。用户想换音色无从下手。
  2. 语速在 App 端是**假闭环**。`FullScreenPlayerScreen.kt:650` 有 0.75x~2.0x
     五档 UI，但 `currentSpeed` 是 `remember { mutableStateOf("1.0x") }` 纯本地
     状态，而 `PlayerController` 只有 play/pause/seekTo，**没有 setSpeed** ——
     点了不生效、不持久化。

本迁移补数据底座，让「音色可选」有地方存：

新增表 1 —— `tts_voices`（音色库，管理员维护）
  一行 = 一个预设音色 = 一份参考音频 + 对应参考文本 + 展示名。
  IndexTTS 靠 ref_audio 克隆音色，所以「音色」在本项目里天然就是
  「(参考音频, 参考文本) 对」，这两列缺一不可 —— 参考文本必须与音频内容一致，
  否则克隆出的音色会念错/含糊。

  - `slug`：稳定业务标识，App 侧可缓存，不随改名而失效
  - `is_default`：全局默认音色。**部分唯一索引**保证至多一条为真，
    否则用户没选音色时「回落默认」会随机命中某条
  - `is_active`：软下架。已选中的音色被下架时，用户偏好行保留（FK SET NULL），
    回落默认而不是报错
  - 软删用 `deleted_at`（与全项目一致）

新增表 2 —— `user_tts_preferences`（每用户一行）
  - `voice_id` 可空：null = 跟随全局默认音色（用户没选过 / 选的那个被删了）
  - `speed`：播放端语速。存服务端是为了**多端同步**，实际变速由
    ExoPlayer 在客户端做，不影响已生成的音频（不必重跑蒸馏）
  - user_id 既是主键又是外键 → 一个用户至多一行，不需要额外唯一约束

补列 —— `distilled_articles.tts_voice_id`
  溯源：这篇音频当时是用哪个音色合成的。**刻意不建 NOT NULL**：
  历史行（本次迁移之前跑出来的）本就无从得知，留 NULL 表示「未知/用全局配置」，
  比回填一个假音色诚实。

**不 seed 默认音色**：迁移里读 env 会把本机绝对路径
（`/Users/hornet/work/.../tingting_ref.wav`）固化进库，换台机器就是死路径。
解析顺序做成「用户偏好 → is_default 音色 → 全局 indextts_ref_audio 配置」，
管理员在后台点一次「从当前配置导入」即可把现役音色收编成库里的第一条。

降级：删列 + 删两表 + 删索引。

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1) 音色库 ────────────────────────────────────────────────────────────
    op.create_table(
        "tts_voices",
        sa.Column("id", sa.String(length=32), nullable=False, comment="ttsv_<uuid24>"),
        sa.Column("slug", sa.String(length=64), nullable=False, comment="稳定业务标识"),
        sa.Column(
            "display_name", sa.String(length=64), nullable=False, comment="展示名，如「婷婷」"
        ),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "ref_audio_url", sa.String(length=512), nullable=False, comment="参考音频 URL/路径"
        ),
        sa.Column("ref_text", sa.Text(), nullable=False, comment="参考文本，须与音频内容一致"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=False), nullable=False, server_default="now()"
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=False), nullable=False, server_default="now()"
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=False), nullable=True),
        sa.PrimaryKeyConstraint("id", name="tts_voices_pkey"),
        sa.UniqueConstraint("slug", name="tts_voices_slug_key"),
    )
    # App 拉音色列表：只要未下架的，按 sort_order 排
    op.create_index(
        "idx_tts_voices_active",
        "tts_voices",
        ["is_active", "sort_order"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # 至多一条 is_default —— 否则「没选音色时回落默认」会随机命中
    op.create_index(
        "idx_tts_voices_single_default",
        "tts_voices",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default AND deleted_at IS NULL"),
    )

    # ── 2) 用户音色/语速偏好 ─────────────────────────────────────────────────
    op.create_table(
        "user_tts_preferences",
        # user_id 同时是 PK 和 FK → 天然「一个用户至多一行」
        sa.Column(
            "user_id",
            sa.BigInteger(),
            nullable=False,
            comment="FK users.id ON DELETE CASCADE",
        ),
        # 选中的音色被删除/下架时置 NULL（SET NULL），用户自动回落默认音色
        sa.Column("voice_id", sa.String(length=32), nullable=True),
        sa.Column("speed", sa.Numeric(precision=3, scale=2), nullable=False, server_default="1.00"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=False), nullable=False, server_default="now()"
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=False), nullable=False, server_default="now()"
        ),
        sa.PrimaryKeyConstraint("user_id", name="user_tts_preferences_pkey"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="user_tts_prefs_user_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["voice_id"],
            ["tts_voices.id"],
            ondelete="SET NULL",
            name="user_tts_prefs_voice_id_fkey",
        ),
    )
    op.create_index(
        "idx_user_tts_prefs_voice",
        "user_tts_preferences",
        ["voice_id"],
    )

    # ── 3) 蒸馏产物溯源：这段音频当时用的哪个音色 ────────────────────────────
    # 刻意可空：历史行无从得知，留 NULL 比回填假音色诚实
    op.add_column(
        "distilled_articles", sa.Column("tts_voice_id", sa.String(length=32), nullable=True)
    )
    op.create_foreign_key(
        "distilled_articles_tts_voice_id_fkey",
        "distilled_articles",
        "tts_voices",
        ["tts_voice_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_distilled_tts_voice", "distilled_articles", ["tts_voice_id"])


def downgrade() -> None:
    op.drop_index("idx_distilled_tts_voice", table_name="distilled_articles")
    op.drop_constraint(
        "distilled_articles_tts_voice_id_fkey", "distilled_articles", type_="foreignkey"
    )
    op.drop_column("distilled_articles", "tts_voice_id")

    op.drop_index("idx_user_tts_prefs_voice", table_name="user_tts_preferences")
    op.drop_table("user_tts_preferences")

    op.drop_index("idx_tts_voices_single_default", table_name="tts_voices")
    op.drop_index("idx_tts_voices_active", table_name="tts_voices")
    op.drop_table("tts_voices")
