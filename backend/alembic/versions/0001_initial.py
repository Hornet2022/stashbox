"""initial schema: users / articles / distilled_articles

本迁移按 backend/common/models 手写，与 ORM 模型逐一对应。
（本机无 Docker / 本地 Postgres，无法跑 alembic autogenerate，故手写；
  待有 PG 环境可直接 `alembic upgrade head` 应用，或重跑 autogenerate 校验等价。）

Revision ID: 0001
Revises: 
Create Date: 2026-09-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ---- users ----
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("open_id", sa.String(64), nullable=True),
        sa.Column("union_id", sa.String(64), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("apple_id", sa.String(64), nullable=True),
        sa.Column("nickname", sa.String(64), nullable=True),
        sa.Column("avatar_url", sa.String(255), nullable=True),
        sa.Column("tier", sa.String(16), nullable=False, server_default="free"),
        sa.Column("student_verified_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("student_expire_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("open_id"),
        sa.UniqueConstraint("union_id"),
        sa.UniqueConstraint("phone"),
        sa.UniqueConstraint("apple_id"),
    )
    op.create_index("idx_users_tier", "users", ["tier"], unique=False)
    op.create_index(
        "idx_users_open_id_active",
        "users",
        ["open_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_users_phone_active",
        "users",
        ["phone"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ---- articles ----
    op.create_table(
        "articles",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("raw_content", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "favorite", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("skip", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_articles_user_status", "articles", ["user_id", "status"], unique=False
    )
    op.create_index("idx_articles_created", "articles", ["created_at"], unique=False)

    # ---- distilled_articles ----
    op.create_table(
        "distilled_articles",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("article_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("script_text", sa.Text(), nullable=True),
        sa.Column("audio_url", sa.String(512), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id"),
    )
    op.create_index(
        "idx_distilled_article_id", "distilled_articles", ["article_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_distilled_article_id", table_name="distilled_articles")
    op.drop_table("distilled_articles")

    op.drop_index("idx_articles_created", table_name="articles")
    op.drop_index("idx_articles_user_status", table_name="articles")
    op.drop_table("articles")

    op.drop_index("idx_users_phone_active", table_name="users")
    op.drop_index("idx_users_open_id_active", table_name="users")
    op.drop_index("idx_users_tier", table_name="users")
    op.drop_table("users")
