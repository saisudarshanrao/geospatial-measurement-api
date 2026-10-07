"""Measurement of individual geometries.

Dispatch is by geometry family rather than by exact type name, so the multi-
part variants fall out for free:

=========================  ==================================
Geometry                   Measurement
=========================  ==================================
Polygon, MultiPolygon      area (plus perimeter, as a bonus)
LineString, LinearRing,    length
MultiLineString
Point, MultiPoint          none defined - reported, not failed
GeometryCollection         area and length of its parts
anything else              reported as unsupported
=========================  ==================================

Every path returns a :class:`~app.models.Measurement`; nothing here raises for
an unmeasurable geometry, because one odd feature must not fail a 10,000
feature file.
"""

from __future__ import annotations

import shapely
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.geo.crs import (
    GEOD_WGS84,
    WGS84,
    CRSResolutionError,
    MeasurementPlan,
    MeasurementStrategy,
    crs_identifier,
    plan_measurement,
    reproject,
)
from app.models import Measurement, MeasurementKind

POLYGONAL = {"Polygon", "MultiPolygon"}
LINEAR = {"LineString", "MultiLineString", "LinearRing"}
PUNTAL = {"Point", "MultiPoint"}
COLLECTION = {"GeometryCollection"}

SUPPORTED_GEOMETRY_TYPES = sorted(POLYGONAL | LINEAR | PUNTAL | COLLECTION)


def _unsupported(reason: str) -> Measurement:
    return Measurement(
        kind=MeasurementKind.UNSUPPORTED,
        measurable=False,
        reason=reason,
    )


def _collection_families(geometry: BaseGeometry) -> tuple[bool, bool]:
    """(has_polygonal, has_linear) anywhere inside a collection, recursively."""
    has_area = has_length = False
    for part in getattr(geometry, "geoms", []):
        if part.is_empty:
            continue
        if part.geom_type in POLYGONAL:
            has_area = True
        elif part.geom_type in LINEAR:
            has_length = True
        elif part.geom_type in COLLECTION:
            nested_area, nested_length = _collection_families(part)
            has_area = has_area or nested_area
            has_length = has_length or nested_length
    return has_area, has_length


def _flatten(geometry: BaseGeometry) -> list[BaseGeometry]:
    """Non-empty, non-collection parts of a (possibly nested) collection."""
    parts: list[BaseGeometry] = []
    for part in getattr(geometry, "geoms", []):
        if part.is_empty:
            continue
        if part.geom_type in COLLECTION:
            parts.extend(_flatten(part))
        else:
            parts.append(part)
    return parts


def _geodesic_area_and_perimeter(geometry: BaseGeometry) -> tuple[float, float]:
    """Ellipsoidal area/perimeter, summing the parts of a multi-geometry."""
    if geometry.geom_type == "MultiPolygon":
        total_area = total_perimeter = 0.0
        for part in geometry.geoms:
            area, perimeter = GEOD_WGS84.geometry_area_perimeter(part)
            total_area += abs(area)
            total_perimeter += abs(perimeter)
        return total_area, total_perimeter
    area, perimeter = GEOD_WGS84.geometry_area_perimeter(geometry)
    return abs(area), abs(perimeter)


def _geodesic_length(geometry: BaseGeometry) -> float:
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        return sum(abs(GEOD_WGS84.geometry_length(part)) for part in geometry.geoms)
    return abs(GEOD_WGS84.geometry_length(geometry))


def _prepare(geometry: BaseGeometry, source: CRS, plan: MeasurementPlan) -> BaseGeometry:
    """Move the geometry into the CRS the plan selected."""
    if plan.is_geodesic:
        return geometry if source.equals(WGS84) else reproject(geometry, source, WGS84)
    assert plan.crs is not None
    return reproject(geometry, source, plan.crs)


def _describe(plan: MeasurementPlan) -> dict[str, str | None]:
    if plan.is_geodesic:
        return {
            "measurement_crs": crs_identifier(WGS84),
            "measurement_crs_name": "WGS 84 (ellipsoidal / geodesic computation)",
            "method": plan.method,
            "note": plan.note,
        }
    assert plan.crs is not None
    return {
        "measurement_crs": plan.crs_label or crs_identifier(plan.crs),
        "measurement_crs_name": (
            plan.crs.name if plan.crs.name and plan.crs.name != "unknown" else plan.crs_label
        ),
        "method": plan.method,
        "note": plan.note,
    }


