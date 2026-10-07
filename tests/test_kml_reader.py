"""KML parsing: namespaces, geometry shapes, attributes and XML safety."""

from __future__ import annotations

import pytest

from app.errors import InvalidGeospatialFileError
from app.geo.readers.kml_reader import read_kml, read_kmz
from samples import builders


def test_reads_every_geometry_kind_in_the_sample_document():
    result = read_kml(builders.sample_kml())

    assert result.crs.to_epsg() == 4326
    assert result.crs_source == "kml_specification"

    kinds = [
        feature.geometry.geom_type if feature.geometry else None for feature in result.features
    ]
    assert kinds == ["Polygon", "LineString", "Point", "GeometryCollection", None, None]


def test_polygon_inner_boundary_becomes_a_hole():
    result = read_kml(builders.sample_kml())
    polygon = result.features[0].geometry

    assert polygon.geom_type == "Polygon"
    assert len(polygon.interiors) == 1


def test_placemark_name_and_extended_data_become_properties():
    feature = read_kml(builders.sample_kml()).features[0]

    assert feature.properties["name"] == "Block A"
    assert feature.properties["owner"] == "City"
    assert feature.properties["survey_no"] == "12/3"
    assert feature.feature_id == "block-a"


def test_folder_names_are_recorded_as_the_layer():
    combo = read_kml(builders.sample_kml()).features[3]

    assert combo.layer == "Sample survey/Mixed"
    assert combo.geometry.geom_type == "GeometryCollection"


def test_placemark_without_geometry_is_reported_as_a_feature_error():
    feature = read_kml(builders.sample_kml()).features[4]

    assert feature.geometry is None
    assert feature.error == "Placemark has no geometry element."
    # The rest of the document still parsed.
    assert feature.properties["name"] == "No geometry here"


def test_model_placemark_is_reported_as_having_no_measurable_geometry():
    feature = read_kml(builders.sample_kml()).features[5]

    assert feature.geometry is None
    assert "Model" in (feature.error or "")


def test_namespaceless_kml_parses():
    """Plenty of real files omit the namespace declaration entirely."""
    payload = b"""<kml><Document><Placemark><name>Bare</name>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>
    0,0 0,1 1,1 1,0 0,0</coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark></Document></kml>"""

    result = read_kml(payload)

    assert len(result.features) == 1
    assert result.features[0].geometry.geom_type == "Polygon"


def test_legacy_google_namespace_parses():
    payload = b"""<kml xmlns="http://earth.google.com/kml/2.1"><Document><Placemark>
    <Point><coordinates>10,20</coordinates></Point></Placemark></Document></kml>"""

    result = read_kml(payload)

    assert result.features[0].geometry.geom_type == "Point"


def test_altitude_is_dropped_so_geometry_is_two_dimensional():
    payload = b"""<kml><Document><Placemark><LineString><coordinates>
    77.0,12.0,100 77.1,12.0,250</coordinates></LineString></Placemark></Document></kml>"""

    geometry = read_kml(payload).features[0].geometry

    assert not geometry.has_z
    assert list(geometry.coords) == [(77.0, 12.0), (77.1, 12.0)]


def test_multigeometry_of_polygons_becomes_a_multipolygon():
    payload = b"""<kml><Document><Placemark><MultiGeometry>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 0,1 1,1 0,0
    </coordinates></LinearRing></outerBoundaryIs></Polygon>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>5,5 5,6 6,6 5,5
    </coordinates></LinearRing></outerBoundaryIs></Polygon>
    </MultiGeometry></Placemark></Document></kml>"""

    geometry = read_kml(payload).features[0].geometry

    assert geometry.geom_type == "MultiPolygon"
    assert len(geometry.geoms) == 2


def test_gx_track_is_read_as_a_line():
    payload = b"""<kml xmlns="http://www.opengis.net/kml/2.2"
        xmlns:gx="http://www.google.com/kml/ext/2.2"><Document><Placemark>
        <gx:Track><gx:coord>77.0 12.0 0</gx:coord><gx:coord>77.1 12.0 0</gx:coord>
        </gx:Track></Placemark></Document></kml>"""

    geometry = read_kml(payload).features[0].geometry

    assert geometry.geom_type == "LineString"


def test_degenerate_ring_is_a_feature_error_not_an_exception():
    payload = b"""<kml><Document><Placemark><name>Too few points</name>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 1,1
    </coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>"""

    feature = read_kml(payload).features[0]

    assert feature.geometry is None
    assert "at least 4 coordinates" in (feature.error or "")


def test_empty_kml_is_not_a_client_error():
    result = read_kml(b"<kml><Document><name>Nothing here</name></Document></kml>")

    assert result.features == []
    assert any("no Placemark" in warning for warning in result.warnings)


def test_non_xml_payload_is_rejected():
    with pytest.raises(InvalidGeospatialFileError):
        read_kml(b"this is not xml at all")


def test_malformed_but_recoverable_kml_parses_with_a_warning():
    payload = b"""<kml><Document><Placemark><name>Unclosed
    <Point><coordinates>5,5</coordinates></Point></Placemark></Document></kml>"""

    result = read_kml(payload)

    assert any("recovery mode" in warning for warning in result.warnings)


def test_external_entities_are_not_resolved():
    """An uploaded KML must not be able to read files off the server (XXE)."""
    payload = b"""<?xml version="1.0"?>
    <!DOCTYPE kml [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
    <kml><Document><Placemark><name>&secret;</name>
    <Point><coordinates>1,2</coordinates></Point></Placemark></Document></kml>"""

    try:
        result = read_kml(payload)
    except InvalidGeospatialFileError:
        return  # refusing the document outright is also a correct outcome

    names = [feature.properties.get("name", "") for feature in result.features]
    assert not any("root:" in str(name) for name in names)


def test_billion_laughs_does_not_expand():
    """Entity expansion must not be used to exhaust memory."""
    payload = (
        b'<?xml version="1.0"?><!DOCTYPE kml ['
        b'<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        b"]><kml><Document><Placemark><name>&c;</name>"
        b"<Point><coordinates>1,2</coordinates></Point></Placemark></Document></kml>"
    )

    try:
        result = read_kml(payload)
    except InvalidGeospatialFileError:
        return

    for feature in result.features:
        assert len(str(feature.properties.get("name", ""))) < 1000


def test_kmz_reads_the_inner_kml_document():
    result = read_kmz(builders.sample_kmz(), max_bytes=10 * 1024 * 1024)

    assert len(result.features) == 6
    assert result.crs.to_epsg() == 4326


def test_kmz_without_a_kml_member_is_rejected():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "no kml here")

    with pytest.raises(InvalidGeospatialFileError):
        read_kmz(buffer.getvalue(), max_bytes=1024)
