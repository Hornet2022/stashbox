"""CP3.7.1 §2.1.C：article_audio_variants 表（多码率音频）

闭环 3（多端一致性 + 离线优先）的数据底座。
CP7.3.0 audio_variant_generator 异步生成 64k / 96k / 128k 三种码率。
unique(distilled_article_id, bitrate) 保证同一文章同一码率只 1 行。

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "article_audio_variants",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("distilled_article_id", sa.String(length=32), nullable=False),
        sa.Column("bitrate", sa.SmallInteger(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("oss_key", sa.String(length=255), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False, server_default=sa.text("'m4a'")),
        sa.Column("duration_sec", sa.SmallInteger(), nullable=False),
        sa.Column(
            "sample_rate", sa.SmallInteger(), nullable=False, server_default=sa.text("24000")
        ),
        sa.Column("mono", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["distilled_article_id"], ["distilled_articles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("distilled_article_id", "bitrate", name="idx_avar_task_bitrate"),
    )
    op.create_index(
        "idx_avar_bitrate_size", "article_audio_variants", ["bitrate", "file_size_bytes"]
    )


def downgrade() -> None:
    op.drop_index("idx_avar_bitrate_size", table_name="article_audio_variants")
    op.drop_table("article_audio_variants")