def measure_geometry(
    geometry: BaseGeometry | None,
    source_crs: CRS | None,
    strategy: MeasurementStrategy = MeasurementStrategy.AUTO,
) -> tuple[Measurement, list[str]]:
    """Measure one geometry.

    Returns the measurement plus any warnings worth surfacing to the caller
    (an assumed CRS, a repaired geometry, ...). Never raises for bad input.
    """
    warnings: list[str] = []

    if geometry is None:
        return _unsupported("Feature has no geometry."), warnings
    geometry_type = geometry.geom_type
    if geometry.is_empty:
        return (
            Measurement(
                kind=MeasurementKind.NONE,
                measurable=False,
                reason=f"Geometry is an empty {geometry_type}.",
            ),
            warnings,
        )
    if source_crs is None:
        return _unsupported("Feature has no CRS, so it cannot be measured."), warnings

    if geometry_type in PUNTAL:
        return (
            Measurement(
                kind=MeasurementKind.NONE,
                measurable=True,
                reason=f"No measurement is defined for {geometry_type} geometry.",
            ),
            warnings,
        )

    if geometry_type not in POLYGONAL | LINEAR | COLLECTION:
        return _unsupported(
            f"Geometry type {geometry_type!r} is not supported for measurement."
        ), warnings

    # Only 2D measurements are reported, so drop any Z ordinate up front. This
    # keeps a 3D KML LineString from being measured as a slope distance in one
    # code path and a plan distance in another.
    if shapely.has_z(geometry):
        geometry = shapely.force_2d(geometry)
        warnings.append("Z ordinates were ignored; measurements are planar (2D).")

    if geometry_type in POLYGONAL and not geometry.is_valid:
        repaired = shapely.make_valid(geometry)
        reason = shapely.is_valid_reason(geometry)
        if repaired.is_empty or repaired.geom_type not in POLYGONAL | COLLECTION:
            return (
                _unsupported(f"Geometry is invalid and could not be repaired ({reason})."),
                warnings,
            )
        warnings.append(f"Invalid geometry was repaired before measuring ({reason}).")
        geometry = repaired
        geometry_type = geometry.geom_type

    wants_area = geometry_type in POLYGONAL
    if geometry_type in COLLECTION:
        has_area, has_length = _collection_families(geometry)
        if not has_area and not has_length:
            return (
                Measurement(
                    kind=MeasurementKind.NONE,
                    measurable=True,
                    reason="GeometryCollection contains only point geometries.",
                ),
                warnings,
            )
        wants_area = has_area

    try:
        plan = plan_measurement(geometry, source_crs, strategy, equal_area=wants_area)
        working = _prepare(geometry, source_crs, plan)
    except CRSResolutionError as exc:
        return _unsupported(str(exc)), warnings
    except Exception as exc:  # pragma: no cover - PROJ edge cases
        return _unsupported(f"Could not project geometry for measurement: {exc}"), warnings

    described = _describe(plan)

    factor = plan.unit_factor
    area_factor = factor * factor

    try:
        if geometry_type in POLYGONAL:
            if plan.is_geodesic:
                area, perimeter = _geodesic_area_and_perimeter(working)
            else:
                area, perimeter = working.area * area_factor, working.length * factor
            return (
                Measurement(
                    kind=MeasurementKind.AREA,
                    measurable=True,
                    area_sq_m=area,
                    perimeter_m=perimeter,
                    **described,
                ),
                warnings,
            )

        if geometry_type in LINEAR:
            length = _geodesic_length(working) if plan.is_geodesic else working.length * factor
            return (
                Measurement(
                    kind=MeasurementKind.LENGTH,
                    measurable=True,
                    length_m=length,
                    **described,
                ),
                warnings,
            )

        # GeometryCollection: measure each family and report both.
        area = perimeter = length = 0.0
        for part in _flatten(working):
            if part.geom_type in POLYGONAL:
                if plan.is_geodesic:
                    part_area, part_perimeter = _geodesic_area_and_perimeter(part)
                else:
                    part_area, part_perimeter = part.area * area_factor, part.length * factor
                area += part_area
                perimeter += part_perimeter
            elif part.geom_type in LINEAR:
                length += _geodesic_length(part) if plan.is_geodesic else part.length * factor
        has_area, has_length = _collection_families(working)
        kind = (
            MeasurementKind.MIXED
            if (has_area and has_length)
            else (MeasurementKind.AREA if has_area else MeasurementKind.LENGTH)
        )
        return (
            Measurement(
                kind=kind,
                measurable=True,
                area_sq_m=area if has_area else None,
                perimeter_m=perimeter if has_area else None,
                length_m=length if has_length else None,
                **described,
            ),
            warnings,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return _unsupported(f"Measurement failed: {exc}"), warnings
