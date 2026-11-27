"""users 加 username 列（唯一用户名，2026-11-27）

为什么
----
`users.nickname` 是**显示名**不是标识：微信登录建号时它是硬编码常量「听友」
（user-service/main.py:480）⇒ **所有内测账号昵称都一样**；而 users 表既没有
nickname 唯一约束也没有它的索引 ⇒ 管理后台按昵称搜不出「是哪一个」。

⇒ 新增 `username`：**唯一用户名**，DB 层唯一索引兜底（唯一性最终由 DB 保证，
不靠应用层自觉）。取值规则见 `common/username.py`（单一真相源）：
微信登录上线前用**设备特征**生成（`d-<机型>_<设备指纹>`），上线后迁到微信派生形态。

为什么唯一索引是 `lower(username)` 表达式索引
-------------------------------------------
用户名语义上**大小写不敏感**（`Ting_23` 与 `ting_23` 应视为同一个）。
用普通 unique 索引的话，两者能同时入库 ⇒ "唯一"名存实亡。
⇒ 索引建在 `lower(username)` 上 ⇒ 大小写不同也判冲突。

回填规则
--------
统一 `t-<id>`（`t` = 兜底形态）。**不回填设备形态** —— 迁移时拿不到设备信息
（设备特征是客户端登录时上报的，服务端库里没有）⇒ 存量用户先拿到兜底名，
新登录用户拿设备形态名。两者都唯一，只是形态不同。

不写"不可变"约束
----------------
微信登录上线时 username 会**整体迁移**（设备派生 → 微信派生）⇒ 必须可改。
本迁移不加任何"不可改"的约束或触发器。

已知边界
--------
* 存量账号的 username 是兜底形态（`t-<id>`），不是设备形态 —— 可读性差一点，
  但唯一性成立。要不要手工改成更好读的名字，由运营侧一次性数据订正决定
  （改法与证据见 scripts/purge_test_users_2026-11-27/ 同级的运维记录）。
* 本列**不含个人信息**：它由设备指纹哈希派生，不是原始 ANDROID_ID。
  设备指纹本身只在客户端上报、不落库（仅埋点 device_id 另有其表）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "username",
            sa.String(length=32),
            nullable=True,
            comment="唯一用户名；暂用形态 d-<机型>_<设备指纹>，微信登录上线后迁到 w- 形态",
        ),
    )

    # 回填：统一 t-<id>，保证非空 + 互不相同。
    # 用 id 而不是昵称 ⇒ 昵称可以重名（本来就会重名），id 不会。
    op.execute("UPDATE users SET username = 't-' || id::text WHERE username IS NULL")

    op.alter_column(
        "users",
        "username",
        existing_type=sa.String(length=32),
        nullable=False,
    )

    # 唯一索引（大小写不敏感）。
    # 为什么不用普通 unique：username 语义上大小写不敏感，
    # 普通索引会让 `Ting_23` 与 `ting_23` 同时入库 ⇒ "唯一"名存实亡。
    op.create_index(
        "uq_users_username_lower",
        "users",
        [sa.text("lower(username)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_column("users", "username")
