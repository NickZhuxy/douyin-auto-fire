"""Encrypted persistence for refreshed browser cookies only.

The initial cookie secret identifies a login generation. Replacing that secret
invalidates any cached cookies, even when the encryption key stays the same.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import ConfigError


_MAGIC = b"DOUYIN-AUTO-FIRE-SESSION\x01"
_NONCE_SIZE = 12
_MAX_STATE_SIZE = 2 * 1024 * 1024
_INVALID_STATE = "加密会话状态无效或已损坏，任务已停止"
_COOKIE_FIELDS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}


class SessionState:
    def __init__(self, path: Path, key: bytes, seed_cookie: str) -> None:
        self.path = path
        self._cipher = AESGCM(key)
        self._seed_hash = hashlib.sha256(seed_cookie.encode("utf-8")).hexdigest()

    @classmethod
    def from_env(cls) -> SessionState | None:
        encoded_key = os.getenv("DOUYIN_STATE_KEY", "").strip()
        if not encoded_key:
            return None
        # Accept canonical URL-safe base64 with or without its trailing padding.
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}=?", encoded_key):
            raise ConfigError("DOUYIN_STATE_KEY 必须是 URL-safe base64 编码的 32 字节密钥")
        try:
            key = base64.b64decode(encoded_key.rstrip("=") + "=", altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            raise ConfigError("DOUYIN_STATE_KEY 必须是 URL-safe base64 编码的 32 字节密钥") from None
        if len(key) != 32 or base64.urlsafe_b64encode(key).decode("ascii").rstrip("=") != encoded_key.rstrip("="):
            raise ConfigError("DOUYIN_STATE_KEY 必须是 URL-safe base64 编码的 32 字节密钥")
        seed_cookie = os.getenv("DOUYIN_COOKIE", "")
        if not seed_cookie.strip():
            raise ConfigError("使用加密会话持久化时必须配置 DOUYIN_COOKIE")
        state_path = os.getenv("DOUYIN_STATE_FILE", "artifacts/session.enc").strip()
        if not state_path:
            raise ConfigError("DOUYIN_STATE_FILE 必须是非空文件路径")
        return cls(Path(state_path).expanduser(), key, seed_cookie)

    def read_cookies(self) -> list[dict[str, Any]] | None:
        try:
            with self.path.open("rb") as stream:
                encoded = stream.read(_MAX_STATE_SIZE + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise ConfigError("无法读取加密会话状态") from None
        if (
            len(encoded) > _MAX_STATE_SIZE
            or len(encoded) < len(_MAGIC) + _NONCE_SIZE + 16
            or not encoded.startswith(_MAGIC)
        ):
            raise ConfigError(_INVALID_STATE)
        nonce_start = len(_MAGIC)
        nonce_end = nonce_start + _NONCE_SIZE
        try:
            cleartext = self._cipher.decrypt(encoded[nonce_start:nonce_end], encoded[nonce_end:], _MAGIC)
            payload = json.loads(cleartext)
        except (InvalidTag, ValueError, UnicodeError):
            raise ConfigError(_INVALID_STATE) from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "seed_sha256", "cookies"}
            or type(payload.get("version")) is not int
            or payload["version"] != 1
            or not isinstance(payload.get("seed_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["seed_sha256"])
            or not _valid_cookies(payload.get("cookies"))
        ):
            raise ConfigError(_INVALID_STATE)
        if not hmac.compare_digest(payload["seed_sha256"], self._seed_hash):
            return None
        return payload["cookies"]

    def write_cookies(self, cookies: list[dict[str, Any]]) -> None:
        if not _valid_cookies(cookies):
            raise ConfigError("会话 Cookie 格式无效")
        # Chromium can return cookies whose name is empty. The browser import
        # already skips these; keep the persisted set consistent with import.
        cookies = [cookie for cookie in cookies if cookie["name"]]
        payload = {"version": 1, "seed_sha256": self._seed_hash, "cookies": cookies}
        try:
            cleartext = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ConfigError("会话 Cookie 格式无效") from None
        nonce = os.urandom(_NONCE_SIZE)
        encoded = _MAGIC + nonce + self._cipher.encrypt(nonce, cleartext, _MAGIC)
        if len(encoded) > _MAX_STATE_SIZE:
            raise ConfigError("加密会话状态过大")
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, filename = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(filename)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise ConfigError("无法保存加密会话状态") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _valid_cookies(cookies: Any) -> bool:
    if not isinstance(cookies, list):
        return False
    for cookie in cookies:
        if not isinstance(cookie, dict):
            return False
        if set(cookie) - _COOKIE_FIELDS:
            return False
        if any(not isinstance(cookie.get(field), str) for field in ("name", "value", "domain", "path")):
            return False
        if not cookie["domain"] or not cookie["path"]:
            return False
        if any(field in cookie and not isinstance(cookie[field], bool) for field in ("secure", "httpOnly")):
            return False
        if "expires" in cookie and (
            type(cookie["expires"]) not in (int, float)
            or (isinstance(cookie["expires"], float) and not math.isfinite(cookie["expires"]))
        ):
            return False
        if "sameSite" in cookie and cookie["sameSite"] not in ("Strict", "Lax", "None"):
            return False
        if cookie.get("partitionKey") is not None and not isinstance(cookie["partitionKey"], str):
            return False
    return True
