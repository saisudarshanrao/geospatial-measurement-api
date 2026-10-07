"""HTTP response models and the mapping from the domain model onto them.

The API layer gets its own types rather than serialising dataclasses directly:
it keeps the wire format stable when the internals move, and it gives FastAPI
enough information to generate a useful OpenAPI document.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import mapping

from app.models import (
    FileStatus,
    FileSummary,
    Measurement,
    MeasurementKind,
    SourceFormat,
    StoredFeature,
    UploadedFile,
)

#: Linear values are reported to the millimetre; areas to the square
#: millimetre. Beyond that the digits are float noise, not measurement.
_LINEAR_DP = 3
_AREA_DP = 3


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


# --------------------------------------------------------------------- errors


class ErrorDetail(BaseModel):
    code: str = Field(examples=["unsupported_file_type"])
    message: str = Field(examples=["Unsupported file type."])
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ---------------------------------------------------------------- measurements


class UnitsModel(BaseModel):
    """Explicit units, so a client never has to infer them from a field name."""

    area: str = "square_metre"
    length: str = "metre"


class MeasurementModel(BaseModel):
    kind: MeasurementKind = Field(description="AREA, LENGTH, MIXED, NONE or UNSUPPORTED.")
    measurable: bool = Field(description="False when no measurement could be produced.")
    measurement_crs: str | None = Field(
        default=None, description="CRS the measurement was computed in.", examples=["EPSG:32643"]
    )
    measurement_crs_name: str | None = Field(default=None, examples=["WGS 84 / UTM zone 43N"])
    method: str | None = Field(
        default=None,
        description="How that CRS was chosen: source_crs, utm, azimuthal or geodesic.",
        examples=["utm"],
    )
    area_sq_m: float | None = None
    area_sq_km: float | None = None
    area_hectares: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    length_km: float | None = None
    reason: str | None = Field(
        default=None, description="Why no measurement was produced, when applicable."
    )
    note: str | None = Field(
        default=None,
        description="How the measurement CRS was chosen. Informational, not a warning.",
        examples=["Reprojected to EPSG:32643, the UTM zone containing this feature."],
    )

    @classmethod
    def from_domain(cls, measurement: Measurement) -> MeasurementModel:
        return cls(
            kind=measurement.kind,
            measurable=measurement.measurable,
            measurement_crs=measurement.measurement_crs,
            measurement_crs_name=measurement.measurement_crs_name,
            method=measurement.method,
            area_sq_m=_round(measurement.area_sq_m, _AREA_DP),
            area_sq_km=_round(measurement.area_sq_km, 9),
            area_hectares=_round(measurement.area_hectares, 7),
            perimeter_m=_round(measurement.perimeter_m, _LINEAR_DP),
            length_m=_round(measurement.length_m, _LINEAR_DP),
            length_km=_round(measurement.length_km, 6),
            reason=measurement.reason,
            note=measurement.note,
        )


class FeatureMeasurementModel(BaseModel):
    feature_index: int = Field(description="Zero-based position of the feature within the file.")
    feature_id: str | None = Field(
        default=None, description="Identifier carried by the source data, if any."
    )
    geometry_type: str | None = Field(default=None, examples=["Polygon"])
    source_crs: str | None = Field(
        default=None, description="CRS the feature's coordinates are in.", examples=["EPSG:4326"]
    )
    layer: str | None = Field(default=None, description="Shapefile stem, or KML folder path.")
    measurement: MeasurementModel | None = None
    geometry: dict[str, Any] | None = Field(
        default=None, description="GeoJSON geometry; present only when requested."
    )
    properties: dict[str, Any] | None = Field(
        default=None, description="Source attributes; present only when requested."
    )
    warnings: list[str] = Field(default_factory=list)
    error: str | None = Field(
        default=None, description="Set when this feature alone could not be read."
    )


class SummaryModel(BaseModel):
    feature_count: int
    measurable_count: int = Field(description="Features that produced an area or a length.")
    unsupported_count: int = Field(description="Features whose geometry cannot be measured.")
    error_count: int = Field(description="Features that could not be read at all.")
    geometry_type_counts: dict[str, int] = Field(default_factory=dict)
    total_area_sq_m: float = 0.0
    total_area_sq_km: float = 0.0
    total_area_hectares: float = 0.0
    total_length_m: float = 0.0
    total_length_km: float = 0.0

    @classmethod
    def from_domain(cls, summary: FileSummary) -> SummaryModel:
        return cls(
            feature_count=summary.feature_count,
            measurable_count=summary.measurable_count,
            unsupported_count=summary.unsupported_count,
            error_count=summary.error_count,
            geometry_type_counts=dict(sorted(summary.geometry_type_counts.items())),
            total_area_sq_m=round(summary.total_area_sq_m, _AREA_DP),
            total_area_sq_km=round(summary.total_area_sq_km, 9),
            total_area_hectares=round(summary.total_area_hectares, 7),
            total_length_m=round(summary.total_length_m, _LINEAR_DP),
            total_length_km=round(summary.total_length_km, 6),
        )


# ----------------------------------------------------------------------- files


class FileInfoModel(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "abc123",
                "filename": "survey.kml",
                "size_bytes": 20480,
                "format": "KML",
                "feature_count": 120,
                "crs": "EPSG:4326",
                "crs_name": "WGS 84",
                "crs_source": "kml_specification",
                "measurement_strategy": "auto",
                "status": "COMPLETED",
                "layers": ["Survey blocks"],
                "created_at": "2026-10-07T09:14:02.511Z",
                "processing_time_ms": 42.108,
                "warnings": [],
                "summary": {
                    "feature_count": 120,
                    "measurable_count": 118,
                    "unsupported_count": 0,
                    "error_count": 2,
                    "geometry_type_counts": {"Polygon": 118, "NONE": 2},
                    "total_area_sq_m": 4821905.117,
                    "total_area_sq_km": 4.821905117,
                    "total_area_hectares": 482.1905117,
                    "total_length_m": 0.0,
                    "total_length_km": 0.0,
                },
            }
        }
    )

    id: str = Field(examples=["abc123"])
    filename: str = Field(examples=["survey.kml"])
    size_bytes: int
    format: SourceFormat | None = Field(default=None, examples=["KML"])
    feature_count: int = Field(examples=[120])
    crs: str | None = Field(default=None, examples=["EPSG:4326"])
    crs_name: str | None = Field(default=None, examples=["WGS 84"])
    crs_source: str | None = Field(
        default=None,
        description="prj, kml_specification or assumed_default.",
        examples=["kml_specification"],
    )
    measurement_strategy: str | None = Field(default=None, examples=["auto"])
    status: FileStatus = Field(examples=["COMPLETED"])
    layers: list[str] = Field(default_factory=list)
    created_at: datetime
    processed_at: datetime | None = None
    processing_time_ms: float | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    summary: SummaryModel

    @classmethod
    def from_domain(cls, record: UploadedFile) -> FileInfoModel:
        return cls(
            id=record.id,
            filename=record.filename,
            size_bytes=record.size_bytes,
            format=record.source_format,
            feature_count=record.feature_count,
            crs=record.crs,
            crs_name=record.crs_name,
            crs_source=record.crs_source,
            measurement_strategy=record.measurement_strategy,
            status=record.status,
            layers=record.layers,
            created_at=record.created_at,
            processed_at=record.processed_at,
            processing_time_ms=record.processing_time_ms,
            warnings=record.warnings,
            error=record.error,
            summary=SummaryModel.from_domain(record.summary),
        )


class PageModel(BaseModel):
    """Pagination envelope shared by the collection endpoints."""

    total: int = Field(description="Total items available, ignoring limit/offset.")
    count: int = Field(description="Items in this response.")
    limit: int
    offset: int


class FileListResponse(BaseModel):
    page: PageModel
    files: list[FileInfoModel]


class MeasurementsResponse(BaseModel):
    file_id: str = Field(examples=["abc123"])
    filename: str
    status: FileStatus
    crs: str | None = Field(default=None, description="CRS of the source file.")
    measurement_strategy: str = Field(
        description="Strategy used for these measurements.", examples=["auto"]
    )
    units: UnitsModel = Field(default_factory=UnitsModel)
    summary: SummaryModel
    page: PageModel
    measurements: list[FeatureMeasurementModel]


class FeaturesResponse(BaseModel):
    file_id: str
    filename: str
    status: FileStatus
    crs: str | None = None
    page: PageModel
    features: list[FeatureMeasurementModel]


class DeleteResponse(BaseModel):
    id: str
    deleted: bool = True


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    version: str
    supported_uploads: list[str]
    supported_geometry_types: list[str]
    measurement_strategies: list[str]


# ------------------------------------------------------------------- mapping


def geometry_to_geojson(feature: StoredFeature) -> dict[str, Any] | None:
    """GeoJSON geometry for a stored feature, or None if it has none."""
    if feature.geometry is None:
        return None
    return mapping(feature.geometry)


def feature_to_model(
    feature: StoredFeature,
    *,
    include_geometry: bool = False,
    include_properties: bool = False,
) -> FeatureMeasurementModel:
    return FeatureMeasurementModel(
        feature_index=feature.index,
        feature_id=feature.feature_id,
        geometry_type=feature.geometry_type,
        source_crs=feature.crs,
        layer=feature.layer,
        measurement=(
            MeasurementModel.from_domain(feature.measurement) if feature.measurement else None
        ),
        geometry=geometry_to_geojson(feature) if include_geometry else None,
        properties=feature.properties if include_properties else None,
        warnings=feature.warnings,
        error=feature.error,
    )
