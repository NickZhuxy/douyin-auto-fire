from __future__ import annotations

import asyncio
import random
import secrets
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Page

from app.douyin import DouyinChat, PageOperationError, first_visible
from app.models import Message, Sticker
from app.selectors import IMAGE_INPUTS, STICKER_BUTTONS, STICKER_PANELS


LATEST_OUTGOING_MESSAGE = (
    '.messageMessageListlist [data-index="0"] '
    '.messageMessageBoxmessageBox:has(.messageMessageBoxcontentBox.messageMessageBoxisFromMe)'
)
STICKER_CONFIRM_ANCHOR = "data-douyin-sender-anchor"
TEXT_CONFIRM_TIMEOUT_MS = 30_000
TEXT_CONFIRM_GRACE_MS = 2_000
SEND_ACK_TIMEOUT_MS = 30_000
SEND_ACK_POLL_MS = 250
SEND_FAILURE_MARKERS = (
    '.ContentSideSendStatusretry',
    "text=发送失败",
    '[aria-label*="重试"]',
    '[title*="重试"]',
    '[class*="sendFailed"]',
    '[class*="SendFailed"]',
)
SEND_PENDING_MARKERS = ('.im-saas-message-spin', '.ContentSideSendStatussending')

# Observed in Douyin's official IM build 1.0.0.946:
# https://lf3-social.iesdouyin.com/obj/douyin-social-cdn/pcim/static/js/async/4187.2096e141.js
# UI/model adapter and retry/pending classes:
# https://lf3-social.iesdouyin.com/obj/douyin-social-cdn/pcim/static/js/async/__federation_expose_default_export.0ccd0cf4.js
# Succeeded=3, Received=4; SelfVisible=-3 must not be treated as delivery.
# Both the legacy SDK and current UI adapter expose serverId as a string.
# Only these two safe fields leave the page; never serialize the message model.
MESSAGE_MODEL_STATUS = """element => {
    if (!element || !element.isConnected) return null;
    const content = element.querySelector('[data-e2e="msg-item-content"]');
    for (const node of [content, element]) {
        if (!node) continue;
        const key = Object.keys(node).find(key =>
            key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$'));
        let fiber = key ? node[key] : null;
        const visited = new Set();
        for (let depth = 0; fiber && depth < 32 && !visited.has(fiber); depth++) {
            visited.add(fiber);
            const model = fiber.memoizedProps && fiber.memoizedProps.message;
            if (model && typeof model === 'object') {
                const status = model.flightStatus;
                const serverId = model.serverId;
                return {
                    flightStatus: typeof status === 'number' && Number.isFinite(status) ? status : null,
                    hasServerId: ['string', 'number', 'bigint'].includes(typeof serverId) &&
                        /^[1-9][0-9]*$/.test(String(serverId))
                };
            }
            fiber = fiber.return;
        }
    }
    return null;
}"""

MATCH_NEW_OUTGOING = """([selector, anchor, previousContent, kind, expected]) => {
    const message = document.querySelector(selector);
    if (!message) return false;
    const body = message.querySelector('[data-e2e="msg-item-content"]') || message;
    if (message.getAttribute('data-douyin-sender-anchor') === anchor &&
        body.innerHTML === previousContent) return false;
    if (kind === 'text') {
        const normalize = value => (value || '').normalize().replace(/\\s+/g, ' ').trim();
        if (!normalize(body.textContent).includes(normalize(expected))) return false;
    } else {
        const images = [...body.querySelectorAll('img')];
        if (!images.length) return false;
        if (expected && !images.some(image => (image.src || '').includes(expected))) return false;
    }
    return message;
}"""


async def send_message(page: Page, chat: DouyinChat, message: Message, stickers: dict[str, Sticker]) -> None:
    if message.type == "random":
        await send_message(page, chat, random.choice(message.choices), stickers)
        return
    if message.type == "text":
        await send_text(chat, message.content or "")
        return
    if message.type == "image":
        if message.path is None:
            raise PageOperationError("图片消息缺少文件路径")
        await send_image(page, message.path.as_posix())
        return
    if message.type == "douyin_sticker":
        sticker = stickers.get(message.sticker or "")
        if sticker is None:
            raise PageOperationError(f"没有原生表情映射: {message.sticker}")
        await send_douyin_sticker(page, sticker)
        return
    raise PageOperationError(f"不支持的消息类型: {message.type}")


async def send_text(chat: DouyinChat, content: str) -> None:
    editor = await chat.message_input()
    page = editor.page
    before = await _mark_latest_outgoing_message(page)
    await editor.click()
    await page.keyboard.insert_text(content)
    await page.keyboard.press("Enter")
    await _confirm_text_sent(page, before, content)


async def _confirm_text_sent(page: Page, before: tuple[str, str], content: str) -> None:
    await _confirm_outgoing_sent(page, before, "text", content, "文字消息")


async def _confirm_outgoing_sent(
    page: Page,
    before: tuple[str, str],
    kind: str,
    expected: str,
    message_label: str,
) -> None:
    anchor, before_content = before
    handle = None
    try:
        arguments = [LATEST_OUTGOING_MESSAGE, anchor, before_content, kind, expected]
        try:
            handle = await page.wait_for_function(
                MATCH_NEW_OUTGOING, arg=arguments, timeout=TEXT_CONFIRM_TIMEOUT_MS,
            )
        except Exception:
            # A delayed IM update can land just after the initial wait expires.
            await page.wait_for_timeout(TEXT_CONFIRM_GRACE_MS)
            handle = await page.evaluate_handle(MATCH_NEW_OUTGOING, arguments)
        message = handle.as_element()
        if message is None:
            raise PageOperationError(f"{message_label}已触发发送，但没有检测到匹配的新消息；不会自动重试")
        # Keep the matched element rather than looking up the latest bubble again:
        # an incoming reply must not redirect the delivery-status check.
        await _confirm_message_ack(page, message, message_label)
    except PageOperationError:
        raise
    except Exception as exc:
        raise PageOperationError(f"{message_label}已触发发送，但无法确认是否发送成功；不会自动重试") from exc
    finally:
        try:
            if handle is not None:
                await handle.dispose()
        finally:
            await _clear_confirmation_anchors(page)


