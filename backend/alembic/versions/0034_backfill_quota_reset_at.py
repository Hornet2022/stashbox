"""回填 users.quota_reset_at：NULL → 下月 1 号（2026-10）

背景（`quota_service.quota_reset_loop` 从没跑起来过那次修复的配套）：

  `quota_reset_at` 是 `TIMESTAMP WITHOUT TIME ZONE`、可空、**无默认值**，
  而建号路径（`user-service/main.py` 的 `wechat_login`）当年从没写过它。
  于是所有存量用户这一列都是 NULL。

  修好定时器后，NULL 行会命中到期判定的 `quota_reset_at IS NULL` 分支 ——
  功能上这是对的（这些人从没被重置过），但后果是**部署后第一轮 tick
  就把全平台当月已用配额清零**：10 月 3 号用掉 3 次的人，10 月 4 号整点又变回 0。
  一次性的免费配额，量不大，但它让「配额」这个数字当场失真，而且原因不好解释。

  所以先把存量 NULL 行按「下月 1 号」补齐：让每个人安安稳稳用到真正的月末边界，
  跨月行为从此可预期。建号路径已同步改成直接写 `next_reset_at_naive()`，
  不会再产生新的 NULL 行，所以本迁移是一次性的。

  值必须与写入路径同源（naive UTC，见 `quota_service.next_reset_at_naive`）：
  写成带时区的值，asyncpg 会在定时器下一次比较时抛
  `can't subtract offset-naive and offset-aware datetimes` —— 正是这次要修的那个坑。

回滚：本迁移只填数据、不改结构，`downgrade` 把 NULL 语义还回去即可
（已补的行保留，它们本来就是有效值）。
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"


def _next_month_start_naive(now: datetime) -> datetime:
    """下月 1 号 00:00，UTC 且 naive —— 与 quota_service.next_reset_at_naive 同义。"""
    if now.month == 12:
        return now.replace(year=now.year + 1, month=1, day=1)
    return now.replace(month=now.month + 1, day=1)


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    target = _next_month_start_naive(now).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    # 只动 NULL 行：已经有值的（admin 手动重置过、或本来就是新的）保持原样
    op.execute(
        sa.text(
            "UPDATE users SET quota_reset_at = :target WHERE quota_reset_at IS NULL"
        ).bindparams(target=target)
    )


def downgrade() -> None:
    """无需回滚：把 NULL 补成有效值不是破坏性变更，逆转它反而会让
    这批人重新落回「到期判定 IS NULL 分支」的老路。留空。"""
