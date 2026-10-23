"""distilled_articles 加 content_parts 列（三类再创作内容分区，2026-10-23）

为什么
----
「关联思考 / 延伸判断 / 拓展方向」在改写 LLM 的 JSON 契约里是三个**独立字段**，
但解析时被**合并进 sections**：

    extra = insights + related + follow_ups
    sections = sections + extra

合并是必要的 —— TTS 要把整稿连着念出来，它需要的是一个连续的段落序列。

但合并之后，下游就**彻底分不清**了：
- 落库只有一列 ``script_text``（一整块字符串）；
- API 只返回 ``script_text``；
- 安卓端把它按空行切段平铺（``ArticleDetailScreen`` 的 ``DistilledScriptCard``），
  **没有任何分区 UI** —— 也就是说这三类内容**一直在生成、用户却永远看不到**。

本列把三栏**原样**另存一份：

    {"sections": [...], "insights": [...], "related": [...], "follow_ups": [...]}

**不动合并行为** —— ``script_text``、TTS、few-shot 入池全部不变，
所以这一列是**纯增量**：读不到（历史数据 / NULL）就当没有分区。

为什么是**一列 JSONB** 而不是六个 Text 列
----------------------------------------
这三类天然是一组（一起产生、一起演进、一起被读）。
拆成六列意味着每加一个分区就要写一次迁移 + 改六处模型/序列化/映射。
一列 JSONB：加分区只是**多一个 key**，不用迁移。

历史数据
--------
0039 之前的行 ``content_parts`` 为 NULL —— 读取方按「空分区」处理，
不报错、不回填（回填是不可能的：合并之后信息已经丢了，
**没法从 script_text 里可靠地切回三类** —— 数量不定、边界不明）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "distilled_articles",
        sa.Column(
            "content_parts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("distilled_articles", "content_parts")
