"""隐私政策同意 + 设备令牌（推送注册）（2026-10-21）

背景
----
内测要补齐的三个功能缺口里，有两个落在 user-service：注销账号、隐私政策同意；
推送通道的"设备注册"也要一张表。这一版迁移把它们的地基铺好。

1. `user_consents` 补三列
   ------------------------
   `user_consents` 表在 0028 就建好了（CP5.6.0），但它记的是
   **个性化开关**（personalization_enabled / cross_user_share_enabled），
   不是"用户是否同意了当前的隐私政策"。

   而 `ui/settings/SettingsScreen.kt` 的注释写得很明白：
   「要重做请从 `GET/PUT /api/v1/user/consent` 端点开始」——
   也就是说**端点一直没做**（现在只有 admin 侧只读的 `/admin/consents`）。

   所以在**同一张表**上补三列，而不是另起一张：
   - `privacy_policy_accepted`：是否同意当前版本
   - `privacy_policy_version`：同意的是哪一版（政策改版要能区分）
   - `privacy_policy_accepted_at`：同意时间

   为什么放同一张表：这张表的语义就是"这个用户的同意记录"。
   拆成两张表会让"这个用户同意了什么"需要 join 两处才能回答全。

2. `device_tokens` 新表
   ------------------
   推送投递需要知道"往哪台设备发"。之前**根本没有这个概念**：
   - `push_notification` 表只存"要推什么"（user_id + title + body）
   - 没有任何地方存"推到哪台设备"
   - 没有 `device_token` 列，也没有任何设备注册端点

   所以推送这件事此前只有"落库"，没有"投递"。这张表是投递的前提。

   字段设计：
   - `token`：厂商通道给的设备令牌（唯一）—— 幂等注册靠它
   - `provider`：哪个通道的 token（jpush / tpns / fcm ...）——
     换通道时同一台设备可能有两条记录，靠这个区分
   - `platform`：android / ios
   - `is_active`：解绑或登出要能停用而不是删（留审计）
   - `deleted_at`：软删，跟仓库里其他表一致

   唯一约束用 (token, provider) 复合 —— 同一厂商的同一 token 只能有一条。

不建索引的理由
--------------
`device_tokens` 的量级 = 设备数 × 通道数，内测阶段几百条。
`token` 上的唯一索引已经覆盖了"按 token 查"（投递时的主要查询形状），
`user_id` 上加普通索引即可 —— 两张表都是小表，不需要更多。

关于 users.deleted_at
---------------------
**不需要迁移**：这一列在 0001_initial 就建好了
（`sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True)`），
而且 0001 里那两个部分索引 `idx_users_open_id_active` /
`idx_users_phone_active` 的 postgresql_where 就是 `deleted_at IS NULL`。

也就是说：**软删除的地基一直都在**，只是 ORM 没映射这一列、
也没有任何写它的代码路径。这次补上 ORM 映射 + 注销端点即可。
"""

from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. user_consents 补隐私政策同意三列 ──────────────────────────────
    op.add_column(
        "user_consents",
        sa.Column(
            "privacy_policy_accepted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "user_consents",
        sa.Column("privacy_policy_version", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "user_consents",
        sa.Column("privacy_policy_accepted_at", sa.TIMESTAMP(), nullable=True),
    )

    # ── 2. device_tokens 表 ──────────────────────────────────────────────
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False, server_default="android"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="none"),
        sa.Column("app_version", sa.String(length=32), nullable=True),
        sa.Column("device_model", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("token", "provider", name="uq_device_tokens_token_provider"),
    )
    op.create_index("idx_device_tokens_user", "device_tokens", ["user_id"])
    # 投递时的主查询形状：某用户所有**可用**的设备
    op.create_index(
        "idx_device_tokens_user_active",
        "device_tokens",
        ["user_id"],
        postgresql_where=sa.text("deleted_at IS NULL AND is_active"),
    )


def downgrade() -> None:
    op.drop_index("idx_device_tokens_user_active", table_name="device_tokens")
    op.drop_index("idx_device_tokens_user", table_name="device_tokens")
    op.drop_table("device_tokens")
    op.drop_column("user_consents", "privacy_policy_accepted_at")
    op.drop_column("user_consents", "privacy_policy_version")
    op.drop_column("user_consents", "privacy_policy_accepted")
