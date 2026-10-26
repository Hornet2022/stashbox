"""articles 加 shared_tags 列（用户剪藏内容共享，2026-10-26）

为什么
----
产品决策：「用户自己从 app 端剪藏的内容，没有共享入口。共享了，其他用户
就可以通过订阅标签看到对应的内容，取消共享，其他用户就看不到。」

⇒ 用户剪藏的内容**默认只有自己可见**；共享后才对订阅者可见。

共享的机制与运营内容（``articles.manual_tags``）**同源但不相同**：

* 运营内容（``manual_tags``）：运营录入时指定分类，蒸馏时**覆盖**
  ``distilled_articles.tags`` —— 运营比 LLM 更清楚这篇该归到哪。
* 用户共享（``shared_tags``）：用户剪藏后，LLM 已给过分类
  （``distilled_articles.tags``）。用户共享时手动选的标签，与已有分类
  **并存（merge）** —— 共享是「额外公开」，不该把 LLM 的分类抹掉。

为什么是 articles 表的一列，而不是塞进 distilled_articles
-------------------------------------------------------
- 共享状态属于**用户对这篇内容的意愿**（要不要公开、公开到哪些标签），
  不是蒸馏产物。放 ``distilled_articles`` 会让「蒸馏结果」与「用户意愿」
  两件事混在一起，且蒸馏重跑时容易把共享状态盖掉。
- 与 ``manual_tags`` 同表的理由一致（见 0040 的说明）：都是「人的选择」，
  不是抓取/蒸馏结果。

为什么 NULL 而不是空数组
----------------------
NULL = 未共享（默认，用户剪藏的内容只有自己可见）。
空数组 = 已共享但没选任何标签（语义上等于未共享，但保留「共享过」的痕迹）。
实现上统一按「空/NULL → 未共享」处理，与 ``distilled_articles.tags``
「NULL/空数组 → 无分类」同一口径。

历史数据
--------
0041 之前的行 ``shared_tags`` 为 NULL —— 行为与现在完全一致
（未共享，只有属主可见）。不回填。

下游
----
* ``content-service``：共享/取消共享接口写本列；
  订阅流/读侧授权按 ``da.tags ∪ shared_tags`` 合并判断；
* ``ai-service``：蒸馏推送触发时按合并后的标签找订阅者。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column(
            "shared_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("articles", "shared_tags")
