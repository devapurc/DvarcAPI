#!/usr/bin/env python3
"""Safe local administration commands for events stored in Cloudflare D1.

This tool invokes Wrangler locally; it does not add write endpoints to the
public API. It validates event input with the same rules as the API and only
touches D1 when a command is explicitly requested.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.events import EVENT_ID_RE, EventError, validate_event, validate_users  # noqa: E402


def sql(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def execute(database: str, command: str, *, require_result: bool = False) -> int:
    completed = subprocess.run(
        ["npx", "wrangler", "d1", "execute", database, "--remote", "--json", "--command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        return completed.returncode
    if require_result:
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            print("ERROR: Wrangler no devolvió un resultado JSON válido", file=sys.stderr)
            return 1
        if not _has_rows(payload):
            print("ERROR: el evento no existe o no fue modificado", file=sys.stderr)
            return 1
    return 0


def _has_rows(value: object) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("results"), list):
            return bool(value["results"])
        if "id" in value:
            return True
        return any(_has_rows(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_rows(item) for item in value)
    return False


def validate_event_id(value: str) -> str:
    if not EVENT_ID_RE.fullmatch(value):
        raise EventError("'id' must use lowercase letters, numbers and dashes")
    return value


def validate_token_hash(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("el hash debe ser SHA-256 hexadecimal de 64 caracteres")
    return value.lower()


def event_from_args(args: argparse.Namespace) -> dict:
    raw = {
        "id": args.event_id,
        "name": args.name,
        "description": args.description,
        "startsAt": args.start,
        "discordUrl": args.discord,
        "users": args.user,
    }
    return validate_event(raw, require_id=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Administra eventos Dvarc en D1 mediante Wrangler")
    parser.add_argument("--database", default="dvarc-db")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list")
    listing.set_defaults(sql="SELECT id, name, starts_at, discord_url, users_json FROM events ORDER BY starts_at, id;")

    add = commands.add_parser("add")
    add.add_argument("event_id")
    add.add_argument("--name", required=True)
    add.add_argument("--start", required=True)
    add.add_argument("--discord", required=True)
    add.add_argument("--description", default="")
    add.add_argument("--user", action="append", default=[])

    edit_event = commands.add_parser("edit")
    edit_event.add_argument("event_id")
    edit_event.add_argument("--name", required=True)
    edit_event.add_argument("--start", required=True)
    edit_event.add_argument("--discord", required=True)
    edit_event.add_argument("--description", default="")
    edit_event.add_argument("--user", action="append", default=[])

    edit = commands.add_parser("edit-users")
    edit.add_argument("event_id")
    edit.add_argument("--user", action="append", default=[])

    remove = commands.add_parser("remove")
    remove.add_argument("event_id")

    delete_token = commands.add_parser("delete-token")
    delete_token.add_argument("token_hash", help="SHA-256 hexadecimal del token")

    args = parser.parse_args()
    if args.command == "list":
        return execute(args.database, args.sql)
    if args.command == "remove":
        try:
            event_id = validate_event_id(args.event_id)
        except EventError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return execute(args.database, f"DELETE FROM events WHERE id = {sql(event_id)} RETURNING id;", require_result=True)
    if args.command == "delete-token":
        try:
            token_hash = validate_token_hash(args.token_hash)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return execute(args.database, f"DELETE FROM tokens WHERE token_hash = {sql(token_hash)} RETURNING token_hash;", require_result=True)
    if args.command == "edit-users":
        try:
            event_id = validate_event_id(args.event_id)
            users = validate_users(args.user)
        except EventError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        encoded_users = json.dumps(users, ensure_ascii=False, separators=(",", ":"))
        return execute(args.database, f"UPDATE events SET users_json = {sql(encoded_users)} WHERE id = {sql(event_id)} RETURNING id;", require_result=True)
    try:
        event = event_from_args(args)
    except EventError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    users = sql(json.dumps(event["users"], ensure_ascii=False))
    if args.command == "edit":
        command = (
            "UPDATE events SET name = " + sql(event["name"]) + ", description = " + sql(event["description"])
            + ", starts_at = " + sql(event["startsAt"]) + ", discord_url = " + sql(event["discordUrl"])
            + ", users_json = " + users + " WHERE id = " + sql(event["id"]) + " RETURNING id;"
        )
    else:
        command = (
            "INSERT INTO events(id, name, description, starts_at, discord_url, users_json) VALUES "
            f"({sql(event['id'])}, {sql(event['name'])}, {sql(event['description'])}, "
            f"{sql(event['startsAt'])}, {sql(event['discordUrl'])}, {users});"
        )
    return execute(args.database, command, require_result=args.command == "edit")


if __name__ == "__main__":
    raise SystemExit(main())
