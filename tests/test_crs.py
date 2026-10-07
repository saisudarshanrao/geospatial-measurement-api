"""CRS selection: the decisions that make the measurements trustworthy."""

from __future__ import annotations

import pytest
from pyproj import CRS
from shapely.geometry import LineString, Polygon

from app.errors import InvalidParameterError
from app.geo.crs import (
    WGS84,
    MeasurementStrategy,
    crs_identifier,
    linear_unit_to_metres,
    plan_measurement,
    reproject,
    resolve_crs,
)

SMALL_POLYGON = Polygon(
    [(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98), (77.59, 12.97)]
)


def test_strategy_parsing_accepts_known_values_and_rejects_others():
    assert MeasurementStrategy.parse("utm") is MeasurementStrategy.UTM
    assert MeasurementStrategy.parse("  GEODESIC ") is MeasurementStrategy.GEODESIC
    assert MeasurementStrategy.parse(None) is MeasurementStrategy.AUTO
    assert MeasurementStrategy.parse("", default=MeasurementStrategy.UTM) is MeasurementStrategy.UTM
    with pytest.raises(InvalidParameterError):
        MeasurementStrategy.parse("mercator")


def test_geographic_input_is_projected_to_the_containing_utm_zone():
    plan = plan_measurement(SMALL_POLYGON, WGS84, MeasurementStrategy.AUTO)

    assert plan.method == "utm"
    # 77.6E, 13N sits in UTM zone 43 north.
    assert crs_identifier(plan.crs) == "EPSG:32643"
    assert plan.unit_factor == pytest.approx(1.0)


def test_utm_zone_is_chosen_per_feature_not_per_file():
    """Two features in different zones each get their own projection."""
    india = plan_measurement(SMALL_POLYGON, WGS84, MeasurementStrategy.AUTO)
    peru = plan_measurement(
        Polygon([(-77.1, -12.1), (-77.0, -12.1), (-77.0, -12.0), (-77.1, -12.0)]),
        WGS84,
        MeasurementStrategy.AUTO,
    )

    assert crs_identifier(india.crs) == "EPSG:32643"
    assert crs_identifier(peru.crs) == "EPSG:32718"  # zone 18 south


def test_already_projected_input_is_measured_in_place():
    plan = plan_measurement(
        Polygon([(500000, 1400000), (500100, 1400000), (500100, 1400100), (500000, 1400100)]),
        CRS.from_epsg(32643),
        MeasurementStrategy.AUTO,
    )

    assert plan.method == "source_crs"
    assert crs_identifier(plan.crs) == "EPSG:32643"
    assert plan.unit_factor == pytest.approx(1.0)
    assert "already projected" in (plan.note or "")


def test_projected_input_in_feet_reports_a_unit_conversion_factor():
    """EPSG:2229 is in US survey feet, so measurements need scaling to metres."""
    california = CRS.from_epsg(2229)
    assert linear_unit_to_metres(california) == pytest.approx(0.3048006096, rel=1e-9)

    plan = plan_measurement(
        Polygon([(6_500_000, 1_800_000), (6_501_000, 1_800_000), (6_501_000, 1_801_000)]),
        california,
        MeasurementStrategy.AUTO,
    )
    assert plan.method == "source_crs"
    assert plan.unit_factor == pytest.approx(0.3048006096, rel=1e-9)


def test_feature_too_wide_for_a_utm_zone_falls_back_to_an_azimuthal_projection():
    wide = Polygon([(-60.0, -10.0), (20.0, -10.0), (20.0, 10.0), (-60.0, 10.0)])

    plan = plan_measurement(wide, WGS84, MeasurementStrategy.AUTO, equal_area=True)

    assert plan.method == "azimuthal"
    # crs_label carries the PROJ definition we built, so describing the CRS
    # never needs a lossy (and warning-raising) to_proj4() export.
    assert "+proj=laea" in (plan.crs_label or "")
    assert "wider than a UTM zone" in (plan.note or "")


def test_azimuthal_fallback_uses_an_equidistant_projection_for_lengths():
    long_line = LineString([(-60.0, 0.0), (20.0, 5.0)])

    plan = plan_measurement(long_line, WGS84, MeasurementStrategy.AUTO, equal_area=False)

    assert plan.method == "azimuthal"
    assert "+proj=aeqd" in (plan.crs_label or "")


def test_polar_feature_falls_back_because_utm_does_not_reach_it():
    polar = Polygon([(10.0, 86.0), (11.0, 86.0), (11.0, 86.5), (10.0, 86.5)])

    plan = plan_measurement(polar, WGS84, MeasurementStrategy.AUTO)

    assert plan.method == "azimuthal"
    assert "latitude range" in (plan.note or "")


def test_geodesic_strategy_skips_projection_entirely():
    plan = plan_measurement(SMALL_POLYGON, WGS84, MeasurementStrategy.GEODESIC)

    assert plan.is_geodesic
    assert plan.crs is None
    assert plan.method == "geodesic"


def test_utm_strategy_reprojects_even_when_the_source_is_projected():
    plan = plan_measurement(
        Polygon([(500000, 1400000), (500100, 1400000), (500100, 1400100)]),
        CRS.from_epsg(32643),
        MeasurementStrategy.UTM,
    )

    assert plan.method == "utm"


def test_non_wgs84_geographic_source_still_resolves_a_utm_zone():
    """NAD83 is geographic but not WGS 84; zone selection must still work."""
    nad83 = CRS.from_epsg(4269)
    parcel = Polygon([(-122.4, 37.7), (-122.3, 37.7), (-122.3, 37.8), (-122.4, 37.8)])

    plan = plan_measurement(parcel, nad83, MeasurementStrategy.AUTO)

    assert plan.method == "utm"
    assert crs_identifier(plan.crs) == "EPSG:32610"


def test_antimeridian_crossing_does_not_look_like_a_global_feature():
    """Longitudes are unwrapped, so a narrow feature at 180 stays narrow."""
    crossing = Polygon([(179.9, -16.5), (-179.9, -16.5), (-179.9, -16.4), (179.9, -16.4)])

    plan = plan_measurement(crossing, WGS84, MeasurementStrategy.AUTO)

    # No single UTM zone spans the antimeridian, so the azimuthal fallback is
    # used - but centred on the antimeridian, not back at Greenwich.
    assert plan.method == "azimuthal"
    centre_lon = float(
        next(
            part.split("=")[1]
            for part in (plan.crs_label or "").split()
            if part.startswith("+lon_0=")
        )
    )
    assert abs(abs(centre_lon) - 180.0) < 0.001


def test_resolve_crs_accepts_epsg_codes_wkt_and_crs_objects():
    assert crs_identifier(resolve_crs(4326)) == "EPSG:4326"
    assert crs_identifier(resolve_crs("EPSG:32643")) == "EPSG:32643"
    assert crs_identifier(resolve_crs(WGS84.to_wkt())) == "EPSG:4326"
    assert resolve_crs(None) is None
    assert resolve_crs("   ") is None


def test_reprojection_is_a_no_op_for_identical_crs():
    same = reproject(SMALL_POLYGON, WGS84, WGS84)
    assert same is SMALL_POLYGON


def test_reprojection_round_trips_within_a_millimetre():
    utm = CRS.from_epsg(32643)
    there = reproject(SMALL_POLYGON, WGS84, utm)
    back = reproject(there, utm, WGS84)

    assert back.equals_exact(SMALL_POLYGON, tolerance=1e-9)
