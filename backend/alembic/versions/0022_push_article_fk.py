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

    # 1. 找孤儿引用：push.article_id 在 articles 里查不到对应记录
    #    修复 2026-09-24：FK 将改为指向 articles.id（业务实情），所以孤儿检查必须查 articles
    orphans = bind.execute(
        sa.text(
            """
            SELECT pn.id, pn.article_id
            FROM push_notifications pn
            LEFT JOIN articles a ON a.id = pn.article_id
            WHERE pn.article_id IS NOT NULL AND a.id IS NULL
            """
        )
    ).fetchall()

    if orphans:
        sample = ", ".join(f"id={r[0]} article_id={r[1]}" for r in orphans[:5])
        raise RuntimeError(
            f"[0022] push_notifications 存在 {len(orphans)} 条孤儿引用 "
            f"（article_id 不在 articles 里），先人工处理后重跑："
            f"{sample}{'...' if len(orphans) > 5 else ''}"
        )

    # 2. 若旧 FK 仍存在,drop it;否则跳过
    bind.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS(
                SELECT 1 FROM pg_constraint
                WHERE conname = 'push_notifications_article_id_fkey'
              ) THEN
                ALTER TABLE push_notifications DROP CONSTRAINT push_notifications_article_id_fkey;
              END IF;
            END$$;
            """
        )
    )

    # 3. 重建 FK 指向 articles.id
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
