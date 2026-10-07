"""CRS resolution and the choice of a CRS to measure in.

The rule this module enforces is the one that matters for correctness:
**never compute area or length from longitude/latitude degrees.** A degree is
not a unit of distance, so ``shapely``'s planar ``.area`` on EPSG:4326
coordinates returns "square degrees", a number whose relationship to square
metres changes with latitude.

So before measuring, every geometry is moved into a CRS whose axes are in
linear units:

``source_crs``
    The file is already projected (its axes are metres, feet, ...). Measure
    where it is and scale by the CRS's unit-to-metre factor. No reprojection,
    so no extra error is introduced.
``utm``
    Geographic input that fits inside a single UTM zone. The zone is picked
    per feature from that feature's own centroid, so a file spanning several
    zones still gets a locally accurate projection for each feature. UTM
    scale distortion is about 1 part in 2500 at the zone edges.
``azimuthal``
    Geographic input too wide for one UTM zone, or beyond UTM's latitude
    range. Falls back to an azimuthal projection centred on the feature:
    Lambert azimuthal equal-area (``laea``) for areas, azimuthal equidistant
    (``aeqd``) for lengths — each exact for the quantity it is used for at
    the projection centre.
``geodesic``
    Opt-in. Skips projection entirely and integrates on the WGS 84 ellipsoid
    via ``pyproj.Geod``. Has no projection distortion at all and is the right
    answer for continent-sized features, but it is slower and is not what
    most GIS tools report, so it is not the default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Geod, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
from shapely.geometry.base import BaseGeometry

from app.errors import InvalidParameterError

WGS84 = CRS.from_epsg(4326)
GEOD_WGS84 = Geod(ellps="WGS84")

#: Widest longitude span (degrees) we still consider a single-UTM-zone feature.
#: A UTM zone is 6 degrees wide; allowing one zone of spill either side keeps
#: distortion acceptable while avoiding a fallback for features that merely
#: straddle a zone boundary.
MAX_UTM_LON_SPAN_DEG = 12.0
#: UTM is only defined between these latitudes.
MAX_UTM_LAT_DEG = 84.0
MIN_UTM_LAT_DEG = -80.0


class MeasurementStrategy(StrEnum):
    """How to choose the CRS that measurements are computed in."""

    #: Projected source CRS as-is, else UTM, else azimuthal. The default.
    AUTO = "auto"
    #: Like AUTO but always reprojects geographic input to UTM, with the
    #: azimuthal fallback only when UTM is undefined for the location.
    UTM = "utm"
    #: Ellipsoidal computation, no projection.
    GEODESIC = "geodesic"

    @classmethod
    def parse(
        cls, value: str | None, *, default: MeasurementStrategy | None = None
    ) -> MeasurementStrategy:
        if value is None or value == "":
            return default or cls.AUTO
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            allowed = ", ".join(s.value for s in cls)
            raise InvalidParameterError(
                f"Unknown measurement strategy {value!r}. Allowed values: {allowed}.",
                details={"allowed": [s.value for s in cls]},
            ) from exc


class CRSResolutionError(ValueError):
    """Raised when a CRS definition cannot be understood."""


def resolve_crs(definition: str | int | CRS | None) -> CRS | None:
    """Best-effort parse of a CRS from WKT, PROJ, an EPSG code or an int."""
    if definition is None:
        return None
    if isinstance(definition, CRS):
        return definition
    if isinstance(definition, int):
        return CRS.from_epsg(definition)
    text = definition.strip()
    if not text:
        return None
    try:
        return CRS.from_user_input(text)
    except Exception as exc:  # pyproj raises several unrelated types
        raise CRSResolutionError(f"Could not interpret CRS definition: {text[:120]!r}") from exc


def crs_identifier(crs: CRS | None) -> str | None:
    """``"EPSG:4326"`` when the CRS is registered, else a stable fallback."""
    if crs is None:
        return None
    authority = crs.to_authority()
    if authority:
        return f"{authority[0]}:{authority[1]}"
    # Unregistered CRS, e.g. the custom azimuthal fallback. Note that
    # ``CRS.to_proj4()`` is deliberately NOT used here: pyproj raises a
    # UserWarning on every lossy export, which would fire on each measurement.
    # Callers that built the CRS themselves pass their own label instead
    # (see MeasurementPlan.crs_label).
    return crs.name if crs.name and crs.name != "unknown" else "unregistered CRS"


def linear_unit_to_metres(crs: CRS) -> float:
    """Metres per one unit of the CRS's horizontal axes.

    1.0 for a metre-based CRS, ~0.3048 for one in feet. Used so a projected
    source CRS can be measured in place and still report SI units.
    """
    if not crs.axis_info:
        return 1.0
    factors = [
        axis.unit_conversion_factor
        for axis in crs.axis_info
        if axis.unit_conversion_factor
        and axis.direction.lower() in {"east", "west", "north", "south"}
    ]
    if not factors:
        factors = [
            axis.unit_conversion_factor for axis in crs.axis_info if axis.unit_conversion_factor
        ]
    return float(factors[0]) if factors else 1.0


@lru_cache(maxsize=512)
def _transformer(source_srs: str, target_srs: str) -> Transformer:
    """Cached transformer. Building one is expensive; reuse is safe and thread-safe."""
    return Transformer.from_crs(
        CRS.from_user_input(source_srs), CRS.from_user_input(target_srs), always_xy=True
    )


def get_transformer(source: CRS, target: CRS) -> Transformer:
    return _transformer(source.srs, target.srs)


def reproject(geometry: BaseGeometry, source: CRS, target: CRS) -> BaseGeometry:
    """Reproject a 2D geometry between two CRS.

    Uses the vectorised ``shapely.transform`` so each geometry needs a single
    call into PROJ regardless of how many vertices it has.
    """
    if source.equals(target):
        return geometry
    transformer = get_transformer(source, target)

    def _transform(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([np.asarray(x, dtype=float), np.asarray(y, dtype=float)])

    projected = shapely.transform(geometry, _transform)
    if not np.isfinite(shapely.get_coordinates(projected)).all():
        raise CRSResolutionError(
            f"Reprojection from {crs_identifier(source)} to {crs_identifier(target)} "
            "produced non-finite coordinates; the geometry is probably outside "
            "the target CRS's area of use."
        )
    return projected


@dataclass(frozen=True)
class MeasurementPlan:
    """The decision about where a single geometry will be measured."""

    #: None means "measure geodesically, no projection".
    crs: CRS | None
    #: ``source_crs`` | ``utm`` | ``azimuthal`` | ``geodesic``
    method: str
    #: Multiplier from CRS units to metres (1.0 except for e.g. US survey feet).
    unit_factor: float = 1.0
    note: str | None = None
    #: Label for a CRS that has no authority code, set when we built it
    #: ourselves so no lossy ``to_proj4()`` export is needed to describe it.
    crs_label: str | None = None

    @property
    def is_geodesic(self) -> bool:
        return self.crs is None


def _lonlat_bounds(geometry: BaseGeometry, source: CRS) -> tuple[float, float, float, float]:
    """Geometry bounds as (min_lon, min_lat, max_lon, max_lat) in WGS 84."""
    geographic = geometry if source.equals(WGS84) else reproject(geometry, source, WGS84)
    min_lon, min_lat, max_lon, max_lat = geographic.bounds
    if max_lon - min_lon > 180.0:
        # Almost certainly an antimeridian crossing rather than a genuinely
        # global feature: re-express negative longitudes in 0..360 so the
        # centre and span come out sensibly.
        lons = shapely.get_coordinates(geographic)[:, 0]
        shifted = np.where(lons < 0, lons + 360.0, lons)
        min_lon, max_lon = float(shifted.min()), float(shifted.max())
    return min_lon, min_lat, max_lon, max_lat


def _azimuthal_crs(lon: float, lat: float, *, equal_area: bool) -> tuple[CRS, str]:
    """An azimuthal projection centred on a point, plus its PROJ string.

    ``laea`` preserves area, ``aeqd`` preserves distance from the centre; both
    are built on WGS 84 so they compose with any geographic source CRS.

    The PROJ string is returned rather than recovered later with
    ``CRS.to_proj4()``, which warns about lossy conversion on every call.
    """
    proj = "laea" if equal_area else "aeqd"
    definition = (
        f"+proj={proj} +lat_0={lat:.8f} +lon_0={lon:.8f} +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m +no_defs"
    )
    return CRS.from_proj4(definition), definition


def _utm_crs(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> CRS | None:
    """The UTM CRS covering a bounding box, or None if PROJ knows of none."""
    # query_utm_crs_info expects longitudes in -180..180.
    west = ((min_lon + 180.0) % 360.0) - 180.0
    east = ((max_lon + 180.0) % 360.0) - 180.0
    if east < west:  # bbox crosses the antimeridian; no single zone fits
        return None
    candidates = query_utm_crs_info(
        datum_name="WGS 84",
        area_of_interest=AreaOfInterest(
            west_lon_degree=west,
            south_lat_degree=max(min_lat, -90.0),
            east_lon_degree=east,
            north_lat_degree=min(max_lat, 90.0),
        ),
    )
    if not candidates:
        return None
    return CRS.from_authority(candidates[0].auth_name, candidates[0].code)


def plan_measurement(
    geometry: BaseGeometry,
    source: CRS,
    strategy: MeasurementStrategy = MeasurementStrategy.AUTO,
    *,
    equal_area: bool = True,
) -> MeasurementPlan:
    """Pick the CRS to measure ``geometry`` in.

    ``equal_area`` only affects the azimuthal fallback: areas want
    ``laea``, lengths want ``aeqd``.
    """
    if strategy is MeasurementStrategy.GEODESIC:
        return MeasurementPlan(
            crs=None, method="geodesic", note="Computed on the WGS 84 ellipsoid."
        )

    if source.is_projected and strategy is MeasurementStrategy.AUTO:
        factor = linear_unit_to_metres(source)
        unit = source.axis_info[0].unit_name if source.axis_info else "unknown"
        return MeasurementPlan(
            crs=source,
            method="source_crs",
            unit_factor=factor,
            note=(
                f"Source CRS is already projected (axis unit: {unit}); "
                "measured without reprojection."
            ),
        )

    # Geographic source (or UTM explicitly requested for a projected one).
    try:
        min_lon, min_lat, max_lon, max_lat = _lonlat_bounds(geometry, source)
    except CRSResolutionError:
        raise
    if not all(math.isfinite(v) for v in (min_lon, min_lat, max_lon, max_lat)):
        raise CRSResolutionError("Geometry has non-finite coordinates.")

    centre_lon = (min_lon + max_lon) / 2.0
    centre_lat = (min_lat + max_lat) / 2.0
    lon_span = max_lon - min_lon

    too_wide = lon_span > MAX_UTM_LON_SPAN_DEG
    out_of_band = max_lat > MAX_UTM_LAT_DEG or min_lat < MIN_UTM_LAT_DEG

    if not too_wide and not out_of_band:
        utm = _utm_crs(min_lon, min_lat, max_lon, max_lat)
        if utm is not None:
            return MeasurementPlan(
                crs=utm,
                method="utm",
                unit_factor=linear_unit_to_metres(utm),
                note=f"Reprojected to {crs_identifier(utm)}, the UTM zone containing this feature.",
            )

    if too_wide:
        why = f"spans {lon_span:.1f} degrees of longitude, wider than a UTM zone"
    elif out_of_band:
        why = "lies outside the latitude range UTM covers"
    else:
        why = "has no UTM zone defined for its location"
    normalised_lon = ((centre_lon + 180.0) % 360.0) - 180.0
    fallback, definition = _azimuthal_crs(normalised_lon, centre_lat, equal_area=equal_area)
    kind = "equal-area" if equal_area else "equidistant"
    return MeasurementPlan(
        crs=fallback,
        method="azimuthal",
        unit_factor=1.0,
        crs_label=definition,
        note=(
            f"Feature {why}; measured in an azimuthal {kind} projection centred on "
            f"({centre_lat:.4f}, {normalised_lon:.4f})."
        ),
    )
