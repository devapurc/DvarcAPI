"""Pydantic models used by the Dvarc API responses."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LoaderType = Literal["fabric", "quilt", "forge", "neoforge", "vanilla"]


class LoaderInfo(BaseModel):
    type: LoaderType
    version: str | None = None


class VersionManifestEntry(BaseModel):
    path: str
    size: int
    sha256: str
    url: str


class VersionSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    minecraft: str
    loader: LoaderInfo
    java: int | None = None
    memory: str | None = None
    size: int
    fileCount: int
    iconUrl: str | None = None
    restricted: bool = False


class VersionDetail(VersionSummary):
    files: list[VersionManifestEntry] = Field(default_factory=list)


class TokenResponse(BaseModel):
    token: str
    createdAt: str


class BindResponse(BaseModel):
    status: str
    username: str | None = None


class MeResponse(BaseModel):
    usernames: list[str]


class EventSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    startsAt: str
    discordUrl: str


class HealthResponse(BaseModel):
    status: str
    versions: int
    time: str
