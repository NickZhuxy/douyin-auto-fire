import json
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._driver import compute_driver_executable

from app.douyin import PageOperationError
from app.models import Message
from app.sender import (
    LATEST_OUTGOING_MESSAGE,
    MATCH_NEW_OUTGOING,
    MESSAGE_MODEL_STATUS,
    SEND_ACK_POLL_MS,
    SEND_FAILURE_MARKERS,
    SEND_PENDING_MARKERS,
    TEXT_CONFIRM_GRACE_MS,
    TEXT_CONFIRM_TIMEOUT_MS,
    _confirm_message_ack,
    _confirm_sticker_sent,
    _confirm_text_sent,
    _sticker_resource_key,
    send_image,
    send_message,
    send_text,
)


def _confirmation_page(status=None):
    page = MagicMock()
    message = MagicMock()
    message.evaluate = AsyncMock(return_value=status or {"flightStatus": 3, "hasServerId": True})
    message.query_selector_all = AsyncMock(return_value=[])
    handle = MagicMock()
    handle.as_element.return_value = message
    handle.dispose = AsyncMock()
    page.wait_for_function = AsyncMock(return_value=handle)
    page.evaluate_handle = AsyncMock(return_value=handle)
    page.wait_for_timeout = AsyncMock()
    page.locator.return_value.evaluate_all = AsyncMock()
    return page, message, handle


def _visible_marker():
    marker = MagicMock()
    marker.is_visible = AsyncMock(return_value=True)
    return marker


@pytest.mark.asyncio
async def test_random_message_delegates_to_selected_choice(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr("app.sender.send_text", send)
    text = Message(type="text", content="你好")
    monkeypatch.setattr("app.sender.random.choice", lambda choices: choices[0])
    chat = MagicMock()
    await send_message(MagicMock(), chat, Message(type="random", choices=(text,)), {})
    send.assert_awaited_once_with(chat, "你好")


@pytest.mark.asyncio
async def test_text_send_does_not_retry_when_confirmation_fails(monkeypatch):
    chat = MagicMock()
    editor = MagicMock()
    editor.click = AsyncMock()
    editor.page.keyboard.insert_text = AsyncMock()
    editor.page.keyboard.press = AsyncMock()
    chat.message_input = AsyncMock(return_value=editor)
    monkeypatch.setattr("app.sender._mark_latest_outgoing_message", AsyncMock(return_value=("anchor", "")))
    monkeypatch.setattr("app.sender._confirm_text_sent", AsyncMock(side_effect=PageOperationError("unconfirmed")))
    with pytest.raises(PageOperationError, match="unconfirmed"):
        await send_text(chat, "续火花")
    editor.page.keyboard.insert_text.assert_awaited_once_with("续火花")
    editor.page.keyboard.press.assert_awaited_once_with("Enter")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "sticker"])
async def test_confirmation_requires_ack_on_the_matched_element(kind):
    page, message, handle = _confirmation_page()
    if kind == "text":
        await _confirm_text_sent(page, ("anchor", "old-content"), "续火花")
        expected = [LATEST_OUTGOING_MESSAGE, "anchor", "old-content", "text", "续火花"]
    else:
        await _confirm_sticker_sent(page, ("anchor", "old-content"), "比心", "resource-key")
        expected = [LATEST_OUTGOING_MESSAGE, "anchor", "old-content", "image", "resource-key"]
    assert page.wait_for_function.await_args.kwargs == {"arg": expected, "timeout": TEXT_CONFIRM_TIMEOUT_MS}
    message.evaluate.assert_awaited_once_with(MESSAGE_MODEL_STATUS)
    # Ack uses the captured element, so a later incoming reply cannot redirect it.
    assert page.locator.call_count == 1  # only confirmation-anchor cleanup
    handle.dispose.assert_awaited_once()
    page.locator.return_value.evaluate_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_confirmation_accepts_late_bubble_only_after_ack():
    page, message, _ = _confirmation_page()
    page.wait_for_function.side_effect = TimeoutError
    await _confirm_text_sent(page, ("anchor", "old-content"), "续火花")
    page.wait_for_timeout.assert_awaited_once_with(TEXT_CONFIRM_GRACE_MS)
    page.evaluate_handle.assert_awaited_once()
    message.evaluate.assert_awaited_once_with(MESSAGE_MODEL_STATUS)


@pytest.mark.asyncio
async def test_confirmation_rejects_missing_new_bubble():
    page, message, handle = _confirmation_page()
    page.wait_for_function.side_effect = TimeoutError
    handle.as_element.return_value = None
    with pytest.raises(PageOperationError, match="没有检测到匹配的新消息"):
        await _confirm_text_sent(page, ("anchor", "old-content"), "续火花")
    message.evaluate.assert_not_awaited()
    handle.dispose.assert_awaited_once()
    page.locator.return_value.evaluate_all.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("flight", [3, 4])
