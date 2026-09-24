from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.browser import _normalize_cookies, open_private_messages
from app.config import ConfigError
from app.selectors import DOUYIN_CHAT_URL


@pytest.mark.asyncio
async def test_opens_chat_directly_before_checking_login() -> None:
    page = MagicMock()
    page.goto = AsyncMock()

    with patch("app.browser._any_visible", new=AsyncMock(side_effect=[False, False, True])):
        await open_private_messages(page)

    page.goto.assert_awaited_once_with(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)


def test_normalizes_cookie_editor_export() -> None:
    cookies = [
        {
            "domain": ".douyin.com",
            "expirationDate": 1800175766.5,
            "hostOnly": False,
            "httpOnly": True,
            "name": "UIFID",
            "path": "/",
            "sameSite": "no_restriction",
            "secure": True,
            "session": False,
            "storeId": None,
            "value": "token",
        }
    ]

    assert _normalize_cookies(cookies) == [
        {
            "name": "UIFID",
            "value": "token",
            "domain": ".douyin.com",
            "path": "/",
            "expires": 1800175766.5,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
    ]


def test_session_cookie_ignores_expiration_date() -> None:
    cookies = [
        {
            "domain": ".douyin.com",
            "expirationDate": 1800175766.5,
            "name": "sessionid",
            "session": True,
            "value": "token",
        }
    ]

    assert _normalize_cookies(cookies)[0]["expires"] == -1


def test_ignores_cookie_editor_empty_name_artifact() -> None:
    cookies = [
        {"domain": "www.douyin.com", "name": "", "value": "douyin.com"},
        {"domain": ".douyin.com", "name": "sessionid", "value": "token"},
    ]

    assert [cookie["name"] for cookie in _normalize_cookies(cookies)] == ["sessionid"]


def test_rejects_cookie_without_domain() -> None:
    with pytest.raises(ConfigError, match="缺少有效的 domain"):
        _normalize_cookies([{"name": "UIFID", "value": "token"}])


def test_auth_summary_does_not_expose_credentials() -> None:
    from app.browser import _auth_cookie_summary
    with patch("app.browser.time.time", return_value=100):
        summary = _auth_cookie_summary([
            {"name": "sessionid", "value": "private-token", "expires": 99},
            {"name": "sessionid_ss", "value": "private-token", "expires": 101},
            {"name": "sid_tt", "value": "private-token", "expires": -1},
            {"name": "private-name", "value": "private-token", "expires": 50},
        ])
    assert summary == {"total": 4, "auth_present": 3, "auth_expired": 1, "auth_session": 1, "all_expired": 2}
    assert "private" not in str(summary)
