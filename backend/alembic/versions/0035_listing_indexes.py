"""补四个缺失索引：三个列表/统计查询的排序列 + tags JSONB 的 GIN（2026-10）

背景是实测出的「查询形状与索引不匹配」。四个都**不是**臆想的优化，每一条
都能指到具体的查询点：

1. `articles(user_id, created_at DESC)`
   三个用户列表端点都是这个形状（content-service/main.py 的
   list_articles / list_pending / list_listened）：
       WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at DESC
   现有两个索引各自只覆盖一半：`idx_articles_user_status(user_id, status)`
   能定位到人但拿不到有序结果，还得再排一次；`idx_articles_created(created_at)`
   能按序扫但要在结果里逐行过滤 user_id。复合索引让两者一次搞定。
   （pending / listened 这两个端点刚补上 ORDER BY + LIMIT，正好由它兜住。）

2. `distilled_articles(status, updated_at)`
   后台统计面板（content-service/admin_router.py 的 stats_enhanced 一族）
   全是这个形状，例如：
       WHERE status = 'failed' AND updated_at > day_ago        （失败数）
       WHERE status = 'done'   AND updated_at > seven_days_ago（趋势图）
   以及 date_trunc('day', updated_at) GROUP BY day 的按天聚合。

3. `distillation_evaluations(created_at)`
   ai-service/admin_router.py 的评测分页是**无过滤**的
   `ORDER BY created_at DESC` + LIMIT；hooks_impl / main.py 里还有
   `task_id = ? AND auto_flag = false ORDER BY created_at DESC`。
   前者只有顺序索引可用 —— 这正是 created_at 单列索引的典型场景。

4. `distilled_articles.tags` GIN
   按标签筛选走的是 JSONB 包含运算符（`DistilledArticle.tags.contains([tag_name])`，
   底层 `@>`）。没有 GIN 索引时这就是整表顺序扫 + 逐行 JSON 解析，
   标签页一被点开就全表扫。用默认 jsonb_ops 而非 jsonb_path_ops：后者索引更小
   更快，但只支持 `@>` 的一个子集，这里没必要冒这个险。

关于锁：CREATE INDEX（非 CONCURRENTLY）会在建索引期间持有 SHARE 锁，
**阻塞写入但不阻塞读取**。本项目数据量还小，这个代价可以接受。
等 articles 涨到百万行时，正确做法是给 alembic 开
`transaction_per_migration = True` 并改用
`op.create_index(..., postgresql_concurrently=True)` —— 那需要改全局迁移配置，
不在本次范围内，不顺手做。

回滚：四个索引互相独立，drop 即可，无数据影响。
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"

# 与各模型 __table_args__ 里的 Index 名保持一致。两边不同名的话，
# autogenerate 会反复认为「索引被删了又建回来」，凭空造迁移。
_SPECS = (
    # (索引名, 表, 列, 是否 GIN)
    ("idx_articles_user_created", "articles", ("user_id", sa.text("created_at DESC")), False),
    ("idx_distilled_status_updated", "distilled_articles", ("status", "updated_at"), False),
    ("idx_eval_created", "distillation_evaluations", ("created_at",), False),
    ("idx_distilled_tags_gin", "distilled_articles", ("tags",), True),
)


def upgrade() -> None:
    for name, table, cols, use_gin in _SPECS:
        op.create_index(name, table, list(cols), postgresql_using="gin" if use_gin else None)


def downgrade() -> None:
    for name, table, _cols, _use_gin in reversed(_SPECS):
        op.drop_index(name, table_name=table)
