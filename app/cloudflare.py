"""Cloudflare D1/R2 stores used by the Python Worker entrypoint.

This module deliberately does not replace the local JSON/filesystem stores.
The local backend remains available for tests and administration; the Worker
uses these async stores through the bindings exposed in ``request.scope``.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import PurePosixPath
from typing import Any

from .auth import TOKEN_BYTES, hash_token, utcnow
from .versions import VersionError, VersionNotFound


def _value(row: Any, name: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        return row[name]
    except Exception:
        return getattr(row, name, default)


def _rows(result: Any) -> list[Any]:
    rows = _value(result, "results", [])
    return list(rows or [])


class D1Store:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def execute(self, sql: str, *params: Any) -> Any:
        return await self.db.prepare(sql).bind(*params).run()

    async def first(self, sql: str, *params: Any) -> Any:
        return await self.db.prepare(sql).bind(*params).first()


class R2RangeError(VersionError):
    status_code = 416

    def __init__(self, size: int) -> None:
        super().__init__(f"range is not satisfiable for an object of {size} bytes")
        self.size = size


def parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range and return inclusive offsets.

    Multiple ranges are intentionally rejected because the API returns one
    binary stream, not multipart/byteranges.
    """

    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or size <= 0:
        raise R2RangeError(size)
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise R2RangeError(size)
    if start_text:
        start = int(start_text)
        if start >= size:
            raise R2RangeError(size)
        end = int(end_text) if end_text else size - 1
        if end < start:
            raise R2RangeError(size)
        return start, min(end, size - 1)
    suffix = int(end_text)
    if suffix <= 0:
        raise R2RangeError(size)
    return max(size - suffix, 0), size - 1


class R2BodyIterator:
    """Adapt a Workers ReadableStream to Starlette's async iterator API."""

    def __init__(self, stream: Any) -> None:
        self._reader = stream.get_reader()

    def __aiter__(self) -> "R2BodyIterator":
        return self

    async def __anext__(self) -> bytes:
        chunk = await self._reader.read()
        done = bool(_value(chunk, "done", False))
        if done:
            raise StopAsyncIteration
        value = _value(chunk, "value")
        if value is None:
            return await self.__anext__()
        return bytes(value)


