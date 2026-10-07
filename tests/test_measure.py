"""Measurement correctness, including the degrees-are-not-metres trap."""

from __future__ import annotations

import pytest
import shapely
from pyproj import CRS, Geod
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)

from app.geo.crs import WGS84, MeasurementStrategy
from app.geo.measure import measure_geometry
from app.models import MeasurementKind

GEOD = Geod(ellps="WGS84")
UTM43N = CRS.from_epsg(32643)


def measure(geometry, crs=WGS84, strategy=MeasurementStrategy.AUTO):
    measurement, warnings = measure_geometry(geometry, crs, strategy)
    return measurement, warnings


def test_projected_square_area_is_exact():
    """A 250 m square in a metre-based CRS must measure 62,500 m exactly."""
    square = Polygon([(500000, 1400000), (500250, 1400000), (500250, 1400250), (500000, 1400250)])

    measurement, _ = measure(square, UTM43N)

    assert measurement.kind is MeasurementKind.AREA
    assert measurement.measurable
    assert measurement.area_sq_m == pytest.approx(62_500.0)
    assert measurement.perimeter_m == pytest.approx(1_000.0)
    assert measurement.method == "source_crs"
    assert measurement.measurement_crs == "EPSG:32643"


def test_area_in_degrees_would_be_wrong_so_the_geometry_is_reprojected():
    """The regression this service exists to prevent.

    The same polygon measured naively on EPSG:4326 coordinates gives 0.0001
    "square degrees". The API must instead report roughly 1.2 million square
    metres, which is what the ellipsoid says.
    """
    parcel = Polygon(
        [(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98), (77.59, 12.97)]
    )
    naive_planar_area = parcel.area
    assert naive_planar_area == pytest.approx(1e-4)  # square degrees: meaningless

    measurement, _ = measure(parcel)
    geodesic_area = abs(GEOD.geometry_area_perimeter(parcel)[0])

    assert measurement.measurement_crs == "EPSG:32643"
    # UTM is conformal, not equal-area: at 2.6 degrees off the zone's central
    # meridian the scale factor leaves ~0.1% of area distortion. That is the
    # documented cost of the default strategy; ?strategy=geodesic removes it.
    assert measurement.area_sq_m == pytest.approx(geodesic_area, rel=0.005)
    assert measurement.area_sq_m > 1_000_000


def test_crs_choice_is_a_note_not_a_warning():
    """A successful measurement must not look like something went wrong."""
    parcel = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])

    measurement, warnings = measure(parcel)

    assert warnings == []
    assert "EPSG:32643" in (measurement.note or "")


def test_unit_conversions_are_consistent():
    parcel = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])

    measurement, _ = measure(parcel)

    assert measurement.area_sq_km == pytest.approx(measurement.area_sq_m / 1e6)
    assert measurement.area_hectares == pytest.approx(measurement.area_sq_m / 1e4)


def test_linestring_length_matches_the_geodesic_distance():
    road = LineString([(77.60, 12.97), (77.61, 12.98), (77.62, 12.98)])

    measurement, _ = measure(road)
    geodesic_length = GEOD.geometry_length(road)

    assert measurement.kind is MeasurementKind.LENGTH
    assert measurement.length_m == pytest.approx(geodesic_length, rel=0.001)
    assert measurement.length_km == pytest.approx(measurement.length_m / 1000)
    assert measurement.area_sq_m is None


def test_point_is_supported_but_has_no_measurement():
    measurement, _ = measure(Point(77.5946, 12.9716))

    assert measurement.kind is MeasurementKind.NONE
    assert measurement.measurable is True  # the feature is fine, it just has no size
    assert measurement.area_sq_m is None
    assert measurement.length_m is None
    assert "No measurement is defined" in (measurement.reason or "")


def test_multipoint_behaves_like_point():
    measurement, _ = measure(MultiPoint([(77.0, 12.0), (77.1, 12.1)]))
    assert measurement.kind is MeasurementKind.NONE


def test_multipolygon_area_is_the_sum_of_its_parts():
    first = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])
    second = Polygon([(77.61, 12.95), (77.62, 12.95), (77.62, 12.96), (77.61, 12.96)])

    combined, _ = measure(MultiPolygon([first, second]))
    part_one, _ = measure(first)
    part_two, _ = measure(second)

    assert combined.area_sq_m == pytest.approx(part_one.area_sq_m + part_two.area_sq_m, rel=0.002)


def test_multilinestring_length_is_the_sum_of_its_parts():
    geometry = MultiLineString([[(77.0, 12.0), (77.01, 12.0)], [(77.0, 12.1), (77.02, 12.1)]])

    measurement, _ = measure(geometry)

    assert measurement.kind is MeasurementKind.LENGTH
    assert measurement.length_m == pytest.approx(GEOD.geometry_length(geometry), rel=0.001)


def test_polygon_with_a_hole_excludes_the_hole():
    shell = [(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)]
    hole = [(77.593, 12.973), (77.595, 12.973), (77.595, 12.975), (77.593, 12.975)]

    solid, _ = measure(Polygon(shell))
    holed, _ = measure(Polygon(shell, [hole]))

    assert holed.area_sq_m < solid.area_sq_m
    # The hole is a fifth of the shell in each direction, so 1/25 of the area.
    assert holed.area_sq_m == pytest.approx(solid.area_sq_m * 24 / 25, rel=0.01)


