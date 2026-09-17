"""create tags + tag_subscriptions tables（CP5.3a）。v1 §11.5 CP5.3。

tags：主题标签表（系统内置 7 大类 + 用户自定义）
tag_subscriptions：用户订阅标签（CP5.4 推送用）
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tags 表
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("category", sa.String(32), nullable=False, server_default="subject"),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("creator_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("idx_tags_slug", "tags", ["slug"], unique=True)
    op.create_index("idx_tags_category", "tags", ["category"])

    # tag_subscriptions 表
    op.create_table(
        "tag_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tag_id", sa.Integer(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("user_id", "tag_id", name="uq_tag_subscription_user_tag"),
    )
    op.create_index("idx_tag_subs_user", "tag_subscriptions", ["user_id"])
    op.create_index("idx_tag_subs_tag", "tag_subscriptions", ["tag_id"])

    # seed 4 系统内置标签（对齐原 mock：科技/财经/生活/时事）
    # 注意：v1 §11.5 CP5.3 说"7 大类"，但 dev 阶段 mock 只有 4 个
    # 按任务包 §4.3 对齐 mock，不发明新标签
    op.execute(
        """
        INSERT INTO tags (slug, name, category, is_system, created_at)
        VALUES
            ('tech', '科技', 'subject', true, NOW()),
            ('finance', '财经', 'subject', true, NOW()),
            ('life', '生活', 'subject', true, NOW()),
            ('news', '时事', 'subject', true, NOW())
        ON CONFLICT (slug) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("idx_tag_subs_tag", table_name="tag_subscriptions")
    op.drop_index("idx_tag_subs_user", table_name="tag_subscriptions")
    op.drop_table("tag_subscriptions")
    op.drop_index("idx_tags_category", table_name="tags")
    op.drop_index("idx_tags_slug", table_name="tags")
    op.drop_table("tags")