async def _confirm_message_ack(
    page: Page,
    message: ElementHandle,
    message_label: str,
    timeout_ms: int = SEND_ACK_TIMEOUT_MS,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while True:
        if await _message_has_visible_marker(message, SEND_FAILURE_MARKERS):
            raise PageOperationError(f"{message_label}发送失败，页面显示重发标记；不会自动重试")
        status = await message.evaluate(MESSAGE_MODEL_STATUS)
        flight = status.get("flightStatus") if isinstance(status, dict) else None
        if flight in (-1, -2, -3):
            reason = "仅自己可见，未确认送达对方" if flight == -3 else "服务器拒绝或发送失败"
            raise PageOperationError(f"{message_label}{reason}；不会自动重试")
        pending = await _message_has_visible_marker(message, SEND_PENDING_MARKERS)
        if flight in (3, 4) and status.get("hasServerId") is True and not pending:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise PageOperationError(f"{message_label}未获得服务器发送确认；为避免重复不会自动重试")
        await page.wait_for_timeout(min(SEND_ACK_POLL_MS, max(1, remaining * 1000)))


async def _message_has_visible_marker(message: ElementHandle, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        for marker in await message.query_selector_all(selector):
            if await marker.is_visible():
                return True
    return False


async def send_image(page: Page, image_path: str) -> None:
    before = await _mark_latest_outgoing_message(page)
    file_input = None
    for selector in IMAGE_INPUTS:
        candidate = page.locator(selector).first
        if await candidate.count():
            file_input = candidate
            break
    if file_input is None:
        raise PageOperationError("找不到图片上传控件")
    await file_input.set_input_files(image_path)
    await page.wait_for_timeout(1_500)

    send_button = page.get_by_role("button", name="发送", exact=True)
    if await send_button.count() and await send_button.first.is_visible():
        await send_button.first.click()
    else:
        await page.keyboard.press("Enter")
    await _confirm_outgoing_sent(page, before, "image", "", "图片消息")


async def send_douyin_sticker(page: Page, sticker: Sticker) -> None:
    before = await _mark_latest_outgoing_message(page)
    button = await first_visible(page, STICKER_BUTTONS)
    await button.click(force=True)
    panel = await first_visible(page, STICKER_PANELS)

    if sticker.category:
        category = panel.get_by_text(sticker.category, exact=True)
        if await category.count() and await category.first.is_visible():
            await category.first.click()

    name = sticker.accessible_name or sticker.name
    item = panel.locator('.emojiEmojiItememojiItem').filter(has_text=name)
    for index in range(await item.count()):
        candidate = item.nth(index)
        description = candidate.locator('.emojiEmojiItememojiItemDesc')
        if await description.count() and (await description.first.inner_text()).strip() == name:
            await _click_and_confirm_sticker(page, candidate, before, name)
            return

    candidates = (
        panel.get_by_role("img", name=name, exact=True),
        panel.get_by_role("button", name=name, exact=True),
        panel.locator(f'[aria-label="{_css_escape(name)}"]'),
        panel.locator(f'[title="{_css_escape(name)}"]'),
        panel.locator(f'[alt="{_css_escape(name)}"]'),
    )
    for candidate in candidates:
        if await candidate.count() and await candidate.first.is_visible():
            await _click_and_confirm_sticker(page, candidate.first, before, name)
            return

    if sticker.fallback_index is not None:
        items = panel.locator('[role="button"], img, [aria-label], [title]')
        if await items.count() > sticker.fallback_index:
            await _click_and_confirm_sticker(page, items.nth(sticker.fallback_index), before, name)
            return
    raise PageOperationError(f"在抖音表情面板中找不到原生表情: {sticker.name}")


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def _mark_latest_outgoing_message(page: Page) -> tuple[str, str]:
    anchor = secrets.token_hex(8)
    latest = page.locator(LATEST_OUTGOING_MESSAGE).first
    if not await latest.count():
        return anchor, ""

    content = latest.locator('[data-e2e="msg-item-content"]').first
    before_content = await content.inner_html() if await content.count() else await latest.inner_html()
    await latest.evaluate(
        "(element, value) => element.setAttribute('data-douyin-sender-anchor', value)",
        anchor,
    )
    return anchor, before_content


async def _click_and_confirm_sticker(page: Page, item, before: tuple[str, str], name: str) -> None:
    resource_key = await _sticker_resource_key(item)
    await item.click(force=True)
    await _confirm_sticker_sent(page, before, name, resource_key)


async def _sticker_resource_key(item) -> str:
    src = await item.get_attribute("src")
    if not src:
        image = item.locator("img").first
        if await image.count():
            src = await image.get_attribute("src")
    if not src:
        return ""
    return urlsplit(src).path.rsplit("/", 1)[-1]


async def _confirm_sticker_sent(
    page: Page,
    before: tuple[str, str],
    name: str,
    resource_key: str = "",
) -> None:
    await _confirm_outgoing_sent(page, before, "image", resource_key, f"原生表情“{name}”")


async def _clear_confirmation_anchors(page: Page) -> None:
    anchors = page.locator(f"[{STICKER_CONFIRM_ANCHOR}]")
    try:
        await anchors.evaluate_all(
            "elements => elements.forEach(element => element.removeAttribute('data-douyin-sender-anchor'))"
        )
    except Exception:
        pass
