"""删掉测试残留的 orders 表（2026-10）

## 这张表从哪来

`backend/tests/admin/test_stats_enhanced.py` 为了测「本月 GMV / 订单数」
这类统计字段，会**自己**建一张 orders 表：

    _seed_orders()    CREATE TABLE IF NOT EXISTS orders (...)
    _cleanup_orders() DROP  TABLE IF EXISTS orders

跑完就删，所以正常情况下库里不该有它。本次发现的是一张 0 行、无索引使用、
无任何外键指向、也没有任何代码引用的残留表 —— 来源是一次被中断的测试运行
（测试失败或进程被杀时，`_cleanup_orders()` 没来得及执行）。

## 为什么写迁移，而不是手动 DROP

因为要让**任何**存在这个漂移的环境都收敛，而不是只修本机。拖动 drift 的
直接后果是 `alembic check` 报

    New upgrade operations detected: [('remove_table', Table('orders', ...))]

而 `alembic check` 是判断「模型与库是否一致」的工具，它红着就等于这个工具
失去意义 —— 久而久之就没人看它的输出了。

## ⚠️ 这不是「补一个业务表」

写迁移时最容易产生的误解是「哦原来 orders 是业务表只是没建迁移」。不是：
- 全仓代码里除了上面那个测试，没有任何地方引用这张表；
- users / articles / distilled_articles 都没有外键指向它；
- 它没有模型、没有 alembic 迁移、本次删除前也是 0 行。

所以这里 DROP 掉是安全的。**若将来真的要做订单功能**，应当新建正式的迁移 +
模型（并带上支付相关的约束与索引），而不是依赖这张测试夹具表。

回滚：`downgrade` 重建一张**空**表以恢复结构。注意它无法恢复数据 ——
但本来就没有数据。若某个环境里 orders 曾被误当作业务表写入过真实行，
先别 downgrade，去查那些行的来源。
"""

from alembic import op

revision = "0036"
down_revision = "0035"


def upgrade() -> None:
    # IF EXISTS：绝大多数环境本来就没有这张表，迁移必须能无条件跑过去。
    # 反过来，0035 的 down_revision 链在只装过 0035 的新库上同样要能跑。
    op.execute("DROP TABLE IF EXISTS orders")


def downgrade() -> None:
    # 只恢复结构（与测试夹具 _seed_orders 的定义一致），不恢复数据 ——
    # 删除时这张表是空的，且全仓无代码引用它。
    op.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        "  id SERIAL PRIMARY KEY,"
        "  amount NUMERIC(10,2) NOT NULL,"
        "  status TEXT NOT NULL,"
        "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")"
    )
