"""articles 加 manual_tags 列（运营指定的分类，2026-10-24）

为什么
----
产品决策：「admin 录入的内容……应该在用户 app 的订阅功能中形成闭环，
**订阅了分类就可以看到，没订阅就看不到**」。

⇒ 运营录入的内容**只靠分类分发**。而分类原先只有一条来源：
蒸馏时 LLM 猜的（``distilled_articles.tags``）。

实测的问题（2026-10-24）：GitHub 开源库的稿子 ``tags`` 就是**空的** ——
LLM 只能从 tags 表的候选里选，代码库主题（Python / 数据库 / Web 框架…）
大概率不在候选里，于是它**一个标签都贴不上**。

⇒ 没有分类 = 永远进不了任何人的订阅流 = **死内容**：
运营录了没人看得到，而且**没有任何报错**（推送函数第一行就是
``if not da.tags: return 0``）。

所以运营录入时必须能**指定**分类。本列存运营指定的（中文标签名数组），
蒸馏落 ``distilled_articles.tags`` 时**优先用它**；没指定才用 LLM 猜的。

为什么是 articles 表的一列，而不是塞进 raw_content
--------------------------------------------------
``raw_content`` 的语义是「抓取/解析结果」，运营的选择**不是**抓取结果。
混在一起会让「原文长什么样」与「运营怎么分类」两件事分不开
（而且 raw_content 已经被蒸馏链路、few-shot 入池等多处消费）。

历史数据
--------
0040 之前的行 ``manual_tags`` 为 NULL —— 行为与现在完全一致
（用 LLM 猜的分类）。不回填（回填没有依据：当时的分类就没定下来）。

下游
----
* ``ai-service/tasks/distill_task.py::_persist_agent_final``
  写 ``distilled_articles.tags`` 时**优先**用本列；
* 本列里的名字也会过一遍 ``_filter_known_tag_names``
  （表里没有的名字写进去只是垃圾 —— 推送是 ``Tag.name.in_(da.tags)``
  精确匹配，查不到就推送不出）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column(
            "manual_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("articles", "manual_tags")
