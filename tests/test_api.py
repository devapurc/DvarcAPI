"""End-to-end tests for the Dvarc API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.events import EventError, EventStore, public_view
from app.main import create_app


def write_version(
    root: Path,
    version_id: str,
    *,
    minecraft: str = "1.21.1",
    loader_type: str = "fabric",
    loader_version: str | None = "0.16.9",
    files: dict[str, bytes] | None = None,
    allowed_users: list[str] | None = None,
) -> Path:
    version_root = root / version_id
    for dirname in ("mods", "resourcepacks", "shaderpacks"):
        (version_root / dirname).mkdir(parents=True, exist_ok=True)
    (version_root / "version.json").write_text(
        json.dumps(
            {
                "name": version_id.title(),
                "description": f"Pack {version_id}",
                "minecraft": minecraft,
                "loader": {"type": loader_type, "version": loader_version},
                "allowedUsers": allowed_users or [],
            }
        ),
        encoding="utf-8",
    )
    for relative, content in (files or {}).items():
        target = version_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return version_root


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    versions = tmp_path / "versions"
    data = tmp_path / "data"
    versions.mkdir()
    data.mkdir()
    write_version(
        versions,
        "demo",
        files={
            "mods/alpha.jar": b"alpha-bytes",
            "resourcepacks/faithful.zip": b"zip-bytes",
            "shaderpacks/bsl.zip": b"shader-bytes",
        },
    )
    write_version(
        versions,
        "vip",
        files={"mods/vip.jar": b"vip-bytes"},
        allowed_users=["Steve"],
    )
    (data / "events.json").write_text(
        json.dumps(
            {
                "version": 1,
                "events": [
                    {
                        "id": "public",
                        "name": "Public event",
                        "startsAt": "2026-12-05T12:00:00Z",
                        "discordUrl": "https://discord.gg/public",
                    },
                    {
                        "id": "steve",
                        "name": "Steve event",
                        "description": "Only for Steve",
                        "startsAt": "2026-12-01T20:00:00-03:00",
                        "discordUrl": "https://discord.gg/steve",
                        "users": ["Steve"],
                    },
                    {
                        "id": "alex",
                        "name": "Alex event",
                        "startsAt": "2026-11-30T18:00:00+00:00",
                        "discordUrl": "https://discord.com/events/1",
                        "users": ["alex"],
                    },
                    {
                        "id": "broken",
                        "name": "Broken event",
                        "startsAt": "not-a-date",
                        "discordUrl": "https://discord.gg/broken",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(versions_dir=versions, data_dir=data, token_rate_limit=0)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def issue(client: TestClient) -> str:
    response = client.get("/api/v1/token")
    assert response.status_code == 200, response.text
    return response.json()["token"]


def headers(token: str) -> dict[str, str]:
    return {"X-Dvarc-Token": token}


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["versions"] == 2


def test_token_creation(client: TestClient) -> None:
    response = client.get("/api/v1/token")
    assert response.status_code == 200
    body = response.json()
    assert len(body["token"]) >= 32
    assert body["createdAt"]


def test_versions_require_token(client: TestClient) -> None:
    assert client.get("/api/v1/versions").status_code == 401
    assert client.get("/api/v1/versions", headers=headers("nope")).status_code == 401


def test_list_and_manifest(client: TestClient) -> None:
    token = issue(client)
    client.get("/api/v1/token/bind", params={"username": "Steve"}, headers=headers(token))
    response = client.get("/api/v1/versions", headers=headers(token))
    assert response.status_code == 200
    versions = {item["id"]: item for item in response.json()}
    assert set(versions) == {"demo", "vip"}
    assert versions["demo"]["fileCount"] == 3
    assert versions["vip"]["restricted"] is True

    detail = client.get("/api/v1/versions/demo", headers=headers(token)).json()
    files = {entry["path"]: entry for entry in detail["files"]}
    assert set(files) == {"mods/alpha.jar", "resourcepacks/faithful.zip", "shaderpacks/bsl.zip"}
    assert files["mods/alpha.jar"]["sha256"] == hashlib.sha256(b"alpha-bytes").hexdigest()
    assert files["mods/alpha.jar"]["size"] == len(b"alpha-bytes")
    assert files["mods/alpha.jar"]["url"] == "/api/v1/versions/demo/files/mods/alpha.jar"


def test_download_with_range_and_etag(client: TestClient) -> None:
    token = issue(client)
    response = client.get("/api/v1/versions/demo/files/mods/alpha.jar", headers=headers(token))
    assert response.status_code == 200
    assert response.content == b"alpha-bytes"
    assert response.headers["x-content-sha256"] == hashlib.sha256(b"alpha-bytes").hexdigest()

    partial = client.get(
        "/api/v1/versions/demo/files/mods/alpha.jar",
        headers={**headers(token), "Range": "bytes=0-4"},
    )
    assert partial.status_code == 206
    assert partial.content == b"alpha"


def test_download_rejects_traversal_and_unknown_files(client: TestClient) -> None:
    token = issue(client)
    assert (
        client.get("/api/v1/versions/demo/files/mods/..%2F..%2Fversion.json", headers=headers(token)).status_code
        == 404
    )
    assert client.get("/api/v1/versions/demo/files/version.json", headers=headers(token)).status_code == 404


def test_bind_idempotent_and_first_token_wins(client: TestClient) -> None:
    token_one = issue(client)
    first = client.get("/api/v1/token/bind", params={"username": "Steve"}, headers=headers(token_one))
    assert first.status_code == 200
    assert first.json()== {"status": "bound", "username": "Steve"}

    same = client.get("/api/v1/token/bind", params={"username": "steve"}, headers=headers(token_one))
    assert same.status_code == 200
    assert same.json()["status"] == "already_bound"

    token_two = issue(client)
    conflict = client.get("/api/v1/token/bind", params={"username": "Steve"}, headers=headers(token_two))
    assert conflict.status_code == 409
    assert conflict.json()["discarded"] is True

    # The discarded token is no longer valid.
    assert client.get("/api/v1/me", headers=headers(token_two)).status_code == 401


def test_bind_conflict_keeps_token_with_other_accounts(client: TestClient) -> None:
    holder = issue(client)
    client.get("/api/v1/token/bind", params={"username": "Steve"}, headers=headers(holder))

    token = issue(client)
    assert client.get("/api/v1/token/bind", params={"username": "Alex"}, headers=headers(token)).status_code == 200
    conflict = client.get("/api/v1/token/bind", params={"username": "Steve"}, headers=headers(token))
    assert conflict.status_code == 409
    assert conflict.json()["discarded"] is False

    me = client.get("/api/v1/me", headers=headers(token)).json()
    assert me["usernames"] == ["Alex"]


def test_allowed_users_filter_versions(client: TestClient) -> None:
    unbound = issue(client)
    listed = {item["id"] for item in client.get("/api/v1/versions", headers=headers(unbound)).json()}
    assert listed == {"demo"}
    assert client.get("/api/v1/versions/vip", headers=headers(unbound)).status_code == 403
    assert (
        client.get("/api/v1/versions/vip/files/mods/vip.jar", headers=headers(unbound)).status_code == 403
    )

    steve = issue(client)
    client.get("/api/v1/token/bind", params={"username": "Steve"}, headers=headers(steve))
    listed = {item["id"] for item in client.get("/api/v1/versions", headers=headers(steve)).json()}
    assert listed == {"demo", "vip"}

    alex = issue(client)
    client.get("/api/v1/token/bind", params={"username": "Alex"}, headers=headers(alex))
    listed = {item["id"] for item in client.get("/api/v1/versions", headers=headers(alex)).json()}
    assert listed == {"demo"}


def test_invalid_username(client: TestClient) -> None:
    token = issue(client)
    response = client.get("/api/v1/token/bind", params={"username": "bad name!"}, headers=headers(token))
    assert response.status_code == 400


def test_events_require_token(client: TestClient) -> None:
    assert client.get("/api/v1/events").status_code == 401
    assert client.get("/api/v1/events", headers=headers("nope")).status_code == 401
    assert client.get("/api/v1/events/public", headers=headers("nope")).status_code == 401


def test_events_filter_by_bound_username(client: TestClient) -> None:
    unbound = issue(client)
    listed = client.get("/api/v1/events", headers=headers(unbound)).json()
    assert [event["id"] for event in listed] == ["public"]
    assert client.get("/api/v1/events/steve", headers=headers(unbound)).status_code == 404
    assert client.get("/api/v1/events/missing", headers=headers(unbound)).status_code == 404

    steve = issue(client)
    client.get("/api/v1/token/bind", params={"username": "Steve"}, headers=headers(steve))
    listed = client.get("/api/v1/events", headers=headers(steve)).json()
    assert [event["id"] for event in listed] == ["steve", "public"]
    detail = client.get("/api/v1/events/steve", headers=headers(steve)).json()
    assert detail["name"] == "Steve event"
    assert detail["description"] == "Only for Steve"
    assert client.get("/api/v1/events/alex", headers=headers(steve)).status_code == 404


def test_events_union_of_usernames(client: TestClient) -> None:
    token = issue(client)
    client.get("/api/v1/token/bind", params={"username": "Steve"}, headers=headers(token))
    client.get("/api/v1/token/bind", params={"username": "Alex"}, headers=headers(token))
    listed = client.get("/api/v1/events", headers=headers(token)).json()
    assert [event["id"] for event in listed] == ["alex", "steve", "public"]
    assert client.get("/api/v1/events/alex", headers=headers(token)).status_code == 200


def test_events_payload_fields(client: TestClient) -> None:
    token = issue(client)
    listed = client.get("/api/v1/events", headers=headers(token)).json()
    event = listed[0]
    assert set(event) == {"id", "name", "description", "startsAt", "discordUrl"}
    assert event["startsAt"] == "2026-12-05T12:00:00+00:00"
    assert event["discordUrl"] == "https://discord.gg/public"


def test_events_pick_up_file_changes(client: TestClient) -> None:
    token = issue(client)
    store = EventStore(client.app.state.settings.data_dir)
    store.add(
        {
            "name": "Late event",
            "startsAt": "2026-11-29T10:00:00-03:00",
            "discordUrl": "https://discord.gg/late",
            "users": [],
        },
        event_id="late",
    )
    listed = client.get("/api/v1/events", headers=headers(token)).json()
    assert [event["id"] for event in listed] == ["late", "public"]


def test_events_cors(client: TestClient) -> None:
    token = issue(client)
    origin = "https://dvarc.apurc.site"
    response = client.get("/api/v1/events", headers={**headers(token), "Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin

    other = client.get("/api/v1/events", headers={**headers(token), "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers

    preflight = client.options(
        "/api/v1/events",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Dvarc-Token",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin


def test_event_validation(tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    base = {
        "name": "Event",
        "startsAt": "2026-12-01T20:00:00-03:00",
        "discordUrl": "https://discord.gg/ok",
    }
    with pytest.raises(EventError):
        store.add({**base, "startsAt": "2026-12-01T20:00:00"})
    with pytest.raises(EventError):
        store.add({**base, "discordUrl": "http://discord.gg/ok"})
    with pytest.raises(EventError):
        store.add({**base, "discordUrl": "https://example.com/invite"})
    with pytest.raises(EventError):
        store.add({**base, "users": ["bad name!"]})

    created = store.add({**base, "users": ["Steve", "steve", "Alex"]})
    assert created["users"] == ["Steve", "Alex"]
    assert created["id"].startswith("event-")
    assert store.list_for([]) == []
    assert store.list_for(["Steve"]) == [public_view(created)]
    assert store.get(created["id"], ["Alex"]) is not None
    assert store.get(created["id"], ["Nobody"]) is None
    assert store.remove(created["id"]) is True
    assert store.remove(created["id"]) is False
