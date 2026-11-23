from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import hash_token
from app.config import Settings
from app.cloudflare import (
    CloudflareEventStore,
    CloudflareTokenStore,
    CloudflareVersionRegistry,
    R2BodyIterator,
    R2RangeError,
    parse_range,
)
from app.main import create_app
from app.versions import VersionError


SCHEMA = """
CREATE TABLE tokens(token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, last_used_at TEXT);
CREATE TABLE token_bindings(username_key TEXT PRIMARY KEY, username TEXT NOT NULL, token_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE rate_limits(rate_key TEXT NOT NULL, bucket INTEGER NOT NULL, hits INTEGER NOT NULL, PRIMARY KEY(rate_key, bucket));
"""


class FakeStatement:
    def __init__(self, db: "FakeD1", sql: str, params: tuple):
        self.db, self.sql, self.params = db, sql, params

    def bind(self, *params):
        self.params = params
        return self

    async def run(self):
        await asyncio.sleep(0)
        cursor = self.db.connection.execute(self.sql, self.params)
        self.db.connection.commit()
        return {"success": True, "meta": {"changes": cursor.rowcount}}

    async def first(self):
        await asyncio.sleep(0)
        cursor = self.db.connection.execute(self.sql, self.params)
        row = cursor.fetchone()
        return dict(row) if row else None

    async def all(self):
        await asyncio.sleep(0)
        cursor = self.db.connection.execute(self.sql, self.params)
        return {"results": [dict(row) for row in cursor.fetchall()]}


class FakeD1:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def prepare(self, sql):
        return FakeStatement(self, sql, ())


class FakeReader:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    async def read(self):
        try:
            return {"done": False, "value": next(self.chunks)}
        except StopIteration:
            return {"done": True}


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def get_reader(self):
        return FakeReader(self.chunks)


class FakeR2Object:
    def __init__(self, content=b"", metadata=None):
        self.content = content
        self.customMetadata = metadata or {}
        self.size = len(content)
        self.body = FakeStream([content])

    async def text(self):
        return self.content.decode()


class FakeBucket:
    def __init__(self, objects):
        self.objects = objects
        self.list_calls = 0
        self.get_calls = []

    async def list(self, **options):
        self.list_calls += 1
        prefix = options.get("prefix", "")
        if options.get("delimiter"):
            prefixes = sorted({key[: key.index("/", len(prefix)) + 1] for key in self.objects if key.startswith(prefix) and "/" in key[len(prefix):]})
            if self.list_calls == 1 and len(prefixes) > 1:
                return {"delimitedPrefixes": prefixes[:1], "truncated": True, "cursor": "next"}
            return {"delimitedPrefixes": prefixes[1:] if self.list_calls > 1 else prefixes, "truncated": False}
        selected = [
            SimpleNamespace(key=key, size=obj.size, customMetadata=obj.customMetadata)
            for key, obj in self.objects.items()
            if key.startswith(prefix)
        ]
        return {"objects": selected, "truncated": False}

    async def get(self, key, **options):
        self.get_calls.append((key, options))
        obj = self.objects.get(key)
        if obj is None or "range" not in options:
            return obj
        spec = options["range"]
        start = int(spec["offset"])
        end = start + int(spec["length"])
        return FakeR2Object(obj.content[start:end], obj.customMetadata)


def test_d1_token_auth_and_first_binding_wins():
    async def run():
        db = FakeD1()
        store = CloudflareTokenStore(db)
        token_a, _ = await store.create_token()
        token_b, _ = await store.create_token()
        digest_a, digest_b = hash_token(token_a), hash_token(token_b)
        results = await asyncio.gather(store.bind("Steve", digest_a), store.bind("steve", digest_b))
        assert {result[0] for result in results} == {"bound", "conflict_discarded"}
        assert await store.usernames(digest_a) == ["Steve"]
        assert await store.verify(token_b) is None
    asyncio.run(run())


def test_bind_conflict_does_not_delete_token_with_another_binding():
    async def run():
        db = FakeD1()
        store = CloudflareTokenStore(db)
        token_a, _ = await store.create_token()
        token_b, _ = await store.create_token()
        digest_a, digest_b = hash_token(token_a), hash_token(token_b)
        assert await store.bind("Alex", digest_b) == ("bound", "Alex")
        assert await store.bind("Steve", digest_a) == ("bound", "Steve")
        assert await store.bind("Steve", digest_b) == ("conflict", "Steve")
        assert await store.verify(token_b) is not None
    asyncio.run(run())


def test_d1_rate_limit_uses_atomic_counter():
    async def run():
        db = FakeD1()
        store = CloudflareTokenStore(db)
        assert await store.rate_allow("1.2.3.4", 2, 60) is True
        assert await store.rate_allow("1.2.3.4", 2, 60) is True
        assert await store.rate_allow("1.2.3.4", 2, 60) is False
        assert await store.rate_allow("5.6.7.8", 2, 60) is True
    asyncio.run(run())


