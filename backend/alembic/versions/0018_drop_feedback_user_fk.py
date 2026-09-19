"""CP7.3-AUDIT-FIX-4 drop feedback.user_id FK — 埋点表不该被 users 引用完整性绑住

feedback 是埋点事件表（CP6.2.1, v1 §4.3.4），不是 users 的关系子表。
0004 建的 FK → users.id 与 common/models/feedback.py 不一致（模型从未声明
FK），跟 0017 修 feedback.article_id 是同一类模型/DB 漂移。

去掉 FK 的实际理由（本机 DB 调研，2026-09-19）：
- 201 行埋点里 12 行 user_id=0（ANONYMOUS_USER_ID），其余 60 个左右的真实
  user_id。user_id=0 之所以能落库，完全依赖 0006 seed 出来的 users.id=0
  这一行存在 —— 把匿名埋点的可用性挂在一条 seed 数据上不合理。
- analytics.track() 吞异常只记 warning（common/analytics.py:58-60），
  FK violation 会静默丢埋点，跟 article_id 那条完全同构。
- 用户被删除 / 尚未刷入时该用户的历史埋点不应连带被拒。

注意：本表 user_id 仍是 NOT NULL —— 0（匿名）是合法值，NULL 不是。
本次只 drop FK，不动列定义，也改到此为止，不涉及 ANONYMOUS_USER_ID 设计。

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-19
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("feedback_user_id_fkey", "feedback", type_="foreignkey")


def downgrade() -> None:
    # 由于 ondelete 行为未定义且 users.id 可能被清，回退前若存在不指向
    # users 的 user_id（例如 users.id=0 被删掉后的匿名埋点）会报 FK
    # violation —— 需先清掉这些行。
    op.create_foreign_key(
        "feedback_user_id_fkey",
        "feedback",
        "users",
        ["user_id"],
        ["id"],
    )
