#!/usr/bin/env python3
"""Safe local administration commands for events stored in Cloudflare D1.

This tool invokes Wrangler locally; it does not add write endpoints to the
public API. It validates event input with the same rules as the API and only
touches D1 when a command is explicitly requested.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.events import EventError, validate_event  # noqa: E402


def sql(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def execute(database: str, command: str) -> int:
    return subprocess.run(
        ["npx", "wrangler", "d1", "execute", database, "--remote", "--command", command],
        cwd=ROOT,
        check=False,
    ).returncode


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

    args = parser.parse_args()
    if args.command == "list":
        return execute(args.database, args.sql)
    if args.command == "remove":
        return execute(args.database, f"DELETE FROM events WHERE id = {sql(args.event_id)};")
    if args.command == "edit-users":
        users = json.dumps(args.user, ensure_ascii=False, separators=(",", ":"))
        return execute(args.database, f"UPDATE events SET users_json = {sql(users)} WHERE id = {sql(args.event_id)};")
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
            + ", users_json = " + users + " WHERE id = " + sql(event["id"]) + ";"
        )
    else:
        command = (
            "INSERT INTO events(id, name, description, starts_at, discord_url, users_json) VALUES "
            f"({sql(event['id'])}, {sql(event['name'])}, {sql(event['description'])}, "
            f"{sql(event['startsAt'])}, {sql(event['discordUrl'])}, {users});"
        )
    return execute(args.database, command)


if __name__ == "__main__":
    raise SystemExit(main())
