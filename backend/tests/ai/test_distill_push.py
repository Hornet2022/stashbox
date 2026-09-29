"""ai-service 蒸馏完成触发订阅推送（CP5.4b）。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from tasks.distill_task import _trigger_subscription_pushes


@pytest.mark.asyncio
async def test_trigger_subscription_pushes_da_not_found_returns_0():
    """_trigger_subscription_pushes 查不到蒸馏结果时返回 0。

    注意查询方式：按 `DistilledArticle.article_id == article_id` 查（传入的是
    **articles.id**），不是 `db.get(DistilledArticle, id)` 按主键查 ——
    后者是原实现，实际调用方传 art_xxx 永远查不到，函数恒返回 0，
    「推送队列」因此永远空着。
    """
    mock_db = MagicMock()
    mock_db.scalar = AsyncMock(return_value=None)

    result = await _trigger_subscription_pushes(
        mock_db,
        article_id="art_xxx",
        exclude_user_id=42,
    )
    assert result == 0
    mock_db.scalar.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_subscription_pushes_da_has_no_tags_returns_0():
    """_trigger_subscription_pushes 在 DistilledArticle.tags 为空时返回 0"""
    mock_da = MagicMock()
    mock_da.tags = None
    mock_da.article_id = None

    mock_db = MagicMock()
    mock_db.scalar = AsyncMock(return_value=mock_da)

    result = await _trigger_subscription_pushes(
        mock_db,
        article_id="art_xxx",
        exclude_user_id=42,
    )
    assert result == 0


@pytest.mark.asyncio
async def test_trigger_subscription_pushes_no_matching_slugs_returns_0():
    """_trigger_subscription_pushes 在 tag name 无对应 slug 时返回 0"""
    mock_da = MagicMock()
    mock_da.tags = ["科技", "商业"]
    mock_da.article_id = None

    # tag name 查不到 slug
    mock_result = MagicMock()
    mock_result.fetchall.return_value = []

    mock_db = MagicMock()
    mock_db.scalar = AsyncMock(return_value=mock_da)
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await _trigger_subscription_pushes(
        mock_db,
        article_id="art_xxx",
        exclude_user_id=42,
    )
    assert result == 0


@pytest.mark.asyncio
async def test_trigger_subscription_pushes_stores_articles_id_not_distilled_id():
    """写入 push_notifications 的必须是 **articles.id**。

    `push_notifications_article_id_fkey` 指向 articles(id)，塞
    distilled_articles.id（dst_xxx）会 ForeignKeyViolation —— 而异常被函数内的
    宽 except 吞成一行 warning，于是推送永远写不进去、也不报错。
    这条锁住 INSERT 用的 id 口径。
    """
    mock_da = MagicMock()
    mock_da.tags = ["科技"]
    mock_da.article_id = "art_target"

    mock_art = MagicMock()
    mock_art.title = "标题"

    mock_db = MagicMock()
    mock_db.scalar = AsyncMock(return_value=mock_da)  # 只有 DistilledArticle 一次 scalar
    mock_db.get = AsyncMock(return_value=mock_art)  # 取标题用 db.get

    def _rows(rows):
        m = MagicMock()
        m.fetchall = MagicMock(return_value=rows)
        return m

    mock_db.execute = AsyncMock(side_effect=[_rows([("tech",)]), _rows([(7,)])])
    mock_db.add_all = MagicMock()
    mock_db.commit = AsyncMock()

    result = await _trigger_subscription_pushes(
        mock_db, article_id="art_target", exclude_user_id=42
    )

    assert result == 1
    added = mock_db.add_all.call_args[0][0]
    assert added[0].article_id == "art_target"  # 不是 dst_target
    assert added[0].deeplink == "/articles/art_target"
