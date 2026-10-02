"""P1-3 统一 push_notifications / admin_operation_logs 的 created_at 列类型

修复（CP11.x 走查 P1-3）：
  两个表的 created_at 之前用 DateTime(timezone=True) + "NOW()"（手写字符串 default），
  与项目其他 model 的 TIMESTAMP + func.now() 风格不一致。

  PG 层：sqlalchemy.TIMESTAMP（无 tz）= timestamp without time zone，
  DateTime(timezone=True) = timestamp with time zone（timestamptz）。
  两种类型在 PG 是不同 column type（pg_type 比较），但 ORM 行为（Python datetime
  处理）相同。本迁移通过 alter column 显式重写为 timestamp without time zone，
  让应用层行为与 ORM 类型声明保持一致。

⚠️ 业务风险：
  如果生产已有数据，timestamp(tz) → timestamp(without tz) 转换 PG 会返回原 UTC 值。
  本项目所有应用写入都按 UTC 解释（无 tz 时 PG 把已有值按 session tz 解释），所以
  大概率安全。但为稳妥，建议运维升级前先 SELECT 现有 created_at 范围确认。

升级顺序：
  1. 先 SELECT 看现有列类型 + 是否有数据（只在有数据时打印警告）
  2. 两个表都 ALTER COLUMN ... TYPE timestamp without time zone USING (created_at AT TIME ZONE 'UTC')
  3. 同步默认表达式：DEFAULT now()

降级：升级到 timestamp with time zone，AT TIME ZONE 'UTC' 反向变换。
"""

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def _inspect_table(bind, table_name: str) -> dict:
    """查 created_at 列当前类型 + 行数。"""
    col_type = bind.execute(
        sa.text(
            """
            SELECT data_type, column_default, is_nullable
            FROM information_schema.columns
            WHERE table_name = :t AND column_name = 'created_at'
            """
        ),
        {"t": table_name},
    ).fetchone()
    row_count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
    return {
        "data_type": col_type[0] if col_type else None,
        "default": col_type[1] if col_type else None,
        "nullable": col_type[2] if col_type else None,
        "row_count": row_count,
    }


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("push_notifications", "admin_operation_logs"):
        info = _inspect_table(bind, table)
        print(f"[0023] {table}.created_at = {info['data_type']} (rows={info['row_count']})")
        if info["row_count"] > 0:
            print(
                f"[0023] ⚠️  {table} 有 {info['row_count']} 行历史数据，"
                "timestamp(tz → no-tz) 转换会把原值按 UTC 解释，请确认业务时区假设无误。"
            )
        # 已经统一成 timestamp without time zone 时 no-op；否则用 AT TIME ZONE 'UTC'
        # 把 timestamptz 显式转 UTC 时刻再写入 timestamp 列，PG 内部表达一致。
        op.execute(
            sa.text(
                f"ALTER TABLE {table} "
                "ALTER COLUMN created_at TYPE timestamp without time zone "
                "USING (created_at AT TIME ZONE 'UTC'), "
                "ALTER COLUMN created_at SET DEFAULT now()"
            )
        )


def downgrade() -> None:
    # 原来这里有 `bind = op.get_bind()`，但下面全程用 `op.execute`，
    # 从没读过它 —— 全仓唯一的 ruff error（F841）就出在这儿。
    # 纯死变量，删掉不改变任何行为；已应用过的 upgrade 不受影响。
    for table in ("push_notifications", "admin_operation_logs"):
        op.execute(
            sa.text(
                f"ALTER TABLE {table} "
                "ALTER COLUMN created_at TYPE timestamp with time zone "
                "USING (created_at AT TIME ZONE 'UTC')"
            )
        )