class CloudflareTokenStore(D1Store):
    async def create_token(self) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        digest = hash_token(token)
        created = utcnow()
        await self.execute(
            "INSERT INTO tokens (token_hash, created_at, last_used_at) VALUES (?, ?, NULL)",
            digest,
            created,
        )
        return token, {"createdAt": created, "lastUsedAt": None, "usernames": []}

    async def verify(self, token: str) -> dict[str, Any] | None:
        digest = hash_token(token)
        row = await self.first(
            "SELECT token_hash, created_at, last_used_at FROM tokens WHERE token_hash = ?",
            digest,
        )
        if row is None:
            return None
        now = utcnow()
        await self.execute("UPDATE tokens SET last_used_at = ? WHERE token_hash = ?", now, digest)
        usernames = await self.usernames(digest)
        return {
            "createdAt": _value(row, "created_at"),
            "lastUsedAt": now,
            "usernames": usernames,
        }

    async def usernames(self, digest: str) -> list[str]:
        result = await self.db.prepare(
            "SELECT username FROM token_bindings WHERE token_hash = ? ORDER BY created_at"
        ).bind(digest).all()
        return [str(_value(row, "username", "")) for row in _rows(result)]

    async def bind(self, username: str, digest: str) -> tuple[str, str | None]:
        token = await self.first("SELECT token_hash FROM tokens WHERE token_hash = ?", digest)
        if token is None:
            return "invalid_token", None

        key = username.casefold()
        existing = await self.first(
            "SELECT username, token_hash FROM token_bindings WHERE username_key = ?", key
        )
        if existing is not None:
            owner = str(_value(existing, "username", username))
            if str(_value(existing, "token_hash", "")) == digest:
                return "already_bound", owner
            count = await self.first(
                "SELECT COUNT(*) AS count FROM token_bindings WHERE token_hash = ?", digest
            )
            if int(_value(count, "count", 0)) == 0:
                await self.execute("DELETE FROM tokens WHERE token_hash = ?", digest)
                return "conflict_discarded", owner
            return "conflict", owner

        # username_key is the primary key. A concurrent insert can still race
        # between the SELECT above and INSERT, so treat the constraint error as
        # a normal conflict and read the winner back from D1.
        try:
            await self.execute(
                "INSERT INTO token_bindings (username_key, username, token_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                key,
                username,
                digest,
                utcnow(),
            )
            return "bound", username
        except Exception as exc:
            existing = await self.first(
                "SELECT username, token_hash FROM token_bindings WHERE username_key = ?", key
            )
            if existing is None:
                raise exc
            owner = str(_value(existing, "username", username))
            if str(_value(existing, "token_hash", "")) == digest:
                return "already_bound", owner
            deleted = await self.first(
                "DELETE FROM tokens WHERE token_hash = ? "
                "AND NOT EXISTS (SELECT 1 FROM token_bindings WHERE token_hash = ?) "
                "RETURNING token_hash",
                digest,
                digest,
            )
            return ("conflict_discarded" if deleted is not None else "conflict"), owner

    async def rate_allow(self, key: str, limit: int, window: float) -> bool:
        if limit <= 0:
            return True
        bucket = int(time.time() // window)
        row = await self.first(
            "INSERT INTO rate_limits (rate_key, bucket, hits) VALUES (?, ?, 1) "
            "ON CONFLICT(rate_key, bucket) DO UPDATE SET hits = hits + 1 "
            "RETURNING hits",
            key,
            bucket,
        )
        return int(_value(row, "hits", limit + 1)) <= limit


class CloudflareEventStore(D1Store):
    async def list_for(self, usernames: list[str]) -> list[dict[str, Any]]:
        result = await self.db.prepare(
            "SELECT id, name, description, starts_at, discord_url, users_json "
            "FROM events ORDER BY starts_at, id"
        ).all()
        current = {name.casefold() for name in usernames}
        visible = []
        for row in _rows(result):
            users = json.loads(str(_value(row, "users_json", "[]")))
            if not users or any(str(name).casefold() in current for name in users):
                visible.append(self._public(row))
        return visible

    async def get(self, event_id: str, usernames: list[str]) -> dict[str, Any] | None:
        row = await self.first(
            "SELECT id, name, description, starts_at, discord_url, users_json FROM events WHERE id = ?",
            event_id,
        )
        if row is None:
            return None
        users = json.loads(str(_value(row, "users_json", "[]")))
        if users and not any(str(name).casefold() in {u.casefold() for u in usernames} for name in users):
            return None
        return self._public(row)

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        return {
            "id": str(_value(row, "id")),
            "name": str(_value(row, "name")),
            "description": str(_value(row, "description", "") or ""),
            "startsAt": str(_value(row, "starts_at")),
            "discordUrl": str(_value(row, "discord_url")),
        }


class CloudflareVersionRegistry:
    """Version manifests and files stored under the private R2 bucket."""

    def __init__(self, bucket: Any) -> None:
        self.bucket = bucket

    async def _object_text(self, key: str) -> str:
        obj = await self.bucket.get(key)
        if obj is None:
            raise VersionNotFound(key)
        return await obj.text()

    async def _ids(self) -> list[str]:
        prefixes: list[str] = []
        cursor = None
        while True:
            options: dict[str, Any] = {"prefix": "versions/", "delimiter": "/", "limit": 1000}
            if cursor:
                options["cursor"] = cursor
            result = await self.bucket.list(**options)
            prefixes.extend(str(item) for item in (_value(result, "delimitedPrefixes", []) or []))
            if not _value(result, "truncated", False):
                break
            cursor = _value(result, "cursor")
        ids = []
        for prefix in prefixes:
            value = str(prefix).removeprefix("versions/").rstrip("/")
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", value):
                ids.append(value)
        return sorted(ids, key=str.casefold)

    async def list_all(self) -> list[dict[str, Any]]:
        result = []
        for version_id in await self._ids():
            try:
                result.append(await self.get(version_id))
            except VersionError:
                continue
        return result

    async def count(self) -> int:
        return len(await self._ids())

    async def get(self, version_id: str) -> dict[str, Any]:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", version_id):
            raise VersionNotFound(version_id)
        try:
            meta = json.loads(await self._object_text(f"versions/{version_id}/version.json"))
        except (json.JSONDecodeError, TypeError) as exc:
            raise VersionError(f"{version_id}: invalid version.json") from exc
        if not isinstance(meta, dict):
            raise VersionError(f"{version_id}: version.json must be an object")
        name = meta.get("name") or version_id
        minecraft = meta.get("minecraft")
        loader = meta.get("loader")
        if not isinstance(name, str) or not name.strip() or not isinstance(minecraft, str) or not isinstance(loader, dict):
            raise VersionError(f"{version_id}: invalid manifest")
        loader_type = str(loader.get("type", "")).strip().lower()
        if loader_type not in {"fabric", "quilt", "forge", "neoforge", "vanilla"}:
            raise VersionError(f"{version_id}: unknown loader")
        loader_version = loader.get("version")
        if loader_type != "vanilla" and (not isinstance(loader_version, str) or not loader_version.strip()):
            raise VersionError(f"{version_id}: loader version is required")
        allowed_users = meta.get("allowedUsers") or []
        if not isinstance(allowed_users, list) or any(not isinstance(item, str) for item in allowed_users):
            raise VersionError(f"{version_id}: allowedUsers must be a list of strings")
        icon = meta.get("icon")
        if icon is not None and (not isinstance(icon, str) or not icon.strip() or "/" in icon or "\\" in icon):
            raise VersionError(f"{version_id}: invalid icon")

        objects: list[Any] = []
        cursor = None
        while True:
            options = {"prefix": f"versions/{version_id}/", "limit": 1000}
            if cursor:
                options["cursor"] = cursor
            page = await self.bucket.list(**options, include=["customMetadata"])
            objects.extend(_value(page, "objects", []) or [])
            if not _value(page, "truncated", False):
                break
            cursor = _value(page, "cursor")

        files = []
        icon = icon or ("icon.png" if any(str(_value(obj, "key", "")).endswith("/icon.png") for obj in objects) else None)
        for obj in objects:
            key = str(_value(obj, "key", ""))
            relative = key.removeprefix(f"versions/{version_id}/")
            if relative == "version.json" or not relative:
                continue
            if not (relative.startswith("mods/") or relative.startswith("resourcepacks/") or relative.startswith("shaderpacks/") or relative == icon):
                continue
            custom = _value(obj, "customMetadata", {}) or {}
            digest = str(_value(custom, "sha256", ""))
            if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise VersionError(f"{version_id}: missing or invalid SHA-256 metadata for {relative}")
            files.append({"path": relative, "size": int(_value(obj, "size", 0)), "sha256": digest})

        if icon and icon not in {str(item["path"]) for item in files}:
            icon = None
        return {
            "id": version_id,
            "name": name.strip(),
            "description": str(meta.get("description") or ""),
            "minecraft": minecraft.strip(),
            "loader": {"type": loader_type, "version": loader_version if loader_type != "vanilla" else None},
            "java": meta.get("java") if isinstance(meta.get("java"), int) else None,
            "memory": meta.get("memory") if isinstance(meta.get("memory"), str) else None,
            "icon": icon,
            "allowedUsers": [item.strip() for item in allowed_users if item.strip()],
            "files": files,
        }

    @staticmethod
    def is_allowed(version: dict[str, Any], usernames: list[str]) -> bool:
        allowed = version.get("allowedUsers") or []
        current = {name.casefold() for name in usernames}
        return not allowed or any(str(name).casefold() in current for name in allowed)

    @staticmethod
    def summary(version: dict[str, Any], base_url: str) -> dict[str, Any]:
        files = version["files"]
        icon = version.get("icon")
        return {
            "id": version["id"],
            "name": version["name"],
            "description": version["description"],
            "minecraft": version["minecraft"],
            "loader": version["loader"],
            "java": version["java"],
            "memory": version["memory"],
            "size": sum(int(item["size"]) for item in files),
            "fileCount": len(files),
            "iconUrl": f"{base_url}/api/v1/versions/{version['id']}/files/{icon}" if icon else None,
            "restricted": bool(version.get("allowedUsers")),
        }

    @classmethod
    def detail(cls, version: dict[str, Any], base_url: str) -> dict[str, Any]:
        data = cls.summary(version, base_url)
        data["files"] = [
            {
                "path": item["path"],
                "size": item["size"],
                "sha256": item["sha256"],
                "url": f"{base_url}/api/v1/versions/{version['id']}/files/{item['path']}",
            }
            for item in version["files"]
        ]
        return data

    async def get_file(self, version: dict[str, Any], relative: str, range_header: str | None = None) -> tuple[Any, dict[str, Any], tuple[int, int] | None]:
        relative = relative.replace("\\", "/").lstrip("/")
        path = PurePosixPath(relative)
        if not relative or ".." in path.parts or path.is_absolute():
            raise VersionNotFound(relative)
        entry = next((item for item in version["files"] if item["path"] == relative), None)
        if entry is None:
            raise VersionNotFound(relative)
        options: dict[str, Any] = {}
        range_info = parse_range(range_header, int(entry["size"]))
        if range_info:
            options["range"] = {"offset": range_info[0], "length": range_info[1] - range_info[0] + 1}
        obj = await self.bucket.get(f"versions/{version['id']}/{relative}", **options)
        if obj is None or not hasattr(obj, "body"):
            raise VersionNotFound(relative)
        return obj, entry, range_info
