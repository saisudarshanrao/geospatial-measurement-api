"""Runtime configuration, read once from the environment.

Everything that an operator might reasonably want to change between
environments (limits, storage location, default CRS strategy) lives here
rather than being hard-coded at the call site.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_MB = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - operator error
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    # --- upload limits -------------------------------------------------
    max_upload_bytes: int = 50 * _MB
    #: Guards against zip bombs: cap on total uncompressed bytes in an archive.
    max_uncompressed_bytes: int = 500 * _MB
    #: Guards against archives with an absurd number of members.
    max_archive_entries: int = 2_000

    # --- storage -------------------------------------------------------
    storage_dir: Path = Path("./var/uploads")
    #: When false the original bytes are discarded after parsing.
    keep_uploads: bool = True

    # --- measurement ---------------------------------------------------
    #: One of "auto", "utm", "geodesic" (see app.geo.crs.MeasurementStrategy).
    default_strategy: str = "auto"

    # --- service -------------------------------------------------------
    log_level: str = "INFO"
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    #: Default/maximum page size for the paginated collection endpoints.
    default_page_size: int = 100
    max_page_size: int = 1_000

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            max_upload_bytes=_env_int("GEOAPI_MAX_UPLOAD_BYTES", 50 * _MB),
            max_uncompressed_bytes=_env_int("GEOAPI_MAX_UNCOMPRESSED_BYTES", 500 * _MB),
            max_archive_entries=_env_int("GEOAPI_MAX_ARCHIVE_ENTRIES", 2_000),
            storage_dir=Path(os.getenv("GEOAPI_STORAGE_DIR", "./var/uploads")),
            keep_uploads=_env_bool("GEOAPI_KEEP_UPLOADS", True),
            default_strategy=os.getenv("GEOAPI_DEFAULT_STRATEGY", "auto"),
            log_level=os.getenv("GEOAPI_LOG_LEVEL", "INFO").upper(),
            cors_origins=_env_list("GEOAPI_CORS_ORIGINS", ["*"]),
            default_page_size=_env_int("GEOAPI_DEFAULT_PAGE_SIZE", 100),
            max_page_size=_env_int("GEOAPI_MAX_PAGE_SIZE", 1_000),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance, used as a FastAPI dependency."""
    return Settings.from_env()
