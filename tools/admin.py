#!/usr/bin/env python
"""Local administration helpers for the Dvarc API.

The HTTP API has no write endpoints. Everything is managed from the host by
adding folders to ``versions/``, editing ``data/events.json`` or by using these
commands.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.auth import TokenStore  # noqa: E402
from app.config import Settings  # noqa: E402
from app.events import EventError, EventStore, parse_start, validate_event  # noqa: E402
from app.versions import VersionError, VersionRegistry  # noqa: E402


def cmd_validate(settings: Settings) -> int:
    registry = VersionRegistry(settings.versions_dir)
    if not settings.versions_dir.is_dir():
        print(f"No existe la carpeta de versiones: {settings.versions_dir}")
        return 1

    entries = sorted(
        (entry for entry in settings.versions_dir.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
        key=lambda item: item.name.casefold(),
    )
    errors = 0
    for entry in entries:
        try:
            version = registry.get(entry.name)
        except VersionError as exc:
            errors += 1
            print(f"ERROR  {entry.name}: {exc}")
            continue
        loader = version.loader
        loader_text = loader["type"] if not loader["version"] else f"{loader['type']} {loader['version']}"
        print(
            f"OK     {version.id}: {version.name} | MC {version.minecraft} | {loader_text} | "
            f"{version.file_count} archivos | {version.total_size} bytes"
        )
    print(f"\n{len(entries) - errors} versiones validas, {errors} con errores")
    return 1 if errors else 0


def cmd_list_tokens(settings: Settings) -> int:
    store = TokenStore(settings.data_dir)
    snapshot = store.snapshot()
    tokens = snapshot["tokens"]
    if not tokens:
        print("No hay tokens emitidos.")
        return 0
    for digest, record in tokens.items():
        usernames = ", ".join(record.get("usernames") or []) or "(sin cuentas)"
        print(f"{digest[:16]}  creado={record.get('createdAt')}  ultimo={record.get('lastUsedAt')}  {usernames}")
    return 0


def cmd_list_bindings(settings: Settings) -> int:
    store = TokenStore(settings.data_dir)
    bindings = store.snapshot()["bindings"]
    if not bindings:
        print("No hay cuentas vinculadas.")
        return 0
    for key, record in sorted(bindings.items()):
        print(f"{record.get('username')}  ->  {str(record.get('token'))[:16]}  ({record.get('createdAt')})")
    return 0


def cmd_revoke_token(settings: Settings, prefix: str) -> int:
    store = TokenStore(settings.data_dir)
    if store.revoke(prefix):
        print(f"Token {prefix} revocado junto con sus vinculaciones.")
        return 0
    print(f"No se encontro un token unico que empiece con {prefix}.")
    return 1


# ------------------------------------------------------------------ events
def _event_users(event: dict) -> str:
    return ", ".join(event.get("users") or []) or "todos"


def cmd_events_list(settings: Settings) -> int:
    store = EventStore(settings.data_dir)
    events = store.all()
    if not events:
        print("No hay eventos.")
        return 0
    events.sort(key=lambda event: (parse_start(event["startsAt"]), event["id"]))
    for event in events:
        print(f"{event['id']}  {event['startsAt']}  {event['name']}  [{_event_users(event)}]")
        if event.get("description"):
            print(f"    {event['description']}")
        print(f"    {event['discordUrl']}")
    return 0


def cmd_events_add(settings: Settings, args: argparse.Namespace) -> int:
    store = EventStore(settings.data_dir)
    event = {
        "name": args.name,
        "description": args.description or "",
        "startsAt": args.start,
        "discordUrl": args.discord,
        "users": args.user or [],
    }
    try:
        created = store.add(event, event_id=args.id)
    except EventError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Evento creado: {created['id']}")
    print(f"  {created['startsAt']}  {created['name']}  [{_event_users(created)}]")
    return 0


def cmd_events_users(settings: Settings, args: argparse.Namespace) -> int:
    store = EventStore(settings.data_dir)
    try:
        updated = store.update_users(args.event_id, args.user or [])
    except EventError as exc:
        print(f"ERROR: {exc}")
        return 1
    if updated is None:
        print(f"No existe el evento {args.event_id}.")
        return 1
    print(f"Evento {updated['id']} ahora es visible para: {_event_users(updated)}")
    return 0


def cmd_events_remove(settings: Settings, args: argparse.Namespace) -> int:
    store = EventStore(settings.data_dir)
    if not store.remove(args.event_id):
        print(f"No existe el evento {args.event_id}.")
        return 1
    print(f"Evento {args.event_id} eliminado.")
    return 0


def cmd_events_validate(settings: Settings) -> int:
    path = Path(settings.data_dir) / "events.json"
    if not path.is_file():
        print(f"No existe {path}.")
        return 1
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: no se puede leer {path}: {exc}")
        return 1

    entries = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        print("ERROR: events.json debe contener una lista 'events'.")
        return 1

    errors = 0
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        try:
            event = validate_event(entry, require_id=True)
        except EventError as exc:
            errors += 1
            print(f"ERROR  #{index}: {exc}")
            continue
        if event["id"] in seen:
            errors += 1
            print(f"ERROR  {event['id']}: id duplicado")
            continue
        seen.add(event["id"])
        print(f"OK     {event['id']}: {event['startsAt']}  {event['name']}  [{_event_users(event)}]")
    print(f"\n{len(entries) - errors} eventos validos, {errors} con errores")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Administracion local de Dvarc API")
    parser.add_argument(
        "--versions-dir",
        default=None,
        help="Carpeta de versiones (por defecto DVARC_VERSIONS_DIR o ./versions)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Carpeta de datos (por defecto DVARC_DATA_DIR o ./data)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="Valida todas las versiones")
    subparsers.add_parser("list-tokens", help="Lista tokens emitidos")
    subparsers.add_parser("list-bindings", help="Lista cuentas vinculadas")

    revoke = subparsers.add_parser("revoke-token", help="Revoca un token por prefijo")
    revoke.add_argument("prefix")

    events = subparsers.add_parser("events", help="Administra los eventos")
    events_sub = events.add_subparsers(dest="events_command", required=True)
    events_sub.add_parser("list", help="Lista todos los eventos")
    events_sub.add_parser("validate", help="Valida data/events.json")

    add = events_sub.add_parser("add", help="Crea un evento")
    add.add_argument("--name", required=True)
    add.add_argument("--start", required=True, help="Fecha y hora ISO 8601 con offset, ej. 2026-12-01T20:00-03:00")
    add.add_argument("--discord", required=True, help="Link https de Discord")
    add.add_argument("--description", default="")
    add.add_argument("--user", action="append", default=[], help="Usuario que puede verlo (repetible; ninguno = todos)")
    add.add_argument("--id", default=None, help="Identificador estable (se genera si se omite)")

    users = events_sub.add_parser("users", help="Reemplaza los usuarios de un evento")
    users.add_argument("event_id")
    users.add_argument("--user", action="append", default=[], help="Usuario permitido (repetible; ninguno = todos)")

    remove = events_sub.add_parser("remove", help="Elimina un evento")
    remove.add_argument("event_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()

    settings = replace(
        settings,
        versions_dir=Path(args.versions_dir).resolve() if args.versions_dir else settings.versions_dir,
        data_dir=Path(args.data_dir).resolve() if args.data_dir else settings.data_dir,
    )

    if args.command == "validate":
        return cmd_validate(settings)
    if args.command == "list-tokens":
        return cmd_list_tokens(settings)
    if args.command == "list-bindings":
        return cmd_list_bindings(settings)
    if args.command == "revoke-token":
        return cmd_revoke_token(settings, args.prefix)
    if args.command == "events":
        if args.events_command == "list":
            return cmd_events_list(settings)
        if args.events_command == "validate":
            return cmd_events_validate(settings)
        if args.events_command == "add":
            return cmd_events_add(settings, args)
        if args.events_command == "users":
            return cmd_events_users(settings, args)
        if args.events_command == "remove":
            return cmd_events_remove(settings, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
