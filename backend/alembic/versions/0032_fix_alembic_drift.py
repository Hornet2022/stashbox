"""CP-DRIFT-FIX：补齐 alembic check 报的 2 处 schema/model drift（2026-09-28）

`alembic check` 报 DB 实际对象和最新迁移期望不一致：

  ① distilled_articles.idx_distilled_ab_group（孤儿索引）
     - 历史迁移 0022/0029 创建过 ab_group 列及 idx_distilled_ab_group 索引；
       后来模型移除了 ab_group，但没显式 DROP INDEX，索引留在 DB。
     - 修复：drop_index('idx_distilled_ab_group', table_name='distilled_articles')

  ② push_notifications.tag_slug → tags.slug 外键（缺失 FK）
     - 原迁移 0008 在 create_table 里声明了 `ForeignKey("tags.slug", ondelete="SET NULL")`，
       但 SQLAlchemy 在某些 alembic env.py 配置下不会发出 FK DDL，导致 DB 里 FK 没建。
       模型 push_notification.py:40-41 当前仍声明此 FK，所以 alembic check 报缺失。
     - 修复：create_foreign_key('push_notifications_tag_slug_fkey',
                              'push_notifications', 'tags',
                              ['tag_slug'], ['slug'],
                              ondelete='SET NULL')
     - 数据完整性已 check：push_notifications 中无孤儿 tag_slug（外键引用都指向真实 tags）。

降级：恢复 FK + 重建索引。
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ① 删孤儿索引
    op.drop_index("idx_distilled_ab_group", table_name="distilled_articles")

    # ② 补 push_notifications.tag_slug → tags.slug 外键
    op.create_foreign_key(
        "push_notifications_tag_slug_fkey",
        "push_notifications",
        "tags",
        ["tag_slug"],
        ["slug"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # 反向：删 FK + 重建索引
    op.drop_constraint(
        "push_notifications_tag_slug_fkey",
        "push_notifications",
        type_="foreignkey",
    )
    op.create_index(
        "idx_distilled_ab_group",
        "distilled_articles",
        ["ab_group"],
    )
