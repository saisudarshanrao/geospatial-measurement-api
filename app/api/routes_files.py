"""``/api/files`` endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Path, Query, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    Pagination,
    get_app_settings,
    get_processor,
    get_repository,
    measurement_strategy,
    optional_measurement_strategy,
    pagination,
)
from app.config import Settings
from app.errors import EmptyUploadError, FileTooLargeError, ResourceNotFoundError
from app.geo.crs import MeasurementStrategy
from app.models import UploadedFile
from app.schemas import (
    DeleteResponse,
    ErrorResponse,
    FeaturesResponse,
    FileInfoModel,
    FileListResponse,
    MeasurementsResponse,
    PageModel,
    SummaryModel,
    UnitsModel,
    feature_to_model,
    geometry_to_geojson,
)
from app.services.processing import FileProcessor, summarise
from app.services.storage import FileRepository, UploadStore, safe_filename

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])

#: Chunk size for streaming an upload off the wire.
_READ_CHUNK = 64 * 1024

FILE_ID_PATH = Path(
    ...,
    description="Identifier returned by the upload endpoint.",
    min_length=1,
    max_length=64,
    examples=["abc123"],
)

_COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Unsupported or malformed request."},
    404: {"model": ErrorResponse, "description": "No such file."},
}


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    """Read an upload into memory, refusing anything over ``limit`` bytes.

    Read in chunks and checked as we go: trusting ``Content-Length`` would let a
    client lie, and ``await upload.read()`` with no argument would buffer the
    whole payload before we got the chance to object.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError(
                f"Uploaded file exceeds the maximum size of {limit} bytes.",
                details={"max_bytes": limit},
            )
        chunks.append(chunk)
    if total == 0:
        raise EmptyUploadError("Uploaded file is empty.")
    return b"".join(chunks)


def _require(repository: FileRepository, file_id: str) -> UploadedFile:
    record = repository.get(file_id)
    if record is None:
        raise ResourceNotFoundError(f"No uploaded file with id {file_id!r}.")
    return record


@router.post(
    "/",
    response_model=FileInfoModel,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and process a geospatial file",
    responses={
        **_COMMON_ERRORS,
        413: {"model": ErrorResponse, "description": "Upload too large."},
        422: {"model": ErrorResponse, "description": "Recognised format, unreadable content."},
    },
)
async def upload_file(
    file: UploadFile = File(
        ...,
        description="A .zip containing a shapefile, a .kml document, or a .kmz archive.",
    ),
    strategy: MeasurementStrategy = Depends(measurement_strategy),
    processor: FileProcessor = Depends(get_processor),
    settings: Settings = Depends(get_app_settings),
) -> FileInfoModel:
    """Upload a geospatial file; it is parsed and measured before the response.

    The response is the same payload as ``GET /api/files/{id}/``, so a client
    that only needs the feature count and status does not have to follow up.
    """
    payload = await _read_upload(file, settings.max_upload_bytes)
    filename = safe_filename(file.filename, fallback="upload.bin")
    # Parsing and measuring are CPU-bound, so they run off the event loop.
    record = await run_in_threadpool(
        processor.process_upload,
        filename=filename,
        payload=payload,
        strategy=strategy,
    )
    return FileInfoModel.from_domain(record)


@router.get(
    "/",
    response_model=FileListResponse,
    summary="List uploaded files, newest first",
)
def list_files(
    page: Pagination = Depends(pagination),
    repository: FileRepository = Depends(get_repository),
) -> FileListResponse:
    records, total = repository.list(limit=page.limit, offset=page.offset)
    return FileListResponse(
        page=PageModel(total=total, count=len(records), limit=page.limit, offset=page.offset),
        files=[FileInfoModel.from_domain(record) for record in records],
    )


