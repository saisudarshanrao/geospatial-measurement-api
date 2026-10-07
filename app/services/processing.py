"""The file-processing pipeline.

One public entry point, :meth:`FileProcessor.process_upload`, which runs:

1. **detect**   - what format is this, really (content first, extension second)
2. **read**     - parse into :class:`~app.geo.readers.base.RawFeature` records
3. **measure**  - pick a measurement CRS per feature and compute
4. **aggregate**- roll per-feature results into a file-level summary
5. **persist**  - store the record so the GET endpoints can serve it

Processing is synchronous: a measurement is CPU-bound arithmetic, and for file
sizes this service accepts it finishes inside the request, so a queue would add
a round trip and a failure mode for no benefit. The status field is nonetheless
a full state machine, so moving step 2-4 onto a worker later is a change in one
place rather than an API break (see README, "Future scope").
"""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from pathlib import Path

from app.config import Settings
from app.errors import AppError, InvalidGeospatialFileError
from app.geo.archive import extract_archive
from app.geo.crs import MeasurementStrategy, crs_identifier
from app.geo.detect import detect_format
from app.geo.measure import measure_geometry
from app.geo.readers.base import ReadResult
from app.geo.readers.kml_reader import read_kml, read_kmz
from app.geo.readers.shapefile_reader import read_shapefile_zip
from app.models import (
    FileStatus,
    FileSummary,
    MeasurementKind,
    SourceFormat,
    StoredFeature,
    UploadedFile,
    utcnow,
)
from app.services.storage import FileRepository, UploadStore

logger = logging.getLogger(__name__)


