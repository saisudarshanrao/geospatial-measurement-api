"""Shared reader types and attribute normalisation."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from pyproj import CRS
from shapely.geometry.base import BaseGeometry


@dataclass
class RawFeature:
    """A feature as read from a file, before measurement.

    ``crs`` lives on the feature rather than only on the file because a single
    zip can legitimately contain several shapefiles with different ``.prj``
    definitions.
    """

    index: int
    geometry: BaseGeometry | None
    crs: CRS | None
    properties: dict[str, Any] = field(default_factory=dict)
    feature_id: str | None = None
    layer: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: Set when this one feature could not be read. The rest of the file is
    #: still returned - a single bad record never aborts a whole upload.
    error: str | None = None


@dataclass
class ReadResult:
    """Everything a reader extracted from one upload."""

    features: list[RawFeature]
    #: File-level CRS, used for the ``crs`` field of the file-info response.
    crs: CRS | None
    #: ``prj`` | ``kml_specification`` | ``assumed_default``
    crs_source: str
    layers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class GeospatialReader(Protocol):
    """Structural type every reader satisfies."""

    def __call__(self, payload: bytes, *, filename: str | None = ...) -> ReadResult: ...


def json_safe(value: Any) -> Any:
    """Coerce an attribute value into something JSON serialisable.

    DBF fields in particular come back as dates, Decimals and raw bytes, none
    of which survive ``json.dumps``.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # JSON has no NaN/Infinity; null is the honest representation.
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        as_float = float(value)
        return as_float if math.isfinite(as_float) else None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00").strip()
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return str(value)


def normalise_properties(raw: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe copy of an attribute dictionary with tidied keys."""
    return {str(key).strip(): json_safe(value) for key, value in raw.items()}