def test_geometry_collection_reports_both_families():
    collection = GeometryCollection(
        [
            Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)]),
            LineString([(77.70, 12.90), (77.71, 12.91)]),
        ]
    )

    measurement, _ = measure(collection)

    assert measurement.kind is MeasurementKind.MIXED
    assert measurement.area_sq_m and measurement.area_sq_m > 0
    assert measurement.length_m and measurement.length_m > 0


def test_collection_of_points_has_nothing_to_measure():
    measurement, _ = measure(GeometryCollection([Point(0, 0), Point(1, 1)]))

    assert measurement.kind is MeasurementKind.NONE
    assert measurement.measurable is True


def test_self_intersecting_polygon_is_repaired_and_reported():
    bowtie = Polygon([(77.0, 12.0), (77.01, 12.01), (77.01, 12.0), (77.0, 12.01)])
    assert not bowtie.is_valid

    measurement, warnings = measure(bowtie)

    assert measurement.measurable
    assert measurement.area_sq_m and measurement.area_sq_m > 0
    assert any("repaired" in warning for warning in warnings)


def test_missing_geometry_is_reported_not_raised():
    measurement, _ = measure(None)

    assert measurement.kind is MeasurementKind.UNSUPPORTED
    assert measurement.measurable is False
    assert "no geometry" in (measurement.reason or "").lower()


def test_missing_crs_is_reported_not_raised():
    measurement, _ = measure(Point(0, 0), crs=None)

    assert measurement.kind is MeasurementKind.UNSUPPORTED
    assert "no CRS" in (measurement.reason or "")


def test_empty_geometry_is_reported():
    measurement, _ = measure(Polygon())

    assert measurement.kind is MeasurementKind.NONE
    assert measurement.measurable is False
    assert "empty" in (measurement.reason or "").lower()


def test_three_dimensional_geometry_is_measured_in_plan_with_a_warning():
    flat = LineString([(77.0, 12.0), (77.01, 12.0)])
    sloped = LineString([(77.0, 12.0, 0.0), (77.01, 12.0, 500.0)])

    flat_measurement, _ = measure(flat)
    sloped_measurement, warnings = measure(sloped)

    assert sloped_measurement.length_m == pytest.approx(flat_measurement.length_m)
    assert any("Z ordinates were ignored" in warning for warning in warnings)


def test_geodesic_and_projected_strategies_agree_for_a_small_feature():
    parcel = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])

    projected, _ = measure(parcel, strategy=MeasurementStrategy.AUTO)
    geodesic, _ = measure(parcel, strategy=MeasurementStrategy.GEODESIC)

    assert geodesic.method == "geodesic"
    assert projected.area_sq_m == pytest.approx(geodesic.area_sq_m, rel=0.005)


def test_equal_area_fallback_matches_the_ellipsoid_once_edges_are_densified():
    """The azimuthal equal-area fallback is accurate for huge features.

    Compared on a densified polygon, so that both strategies are integrating
    the *same* boundary (see the test below for why that matters), the
    projected and ellipsoidal answers agree to better than 0.01%.
    """
    wide = shapely.segmentize(
        Polygon([(-60.0, -10.0), (20.0, -10.0), (20.0, 10.0), (-60.0, 10.0)]), 0.25
    )

    projected, _ = measure(wide, strategy=MeasurementStrategy.AUTO)
    geodesic, _ = measure(wide, strategy=MeasurementStrategy.GEODESIC)

    assert projected.method == "azimuthal"
    assert projected.area_sq_m == pytest.approx(geodesic.area_sq_m, rel=0.0001)


def test_long_edges_mean_the_two_strategies_answer_different_questions():
    """A documented limitation, pinned so it cannot change silently.

    A polygon with four vertices spanning 80 degrees of longitude does not
    define a unique region: the planar strategies join vertices by straight
    lines in the coordinate space, while the geodesic strategy follows great
    circles, which bow polewards. The areas therefore differ by ~14% - not
    because either is wrong, but because the source data is ambiguous. The fix
    belongs to the data: densify long edges (see the test above).
    """
    sparse = Polygon([(-60.0, -10.0), (20.0, -10.0), (20.0, 10.0), (-60.0, 10.0)])
    densified = shapely.segmentize(sparse, 0.25)

    sparse_projected, _ = measure(sparse, strategy=MeasurementStrategy.AUTO)
    sparse_geodesic, _ = measure(sparse, strategy=MeasurementStrategy.GEODESIC)
    densified_projected, _ = measure(densified, strategy=MeasurementStrategy.AUTO)

    assert sparse_geodesic.area_sq_m > sparse_projected.area_sq_m * 1.1
    # Densifying moves the planar answer only slightly; it is the geodesic
    # reading of sparse vertices that is the outlier.
    assert densified_projected.area_sq_m == pytest.approx(sparse_projected.area_sq_m, rel=0.05)


def test_feet_based_projected_crs_is_converted_to_square_metres():
    """A 1000 ft square is 92,903 m, not 1,000,000."""
    california = CRS.from_epsg(2229)  # US survey feet
    square = Polygon(
        [
            (6_500_000, 1_800_000),
            (6_501_000, 1_800_000),
            (6_501_000, 1_801_000),
            (6_500_000, 1_801_000),
        ]
    )

    measurement, _ = measure(square, california)

    assert measurement.area_sq_m == pytest.approx(92_903.4, rel=1e-4)
    assert measurement.perimeter_m == pytest.approx(4 * 1000 * 0.3048006096, rel=1e-6)