async def test_ack_accepts_server_acknowledged_message(flight):
    page, message, _ = _confirmation_page({"flightStatus": flight, "hasServerId": True})
    await _confirm_message_ack(page, message, "文字消息", timeout_ms=0)
    page.wait_for_timeout.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("flight", [-1, -2, -3])
@pytest.mark.parametrize("kind", ["text", "sticker"])
async def test_confirmation_rejects_failed_rejected_or_self_visible_message(flight, kind):
    page, _, handle = _confirmation_page({"flightStatus": flight, "hasServerId": True})
    with pytest.raises(PageOperationError, match="不会自动重试"):
        if kind == "text":
            await _confirm_text_sent(page, ("anchor", "old-content"), "续火花")
        else:
            await _confirm_sticker_sent(page, ("anchor", "old-content"), "比心")
    handle.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("flight", [0, 1, 2])
async def test_ack_waits_for_pending_model_to_receive_server_id(flight):
    page, message, _ = _confirmation_page()
    message.evaluate.side_effect = [
        {"flightStatus": flight, "hasServerId": False},
        {"flightStatus": 3, "hasServerId": True},
    ]
    await _confirm_message_ack(page, message, "文字消息")
    page.wait_for_timeout.assert_awaited_once_with(SEND_ACK_POLL_MS)


