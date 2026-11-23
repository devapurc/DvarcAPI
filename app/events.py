"""User scoped events stored in ``data/events.json``.

Events are managed from the host (admin CLI) and served read-only to clients.
An event is visible when the token has at least one bound username listed in
``users``. An empty ``users`` list means the event is public for every valid
token.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("dvarcapi.events")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
EVENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

DISCORD_HOSTS = {
    "discord.gg",
    "discord.com",
    "www.discord.com",
    "discordapp.com",
    "www.discordapp.com",
}

MAX_NAME = 120
MAX_DESCRIPTION = 2000


class EventError(ValueError):
    """Raised when an event definition is invalid."""


def parse_start(value: Any) -> datetime:
    """Parse an ISO 8601 date-time that must include a timezone offset."""

    if not isinstance(value, str) or not value.strip():
        raise EventError("'startsAt' must be an ISO 8601 date-time string")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventError(f"'startsAt' is not a valid date-time: {raw}") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise EventError("'startsAt' must include a timezone offset, e.g. -03:00")
    return parsed


def normalize_start(value: Any) -> str:
    return parse_start(value).isoformat(timespec="seconds")


def validate_users(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EventError("'users' must be a list of usernames")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = item.strip()
        if not name:
            continue
        if not USERNAME_RE.match(name):
            raise EventError(f"invalid username in 'users': {name}")
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


def validate_discord_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventError("'discordUrl' is required")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise EventError("'discordUrl' must start with https://")
    if (parsed.hostname or "").casefold() not in DISCORD_HOSTS:
        raise EventError("'discordUrl' must point to discord.gg or discord.com")
    return url


def validate_event(raw: Any, *, require_id: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EventError("an event must be a JSON object")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise EventError("'name' is required")
    name = name.strip()
    if len(name) > MAX_NAME:
        raise EventError(f"'name' cannot exceed {MAX_NAME} characters")

    description = raw.get("description") or ""
    if not isinstance(description, str):
        raise EventError("'description' must be a string")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION:
        raise EventError(f"'description' cannot exceed {MAX_DESCRIPTION} characters")

    starts_at = normalize_start(raw.get("startsAt"))
    discord_url = validate_discord_url(raw.get("discordUrl"))
    users = validate_users(raw.get("users"))

    event_id = raw.get("id")
    if event_id is None:
        if require_id:
            raise EventError("'id' is required")
        event_id = ""
    else:
        if not isinstance(event_id, str) or not EVENT_ID_RE.match(event_id.strip()):
            raise EventError("'id' must use lowercase letters, numbers and dashes")
        event_id = event_id.strip()

    return {
        "id": event_id,
        "name": name,
        "description": description,
        "startsAt": starts_at,
        "discordUrl": discord_url,
        "users": users,
    }


def public_view(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event["id"],
        "name": event["name"],
        "description": event["description"],
        "startsAt": event["startsAt"],
        "discordUrl": event["discordUrl"],
    }


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return (slug or "event")[:40].strip("-") or "event"


class EventStore:
    """JSON backed event store with atomic writes and lazy reloads."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "events.json"
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._stamp: tuple[int, int] | None = None
        self._load(force=True)

    # ------------------------------------------------------------- loading
    def _current_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self, *, force: bool = False) -> None:
        stamp = self._current_stamp()
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp
        if stamp is None:
            self._events = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cannot read %s: %s", self.path, exc)
            self._events = []
            return

        entries = raw.get("events") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            logger.warning("%s must contain an 'events' list", self.path)
            self._events = []
            return

        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            try:
                event = validate_event(entry, require_id=True)
            except EventError as exc:
                logger.warning("ignoring invalid event #%s: %s", index, exc)
                continue
            if event["id"] in seen:
                logger.warning("ignoring duplicated event id: %s", event["id"])
                continue
            seen.add(event["id"])
            events.append(event)
        self._events = events

    def _ensure_loaded(self) -> None:
        self._load()

    def _save(self) -> None:
        payload = {"version": 1, "events": self._events}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._stamp = self._current_stamp()

    # ----------------------------------------------------------------- api
    def list_for(self, usernames: list[str]) -> list[dict[str, Any]]:
        current = {name.casefold() for name in usernames}
        with self._lock:
            self._ensure_loaded()
            visible = [
                event
                for event in self._events
                if not event["users"] or any(name.casefold() in current for name in event["users"])
            ]
            visible.sort(key=lambda event: (parse_start(event["startsAt"]), event["id"]))
            return [public_view(event) for event in visible]

    def get(self, event_id: str, usernames: list[str]) -> dict[str, Any] | None:
        current = {name.casefold() for name in usernames}
        with self._lock:
            self._ensure_loaded()
            for event in self._events:
                if event["id"] != event_id:
                    continue
                if not event["users"] or any(name.casefold() in current for name in event["users"]):
                    return public_view(event)
                return None
        return None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            return [dict(event) for event in self._events]

    def add(self, raw: dict[str, Any], event_id: str | None = None) -> dict[str, Any]:
        data = dict(raw)
        if event_id:
            data["id"] = event_id
        with self._lock:
            self._ensure_loaded()
            candidate_id = str(data.get("id") or "").strip()
            if candidate_id:
                if not EVENT_ID_RE.match(candidate_id):
                    raise EventError("'id' must use lowercase letters, numbers and dashes")
                if any(event["id"] == candidate_id for event in self._events):
                    raise EventError(f"an event with id '{candidate_id}' already exists")
                data["id"] = candidate_id
            event = validate_event(data, require_id=bool(candidate_id))
            if not candidate_id:
                event["id"] = self._generate_id(event["name"])
            self._events.append(event)
            self._save()
            return dict(event)

    def update_users(self, event_id: str, users: list[str]) -> dict[str, Any] | None:
        validated = validate_users(users)
        with self._lock:
            self._ensure_loaded()
            for event in self._events:
                if event["id"] == event_id:
                    event["users"] = validated
                    self._save()
                    return dict(event)
        return None

    def remove(self, event_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            before = len(self._events)
            self._events = [event for event in self._events if event["id"] != event_id]
            if len(self._events) == before:
                return False
            self._save()
            return True

    def _generate_id(self, name: str) -> str:
        existing = {event["id"] for event in self._events}
        base = slugify(name)
        for _ in range(100):
            candidate = f"{base}-{secrets.token_hex(2)}"
            if candidate not in existing:
                return candidate
        raise EventError("cannot generate a unique event id; pass --id explicitly")