def test_d1_rate_limit_cleanup_keeps_current_bucket():
    async def run():
        db = FakeD1()
        store = CloudflareTokenStore(db)
        current_bucket = int(time.time() // 60)
        db.connection.execute("INSERT INTO rate_limits VALUES (?, ?, ?)", ("old", current_bucket - 10, 99))
        db.connection.execute("INSERT INTO rate_limits VALUES (?, ?, ?)", ("current", current_bucket, 1))
        db.connection.commit()
        assert await store.rate_allow("new", 2, 60) is True
        assert db.connection.execute("SELECT COUNT(*) FROM rate_limits WHERE rate_key = 'old'").fetchone()[0] == 0
        assert db.connection.execute("SELECT hits FROM rate_limits WHERE rate_key = 'current'").fetchone()[0] == 1
    asyncio.run(run())


def test_range_parser_covers_valid_invalid_and_suffix_ranges():
    assert parse_range(None, 10) is None
    assert parse_range("bytes=2-5", 10) == (2, 5)
    assert parse_range("bytes=2-", 10) == (2, 9)
    assert parse_range("bytes=-3", 10) == (7, 9)
    assert parse_range("bytes=2-99", 10) == (2, 9)
    for value in ("bytes=10-10", "bytes=5-2", "bytes=-0", "bytes=1-2,4-5", "nonsense"):
        with pytest.raises(R2RangeError):
            parse_range(value, 10)


def test_r2_stream_adapter_does_not_buffer_body():
    async def run():
        iterator = R2BodyIterator(FakeStream([b"a", b"b", b"c"]))
        chunks = [chunk async for chunk in iterator]
        assert chunks == [b"a", b"b", b"c"]
    asyncio.run(run())


def test_416_response_contains_content_range_size():
    app = create_app(Settings(versions_dir=Path("."), data_dir=Path(".")), cloudflare=True)
    request = Request({"type": "http", "headers": []})
    response = asyncio.run(app.exception_handlers[VersionError](request, R2RangeError(1234)))
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */1234"


def test_r2_registry_paginates_and_requires_sha_metadata():
    async def run():
        digest = "a" * 64
        objects = {
            "versions/one/version.json": FakeR2Object(json.dumps({"name": "One", "minecraft": "1.21.1", "loader": {"type": "vanilla"}, "allowedUsers": []}).encode()),
            "versions/one/mods/a.jar": FakeR2Object(b"a", {"sha256": digest}),
            "versions/two/version.json": FakeR2Object(json.dumps({"name": "Two", "minecraft": "1.21.1", "loader": {"type": "vanilla"}, "allowedUsers": []}).encode()),
            "versions/two/mods/b.jar": FakeR2Object(b"b"),
        }
        registry = CloudflareVersionRegistry(FakeBucket(objects))
        versions = await registry.list_all()
        assert [version["id"] for version in versions] == ["one"]
    asyncio.run(run())


def test_r2_download_delegates_range_and_publishes_matching_sha256():
    async def run():
        content = b"0123456789"
        digest = hashlib.sha256(content).hexdigest()
        objects = {
            "versions/one/version.json": FakeR2Object(json.dumps({"name": "One", "minecraft": "1.21.1", "loader": {"type": "vanilla"}}).encode()),
            "versions/one/mods/a.jar": FakeR2Object(content, {"sha256": digest}),
        }
        bucket = FakeBucket(objects)
        registry = CloudflareVersionRegistry(bucket)
        version = await registry.get("one")
        obj, entry, range_info = await registry.get_file(version, "mods/a.jar", "bytes=-3")
        assert range_info == (7, 9)
        assert bucket.get_calls[-1][1]["range"] == {"offset": 7, "length": 3}
        assert obj.content == content[7:]
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        with pytest.raises(R2RangeError):
            await registry.get_file(version, "mods/a.jar", "bytes=99-")
    asyncio.run(run())


def test_d1_events_filter_users():
    async def run():
        db = FakeD1()
        db.connection.execute("CREATE TABLE events(id TEXT PRIMARY KEY, name TEXT, description TEXT, starts_at TEXT, discord_url TEXT, users_json TEXT)")
        db.connection.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", ("public", "Public", "", "2026-01-01", "https://discord.gg/x", "[]"))
        db.connection.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", ("private", "Private", "", "2026-01-02", "https://discord.gg/y", '["Steve"]'))
        db.connection.commit()
        store = CloudflareEventStore(db)
        assert [event["id"] for event in await store.list_for(["steve"])] == ["public", "private"]
        assert [event["id"] for event in await store.list_for(["Alex"])] == ["public"]
    asyncio.run(run())


def test_cloudflare_app_cors_allows_launcher_origin():
    settings = Settings(versions_dir=Path("."), data_dir=Path("."), cors_origins=("https://dvarc.apurc.site",))
    response = TestClient(create_app(settings, cloudflare=True)).options(
        "/api/v1/health",
        headers={
            "Origin": "https://dvarc.apurc.site",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://dvarc.apurc.site"

