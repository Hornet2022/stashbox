"""文章表 - 用户加入的原始内容（待蒸馏）。"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Article(Base, TimestampMixin):
    """文章表（v1 §4.3.1）。

    favorite / skip 为布尔标记，保留 CP1.4 的收藏 / 跳过语义
    （v1 §4.3.1 仅列 status 字段，此处补齐以兼容既有端点）。
    """

    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # art_xxx
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(
        String(32), default="unknown", nullable=False
    )  # wechat/douyin/web/d9/clawbot/pdf
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )  # pending/distilling/ready/listened/failed
    raw_content: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )  # 抓取后的原始内容（L4 多模态理解输入）
    # 0040：**运营指定的分类**（2026-10-24 产品拍板）。
    #
    # 产品决策：「admin 录入的内容……应该在用户 app 的订阅功能中形成闭环，
    # **订阅了分类就可以看到，没订阅就看不到**」。
    #
    # ⇒ 这类内容的分发**只认分类**。而分类原先只有一条来源：蒸馏时 LLM 猜的
    # （`distilled_articles.tags`）。实测 GitHub 稿的 tags 就是**空的** ——
    # 没有分类 = 永远进不了任何人的订阅流 = **死内容**（运营录了没人看得到，
    # 而且没有任何报错）。
    #
    # 所以运营录入时必须能**指定**分类。本列存运营指定的（中文标签名数组），
    # 蒸馏落 `distilled_articles.tags` 时**优先用它**；没指定才用 LLM 猜的。
    #
    # 为什么是 articles 表的一列而不是塞进 raw_content：raw_content 是
    # 「抓取/解析结果」，运营的选择不是抓取结果 —— 混在一起会让
    # 「原文长什么样」与「运营怎么分类」两件事分不开。
    manual_tags: Mapped[list | None] = mapped_column(
        JSONB, nullable=True
    )  # 运营指定的分类（中文标签名数组，NULL = 未指定，用 LLM 猜的）
    # 0041：**用户共享的标签**（2026-10-26 产品拍板）。
    #
    # 用户剪藏的内容**默认只有自己可见**。共享后，订阅了这些标签的**其他用户**
    # 才能通过订阅流看到、读、听；取消共享则恢复只有属主可见。
    #
    # 与 `manual_tags`（运营）的区别：运营是**覆盖** `da.tags`；用户共享是
    # **并存（merge）** —— 共享是「额外公开」，不该把 LLM 已给的分类抹掉。
    # 所以订阅流 / 读侧授权 / 推送都按 `da.tags ∪ shared_tags` 合并判断。
    #
    # NULL / 空数组 = 未共享（默认）。属主**永远**能看自己的内容，共享开关
    # 只影响「别人能不能看到」（见 `_subscription_grants_access` 的说明）。
    shared_tags: Mapped[list | None] = mapped_column(
        JSONB, nullable=True
    )  # 用户共享时手动选的公开标签（中文标签名数组，NULL = 未共享）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )  # CP5.2 用户端重试计数
    audio_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )  # CP10 蒸馏完成后写回的 OSS URL

    # CP1.4 端点保留字段
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    skip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("idx_articles_user_status", "user_id", "status"),
        Index("idx_articles_created", "created_at"),
        # 0035：三个用户列表端点的形状是
        # WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at DESC。
        # 上面两个索引各覆盖一半（一个定位到人拿不到有序结果，一个按序扫再逐行过滤），
        # 复合索引一次搞定。
        Index("idx_articles_user_created", "user_id", text("created_at DESC")),
    )
