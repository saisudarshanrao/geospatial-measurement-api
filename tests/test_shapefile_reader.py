"""Shapefile reading and the safety limits on the archive that carries it."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import shapefile

from app.errors import InvalidGeospatialFileError, UnsupportedFileError
from app.geo.archive import extract_archive
from app.geo.readers.shapefile_reader import read_shapefile_zip
from samples import builders


def read(payload: bytes, tmp_path: Path, **limits):
    extracted = extract_archive(
        payload,
        tmp_path / "extracted",
        max_entries=limits.get("max_entries", 100),
        max_uncompressed_bytes=limits.get("max_uncompressed_bytes", 8 * 1024 * 1024),
    )
    return read_shapefile_zip(extracted.root, extracted.members)


def test_reads_polygons_with_attributes_and_crs_from_the_prj(tmp_path: Path):
    result = read(builders.sample_polygon_shapefile_4326(), tmp_path)

    assert result.crs.to_epsg() == 4326
    assert result.crs_source == "prj"
    assert result.layers == ["parcels"]
    assert len(result.features) == 2

    first = result.features[0]
    assert first.geometry.geom_type == "Polygon"
    assert first.properties == {"name": "North block", "parcel_id": 1}
    # No exact id/fid/objectid field here, so "name" is the last-resort
    # candidate. Matching is exact, so "parcel_id" is deliberately not treated
    # as an identifier - substring matching would misfire on real-world schemas.
    assert first.feature_id == "North block"
    assert first.layer == "parcels"


def test_an_explicit_id_field_outranks_a_name_field(tmp_path: Path):
    payload = builders.build_shapefile_zip(
        shape_type=shapefile.POINT,
        fields=[("name", "C", 40), ("id", "C", 10)],
        records=[((10.0, 20.0), ["Labelled", "P-42"])],
        layer_name="points",
    )

    assert read(payload, tmp_path).features[0].feature_id == "P-42"


def test_projected_prj_is_recognised(tmp_path: Path):
    result = read(builders.sample_polygon_shapefile_utm(), tmp_path)

    assert result.crs.is_projected
    assert result.crs.to_epsg() == 32643


def test_missing_prj_assumes_wgs84_and_says_so(tmp_path: Path):
    result = read(builders.sample_point_shapefile_no_prj(), tmp_path)

    assert result.crs.to_epsg() == 4326
    assert result.crs_source == "assumed_default"
    assert any("no .prj" in warning for warning in result.warnings)
    assert len(result.features) == 2


def test_polylines_are_read(tmp_path: Path):
    result = read(builders.sample_line_shapefile_4326(), tmp_path)

    assert result.features[0].geometry.geom_type == "LineString"


def test_null_shape_is_a_feature_without_geometry(tmp_path: Path):
    payload = builders.build_shapefile_zip(
        shape_type=shapefile.POLYGON,
        fields=[("name", "C", 40)],
        records=[
            ([builders.square_ring(77.5, 12.9, 0.01, 0.01)], ["Real"]),
            (None, ["Missing geometry"]),
        ],
        layer_name="mixed",
    )

    result = read(payload, tmp_path)

    assert result.features[0].geometry is not None
    assert result.features[1].geometry is None
    assert any("NULL shape" in warning for warning in result.features[1].warnings)


def test_several_shapefiles_in_one_archive_are_all_read(tmp_path: Path):
    """Each set keeps its own layer name and its own CRS."""
    geographic = builders.sample_polygon_shapefile_4326()
    projected = builders.sample_polygon_shapefile_utm()

    merged = io.BytesIO()
    with zipfile.ZipFile(merged, "w") as archive:
        for source, folder in ((geographic, "a"), (projected, "b")):
            with zipfile.ZipFile(io.BytesIO(source)) as inner:
                for name in inner.namelist():
                    archive.writestr(f"{folder}/{name}", inner.read(name))

    result = read(merged.getvalue(), tmp_path)

    assert sorted(result.layers) == ["parcels", "square_utm"]
    assert len(result.features) == 3
    crs_by_layer = {feature.layer: feature.crs.to_epsg() for feature in result.features}
    assert crs_by_layer == {"parcels": 4326, "square_utm": 32643}
    assert any("contains 2 shapefiles" in warning for warning in result.warnings)


def test_archive_without_a_shp_is_rejected(tmp_path: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "nothing geospatial here")

    with pytest.raises(InvalidGeospatialFileError, match=r"no \.shp file"):
        read(buffer.getvalue(), tmp_path)


def test_missing_shx_and_dbf_are_warned_about(tmp_path: Path):
    payload = builders.build_shapefile_zip(
        shape_type=shapefile.POLYGON,
        fields=[("name", "C", 40)],
        records=[([builders.square_ring(77.5, 12.9, 0.01, 0.01)], ["Lonely"])],
        layer_name="partial",
        omit=[".dbf"],
    )

    result = read(payload, tmp_path)

    assert any(".dbf" in warning for warning in result.warnings)


def test_cpg_encoding_is_honoured(tmp_path: Path):
    """A latin-1 DBF declared via .cpg must not come back as mojibake."""
    payload = builders.build_shapefile_zip(
        shape_type=shapefile.POINT,
        fields=[("name", "C", 40)],
        records=[((10.0, 20.0), ["Köln"])],
        layer_name="cities",
        extra_members={"cities.cpg": b"UTF-8"},
    )

    result = read(payload, tmp_path)

    assert result.features[0].properties["name"] == "Köln"


# --------------------------------------------------------------- archive safety


def test_path_traversal_member_is_refused(tmp_path: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../escaped.shp", b"nope")

    with pytest.raises(InvalidGeospatialFileError, match="unsafe member path"):
        read(buffer.getvalue(), tmp_path)


def test_absolute_member_path_is_refused(tmp_path: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("/etc/cron.d/evil", b"nope")

    with pytest.raises(InvalidGeospatialFileError, match="unsafe member path"):
        read(buffer.getvalue(), tmp_path)


def test_zip_bomb_is_refused_before_it_expands(tmp_path: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.shp", b"\0" * (4 * 1024 * 1024))

    with pytest.raises(InvalidGeospatialFileError, match="expands to more than"):
        read(buffer.getvalue(), tmp_path, max_uncompressed_bytes=1024)


def test_too_many_members_is_refused(tmp_path: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(20):
            archive.writestr(f"file{index}.txt", b"x")

    with pytest.raises(InvalidGeospatialFileError, match="more than the"):
        read(buffer.getvalue(), tmp_path, max_entries=5)


def test_empty_archive_is_refused(tmp_path: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass

    with pytest.raises(InvalidGeospatialFileError, match="no files"):
        read(buffer.getvalue(), tmp_path)


def test_non_zip_payload_is_refused(tmp_path: Path):
    with pytest.raises(UnsupportedFileError, match="not a readable zip"):
        read(b"definitely not a zip", tmp_path)
