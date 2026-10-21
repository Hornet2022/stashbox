"""push_notifications.status 的默认值改成 'pending'（2026-10-21）

为什么
----
`status` 这一列原来默认是 ``'sent'``，模型注释写着
「历史行已发出,无失败回执」—— 但那时**根本没有投递**：
`push_notifications` 只落库，没有任何进程把消息发到设备上。
管理后台的「失败重发」更是直接把状态改成 'sent' 而不发任何请求。

于是这列承载了两个互相矛盾的意思：
- 「这条通知存在」（在应用内通知中心里能看到）
- 「这条通知已经发到用户手机上了」（从来没有发生过）

2026-10-21 把 ``status`` 收窄为**只表示投递状态**：

    pending  已创建，尚未尝试投递
    sending  投递中（已被某个 worker 认领）
    sent     投递成功
    failed   投递失败（error 列写明原因）

应用内通知中心不受影响 —— 客户端拉 ``GET /api/v1/notifications`` 时
按 ``read_at`` 过滤，根本不看 status（见 user-service/main.py 的
list_notifications）。

**只改列的默认值，不动历史行。**
历史行的 'sent' 保持原样：它们是"当时的状态快照"。
更重要的是不要把历史行刷成 'pending' —— 那会让部署后一瞬间
把积压的历史通知全推给用户（一次没必要的推送风暴）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "push_notifications",
        "status",
        existing_type=sa.String(length=16),
        server_default=sa.text("'pending'"),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "push_notifications",
        "status",
        existing_type=sa.String(length=16),
        server_default=sa.text("'sent'"),
        existing_nullable=False,
    )
