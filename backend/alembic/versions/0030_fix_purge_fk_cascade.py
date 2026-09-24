"""修 CP-DELETE 回归：新表 FK 缺级联规则 → 删除 ready 文章 500

背景（2026-09-24 真机验收发现）
--------------------------------
0024（distillation_evaluations）与 0026（article_audio_variants）建表时
FK 均未声明 ondelete，而 `article_purge.purge_article` 按 FK 依赖顺序
硬删 `distilled_articles` 时被这两张表阻塞：

    真机复现：DELETE /api/v1/articles/art_85d9f8... → 500
    ForeignKeyViolationError: delete on "distilled_articles" violates
    FK constraint "distillation_evaluations_task_id_fkey"
    (Key id=dst_6bf20f2d... is still referenced)

迁移前 DB 无这两张表，删除路径正常；这是 0024/0026 落地时未与
CP-DELETE 清理逻辑对齐引入的回归。用户端与 admin 端共用
`purge_article`，两条删除路径同样受影响。

修复（按数据语义分两类，与项目既有取舍一致）
--------------------------------------------
1. **article_audio_variants** —— 多码率音频是 `distilled_articles` 的
   派生物，同生命周期，蒸馏产物删除后变体毫无意义
   → FK 改 `ON DELETE CASCADE`

2. **distillation_evaluations** —— 4 维听感评分是**用户主观数据**，
   驱动 few_shot_examples（高分入池）/ user_listening_patterns（画像）/
   A/B 归因，是最核心的画像训练资产。与既有 `feedback_v2.article_id`
   置 NULL 保留埋点的语义一致
   → `task_id` 改 nullable + FK 改 `ON DELETE SET NULL`

应用层 `article_purge.purge_article` 同步显式按序处理，DB 级联作为兜底
（防止未来新增代码路径直接删 distilled_articles 再踩同一个坑）。

数据安全
--------
两处改动均不丢既有数据：CASCADE 只在删除父行时生效；SET NULL 仅放宽
约束。测试环境既有 1 条 evaluation / 2 条 variant 保持不变。

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. article_audio_variants：派生数据，随蒸馏产物 CASCADE ──
    op.drop_constraint(
        "article_audio_variants_distilled_article_id_fkey",
        "article_audio_variants",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "article_audio_variants_distilled_article_id_fkey",
        "article_audio_variants",
        "distilled_articles",
        ["distilled_article_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ── 2. distillation_evaluations：评分保留，仅断引用（SET NULL） ──
    #    先放宽 NOT NULL，否则 SET NULL 会失败
    op.alter_column(
        "distillation_evaluations",
        "task_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.drop_constraint(
        "distillation_evaluations_task_id_fkey",
        "distillation_evaluations",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "distillation_evaluations_task_id_fkey",
        "distillation_evaluations",
        "distilled_articles",
        ["task_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # 回滚 SET NULL → 原 NOT NULL 语义下不允许 task_id 为空：
    # 置空的行是被删文章遗留下的评分，原设计无此状态，直接清掉。
    op.execute("DELETE FROM distillation_evaluations WHERE task_id IS NULL")
    op.drop_constraint(
        "distillation_evaluations_task_id_fkey",
        "distillation_evaluations",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "distillation_evaluations_task_id_fkey",
        "distillation_evaluations",
        "distilled_articles",
        ["task_id"],
        ["id"],
    )
    op.alter_column(
        "distillation_evaluations",
        "task_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )

    op.drop_constraint(
        "article_audio_variants_distilled_article_id_fkey",
        "article_audio_variants",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "article_audio_variants_distilled_article_id_fkey",
        "article_audio_variants",
        "distilled_articles",
        ["distilled_article_id"],
        ["id"],
    )
