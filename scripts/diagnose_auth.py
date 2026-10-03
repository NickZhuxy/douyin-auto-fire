"""Read-only login diagnosis; emits aggregate JSON only, never credentials.

Run from the repository root, or pass --repo-dir. This script never calls the
sender, writes SessionState, saves browser storage, or changes GitHub secrets.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit


AUTH_NAMES = frozenset({"sessionid", "sessionid_ss", "sid_tt"})
COOKIE_FIELDS = frozenset({"name", "value", "domain", "path", "expires", "expirationDate", "session", "httpOnly", "secure", "sameSite", "partitionKey"})
PATH_WORDS = frozenset({
    "aweme", "web", "api", "im", "user", "self", "user_info", "userinfo",
    "info", "account", "passport", "login", "check", "status", "init",
    "friend", "friends", "list", "query", "conversation", "conversations",
    "message", "messages", "get", "send", "token", "batch", "config",
    "client", "settings", "relation", "follow", "following", "v1", "v2", "v3",
})


def emit(event: str, **metadata: object) -> None:
    print(json.dumps({"event": event, **metadata}, ensure_ascii=True, allow_nan=False), flush=True)


def cookie_metadata(cookies: list[dict]) -> dict:
    now = time.time()
    named = [cookie for cookie in cookies if isinstance(cookie.get("name"), str) and cookie["name"]]
    auth = [cookie for cookie in named if cookie["name"] in AUTH_NAMES]

    def expired(cookie: dict) -> bool:
        value = cookie.get("expires", cookie.get("expirationDate", -1))
        return type(value) in (int, float) and 0 <= value <= now

    names = Counter(cookie["name"] for cookie in named)
    auth_names = Counter(cookie["name"] for cookie in auth)
    identities = Counter(_identity(cookie) for cookie in named)
    return {
        "total": len(cookies), "named": len(named), "unnamed": len(cookies) - len(named),
        "auth_present": len(auth), "auth_expired": sum(map(expired, auth)),
        "all_expired": sum(map(expired, cookies)),
        "auth_session": sum(cookie.get("expires", -1) == -1 for cookie in auth),
        "duplicate_name_groups": sum(count > 1 for count in names.values()),
        "auth_duplicate_name_groups": sum(count > 1 for count in auth_names.values()),
        "duplicate_identity_entries": sum(count - 1 for count in identities.values()),
        "partition_field_present": sum("partitionKey" in cookie for cookie in cookies),
        "partition_nonempty": sum(bool(cookie.get("partitionKey")) for cookie in cookies),
        "partition_null": sum("partitionKey" in cookie and cookie["partitionKey"] is None for cookie in cookies),
        "cookies_with_extra_fields": sum(bool(set(cookie) - COOKIE_FIELDS) for cookie in cookies),
    }


def _identity(cookie: dict) -> tuple:
    # These identities and their values are used only in memory, never emitted.
    return tuple(cookie.get(field) if isinstance(cookie.get(field), str) else "" for field in ("name", "domain", "path", "partitionKey"))


def compare_cookies(before: list[dict], after: list[dict]) -> dict:
    def groups(cookies: list[dict], auth_only: bool) -> dict:
        result = defaultdict(list)
        for cookie in cookies:
            if not cookie.get("name") or (auth_only and cookie.get("name") not in AUTH_NAMES):
                continue
            result[_identity(cookie)].append(cookie.get("value"))
        return {identity: Counter(values) for identity, values in result.items()}

    result = {}
    for prefix, auth_only in (("all", False), ("auth", True)):
        left, right = groups(before, auth_only), groups(after, auth_only)
        common = left.keys() & right.keys()
        result.update({
            f"{prefix}_matched_identities": len(common),
            f"{prefix}_added_identities": len(right.keys() - left.keys()),
            f"{prefix}_removed_identities": len(left.keys() - right.keys()),
            f"{prefix}_value_changed_identities": sum(left[key] != right[key] for key in common),
            f"{prefix}_value_equal_identities": sum(left[key] == right[key] for key in common),
            f"{prefix}_all_equal": left == right,
        })
    return result


def print_message_configuration() -> None:
    raw = os.getenv("DOUYIN_CONFIG", "")
    if not raw:
        emit("message_configuration", configured=False)
        return
    try:
        config = json.loads(raw)
        targets = config.get("targets")
        if targets is None:
            targets = [{"messages": config.get("messages", [])} for _ in config.get("friends", [])]
        if not isinstance(targets, list):
            raise ValueError
        counts: Counter = Counter()
        top_level = 0

        def visit(message: object) -> None:
            if not isinstance(message, dict):
                counts["invalid"] += 1
                return
            kind = message.get("type")
            counts[kind if kind in {"text", "image", "douyin_sticker", "sticker", "random"} else "other"] += 1
            if kind == "random" and isinstance(message.get("choices"), list):
                for choice in message["choices"]:
                    visit(choice)

        for target in targets:
            messages = target.get("messages", []) if isinstance(target, dict) else []
            if not isinstance(messages, list):
                raise ValueError
            top_level += len(messages)
            for message in messages:
                visit(message)
        emit("message_configuration", configured=True, target_count=len(targets),
             top_level_message_count=top_level, configured_node_types=dict(counts))
    except Exception:
        emit("message_configuration", configured=True, parse_valid=False)


def endpoint_path(url: str) -> str:
    # Keep only known endpoint words; dynamic path segments cannot disclose IDs.
    path = urlsplit(url).path
    return "/" + "/".join(part if part in PATH_WORDS else ":segment" for part in path.split("/") if part) + ("/" if path.endswith("/") else "")


async def any_visible(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            for index in range(min(await locator.count(), 20)):
                if await locator.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


async def inspect_context(browser, label: str, raw_cookies: list[dict], timeout: float, network: bool, imports: tuple) -> str:
    normalize, chat_url, login_markers, risk_markers, search_markers = imports
    context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
    tasks: set[asyncio.Task] = set()
    observed_codes: set[tuple] = set()
    try:
        cookies = normalize(raw_cookies)
        emit("cookie_snapshot", source=label, phase="normalized", **cookie_metadata(cookies))
        await context.add_cookies(cookies)
        imported = await context.cookies()
        emit("cookie_snapshot", source=label, phase="after_import", **cookie_metadata(imported))
        emit("cookie_comparison", source=label, comparison="normalized_to_imported", **compare_cookies(cookies, imported))
        page = await context.new_page()

        async def observe_response(response) -> None:
            try:
                if len(observed_codes) >= 40 or response.request.resource_type not in {"xhr", "fetch"}:
                    return
                host = urlsplit(response.url).hostname or ""
                if host != "douyin.com" and not host.endswith(".douyin.com"):
                    return
                numeric = {}
                if "json" in (response.headers.get("content-type") or "").lower():
                    body = await response.json()
                    if isinstance(body, dict):
                        for field in ("status_code", "code", "status"):
                            value = body.get(field)
                            if type(value) in (int, float) and math.isfinite(value) and -1_000_000 <= value <= 1_000_000:
                                numeric[field] = value
                path = endpoint_path(response.url)
                identity = (path, response.status, tuple(sorted(numeric.items())))
                if identity not in observed_codes and len(observed_codes) < 40:
                    observed_codes.add(identity)
                    emit("api_status", source=label, endpoint_path=path, http_status=response.status, numeric_api_status=numeric)
            except Exception:
                # No exception text: HTTP/JSON errors can include response data.
                return

        def schedule_response(response) -> None:
            if len(tasks) >= 40:
                return
            task = asyncio.create_task(observe_response(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        if network:
            page.on("response", schedule_response)
        navigation_started = time.monotonic()
        try:
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=45_000)
            emit("navigation", source=label, completed=True, elapsed_seconds=round(time.monotonic() - navigation_started, 1))
        except Exception as exc:
            emit("navigation", source=label, completed=False, error_type=type(exc).__name__, elapsed_seconds=round(time.monotonic() - navigation_started, 1))

        started = time.monotonic()
        previous = None
        last_report = -10.0
        ever = {"login_required": False, "risk_challenge": False, "search_visible": False}
        first_seen = {}
        while True:
            elapsed = time.monotonic() - started
            state = {
                "risk_challenge": await any_visible(page, risk_markers),
                "login_required": await any_visible(page, login_markers),
                "search_visible": await any_visible(page, search_markers),
            }
            for name, visible in state.items():
                ever[name] |= visible
                if visible and name not in first_seen:
                    first_seen[name] = round(elapsed, 1)
            if state != previous or elapsed - last_report >= 10 or elapsed >= timeout:
                emit("page_state", source=label, elapsed_seconds=round(elapsed, 1), **state)
                previous, last_report = state, elapsed
            if state["risk_challenge"]:
                emit("context_result", source=label, outcome="stopped_on_risk_challenge", ever_visible=ever, first_seen_seconds=first_seen)
                return "risk"
            if elapsed >= timeout:
                current = await context.cookies()
                emit("cookie_snapshot", source=label, phase="after_observation", **cookie_metadata(current))
                emit("cookie_comparison", source=label, comparison="imported_to_observed", **compare_cookies(imported, current))
                emit("context_result", source=label, outcome="observation_complete", final_state=state, ever_visible=ever, first_seen_seconds=first_seen)
                return "observed"
            await asyncio.sleep(min(2, max(0, timeout - elapsed)))
    except Exception as exc:
        emit("context_result", source=label, outcome="diagnostic_error", error_type=type(exc).__name__)
        return "error"
    finally:
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await context.close()


async def diagnose(options: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(options.repo_dir).resolve()))
    from app.browser import _normalize_cookies
    from app.selectors import DOUYIN_CHAT_URL, LOGIN_REQUIRED_MARKERS, RISK_MARKERS, SEARCH_INPUTS
    from app.session_state import SessionState
    from playwright.async_api import async_playwright

    try:
        seed = json.loads(os.getenv("DOUYIN_COOKIE", ""))
        if not isinstance(seed, list) or any(not isinstance(cookie, dict) for cookie in seed):
            raise ValueError
        # Validate before comparisons; no values, names, or parse excerpts leak.
        normalized_seed = _normalize_cookies(seed)
    except Exception as exc:
        emit("seed_input", valid=False, error_type=type(exc).__name__)
        return 2
    emit("cookie_snapshot", source="seed", phase="raw", **cookie_metadata(seed))
    print_message_configuration()
    cached = None
    cache_read_error = False
    try:
        state = SessionState.from_env()
        cached = state.read_cookies() if state else None
        emit("encrypted_cache", enabled=state is not None, cookies_available=cached is not None)
    except Exception as exc:
        cache_read_error = True
        emit("encrypted_cache", readable=False, error_type=type(exc).__name__)
    if cached is not None:
        emit("cookie_snapshot", source="cached", phase="raw", **cookie_metadata(cached))
        emit("cookie_comparison", comparison="seed_to_cached", **compare_cookies(normalized_seed, cached))
    imports = (_normalize_cookies, DOUYIN_CHAT_URL, LOGIN_REQUIRED_MARKERS, RISK_MARKERS, SEARCH_INPUTS)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        had_error = cache_read_error
        try:
            for label, cookies in (("seed", seed), ("cached", cached)):
                if cookies is None:
                    emit("context_result", source=label, outcome="no_matching_cache")
                    continue
                outcome = await inspect_context(browser, label, cookies, options.timeout_seconds, options.network, imports)
                if outcome == "risk":
                    emit("diagnostic_result", outcome="stopped_on_risk_challenge")
                    return 2
                had_error |= outcome == "error"
        finally:
            await browser.close()
    emit("diagnostic_result", outcome="diagnostic_error" if had_error else "read_only_observation_complete")
    return 2 if had_error else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Douyin login diagnosis with aggregate metadata only")
    parser.add_argument("--repo-dir", default=str(Path.cwd()))
    parser.add_argument("--timeout-seconds", type=float, default=90)
    parser.add_argument("--network", action="store_true", help="Include bounded numeric API status summaries")
    options = parser.parse_args()
    if not 0 < options.timeout_seconds <= 90:
        emit("diagnostic_result", outcome="invalid_timeout")
        return 2
    try:
        return asyncio.run(diagnose(options))
    except Exception as exc:
        emit("diagnostic_result", outcome="diagnostic_error", error_type=type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
