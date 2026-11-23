"""Discovery and validation of the version folders.

A version is a directory inside ``versions/`` with this layout::

    versions/<id>/
        version.json
        icon.png              (optional)
        mods/*.jar
        resourcepacks/*.zip
        shaderpacks/*.zip

The service never writes into ``versions/``: administrators add or remove
folders directly on the host machine.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import LoaderInfo, VersionDetail, VersionManifestEntry, VersionSummary

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MANAGED_DIRS = ("mods", "resourcepacks", "shaderpacks")
LOADER_TYPES = {"fabric", "quilt", "forge", "neoforge", "vanilla"}
CHUNK_SIZE = 1024 * 1024


class VersionError(Exception):
    status_code = 400


class VersionNotFound(VersionError):
    status_code = 404


class VersionForbidden(VersionError):
    status_code = 403


@dataclass
class VersionData:
    id: str
    meta: dict[str, Any]
    root: Path
    files: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return str(self.meta["name"])

    @property
    def description(self) -> str:
        return str(self.meta.get("description") or "")

    @property
    def minecraft(self) -> str:
        return str(self.meta["minecraft"])

    @property
    def loader(self) -> dict[str, Any]:
        return dict(self.meta["loader"])

    @property
    def java(self) -> int | None:
        value = self.meta.get("java")
        return value if isinstance(value, int) else None

    @property
    def memory(self) -> str | None:
        value = self.meta.get("memory")
        return str(value) if value else None

    @property
    def icon(self) -> str | None:
        value = self.meta.get("icon")
        return str(value) if value else None

    @property
    def allowed_users(self) -> list[str]:
        return list(self.meta.get("allowedUsers") or [])

    @property
    def restricted(self) -> bool:
        return bool(self.allowed_users)

    @property
    def total_size(self) -> int:
        return sum(int(entry["size"]) for entry in self.files)

    @property
    def file_count(self) -> int:
        return len(self.files)

    def is_allowed(self, usernames: list[str]) -> bool:
        allowed = self.allowed_users
        if not allowed:
            return True
        current = {name.casefold() for name in usernames}
        return any(name.casefold() in current for name in allowed)

    def summary(self, base_url: str = "") -> VersionSummary:
        return VersionSummary(
            id=self.id,
            name=self.name,
            description=self.description,
            minecraft=self.minecraft,
            loader=LoaderInfo(**self.loader),
            java=self.java,
            memory=self.memory,
            size=self.total_size,
            fileCount=self.file_count,
            iconUrl=self._file_url(self.icon, base_url) if self.icon else None,
            restricted=self.restricted,
        )

    def detail(self, base_url: str = "") -> VersionDetail:
        return VersionDetail(
            **self.summary(base_url).model_dump(),
            files=[
                VersionManifestEntry(
                    path=str(entry["path"]),
                    size=int(entry["size"]),
                    sha256=str(entry["sha256"]),
                    url=self._file_url(str(entry["path"]), base_url),
                )
                for entry in self.files
            ],
        )

    def _file_url(self, path: str | None, base_url: str) -> str:
        if not path:
            return ""
        encoded = quote(path, safe="/")
        return f"{base_url}/api/v1/versions/{quote(self.id)}/files/{encoded}"

    def sha256_for(self, relative_path: str) -> str | None:
        for entry in self.files:
            if entry["path"] == relative_path:
                return str(entry["sha256"])
        return None


class VersionRegistry:
    def __init__(self, versions_dir: Path) -> None:
        self.root = Path(versions_dir)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[str, VersionData]] = {}
        self._hash_cache: dict[str, tuple[int, int, str]] = {}

    # ------------------------------------------------------------------ scan
    def _hash_file(self, path: Path, mtime_ns: int, size: int) -> str:
        key = str(path)
        cached = self._hash_cache.get(key)
        if cached is not None and cached[0] == mtime_ns and cached[1] == size:
            return cached[2]
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        self._hash_cache[key] = (mtime_ns, size, value)
        return value

    def _read_meta(self, root: Path) -> dict[str, Any]:
        meta_path = root / "version.json"
        if not meta_path.is_file():
            raise VersionNotFound(f"{root.name}: missing version.json")
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VersionError(f"{root.name}: invalid version.json ({exc})") from exc
        if not isinstance(raw, dict):
            raise VersionError(f"{root.name}: version.json must be an object")

        name = raw.get("name") or root.name
        minecraft = raw.get("minecraft")
        if not isinstance(name, str) or not name.strip():
            raise VersionError(f"{root.name}: invalid 'name'")
        if not isinstance(minecraft, str) or not minecraft.strip():
            raise VersionError(f"{root.name}: invalid 'minecraft'")

        loader_raw = raw.get("loader")
        if not isinstance(loader_raw, dict):
            raise VersionError(f"{root.name}: invalid 'loader'")
        loader_type = str(loader_raw.get("type", "")).strip().lower()
        if loader_type not in LOADER_TYPES:
            raise VersionError(f"{root.name}: unknown loader type '{loader_type}'")
        loader_version = loader_raw.get("version")
        if loader_type != "vanilla":
            if not isinstance(loader_version, str) or not loader_version.strip():
                raise VersionError(f"{root.name}: loader '{loader_type}' needs a version")
            loader_version = loader_version.strip()
        else:
            loader_version = None

        java = raw.get("java")
        if java is not None and not isinstance(java, int):
            raise VersionError(f"{root.name}: 'java' must be an integer")

        memory = raw.get("memory")
        if memory is not None and not isinstance(memory, str):
            raise VersionError(f"{root.name}: 'memory' must be a string")

        allowed = raw.get("allowedUsers") or []
        if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
            raise VersionError(f"{root.name}: 'allowedUsers' must be a list of strings")

        icon = raw.get("icon")
        if icon is None:
            icon = "icon.png" if (root / "icon.png").is_file() else None
        elif not isinstance(icon, str) or not icon.strip():
            raise VersionError(f"{root.name}: invalid 'icon'")
        else:
            icon = icon.strip().replace("\\", "/")
            if "/" in icon or icon in {".", ".."}:
                raise VersionError(f"{root.name}: 'icon' must be a file name")
            if not (root / icon).is_file():
                raise VersionError(f"{root.name}: icon '{icon}' does not exist")

        return {
            "name": name.strip(),
            "description": str(raw.get("description") or "").strip(),
            "minecraft": minecraft.strip(),
            "loader": {"type": loader_type, "version": loader_version},
            "java": java,
            "memory": memory.strip() if isinstance(memory, str) else None,
            "icon": icon,
            "allowedUsers": [item.strip() for item in allowed if item.strip()],
        }

    def _scan(self, root: Path) -> tuple[VersionData, str]:
        meta = self._read_meta(root)
        stamp_parts = [f"meta:{json.dumps(meta, sort_keys=True)}"]
        files: list[dict[str, Any]] = []
        for dirname in MANAGED_DIRS:
            directory = root / dirname
            if not directory.is_dir():
                continue
            for path in sorted(item for item in directory.rglob("*") if item.is_file()):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                relative = path.relative_to(root).as_posix()
                stamp_parts.append(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}")
                files.append(
                    {
                        "path": relative,
                        "size": stat.st_size,
                        "sha256": self._hash_file(path, stat.st_mtime_ns, stat.st_size),
                    }
                )
        stamp = hashlib.sha1("\n".join(stamp_parts).encode("utf-8")).hexdigest()
        return VersionData(id=root.name, meta=meta, root=root, files=files), stamp

    # ------------------------------------------------------------------ api
    def get(self, version_id: str) -> VersionData:
        if not SLUG_RE.match(version_id):
            raise VersionNotFound(version_id)
        root = (self.root / version_id).resolve()
        try:
            root.relative_to(self.root.resolve())
        except ValueError as exc:
            raise VersionNotFound(version_id) from exc
        if not root.is_dir():
            raise VersionNotFound(version_id)

        with self._lock:
            data, stamp = self._scan(root)
            cached = self._cache.get(version_id)
            if cached is not None and cached[0] == stamp:
                return cached[1]
            self._cache[version_id] = (stamp, data)
            return data

    def list_all(self) -> list[VersionData]:
        if not self.root.is_dir():
            return []
        result: list[VersionData] = []
        for entry in sorted(self.root.iterdir(), key=lambda item: item.name.casefold()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                result.append(self.get(entry.name))
            except VersionError:
                # Invalid versions are simply not published.
                continue
        return result

    def count(self) -> int:
        return len(self.list_all())

    def resolve_file(self, version: VersionData, relative_path: str) -> tuple[Path, str]:
        relative = relative_path.replace("\\", "/").lstrip("/")
        if not relative or relative.startswith("../") or "/../" in relative or relative == "..":
            raise VersionNotFound(relative_path)
        allowed = {str(entry["path"]) for entry in version.files}
        if version.icon:
            allowed.add(version.icon)
        if relative not in allowed:
            raise VersionNotFound(relative_path)

        root = version.root.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise VersionNotFound(relative_path) from exc
        if not target.is_file():
            raise VersionNotFound(relative_path)

        digest = version.sha256_for(relative)
        if digest is None:
            digest = self._hash_file(target, target.stat().st_mtime_ns, target.stat().st_size)
        return target, digest
