from unittest.mock import AsyncMock, MagicMock

import pytest

from app.douyin import DouyinChat


@pytest.mark.asyncio
async def test_search_result_accepts_visible_partial_text() -> None:
    page = MagicMock()
    rows = MagicMock()
    page.locator.return_value.filter.return_value = rows
    rows.count = AsyncMock(return_value=0)
    exact = MagicMock()
    partial = MagicMock()
    page.get_by_text.side_effect = [exact, partial]
    exact.count = AsyncMock(return_value=0)
    partial.count = AsyncMock(return_value=1)
    candidate = MagicMock()
    candidate.is_visible = AsyncMock(return_value=True)
    partial.nth.return_value = candidate

    result = await DouyinChat(page)._search_result("好友")

    assert result is candidate


@pytest.mark.asyncio
async def test_search_result_ignores_hidden_exact_match() -> None:
    page = MagicMock()
    rows = MagicMock()
    page.locator.return_value.filter.return_value = rows
    rows.count = AsyncMock(return_value=0)
    exact = MagicMock()
    partial = MagicMock()
    page.get_by_text.side_effect = [exact, partial]
    exact.count = AsyncMock(return_value=1)
    hidden = MagicMock()
    hidden.is_visible = AsyncMock(return_value=False)
    exact.nth.return_value = hidden
    partial.count = AsyncMock(return_value=1)
    visible = MagicMock()
    visible.is_visible = AsyncMock(return_value=True)
    partial.nth.return_value = visible

    result = await DouyinChat(page)._search_result("好友")

    assert result is visible


@pytest.mark.asyncio
async def test_waits_for_chat_after_delayed_navigation():
    from app.douyin import PageOperationError
    chat = DouyinChat(MagicMock(), timeout_ms=1000)
    chat._confirm_opened = AsyncMock(side_effect=[PageOperationError("loading"), None])
    await chat._wait_for_opened("friend")
    assert chat._confirm_opened.await_count == 2


@pytest.mark.asyncio
async def test_chat_confirmation_failure_is_not_ignored():
    from app.douyin import PageOperationError
    page = MagicMock()
    page.evaluate = AsyncMock(return_value={})
    chat = DouyinChat(page, timeout_ms=0)
    chat._confirm_opened = AsyncMock(side_effect=PageOperationError("not opened"))
    with pytest.raises(PageOperationError, match="not opened"):
        await chat._wait_for_opened("friend")


@pytest.mark.asyncio
async def test_search_ignores_hidden_conversation_cache():
    page = MagicMock()
    empty = MagicMock()
    empty.count = AsyncMock(return_value=0)
    rows = MagicMock()
    rows.count = AsyncMock(return_value=1)
    hidden = MagicMock()
    hidden.is_visible = AsyncMock(return_value=False)
    rows.nth.return_value = hidden
    def locator(selector):
        group = MagicMock()
        group.filter.return_value = rows if selector == '[data-e2e="conversation-item"]' else empty
        group.first.count = AsyncMock(return_value=0)
        return group
    page.locator.side_effect = locator
    page.get_by_text.return_value = empty
    assert await DouyinChat(page)._search_result("friend") is None
    hidden.get_attribute.assert_not_called()
