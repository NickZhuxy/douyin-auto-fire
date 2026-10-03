from __future__ import annotations

import logging
import time

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.config import ConfigError, parse_auth_json
from app.models import Settings
from app.session_state import SessionState
from app.selectors import CHAT_READY_MARKERS, DOUYIN_CHAT_URL, LOGIN_REQUIRED_MARKERS, RISK_MARKERS


class AuthenticationError(RuntimeError):
    pass


class PageLoadError(RuntimeError):
    pass


class RiskControlError(RuntimeError):
    pass


@dataclass
class BrowserSession:
    page: Page
    context: BrowserContext
    authenticated: bool = False


@asynccontextmanager
async def open_douyin(settings: Settings) -> AsyncIterator[BrowserSession]:
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    session: BrowserSession | None = None
    saved_session: SessionState | None = None
    try:
        playwright = await async_playwright().start()
        launch_args = {"headless": settings.headless}
        if settings.browser_path:
            launch_args["executable_path"] = settings.browser_path
        browser = await playwright.chromium.launch(**launch_args)

        context_args = {"viewport": {"width": 1440, "height": 1000}, "locale": "zh-CN"}
        if settings.storage_state:
            state = parse_auth_json(settings.storage_state, "DOUYIN_STORAGE_STATE")
            if not isinstance(state, dict):
                raise ConfigError("DOUYIN_STORAGE_STATE 必须是 JSON 对象")
            context_args["storage_state"] = state
        context = await browser.new_context(**context_args)
        if not settings.storage_state and settings.cookie:
            cookies = parse_auth_json(settings.cookie, "DOUYIN_COOKIE")
            if not isinstance(cookies, list):
                raise ConfigError("DOUYIN_COOKIE 必须是 Cookie 数组")
            saved_session = SessionState.from_env()
            if saved_session:
                (settings.artifacts_dir / "session.updated").unlink(missing_ok=True)
            refreshed = saved_session.read_cookies() if saved_session else None
            if refreshed is not None:
                cookies = refreshed
                logging.getLogger("douyin_sender").info("已恢复上次验证后保存的加密登录 Cookie")
            normalized = _normalize_cookies(cookies)
            logging.getLogger("douyin_sender").info(
                "Cookie metadata (no values): %s", _auth_cookie_summary(normalized)
            )
            await context.add_cookies(normalized)

        page = await context.new_page()
        if settings.trace:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        session = BrowserSession(page=page, context=context)
        yield session
    finally:
        try:
            if context and saved_session and session and session.authenticated:
                verification_page = await context.new_page()
                try:
                    await open_private_messages(verification_page)
                    cookies = [c for c in await context.cookies()
                               if c["domain"].lstrip(".") == "douyin.com" or c["domain"].endswith(".douyin.com")]
                finally:
                    await verification_page.close()
                saved_session.write_cookies(cookies)
                marker = settings.artifacts_dir / "session.updated"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
                logging.getLogger("douyin_sender").info("已加密保存本次浏览器 Cookie，供下次任务继续使用")
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()


async def verify_login(page: Page, timeout_ms: int = 15_000) -> None:
    await _wait_for_chat_ready(page, timeout_ms)


async def open_private_messages(page: Page, timeout_ms: int = 60_000) -> None:
    await page.goto(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
    await _wait_for_chat_ready(page, timeout_ms)


async def _wait_for_chat_ready(page: Page, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1_000
    login_grace_seconds = min(5, timeout_ms / 1_000)
    login_visible_since: float | None = None
    while True:
        if await _any_visible_now(page, RISK_MARKERS):
            raise RiskControlError("抖音私信页面要求进行安全验证，任务已停止")
        login_required = await _any_visible_now(page, LOGIN_REQUIRED_MARKERS)
        now = time.monotonic()
        if login_required:
            if login_visible_since is None:
                login_visible_since = now
            if now >= deadline and now - login_visible_since >= login_grace_seconds:
                raise AuthenticationError("进入抖音私信页面后登录状态失效")
        else:
            login_visible_since = None
            if await _any_visible_now(page, CHAT_READY_MARKERS):
                return
        if now >= deadline:
            raise PageLoadError("抖音聊天页面未加载完成：等待好友搜索框超时，不能据此判定登录失效")
        await page.wait_for_timeout(min(250, (deadline - now) * 1_000))


async def save_trace(session: BrowserSession, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await session.context.tracing.stop(path=path)


async def _any_visible_now(page: Page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        if await page.locator(selector).first.is_visible():
            return True
    return False


def _normalize_cookies(cookies: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for index, cookie in enumerate(cookies):
        if not isinstance(cookie, dict):
            raise ConfigError(f"DOUYIN_COOKIE[{index}] 必须是对象")

        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        if name == "":
            continue
        if not isinstance(name, str) or not isinstance(value, str):
            raise ConfigError(f"DOUYIN_COOKIE[{index}] 缺少有效的 name 或 value")
        if not isinstance(domain, str) or not domain:
            raise ConfigError(f"DOUYIN_COOKIE[{index}] 缺少有效的 domain")

        expires = cookie.get("expires", cookie.get("expirationDate", -1))
        if cookie.get("session") is True:
            expires = -1
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            expires = -1

        normalized.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cookie.get("path") if isinstance(cookie.get("path"), str) else "/",
                "expires": expires,
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": bool(cookie.get("secure", False)),
                "sameSite": _normalize_same_site(cookie.get("sameSite")),
            }
        )
    if not normalized:
        raise ConfigError("DOUYIN_COOKIE 没有有效 Cookie")
    return normalized


def _normalize_same_site(value: Any) -> str:
    mapping = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
    }
    return mapping.get(str(value).lower(), "Lax")


def _auth_cookie_summary(cookies: list[dict[str, Any]]) -> dict[str, int]:
    """Only emit aggregate metadata; never cookie values or arbitrary names."""
    auth = [c for c in cookies if c.get("name") in {"sessionid", "sessionid_ss", "sid_tt"}]
    now = time.time()
    return {
        "total": len(cookies),
        "auth_present": len(auth),
        "auth_expired": sum(0 <= c.get("expires", -1) <= now for c in auth),
        "auth_session": sum(c.get("expires", -1) == -1 for c in auth),
        "all_expired": sum(0 <= c.get("expires", -1) <= now for c in cookies),
    }
