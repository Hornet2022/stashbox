"""extend users.tier to include admin/operator roles（CP1.8）。

v1 §3.6：admin + operator 角色扩展到 tier 字段。
原 4 值 free/student/member/pro → 6 值 free/student/member/pro/operator/admin。

注意：0001_initial.py 未创建 tier CHECK constraint（本migration新建）；
如 constraint 名非 users_tier_check，按实际名调整。
admin_seed 用户不存在于本机 DB，UPDATE 已跳过， CHECK constraint 仍生效。
"""
from alembic import op

# revision identifiers
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL：新建 CHECK constraint（0001 无 CHECK，故无 drop）
    op.create_check_constraint(
        "users_tier_check",
        "users",
        "tier IN ('free', 'student', 'member', 'pro', 'operator', 'admin')",
    )
    # admin_seed 用户不存在于本机 DB，跳过 UPDATE——[known issues] 报备


def downgrade() -> None:
    op.drop_constraint("users_tier_check", "users", type_="check")
    op.create_check_constraint(
        "users_tier_check",
        "users",
        "tier IN ('free', 'student', 'member', 'pro')",
    )
    op.execute("UPDATE users SET tier = 'member' WHERE tier IN ('admin', 'operator')")
