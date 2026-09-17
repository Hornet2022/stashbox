"""ai-service 蒸馏完成触发订阅推送（CP5.4b）。"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from tasks.distill_task import _trigger_subscription_pushes


@pytest.mark.asyncio
async def test_trigger_subscription_pushes_da_not_found_returns_0():
    """_trigger_subscription_pushes 在 DistilledArticle 不存在时返回 0"""
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)

    result = await _trigger_subscription_pushes(
        mock_db, article_id="dst_xxx", exclude_user_id=42,
    )
    assert result == 0
    mock_db.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_subscription_pushes_da_has_no_tags_returns_0():
    """_trigger_subscription_pushes 在 DistilledArticle.tags 为空时返回 0"""
    mock_da = MagicMock()
    mock_da.tags = None
    mock_da.article_id = None

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=mock_da)

    result = await _trigger_subscription_pushes(
        mock_db, article_id="dst_xxx", exclude_user_id=42,
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
    mock_db.get = AsyncMock(return_value=mock_da)
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await _trigger_subscription_pushes(
        mock_db, article_id="dst_xxx", exclude_user_id=42,
    )
    assert result == 0
