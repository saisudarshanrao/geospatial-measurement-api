"""FastAPI dependencies.

The repository, upload store and processor are built once in
``app.main.create_app`` and hung off ``app.state``. Resolving them through
dependencies (rather than module-level globals) is what lets a test build an
isolated app with its own temporary storage.
"""

from __future__ import annotations

from fastapi import Depends, Query, Request

from app.config import Settings, get_settings
from app.errors import InvalidParameterError
from app.geo.crs import MeasurementStrategy
from app.services.processing import FileProcessor
from app.services.storage import FileRepository


def get_repository(request: Request) -> FileRepository:
    return request.app.state.repository


def get_processor(request: Request) -> FileProcessor:
    return request.app.state.processor


def get_app_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


class Pagination:
    """Validated ``limit``/``offset`` pair."""

    def __init__(self, limit: int, offset: int) -> None:
        self.limit = limit
        self.offset = offset

    def slice(self, items: list) -> list:
        return items[self.offset : self.offset + self.limit]


def pagination(
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Maximum items to return. Defaults to the server page size.",
    ),
    offset: int = Query(default=0, ge=0, description="Items to skip."),
    settings: Settings = Depends(get_app_settings),
) -> Pagination:
    effective = settings.default_page_size if limit is None else limit
    if effective > settings.max_page_size:
        raise InvalidParameterError(
            f"limit must not exceed {settings.max_page_size}.",
            details={"limit": effective, "max_limit": settings.max_page_size},
        )
    return Pagination(limit=effective, offset=offset)


_STRATEGY_DESCRIPTION = (
    "CRS strategy for measurement: 'auto' (projected source CRS, else UTM, else "
    "azimuthal), 'utm' (always reproject geographic input to UTM) or 'geodesic' "
    "(ellipsoidal, no projection)."
)


def measurement_strategy(
    strategy: str | None = Query(
        default=None,
        description=f"{_STRATEGY_DESCRIPTION} Defaults to the server setting.",
        examples=["auto"],
    ),
    settings: Settings = Depends(get_app_settings),
) -> MeasurementStrategy:
    """Strategy for a new upload, falling back to the server default."""
    default = MeasurementStrategy.parse(settings.default_strategy)
    return MeasurementStrategy.parse(strategy, default=default)


def optional_measurement_strategy(
    strategy: str | None = Query(
        default=None,
        description=(
            f"{_STRATEGY_DESCRIPTION} Omit to get the measurements exactly as the "
            "file was processed; supplying a different value recomputes them."
        ),
        examples=["geodesic"],
    ),
) -> MeasurementStrategy | None:
    """Strategy for reading measurements back.

    Returns None when the caller did not ask for one, so the route can honour
    the strategy the file was processed with rather than silently re-measuring
    with the server default.
    """
    if strategy is None or strategy == "":
        return None
    return MeasurementStrategy.parse(strategy)
