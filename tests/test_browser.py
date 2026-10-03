from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.browser import _normalize_cookies, open_private_messages
from app.config import ConfigError
from app.selectors import DOUYIN_CHAT_URL


class PollingPage:
    def __init__(self, transitions):
        self.now = 0.0
        self.transitions = transitions
        self.goto = AsyncMock()
        self.close = AsyncMock()
        self.checked_selectors = []
        self.wait_for_timeout = AsyncMock(side_effect=self._advance)

    async def _advance(self, milliseconds):
        self.now += milliseconds / 1_000

    def locator(self, selector):
        from types import SimpleNamespace
        from app.selectors import CHAT_READY_MARKERS, LOGIN_REQUIRED_MARKERS, RISK_MARKERS

        async def is_visible():
            self.checked_selectors.append(selector)
            state = next(state for at, state in reversed(self.transitions) if at <= self.now)
            return (
                (state == "ready" and selector in CHAT_READY_MARKERS)
                or (state == "login" and selector in LOGIN_REQUIRED_MARKERS)
                or (state == "risk" and selector in RISK_MARKERS)
                or (state == "general_search" and selector == 'input[placeholder*="搜索"]')
            )

        return SimpleNamespace(first=SimpleNamespace(is_visible=AsyncMock(side_effect=is_visible)))


@pytest.fixture
def polling_page(monkeypatch):
    def build(transitions):
        page = PollingPage(transitions)
        monkeypatch.setattr("app.browser.time.monotonic", lambda: page.now)
        return page
    return build


@pytest.mark.asyncio
async def test_opens_chat_directly_before_checking_login(polling_page) -> None:
    page = polling_page([(0, "ready")])
    await open_private_messages(page)
    page.goto.assert_awaited_once_with(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)


@pytest.mark.asyncio
async def test_login_shell_can_hydrate_into_authenticated_chat(polling_page):
    page = polling_page([(0, "login"), (8, "ready")])
    await open_private_messages(page, timeout_ms=10_000)
    assert page.now == 8


@pytest.mark.asyncio
async def test_persistent_login_only_fails_after_full_readiness_budget(polling_page):
    from app.browser import AuthenticationError
    page = polling_page([(0, "login")])
    with pytest.raises(AuthenticationError, match="登录状态失效"):
        await open_private_messages(page, timeout_ms=10_000)
    assert page.now == 10


@pytest.mark.asyncio
async def test_late_login_shell_is_not_enough_to_declare_auth_failure(polling_page):
    from app.browser import PageLoadError
    page = polling_page([(0, "loading"), (9, "login")])
    with pytest.raises(PageLoadError, match="未加载完成"):
        await open_private_messages(page, timeout_ms=10_000)


@pytest.mark.asyncio
async def test_login_grace_resets_when_login_shell_disappears(polling_page):
    from app.browser import PageLoadError
    page = polling_page([(0, "login"), (7, "loading"), (8, "login")])
    with pytest.raises(PageLoadError, match="未加载完成"):
        await open_private_messages(page, timeout_ms=10_000)


@pytest.mark.asyncio
async def test_risk_challenge_stops_without_waiting(polling_page):
    from app.browser import RiskControlError
    page = polling_page([(0, "risk")])
    with pytest.raises(RiskControlError, match="安全验证"):
        await open_private_messages(page)
    page.wait_for_timeout.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_pre_send_check_waits_for_its_entire_budget(polling_page):
    from app.browser import AuthenticationError, verify_login
    page = polling_page([(0, "login")])
    with pytest.raises(AuthenticationError):
        await verify_login(page, timeout_ms=3_000)
    assert page.now == 3


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


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["loading", "general_search"])
async def test_unloaded_chat_is_not_reported_as_expired_login(polling_page, state):
    from app.browser import PageLoadError
    page = polling_page([(0, state)])
    with pytest.raises(PageLoadError, match="未加载完成"):
        await open_private_messages(page, timeout_ms=1_000)
    assert 'input[placeholder*="搜索"]' not in page.checked_selectors


@pytest.fixture
def mocked_browser(tmp_path):
    import json
    from types import SimpleNamespace
    seed = [{"name": "sessionid", "value": "seed", "domain": ".douyin.com"}]
    refreshed = [{"name": "sessionid", "value": "refreshed", "domain": ".douyin.com"}]
    settings = SimpleNamespace(headless=True, browser_path=None, storage_state=None,
                               cookie=json.dumps(seed), trace=False, artifacts_dir=tmp_path)
    context = MagicMock()
    context.add_cookies = AsyncMock()
    context.new_page = AsyncMock()
    context.cookies = AsyncMock(return_value=refreshed)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    playwright.stop = AsyncMock()
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    cache = MagicMock()
    cache.read_cookies.return_value = refreshed
    with patch("app.browser.async_playwright", return_value=starter), patch("app.browser.SessionState.from_env", return_value=cache):
        yield SimpleNamespace(settings=settings, context=context, cache=cache, refreshed=refreshed)


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
async def test_only_freshly_verified_sessions_persist_refreshed_cookies(tmp_path, mocked_browser, polling_page, authenticated):
    from app.browser import open_douyin
    original_page = MagicMock()
    fresh_page = polling_page([(0, "ready")])
    mocked_browser.context.new_page.side_effect = [original_page, fresh_page]
    mocked_browser.context.cookies.return_value = mocked_browser.refreshed + [
        {"name": "unrelated", "value": "other", "domain": "example.com"}
    ]
    async with open_douyin(mocked_browser.settings) as session:
        assert session.page is original_page
        session.authenticated = authenticated
    assert mocked_browser.context.add_cookies.await_args.args[0][0]["value"] == "refreshed"
    assert mocked_browser.context.new_page.await_count == 1 + int(authenticated)
    assert mocked_browser.cache.write_cookies.call_count == int(authenticated)
    if authenticated:
        fresh_page.goto.assert_awaited_once_with(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
        fresh_page.close.assert_awaited_once()
        mocked_browser.cache.write_cookies.assert_called_once_with(mocked_browser.refreshed)
    else:
        fresh_page.goto.assert_not_awaited()
        fresh_page.close.assert_not_awaited()
    assert (tmp_path / "session.updated").exists() == authenticated
    mocked_browser.context.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_original_page_cannot_persist_cookies_rejected_by_fresh_page(tmp_path, mocked_browser, polling_page):
    from app.browser import AuthenticationError, open_douyin
    original_page = PollingPage([(0, "ready")])
    fresh_page = polling_page([(0, "login")])
    mocked_browser.context.new_page.side_effect = [original_page, fresh_page]
    marker = tmp_path / "session.updated"
    marker.touch()
    with pytest.raises(AuthenticationError, match="登录状态失效"):
        async with open_douyin(mocked_browser.settings) as session:
            session.authenticated = True
    fresh_page.goto.assert_awaited_once()
    assert fresh_page.now == 60
    fresh_page.close.assert_awaited_once()
    mocked_browser.cache.write_cookies.assert_not_called()
    mocked_browser.context.cookies.assert_not_awaited()
    assert not marker.exists()
    mocked_browser.context.close.assert_awaited_once()