@pytest.mark.asyncio
async def test_pending_ack_stays_on_sent_bubble_when_new_reply_arrives():
    page, message, _ = _confirmation_page()
    message.evaluate.side_effect = [
        {"flightStatus": 2, "hasServerId": False},
        {"flightStatus": 4, "hasServerId": True},
    ]
    async def receive_reply(_timeout):
        # The newest DOM entry is now incoming; only the retained outgoing
        # ElementHandle can supply the acknowledgement on the next poll.
        page.wait_for_function.side_effect = AssertionError("must not reselect latest message")
        page.evaluate_handle.side_effect = AssertionError("must not reselect latest message")

    page.wait_for_timeout.side_effect = receive_reply
    await _confirm_text_sent(page, ("anchor", "old-content"), "续火花")
    assert message.evaluate.await_count == 2
    page.wait_for_function.assert_awaited_once()
    page.evaluate_handle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    None,
    {"flightStatus": None, "hasServerId": True},
    {"flightStatus": 3, "hasServerId": False},
    {"flightStatus": 0, "hasServerId": False},
    {"flightStatus": 1, "hasServerId": False},
    {"flightStatus": 2, "hasServerId": True},
])
async def test_ack_never_treats_missing_model_or_pending_state_as_success(state):
    page, message, _ = _confirmation_page()
    message.evaluate.return_value = state
    with pytest.raises(PageOperationError, match="未获得服务器发送确认"):
        await _confirm_message_ack(page, message, "文字消息", timeout_ms=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", SEND_FAILURE_MARKERS)
async def test_ack_rejects_visible_failure_marker_including_actual_retry_icon(selector):
    page, message, _ = _confirmation_page()
    marker = _visible_marker()
    message.query_selector_all.side_effect = lambda query: [marker] if query == selector else []
    with pytest.raises(PageOperationError, match="发送失败"):
        await _confirm_message_ack(page, message, "文字消息", timeout_ms=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", SEND_PENDING_MARKERS)
async def test_ack_does_not_accept_visible_pending_indicator(selector):
    page, message, _ = _confirmation_page()
    marker = _visible_marker()
    message.query_selector_all.side_effect = lambda query: [marker] if query == selector else []
    with pytest.raises(PageOperationError, match="未获得服务器发送确认"):
        await _confirm_message_ack(page, message, "文字消息", timeout_ms=0)


@pytest.mark.asyncio
async def test_image_send_also_requires_common_confirmation(monkeypatch):
    page = MagicMock()
    page.wait_for_timeout = AsyncMock()
    file_input = page.locator.return_value.first
    file_input.count = AsyncMock(return_value=1)
    file_input.set_input_files = AsyncMock()
    page.get_by_role.return_value.count = AsyncMock(return_value=0)
    page.keyboard.press = AsyncMock()
    before = ("anchor", "old-content")
    monkeypatch.setattr("app.sender._mark_latest_outgoing_message", AsyncMock(return_value=before))
    confirm = AsyncMock(side_effect=PageOperationError("unconfirmed image"))
    monkeypatch.setattr("app.sender._confirm_outgoing_sent", confirm)
    with pytest.raises(PageOperationError, match="unconfirmed image"):
        await send_image(page, "image.png")
    file_input.set_input_files.assert_awaited_once_with("image.png")
    page.keyboard.press.assert_awaited_once_with("Enter")
    confirm.assert_awaited_once_with(page, before, "image", "", "图片消息")


@pytest.mark.asyncio
async def test_missing_sticker_mapping_fails():
    with pytest.raises(PageOperationError, match="没有原生表情映射"):
        await send_message(AsyncMock(), AsyncMock(), Message(type="douyin_sticker", sticker="比心"), {})


@pytest.mark.asyncio
async def test_image_message_requires_path():
    with pytest.raises(PageOperationError, match="缺少文件路径"):
        await send_message(AsyncMock(), AsyncMock(), Message(type="image", path=None), {})


@pytest.mark.asyncio
async def test_sticker_resource_key_ignores_signed_query_string():
    item = MagicMock()
    item.get_attribute = AsyncMock(return_value="https://p26-sign.douyinpic.com/obj/im-resource/sticker-key?x-signature=temporary")
    assert await _sticker_resource_key(item) == "sticker-key"


def _javascript_result(script, fixture):
    # Playwright bundles Node on every supported platform; no browser download or
    # additional test dependency is needed to exercise the actual page-side code.
    node, _ = compute_driver_executable()
    result = subprocess.run(
        [str(node), "-e", f"const inspect = ({script});\n{fixture}"],
        check=True, capture_output=True, text=True, timeout=5,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("flight, server_id, expected", [
    (3, "1234567890123456789", True),
    (4, "12", True),
    (3, "0", False),
    (3, "-1", False),
    (3, None, False),
    (-3, "12", True),
    (2, "0", False),
])
def test_model_reader_traverses_fiber_and_returns_only_safe_fields(flight, server_id, expected):
    model = json.dumps({"flightStatus": flight, "serverId": server_id, "content": "must stay inside page", "token": "must not return"})
    fixture = f"""
        const content = {{__reactFiber$fixture: {{memoizedProps: {{}}, return: {{memoizedProps: {{message: {model}}}}}}}}};
        const element = {{isConnected: true, querySelector: () => content}};
        console.log(JSON.stringify(inspect(element)));
    """
    assert _javascript_result(MESSAGE_MODEL_STATUS, fixture) == {"flightStatus": flight, "hasServerId": expected}


def test_model_reader_stops_on_cycle_and_missing_or_detached_model():
    fixture = """
        const fiber = {memoizedProps: {}};
        fiber.return = fiber;
        const element = {isConnected: true, querySelector: () => null, __reactFiber$fixture: fiber};
        const cyclic = inspect(element);
        element.isConnected = false;
        console.log(JSON.stringify([cyclic, inspect(element)]));
    """
    assert _javascript_result(MESSAGE_MODEL_STATUS, fixture) == [None, None]


def test_model_reader_has_bounded_parent_traversal():
    fixture = """
        let fiber = {memoizedProps: {message: {flightStatus: 3, serverId: '1'}}};
        for (let i = 0; i < 40; i++) fiber = {memoizedProps: {}, return: fiber};
        const element = {isConnected: true, querySelector: () => null, __reactFiber$fixture: fiber};
        console.log(JSON.stringify(inspect(element)));
    """
    assert _javascript_result(MESSAGE_MODEL_STATUS, fixture) is None


@pytest.mark.parametrize("kind, expected, old_anchor, html, text, images, matches", [
    ("text", "续火花", "anchor", "old-content", "续火花", [], False),
    ("text", "续火花", None, "new-content", "续火花", [], True),
    ("text", "续火花", None, "new-content", "其他消息", [], False),
    ("image", "sticker-a", None, "new-content", "", ["https://example.invalid/sticker-b"], False),
    ("image", "sticker-a", None, "new-content", "", ["https://example.invalid/sticker-a"], True),
    ("image", "", None, "new-content", "", [], False),
])
def test_new_bubble_matcher_rejects_old_or_wrong_content(kind, expected, old_anchor, html, text, images, matches):
    fixture = f"""
        const body = {{innerHTML: {json.dumps(html)}, textContent: {json.dumps(text)}, querySelectorAll: () => {json.dumps([{'src': src} for src in images])}}};
        const message = {{querySelector: () => body, getAttribute: () => {json.dumps(old_anchor)}}};
        const document = {{querySelector: () => message}};
        console.log(JSON.stringify(Boolean(inspect(['selector', 'anchor', 'old-content', {json.dumps(kind)}, {json.dumps(expected)}]))));
    """
    assert _javascript_result(MATCH_NEW_OUTGOING, fixture) is matches
