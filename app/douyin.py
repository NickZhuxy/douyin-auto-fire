from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Locator, Page

from app.selectors import MESSAGE_INPUTS, SEARCH_INPUTS


class PageOperationError(RuntimeError):
    pass


class DouyinChat:
    def __init__(self, page: Page, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    async def open_target(self, name: str) -> None:
        search = await first_visible(self.page, SEARCH_INPUTS, self.timeout_ms)
        await search.click()
        await search.fill("")
        await search.fill(name)
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        result = await self._search_result(name)
        while result is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            result = await self._search_result(name)
        if result is None:
            raise PageOperationError(f"搜索不到可见好友: {name}")
        logging.getLogger("douyin_sender").info("Target control metadata: %s", await result.evaluate("el => ({tag: el.tagName, classes: el.className, visible: !!(el.offsetWidth || el.offsetHeight)})"))
        await result.click(timeout=self.timeout_ms)
        await self._wait_for_opened(name)

    async def _search_result(self, name: str) -> Locator | None:
        # Search mode renders a separate SearchPanel. Its "发消息" action is the
        # correct control; clicking the hidden conversation cache does not mount
        # the composer.
        search_items = self.page.locator('[class*="SearchPanelitem"]').filter(has_text=name)
        for index in range(await search_items.count()):
            item = search_items.nth(index)
            button = item.locator('[class*="SearchPanelitemchat_btn"]').first
            if await button.count() and await button.is_visible():
                return button

        # The nickname node can be hidden while its conversation row is visible.
        # Locate and click the complete row instead of relying on text visibility.
        row_selectors = (
            '[data-e2e="conversation-item"]',
            '[class*="conversationConversationItem"]',
            '[class*="conversation-item"]',
            '[class*="ConversationItem"]',
        )
        for selector in row_selectors:
            rows = self.page.locator(selector).filter(has_text=name)
            for index in range(await rows.count()):
                row = rows.nth(index)
                try:
                    if not await row.is_visible():
                        continue
                    class_name = await row.get_attribute("class") or ""
                    if "wrapper" in class_name or await row.get_attribute("data-e2e") == "conversation-item":
                        return row
                except Exception:
                    continue

        candidates = [self.page.get_by_text(name, exact=True), self.page.get_by_text(name, exact=False)]
        for candidate_group in candidates:
            count = await candidate_group.count()
            visible: list[Locator] = []
            for index in range(count):
                candidate = candidate_group.nth(index)
                try:
                    if await candidate.is_visible():
                        visible.append(candidate)
                except Exception:
                    continue
            if len(visible) == 1:
                return visible[0]
            if len(visible) > 1:
                return visible[0]

        # Some Douyin builds render the title itself as hidden, but keep a visible
        # ancestor as the actionable result. Find that ancestor from the hidden title.
        hidden_titles = self.page.locator('[class*="conversationConversationItemtitle"]').filter(has_text=name)
        for index in range(await hidden_titles.count()):
            row = hidden_titles.nth(index).locator(
                "xpath=ancestor::*[contains(@class, 'conversationConversationItem')][1]"
            )
            if await row.count() and await row.is_visible():
                return row

        for selector in (f'[title="{_css_escape(name)}"]', f'[aria-label="{_css_escape(name)}"]'):
            candidate = self.page.locator(selector).first
            if await candidate.count() and await candidate.is_visible():
                return candidate
        return None

    async def message_input(self) -> Locator:
        return await first_visible(self.page, MESSAGE_INPUTS, self.timeout_ms)

    async def _wait_for_opened(self, name: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        while True:
            try:
                await self._confirm_opened(name)
                return
            except PageOperationError:
                if asyncio.get_running_loop().time() >= deadline:
                    metadata = await self.page.evaluate("""name => ({
                        path: location.pathname,
                        exactMatches: [...document.querySelectorAll('*')].filter(e => e.childElementCount === 0 && e.textContent.trim() === name).map(e => ({tag:e.tagName, classes:e.className, visible:!!(e.offsetWidth || e.offsetHeight)})).slice(0, 10),
                        editors: [...document.querySelectorAll('[contenteditable="true"],textarea')].map(e => ({tag:e.tagName, classes:e.className, visible:!!(e.offsetWidth || e.offsetHeight)})).slice(0, 10)
                    })""", name)
                    logging.getLogger("douyin_sender").info("Chat structure metadata (no text): %s", metadata)
                    raise
                await asyncio.sleep(0.25)

    async def _confirm_opened(self, name: str) -> None:
        # Dry-run only needs to prove that the target conversation opened. Some
        # accounts do not mount the composer until it receives focus or a real send.
        markers = (
            '[class*="RightPanelHeader"]',
            '[class*="messageContent"]',
            '[class*="chatContent"]',
            '[class*="MessagePanel"]',
        )
        for selector in markers:
            locator = self.page.locator(selector).filter(has_text=name).first
            if await locator.count():
                return
        text = self.page.get_by_text(name, exact=True)
        for index in range(await text.count()):
            candidate = text.nth(index)
            class_name = await candidate.get_attribute("class") or ""
            if "conversationConversationItemtitle" not in class_name:
                return
        body_text = await self.page.locator("body").inner_text()
        if name in body_text and "发消息" in body_text:
            return
        raise PageOperationError(f"点击搜索结果后无法确认聊天已打开: {name}")


async def first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int = 15_000) -> Locator:
    per_selector = max(500, timeout_ms // max(1, len(selectors)))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=per_selector)
            return locator
        except Exception:
            continue
    raise PageOperationError(f"找不到页面元素，已尝试: {', '.join(selectors)}")


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
