"""Configuration for the Dvarc API.

Every value can be overridden through environment variables so the service can
be deployed without touching the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CORS_ORIGINS = (
    "https://dvarc.apurc.site",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
)


def _resolve(value: str | None, default: Path) -> Path:
    if not value or not value.strip():
        return default
    path = Path(value.strip())
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _list_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    items = tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())
    return items or default


@dataclass(frozen=True)
class Settings:
    versions_dir: Path
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    token_rate_limit: int = 10
    token_rate_window: float = 60.0
    public_base_url: str | None = None
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            versions_dir=_resolve(os.environ.get("DVARC_VERSIONS_DIR"), PROJECT_ROOT / "versions"),
            data_dir=_resolve(os.environ.get("DVARC_DATA_DIR"), PROJECT_ROOT / "data"),
            host=os.environ.get("DVARC_HOST", "127.0.0.1"),
            port=_int_env("DVARC_PORT", 8765),
            token_rate_limit=_int_env("DVARC_TOKEN_RATE_LIMIT", 10),
            token_rate_window=_float_env("DVARC_TOKEN_RATE_WINDOW", 60.0),
            public_base_url=(os.environ.get("DVARC_PUBLIC_BASE_URL") or "").rstrip("/") or None,
            cors_origins=_list_env("DVARC_CORS_ORIGINS", DEFAULT_CORS_ORIGINS),
        )

    def ensure_dirs(self) -> None:
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