@router.get(
    "/{file_id}/",
    response_model=FileInfoModel,
    summary="Information about an uploaded file",
    responses=_COMMON_ERRORS,
)
def get_file(
    file_id: str = FILE_ID_PATH,
    repository: FileRepository = Depends(get_repository),
) -> FileInfoModel:
    return FileInfoModel.from_domain(_require(repository, file_id))


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Measurements for the features in a file",
    responses=_COMMON_ERRORS,
)
def get_measurements(
    file_id: str = FILE_ID_PATH,
    page: Pagination = Depends(pagination),
    strategy: MeasurementStrategy | None = Depends(optional_measurement_strategy),
    include_geometry: bool = Query(
        default=False, description="Include each feature's GeoJSON geometry."
    ),
    include_properties: bool = Query(
        default=False, description="Include each feature's source attributes."
    ),
    repository: FileRepository = Depends(get_repository),
    processor: FileProcessor = Depends(get_processor),
) -> MeasurementsResponse:
    """Per-feature measurements, plus file-level totals.

    Passing a ``strategy`` different from the one the file was processed with
    recomputes the measurements from the retained geometries - useful for
    comparing the projected result against a geodesic one.
    """
    record = _require(repository, file_id)

    # No explicit strategy means "as processed": re-measuring with the server
    # default would silently contradict the file's own measurement_strategy.
    effective = strategy or MeasurementStrategy.parse(record.measurement_strategy)
    if effective.value == record.measurement_strategy:
        features = record.features
        summary = record.summary
    else:
        features = processor.remeasure(record, effective)
        summary = summarise(features)

    window = page.slice(features)
    return MeasurementsResponse(
        file_id=record.id,
        filename=record.filename,
        status=record.status,
        crs=record.crs,
        measurement_strategy=effective.value,
        units=UnitsModel(),
        summary=SummaryModel.from_domain(summary),
        page=PageModel(
            total=len(features), count=len(window), limit=page.limit, offset=page.offset
        ),
        measurements=[
            feature_to_model(
                feature,
                include_geometry=include_geometry,
                include_properties=include_properties,
            )
            for feature in window
        ],
    )


@router.get(
    "/{file_id}/features/",
    response_model=FeaturesResponse,
    summary="Full feature records: geometry, CRS, properties and measurement",
    responses=_COMMON_ERRORS,
)
def get_features(
    file_id: str = FILE_ID_PATH,
    page: Pagination = Depends(pagination),
    include_geometry: bool = Query(
        default=True, description="Include each feature's GeoJSON geometry."
    ),
    repository: FileRepository = Depends(get_repository),
) -> FeaturesResponse:
    """Everything extracted per feature: index, id, geometry type, geometry,
    CRS, attributes and the measurement."""
    record = _require(repository, file_id)
    window = page.slice(record.features)
    return FeaturesResponse(
        file_id=record.id,
        filename=record.filename,
        status=record.status,
        crs=record.crs,
        page=PageModel(
            total=record.feature_count, count=len(window), limit=page.limit, offset=page.offset
        ),
        features=[
            feature_to_model(feature, include_geometry=include_geometry, include_properties=True)
            for feature in window
        ],
    )


@router.get(
    "/{file_id}/geojson/",
    summary="The file's features as a GeoJSON FeatureCollection",
    response_model=None,
    responses=_COMMON_ERRORS,
)
def get_geojson(
    file_id: str = FILE_ID_PATH,
    page: Pagination = Depends(pagination),
    repository: FileRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Features as GeoJSON, with the measurement folded into each feature's
    properties.

    Coordinates are returned in the file's own CRS, which is declared in the
    non-standard ``crs`` member. RFC 7946 mandates WGS 84, so a client that
    needs strict GeoJSON should check that member.
    """
    record = _require(repository, file_id)
    window = page.slice(record.features)
    features: list[dict[str, Any]] = []
    for feature in window:
        properties = dict(feature.properties)
        if feature.measurement is not None:
            measurement = feature.measurement
            properties["_measurement"] = {
                "kind": measurement.kind.value,
                "area_sq_m": measurement.area_sq_m,
                "perimeter_m": measurement.perimeter_m,
                "length_m": measurement.length_m,
                "measurement_crs": measurement.measurement_crs,
            }
        features.append(
            {
                "type": "Feature",
                "id": feature.feature_id or feature.index,
                "geometry": geometry_to_geojson(feature),
                "properties": properties,
            }
        )
    return {
        "type": "FeatureCollection",
        "crs": record.crs,
        "features": features,
    }


@router.delete(
    "/{file_id}/",
    response_model=DeleteResponse,
    summary="Delete an uploaded file and its stored bytes",
    responses=_COMMON_ERRORS,
)
def delete_file(
    file_id: str = FILE_ID_PATH,
    repository: FileRepository = Depends(get_repository),
    settings: Settings = Depends(get_app_settings),
) -> DeleteResponse:
    _require(repository, file_id)
    repository.delete(file_id)
    UploadStore(settings.storage_dir, enabled=settings.keep_uploads).delete(file_id)
    logger.info("file_deleted", extra={"file_id": file_id})
    return DeleteResponse(id=file_id)
