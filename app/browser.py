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
from app.selectors import DOUYIN_CHAT_URL, LOGIN_MARKERS, LOGIN_REQUIRED_MARKERS, RISK_MARKERS


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


@asynccontextmanager
async def open_douyin(settings: Settings) -> AsyncIterator[BrowserSession]:
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
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
            logging.getLogger("douyin_sender").info(
                "Storage state metadata (no values): origins=%d, cookies=%s",
                len(state.get("origins", [])), _auth_cookie_summary(state.get("cookies", [])),
            )
            context_args["storage_state"] = state
        context = await browser.new_context(**context_args)
        if not settings.storage_state and settings.cookie:
            cookies = parse_auth_json(settings.cookie, "DOUYIN_COOKIE")
            if not isinstance(cookies, list):
                raise ConfigError("DOUYIN_COOKIE 必须是 Cookie 数组")
            normalized = _normalize_cookies(cookies)
            logging.getLogger("douyin_sender").info(
                "Cookie metadata (no values): %s", _auth_cookie_summary(normalized)
            )
            await context.add_cookies(normalized)

        page = await context.new_page()
        if settings.trace:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        yield BrowserSession(page=page, context=context)
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()


async def verify_login(page: Page, timeout_ms: int = 15_000) -> None:
    if await _any_visible(page, RISK_MARKERS, timeout_ms=2_000):
        raise RiskControlError("抖音要求进行安全验证，任务已停止")
    if await _any_visible(page, LOGIN_REQUIRED_MARKERS, timeout_ms=2_000):
        raise AuthenticationError("抖音登录状态已失效")
    if not await _any_visible(page, LOGIN_MARKERS, timeout_ms=timeout_ms):
        raise AuthenticationError("未检测到抖音私信页面，登录状态可能失效或页面结构已变化")


async def open_private_messages(page: Page, timeout_ms: int = 60_000) -> None:
    await page.goto(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
    if await _any_visible(page, RISK_MARKERS, timeout_ms=2_000):
        raise RiskControlError("抖音私信页面要求进行安全验证，任务已停止")
    if await _any_visible(page, LOGIN_REQUIRED_MARKERS, timeout_ms=2_000):
        raise AuthenticationError("进入抖音私信页面后登录状态失效")
    if not await _any_visible(page, ('input[placeholder*="搜索"]', '[role="textbox"][placeholder*="搜索"]'), timeout_ms):
        raise PageLoadError("抖音聊天页面未加载完成：等待好友搜索框超时，不能据此判定登录失效")


async def save_trace(session: BrowserSession, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await session.context.tracing.stop(path=path)


async def _any_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int) -> bool:
    per_selector = max(250, timeout_ms // max(1, len(selectors)))
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=per_selector)
            return True
        except Exception:
            continue
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
