"""Internal domain model.

These are plain dataclasses, deliberately independent of both the HTTP layer
(``app.schemas``) and the storage layer. The parsed shapely geometry is kept
on :class:`StoredFeature` so measurements can be recomputed with a different
CRS strategy without re-reading the uploaded file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from shapely.geometry.base import BaseGeometry


def utcnow() -> datetime:
    return datetime.now(UTC)


class FileStatus(StrEnum):
    """Lifecycle of an uploaded file."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    #: Parsed, but at least one feature could not be read or measured.
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    #: Reserved for asynchronous processing. While processing is synchronous a
    #: failure is returned in the upload response and no record is kept, so this
    #: state is part of the published vocabulary rather than something the
    #: current code path produces.
    FAILED = "FAILED"


class SourceFormat(StrEnum):
    SHAPEFILE = "SHAPEFILE"
    KML = "KML"
    KMZ = "KMZ"


class MeasurementKind(StrEnum):
    AREA = "AREA"
    LENGTH = "LENGTH"
    #: Geometry collections that contain both polygonal and linear parts.
    MIXED = "MIXED"
    #: Valid geometry for which no measurement is defined, e.g. a Point.
    NONE = "NONE"
    #: Geometry type the service cannot measure.
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class Measurement:
    """Result of measuring a single geometry."""

    kind: MeasurementKind
    measurable: bool
    #: CRS the measurement was actually computed in, e.g. ``EPSG:32643``.
    measurement_crs: str | None = None
    measurement_crs_name: str | None = None
    #: How that CRS was chosen: ``source_crs``/``utm``/``azimuthal``/``geodesic``.
    method: str | None = None
    area_sq_m: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    #: Human-readable explanation when ``measurable`` is false.
    reason: str | None = None
    #: Informational explanation of the CRS choice. Distinct from a warning:
    #: nothing is wrong, this just says where the numbers came from.
    note: str | None = None

    @property
    def area_sq_km(self) -> float | None:
        return None if self.area_sq_m is None else self.area_sq_m / 1_000_000

    @property
    def area_hectares(self) -> float | None:
        return None if self.area_sq_m is None else self.area_sq_m / 10_000

    @property
    def length_km(self) -> float | None:
        return None if self.length_m is None else self.length_m / 1_000


@dataclass
class StoredFeature:
    """One feature read from an uploaded file."""

    #: Zero-based position within the file, always present.
    index: int
    #: Identifier carried by the source data (KML ``id``/``name``, DBF key) if any.
    feature_id: str | None
    geometry_type: str | None
    geometry: BaseGeometry | None
    #: CRS of ``geometry`` as an authority string, e.g. ``EPSG:4326``.
    crs: str | None
    properties: dict[str, Any] = field(default_factory=dict)
    #: Source layer: the shapefile stem, or the KML folder path.
    layer: str | None = None
    measurement: Measurement | None = None
    warnings: list[str] = field(default_factory=list)
    #: Set when this individual feature failed; the file as a whole still loads.
    error: str | None = None


@dataclass
class FileSummary:
    """Aggregate numbers across every feature in a file."""

    feature_count: int = 0
    measurable_count: int = 0
    unsupported_count: int = 0
    error_count: int = 0
    geometry_type_counts: dict[str, int] = field(default_factory=dict)
    total_area_sq_m: float = 0.0
    total_length_m: float = 0.0

    @property
    def total_area_sq_km(self) -> float:
        return self.total_area_sq_m / 1_000_000

    @property
    def total_area_hectares(self) -> float:
        return self.total_area_sq_m / 10_000

    @property
    def total_length_km(self) -> float:
        return self.total_length_m / 1_000


@dataclass
class UploadedFile:
    """Everything the service knows about one upload."""

    id: str
    filename: str
    size_bytes: int
    status: FileStatus = FileStatus.PENDING
    source_format: SourceFormat | None = None
    #: File-level CRS. Individual features carry their own, which can differ
    #: when a zip holds several shapefiles.
    crs: str | None = None
    crs_name: str | None = None
    #: Where the CRS came from: ``prj``/``kml_specification``/``assumed_default``.
    crs_source: str | None = None
    measurement_strategy: str | None = None
    layers: list[str] = field(default_factory=list)
    features: list[StoredFeature] = field(default_factory=list)
    summary: FileSummary = field(default_factory=FileSummary)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    stored_path: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    processed_at: datetime | None = None
    processing_time_ms: float | None = None

    @property
    def feature_count(self) -> int:
        return len(self.features)
