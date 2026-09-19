"""CP7.3.1 drop feedback.article_id FK — allow "n/a" sentinel

feedback 是埋点事件表（CP6.2.1, v1 §4.3.4），不是 articles 的关系子表：
非文章事件（tag_subscribe / tag_unsubscribe / tag_create / user_login /
service_start 等）没有关联文章，track() 统一传 article_id="n/a" 占位
（列 NOT NULL）。0004 建的 FK → articles.id 会拒掉所有这些行，
analytics.track() 只能吞异常记 warning —— 全库非文章埋点实际是空转。

去掉 FK 后：
- "n/a" sentinel 可正常落库，非文章埋点有数据（CP7.4 真听链路依赖）
- 文章被删时其历史埋点保留（事件日志该保留，不该被引用完整性连带清掉）
- 与 common/models/feedback.py 对齐（模型本来就沒声明 FK，是 0004 的漂移）

注意：本表 article_id 仍是 NOT NULL —— "n/a" 是合法值，NULL 不是。

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-19
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("feedback_article_id_fkey", "feedback", type_="foreignkey")


def downgrade() -> None:
    # 若表里已有 article_id 不指向 articles 的行（"n/a" sentinel），
    # 这里会报 FK violation —— 想回退需先清掉这些埋点行。
    op.create_foreign_key(
        "feedback_article_id_fkey",
        "feedback",
        "articles",
        ["article_id"],
        ["id"],
    )