class FileProcessor:
    """Turns an uploaded payload into a stored, measured :class:`UploadedFile`."""

    def __init__(
        self,
        repository: FileRepository,
        upload_store: UploadStore,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._uploads = upload_store
        self._settings = settings

    # ------------------------------------------------------------------ public

    def process_upload(
        self,
        *,
        filename: str,
        payload: bytes,
        strategy: MeasurementStrategy,
    ) -> UploadedFile:
        file_id = uuid.uuid4().hex[:12]
        record = UploadedFile(
            id=file_id,
            filename=filename,
            size_bytes=len(payload),
            status=FileStatus.PENDING,
            measurement_strategy=strategy.value,
        )

        stored_path = self._uploads.save(file_id, filename, payload)
        record.stored_path = str(stored_path) if stored_path else None

        started = time.perf_counter()
        record.status = FileStatus.PROCESSING
        try:
            detected = detect_format(payload, filename)
            record.source_format = detected.source_format
            if detected.note:
                record.warnings.append(detected.note)

            read_result = self._read(detected.source_format, payload, filename)
            record.crs = crs_identifier(read_result.crs)
            record.crs_name = read_result.crs.name if read_result.crs else None
            record.crs_source = read_result.crs_source
            record.layers = read_result.layers
            record.warnings.extend(read_result.warnings)

            record.features = self._measure_all(read_result, strategy)
            record.summary = summarise(record.features)
            record.status = (
                FileStatus.COMPLETED_WITH_ERRORS
                if record.summary.error_count or record.summary.unsupported_count
                else FileStatus.COMPLETED
            )
        except AppError as exc:
            # Processing is synchronous, so the error goes back in the POST
            # response and the caller never learns this id. Persisting a FAILED
            # record would leave something unreachable in the repository and in
            # the list endpoint, so the partial upload is discarded instead.
            logger.warning(
                "file_processing_failed", extra={"file_id": file_id, "reason": exc.message}
            )
            self._discard(file_id)
            raise
        except Exception as exc:  # unexpected: log it, then surface a 422
            logger.exception("file_processing_crashed", extra={"file_id": file_id})
            self._discard(file_id)
            raise InvalidGeospatialFileError(
                f"Unexpected error while processing file: {exc}"
            ) from exc

        self._finish(record, started)
        logger.info(
            "file_processed",
            extra={
                "file_id": file_id,
                "format": record.source_format.value if record.source_format else None,
                "features": record.feature_count,
                "status": record.status.value,
                "duration_ms": record.processing_time_ms,
            },
        )
        return record

    def remeasure(self, record: UploadedFile, strategy: MeasurementStrategy) -> list[StoredFeature]:
        """Recompute measurements for a stored file under a different strategy.

        Geometries are retained in the record, so switching from the projected
        default to a geodesic computation does not need the upload again. The
        result is returned rather than written back: the stored record keeps the
        strategy it was processed with.
        """
        remeasured: list[StoredFeature] = []
        for feature in record.features:
            measurement, warnings = measure_geometry(feature.geometry, _crs_of(feature), strategy)
            remeasured.append(
                StoredFeature(
                    index=feature.index,
                    feature_id=feature.feature_id,
                    geometry_type=feature.geometry_type,
                    geometry=feature.geometry,
                    crs=feature.crs,
                    properties=feature.properties,
                    layer=feature.layer,
                    measurement=measurement,
                    warnings=[*feature.warnings, *warnings]
                    if feature.error is None
                    else feature.warnings,
                    error=feature.error,
                )
            )
        return remeasured

    # ----------------------------------------------------------------- private

    def _read(self, source_format: SourceFormat, payload: bytes, filename: str) -> ReadResult:
        if source_format is SourceFormat.KML:
            return read_kml(payload, filename=filename)
        if source_format is SourceFormat.KMZ:
            return read_kmz(
                payload,
                filename=filename,
                max_bytes=self._settings.max_uncompressed_bytes,
            )
        # Shapefiles are a set of sibling files, so pyshp needs them on disk.
        # The extraction directory is temporary: once features are parsed the
        # geometries live in memory and the extracted copy is dead weight.
        with tempfile.TemporaryDirectory(prefix="geoapi-shp-") as workspace:
            extracted = extract_archive(
                payload,
                Path(workspace),
                max_entries=self._settings.max_archive_entries,
                max_uncompressed_bytes=self._settings.max_uncompressed_bytes,
            )
            return read_shapefile_zip(extracted.root, extracted.members)

    def _measure_all(
        self, read_result: ReadResult, strategy: MeasurementStrategy
    ) -> list[StoredFeature]:
        features: list[StoredFeature] = []
        for raw in read_result.features:
            crs = raw.crs if raw.crs is not None else read_result.crs
            warnings = list(raw.warnings)
            if raw.error is not None:
                features.append(
                    StoredFeature(
                        index=raw.index,
                        feature_id=raw.feature_id,
                        geometry_type=None,
                        geometry=None,
                        crs=crs_identifier(crs),
                        properties=raw.properties,
                        layer=raw.layer,
                        measurement=None,
                        warnings=warnings,
                        error=raw.error,
                    )
                )
                continue

            measurement, measure_warnings = measure_geometry(raw.geometry, crs, strategy)
            warnings.extend(measure_warnings)
            features.append(
                StoredFeature(
                    index=raw.index,
                    feature_id=raw.feature_id,
                    geometry_type=raw.geometry.geom_type if raw.geometry is not None else None,
                    geometry=raw.geometry,
                    crs=crs_identifier(crs),
                    properties=raw.properties,
                    layer=raw.layer,
                    measurement=measurement,
                    warnings=warnings,
                )
            )
        return features

    def _discard(self, file_id: str) -> None:
        """Remove anything written for an upload that could not be processed."""
        self._uploads.delete(file_id)

    def _finish(self, record: UploadedFile, started: float) -> None:
        record.processed_at = utcnow()
        record.processing_time_ms = round((time.perf_counter() - started) * 1000, 3)
        self._repository.add(record)


def _crs_of(feature: StoredFeature):
    from app.geo.crs import resolve_crs

    try:
        return resolve_crs(feature.crs)
    except Exception:  # pragma: no cover - stored identifiers are round-trippable
        return None


def summarise(features: list[StoredFeature]) -> FileSummary:
    """Roll per-feature results into file-level totals."""
    summary = FileSummary(feature_count=len(features))
    for feature in features:
        key = feature.geometry_type or "NONE"
        summary.geometry_type_counts[key] = summary.geometry_type_counts.get(key, 0) + 1

        if feature.error is not None:
            summary.error_count += 1
            continue

        measurement = feature.measurement
        if measurement is None:
            continue
        if measurement.kind is MeasurementKind.UNSUPPORTED:
            summary.unsupported_count += 1
            continue
        if measurement.measurable and measurement.kind is not MeasurementKind.NONE:
            summary.measurable_count += 1
        if measurement.area_sq_m:
            summary.total_area_sq_m += measurement.area_sq_m
        if measurement.length_m:
            summary.total_length_m += measurement.length_m
    return summary
