import base64
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import ConfigError
from app.session_state import SessionState, _MAGIC


KEY = bytes(range(32))
SEED = "sessionid=initial-secret-value"
COOKIES = [{
    "name": "sessionid",
    "value": "refreshed-secret-value",
    "domain": ".douyin.com",
    "path": "/",
    "expires": 1999999999.0,
    "httpOnly": True,
    "secure": True,
    "sameSite": "Lax",
}]


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionState:
    monkeypatch.setenv("DOUYIN_STATE_KEY", base64.urlsafe_b64encode(KEY).decode("ascii"))
    monkeypatch.setenv("DOUYIN_COOKIE", SEED)
    monkeypatch.setenv("DOUYIN_STATE_FILE", str(tmp_path / "nested" / "session.enc"))
    result = SessionState.from_env()
    assert result is not None
    return result


def test_disabled_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOUYIN_STATE_KEY", raising=False)
    assert SessionState.from_env() is None


def test_default_state_path(state: SessionState, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOUYIN_STATE_FILE")
    assert SessionState.from_env().path == Path("artifacts/session.enc")


def test_roundtrip_is_cookie_only_and_not_plaintext(state: SessionState) -> None:
    assert state.read_cookies() is None
    state.write_cookies(COOKIES)
    assert SessionState.from_env().read_cookies() == COOKIES
    encoded = state.path.read_bytes()
    assert SEED.encode() not in encoded
    assert COOKIES[0]["value"].encode() not in encoded
    nonce_start = len(_MAGIC)
    payload = json.loads(AESGCM(KEY).decrypt(encoded[nonce_start:nonce_start + 12], encoded[nonce_start + 12:], _MAGIC))
    assert set(payload) == {"version", "seed_sha256", "cookies"}
    assert payload["version"] == 1
    assert payload["seed_sha256"] == hashlib.sha256(SEED.encode()).hexdigest()


def test_new_nonce_each_write(state: SessionState) -> None:
    state.write_cookies(COOKIES)
    original = state.path.read_bytes()
    state.write_cookies(COOKIES)
    assert state.path.read_bytes() != original
    assert state.read_cookies() == COOKIES


def test_context_cookies_with_empty_name_persist_named_login_cookies(state: SessionState) -> None:
    # Real Chromium can recreate an unnamed cookie after importing the seed.
    # It must not prevent saving the refreshed, named authentication cookie.
    unnamed = {**COOKIES[0], "name": "", "value": "unnamed-cookie-value"}
    browser_cookies = [COOKIES[0].copy(), unnamed]

    state.write_cookies(browser_cookies)

    assert state.read_cookies() == COOKIES
    assert browser_cookies == [COOKIES[0], unnamed]
    assert len(browser_cookies) == 2


@pytest.mark.parametrize("partition_key", [None, "https://example.com"])
def test_optional_playwright_partition_key_roundtrips(state: SessionState, partition_key: str | None) -> None:
    cookies = [{**COOKIES[0], "partitionKey": partition_key}]
    state.write_cookies(cookies)
    assert state.read_cookies() == cookies


def test_unnamed_cookie_still_rejects_non_cookie_data(state: SessionState) -> None:
    cookies = [*COOKIES, {**COOKIES[0], "name": "", "localStorage": {"private": "data"}}]
    with pytest.raises(ConfigError, match="格式无效"):
        state.write_cookies(cookies)
    assert not state.path.exists()


def test_manual_cookie_refresh_invalidates_cache(state: SessionState, monkeypatch: pytest.MonkeyPatch) -> None:
    state.write_cookies(COOKIES)
    monkeypatch.setenv("DOUYIN_COOKIE", "sessionid=new-manual-login")
    replacement = SessionState.from_env()
    assert replacement.read_cookies() is None
    replacement.write_cookies(COOKIES)
    assert replacement.read_cookies() == COOKIES
    assert state.read_cookies() is None


@pytest.mark.parametrize("change", ["truncate", "ciphertext", "nonce", "header"])
def test_corrupt_cache_fails_closed(state: SessionState, change: str) -> None:
    state.write_cookies(COOKIES)
    encoded = bytearray(state.path.read_bytes())
    if change == "truncate":
        encoded = encoded[:10]
    else:
        index = {"ciphertext": -1, "nonce": len(_MAGIC), "header": 0}[change]
        encoded[index] ^= 1
    state.path.write_bytes(encoded)
    with pytest.raises(ConfigError, match="已损坏") as error:
        state.read_cookies()
    assert COOKIES[0]["value"] not in str(error.value)
    assert error.value.__suppress_context__ or error.value.__context__ is None


def test_different_key_fails_closed(state: SessionState, monkeypatch: pytest.MonkeyPatch) -> None:
    state.write_cookies(COOKIES)
    monkeypatch.setenv("DOUYIN_STATE_KEY", base64.urlsafe_b64encode(bytes(reversed(KEY))).decode())
    with pytest.raises(ConfigError, match="已损坏"):
        SessionState.from_env().read_cookies()


@pytest.mark.parametrize("bad_key", ["secret-key-value", "!" * 44, "A" * 42, "A" * 44, "A" * 43 + "==", "é" * 44, "A" * 42 + "B="])
def test_invalid_key_is_rejected_without_disclosure(state: SessionState, monkeypatch: pytest.MonkeyPatch, bad_key: str) -> None:
    monkeypatch.setenv("DOUYIN_STATE_KEY", bad_key)
    with pytest.raises(ConfigError, match="32 字节") as error:
        SessionState.from_env()
    assert bad_key not in str(error.value)


def test_unpadded_key_is_supported(state: SessionState, monkeypatch: pytest.MonkeyPatch) -> None:
    state.write_cookies(COOKIES)
    monkeypatch.setenv("DOUYIN_STATE_KEY", base64.urlsafe_b64encode(KEY).decode().rstrip("="))
    assert SessionState.from_env().read_cookies() == COOKIES


def test_missing_seed_is_rejected(state: SessionState, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOUYIN_COOKIE")
    with pytest.raises(ConfigError, match="DOUYIN_COOKIE"):
        SessionState.from_env()


def test_state_file_has_owner_only_permissions_even_on_replacement(state: SessionState) -> None:
    state.write_cookies(COOKIES)
    assert stat.S_IMODE(state.path.stat().st_mode) == 0o600
    state.path.chmod(0o644)
    state.write_cookies(COOKIES)
    assert stat.S_IMODE(state.path.stat().st_mode) == 0o600
    assert list(state.path.parent.iterdir()) == [state.path]


def test_failed_atomic_replace_preserves_prior_cache_and_cleans_temporary(state: SessionState, monkeypatch: pytest.MonkeyPatch) -> None:
    state.write_cookies(COOKIES)
    original = state.path.read_bytes()

    def fail_replace(*args: object) -> None:
        raise OSError("some-sensitive-path")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ConfigError, match="无法保存") as error:
        state.write_cookies([{**COOKIES[0], "value": "another-secret"}])
    assert "some-sensitive-path" not in str(error.value)
    assert state.path.read_bytes() == original
    assert list(state.path.parent.iterdir()) == [state.path]


@pytest.mark.parametrize("payload", [
    {"version": 2, "seed_sha256": hashlib.sha256(SEED.encode()).hexdigest(), "cookies": COOKIES},
    {"version": True, "seed_sha256": hashlib.sha256(SEED.encode()).hexdigest(), "cookies": COOKIES},
    {"version": 1, "seed_sha256": "bad-hash", "cookies": COOKIES},
    {"version": 1, "seed_sha256": hashlib.sha256(SEED.encode()).hexdigest(), "cookies": "bad-cookies"},
    {"version": 1, "seed_sha256": hashlib.sha256(SEED.encode()).hexdigest(), "cookies": [{"name": "sessionid"}]},
    {"version": 1, "seed_sha256": hashlib.sha256(SEED.encode()).hexdigest(), "cookies": COOKIES, "origins": []},
    [],
])
def test_authenticated_but_invalid_payload_fails_closed(state: SessionState, payload: object) -> None:
    nonce = os.urandom(12)
    encoded = _MAGIC + nonce + AESGCM(KEY).encrypt(nonce, json.dumps(payload).encode(), _MAGIC)
    state.path.parent.mkdir(parents=True)
    state.path.write_bytes(encoded)
    with pytest.raises(ConfigError, match="已损坏"):
        state.read_cookies()


@pytest.mark.parametrize("cookies", [None, {}, ["bad"], [{"name": "sessionid", "value": "secret"}], [{**COOKIES[0], "expires": float("nan")}], [{**COOKIES[0], "secure": "true"}], [{**COOKIES[0], "sameSite": "invalid"}], [{**COOKIES[0], "localStorage": {"private": "data"}}], [{**COOKIES[0], "partitionKey": {"origin": "https://example.com"}}]])
def test_invalid_cookies_are_never_written(state: SessionState, cookies: object) -> None:
    with pytest.raises(ConfigError, match="格式无效"):
        state.write_cookies(cookies)
    assert not state.path.exists()
