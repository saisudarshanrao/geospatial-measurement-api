"""Format detection leads with content, not the filename."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.errors import EmptyUploadError, UnsupportedFileError
from app.geo.detect import detect_format
from app.models import SourceFormat
from samples import builders


def test_shapefile_archive_is_detected():
    detected = detect_format(builders.sample_polygon_shapefile_4326(), "parcels.zip")

    assert detected.source_format is SourceFormat.SHAPEFILE
    assert any(name.endswith(".shp") for name in detected.archive_names or [])


def test_kml_is_detected():
    assert detect_format(builders.sample_kml(), "survey.kml").source_format is SourceFormat.KML


def test_kmz_is_detected_from_its_contents_not_its_name():
    """A zipped KML uploaded as .zip is still a KMZ."""
    detected = detect_format(builders.sample_kmz(), "survey.zip")

    assert detected.source_format is SourceFormat.KMZ
    assert "read as KMZ" in (detected.note or "")


def test_kml_with_a_misleading_extension_is_still_kml():
    assert detect_format(builders.sample_kml(), "survey.txt").source_format is SourceFormat.KML


def test_kml_with_a_leading_byte_order_mark_is_detected():
    payload = b"\xef\xbb\xbf" + builders.sample_kml()

    assert detect_format(payload, "survey.kml").source_format is SourceFormat.KML


def test_empty_upload_is_rejected():
    with pytest.raises(EmptyUploadError):
        detect_format(b"", "empty.kml")


def test_bare_shp_gets_an_explanatory_error():
    payload = b"\x00\x00\x27\x0a" + b"\x00" * 100

    with pytest.raises(UnsupportedFileError, match="whole shapefile set"):
        detect_format(payload, "parcels.shp")


def test_archive_with_neither_shp_nor_kml_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data.csv", "a,b\n1,2\n")

    with pytest.raises(UnsupportedFileError, match=r"neither a \.shp nor a \.kml"):
        detect_format(buffer.getvalue(), "data.zip")


def test_non_kml_xml_is_rejected_with_a_clear_message():
    payload = b"<?xml version='1.0'?><gpx><trk><name>A walk</name></trk></gpx>"

    with pytest.raises(UnsupportedFileError, match="does not look like KML"):
        detect_format(payload, "walk.gpx")


def test_arbitrary_binary_is_rejected():
    with pytest.raises(UnsupportedFileError, match="Unsupported file type"):
        detect_format(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "image.png")
