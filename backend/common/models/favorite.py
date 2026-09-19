"""收藏表（CP5.5）。"""

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Favorite(Base, TimestampMixin):
    """收藏 + folder + note。"""

    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    # 与 articles.id / later_listens.article_id 对齐：String(32)。
    # article_id 由 content-service 生成为 f"art_{uuid4().hex[:24]}"，定长 28，
    # 且 FK 指向 articles.id（同为 varchar(32)），不可能超过 32。
    article_id: Mapped[str] = mapped_column(String(32), ForeignKey("articles.id"), nullable=False)
    folder: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "article_id", "folder", name="uq_favorites_user_article_folder"
        ),
        Index("idx_favorites_user_folder", "user_id", "folder"),
    )
