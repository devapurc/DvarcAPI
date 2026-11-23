"""Token issuing, verification and username bindings.

Only the SHA-256 digest of a token is ever written to disk. The plain token is
returned once to the client and never stored by the server.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOKEN_BYTES = 32
TOUCH_INTERVAL = 60.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


class TokenStore:
    """JSON backed token store with atomic writes.

    Structure of ``data/tokens.json``::

        {
          "version": 1,
          "tokens": {
            "<sha256>": {"createdAt": "...", "lastUsedAt": "...", "usernames": []}
          },
          "bindings": {
            "<username lowercase>": {"username": "...", "token": "<sha256>", "createdAt": "..."}
          }
        }
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "tokens.json"
        self._lock = threading.RLock()
        self._tokens: dict[str, dict[str, Any]] = {}
        self._bindings: dict[str, dict[str, Any]] = {}
        self._last_touch_persist = 0.0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        tokens = raw.get("tokens")
        bindings = raw.get("bindings")
        if isinstance(tokens, dict):
            self._tokens = tokens
        if isinstance(bindings, dict):
            self._bindings = bindings

    def _save(self) -> None:
        payload = {"version": 1, "tokens": self._tokens, "bindings": self._bindings}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def create_token(self) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        digest = hash_token(token)
        record: dict[str, Any] = {
            "createdAt": utcnow(),
            "lastUsedAt": None,
            "usernames": [],
        }
        with self._lock:
            self._tokens[digest] = record
            self._save()
        return token, record

    def verify(self, token: str) -> dict[str, Any] | None:
        digest = hash_token(token)
        with self._lock:
            record = self._tokens.get(digest)
            if record is None:
                return None
            record["lastUsedAt"] = utcnow()
            now = time.monotonic()
            if now - self._last_touch_persist >= TOUCH_INTERVAL:
                self._last_touch_persist = now
                try:
                    self._save()
                except OSError:
                    pass
            return record

    def bind(self, username: str, token_digest: str) -> tuple[str, str | None]:
        """Bind a username to a token.

        Returns ``(status, owner)`` where status is one of ``bound``,
        ``already_bound``, ``conflict``, ``conflict_discarded`` or
        ``invalid_token``.

        The first token that bound a username wins. When a different token
        tries to bind the same username and it is not used by any other
        account, that incoming token is discarded.
        """

        key = username.casefold()
        with self._lock:
            record = self._tokens.get(token_digest)
            if record is None:
                return "invalid_token", None

            existing = self._bindings.get(key)
            if existing is not None:
                owner = existing.get("username", username)
                if existing.get("token") == token_digest:
                    return "already_bound", owner
                if not record.get("usernames"):
                    self._tokens.pop(token_digest, None)
                    self._save()
                    return "conflict_discarded", owner
                return "conflict", owner

            record.setdefault("usernames", []).append(username)
            self._bindings[key] = {
                "username": username,
                "token": token_digest,
                "createdAt": utcnow(),
            }
            self._save()
            return "bound", username

    def usernames(self, token_digest: str) -> list[str]:
        with self._lock:
            record = self._tokens.get(token_digest)
            if record is None:
                return []
            return list(record.get("usernames", []))

    def revoke(self, prefix: str) -> int:
        prefix = prefix.strip().lower()
        with self._lock:
            matches = [digest for digest in self._tokens if digest.startswith(prefix)]
            if len(matches) != 1:
                return 0
            digest = matches[0]
            record = self._tokens.pop(digest, None)
            if record is None:
                return 0
            for name in record.get("usernames", []):
                self._bindings.pop(str(name).casefold(), None)
            self._save()
            return 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "tokens": json.loads(json.dumps(self._tokens)),
                "bindings": json.loads(json.dumps(self._bindings)),
            }


class RateLimiter:
    """Small in-memory sliding window limiter for token issuance."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, [])
            cutoff = now - self.window
            while hits and hits[0] < cutoff:
                hits.pop(0)
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True
