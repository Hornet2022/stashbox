"""P1-2 push_notifications.article_id FK 改指 articles.id（原来指 distilled_articles）

修正业务侧 vs ORM 侧不一致：content-service 的 push_retry_message(user_id, article_id)
收到的是 articles.id（art_xxx），但 ORM FK 指向 distilled_articles.id（dst_xxx）—— 任何 push
写入都会因为 FK 约束失败或者指向错误实体而 silently 拒绝 / 留孤儿引用。

升级顺序：
  1. 先查孤儿引用（push_notifications.article_id 在 distilled_articles 里查不到对应记录）
  2. 没孤儿才安全 drop & recreate FK
  3. 有孤儿则 raise，运维人工决策（delete 孤儿 / 改 article_id 对齐）
"""

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. 找孤儿引用：push.article_id 在 distilled_articles 里不存在的行
    orphans = bind.execute(
        sa.text(
            """
            SELECT pn.id, pn.article_id
            FROM push_notifications pn
            LEFT JOIN distilled_articles da ON da.id = pn.article_id
            WHERE pn.article_id IS NOT NULL AND da.id IS NULL
            """
        )
    ).fetchall()

    if orphans:
        sample = ", ".join(f"id={r[0]} article_id={r[1]}" for r in orphans[:5])
        raise RuntimeError(
            f"[0022] push_notifications 存在 {len(orphans)} 条孤儿引用 "
            f"（article_id 不在 distilled_articles 里），先人工处理后重跑："
            f"{sample}{'...' if len(orphans) > 5 else ''}"
        )

    # 2. drop old FK + recreate 指向 articles.id
    op.drop_constraint(
        "push_notifications_article_id_fkey",
        "push_notifications",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "push_notifications_article_id_fkey",
        "push_notifications",
        "articles",
        ["article_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "push_notifications_article_id_fkey",
        "push_notifications",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "push_notifications_article_id_fkey",
        "push_notifications",
        "distilled_articles",
        ["article_id"],
        ["id"],
        ondelete="CASCADE",
    )
