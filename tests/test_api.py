"""End-to-end tests through the HTTP layer."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from samples import builders

# ------------------------------------------------------------------- happy path


def test_upload_kml_returns_file_information(upload):
    body = upload(builders.sample_kml(), "survey.kml")

    assert body["filename"] == "survey.kml"
    assert body["format"] == "KML"
    assert body["crs"] == "EPSG:4326"
    assert body["crs_name"] == "WGS 84"
    assert body["crs_source"] == "kml_specification"
    assert body["feature_count"] == 6
    # Two placemarks in the sample have no usable geometry, which is reported
    # rather than fatal.
    assert body["status"] == "COMPLETED_WITH_ERRORS"
    assert body["summary"]["error_count"] == 2
    assert body["summary"]["measurable_count"] == 3  # polygon, line, collection
    assert body["processing_time_ms"] >= 0
    assert body["id"]


def test_upload_shapefile_zip_returns_file_information(upload):
    body = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip")

    assert body["format"] == "SHAPEFILE"
    assert body["crs"] == "EPSG:4326"
    assert body["crs_source"] == "prj"
    assert body["feature_count"] == 2
    assert body["status"] == "COMPLETED"
    assert body["layers"] == ["parcels"]
    assert body["summary"]["geometry_type_counts"] == {"Polygon": 2}


def test_upload_kmz(upload):
    body = upload(builders.sample_kmz(), "survey.kmz")

    assert body["format"] == "KMZ"
    assert body["feature_count"] == 6


def test_get_file_matches_the_upload_response(client: TestClient, upload):
    created = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip")

    fetched = client.get(f"/api/files/{created['id']}/")

    assert fetched.status_code == 200
    assert fetched.json() == created


def test_measurements_report_area_in_square_metres(client: TestClient, upload):
    created = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip")

    response = client.get(f"/api/files/{created['id']}/measurements/")

    assert response.status_code == 200
    body = response.json()
    assert body["units"] == {"area": "square_metre", "length": "metre"}
    assert body["measurement_strategy"] == "auto"
    assert body["page"]["total"] == 2

    first = body["measurements"][0]
    assert first["feature_index"] == 0
    assert first["geometry_type"] == "Polygon"
    assert first["source_crs"] == "EPSG:4326"
    measurement = first["measurement"]
    assert measurement["kind"] == "AREA"
    assert measurement["measurable"] is True
    # A 0.01 x 0.01 degree block near 13N is roughly 1.2 km.
    assert 1_100_000 < measurement["area_sq_m"] < 1_300_000
    assert measurement["measurement_crs"] == "EPSG:32643"
    assert measurement["method"] == "utm"
    assert measurement["length_m"] is None
    assert measurement["area_hectares"] == pytest.approx(measurement["area_sq_m"] / 1e4)


def test_projected_source_file_is_measured_exactly(client: TestClient, upload):
    """A 250 m square in EPSG:32643 must come back as 62,500 m."""
    created = upload(builders.sample_polygon_shapefile_utm(side_metres=250.0), "square.zip")

    body = client.get(f"/api/files/{created['id']}/measurements/").json()
    measurement = body["measurements"][0]["measurement"]

    assert measurement["area_sq_m"] == pytest.approx(62_500.0)
    assert measurement["perimeter_m"] == pytest.approx(1_000.0)
    assert measurement["method"] == "source_crs"
    assert measurement["measurement_crs"] == "EPSG:32643"


def test_linestring_file_reports_length_only(client: TestClient, upload):
    created = upload(builders.sample_line_shapefile_4326(), "lines.zip")

    measurement = client.get(f"/api/files/{created['id']}/measurements/").json()["measurements"][0][
        "measurement"
    ]

    assert measurement["kind"] == "LENGTH"
    # 0.1 degree of longitude at the equator is about 11.1 km.
    assert 11_000 < measurement["length_m"] < 11_200
    assert measurement["area_sq_m"] is None


def test_points_are_reported_as_having_no_measurement(client: TestClient, upload):
    created = upload(builders.sample_point_shapefile_no_prj(), "cities.zip")

    body = client.get(f"/api/files/{created['id']}/measurements/").json()

    assert body["summary"]["measurable_count"] == 0
    assert body["summary"]["error_count"] == 0
    for item in body["measurements"]:
        measurement = item["measurement"]
        assert measurement["kind"] == "NONE"
        assert measurement["measurable"] is True
        assert "No measurement is defined" in measurement["reason"]


def test_unreadable_features_are_reported_without_failing_the_file(client: TestClient, upload):
    created = upload(builders.sample_kml(), "survey.kml")

    body = client.get(f"/api/files/{created['id']}/measurements/").json()

    broken = [item for item in body["measurements"] if item["error"]]
    assert len(broken) == 2
    assert body["status"] == "COMPLETED_WITH_ERRORS"
    # The good features still measured.
    assert body["summary"]["total_area_sq_m"] > 0


def test_summary_totals_add_up(client: TestClient, upload):
    created = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip")

    body = client.get(f"/api/files/{created['id']}/measurements/").json()
    areas = [item["measurement"]["area_sq_m"] for item in body["measurements"]]

    assert body["summary"]["total_area_sq_m"] == pytest.approx(sum(areas), rel=1e-9)
    assert body["summary"]["total_area_hectares"] == pytest.approx(sum(areas) / 1e4, rel=1e-9)


# ----------------------------------------------------------------- CRS strategy


def test_geodesic_strategy_can_be_requested_at_upload_time(client: TestClient, upload):
    created = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip", strategy="geodesic")

    assert created["measurement_strategy"] == "geodesic"
    measurement = client.get(f"/api/files/{created['id']}/measurements/").json()["measurements"][0][
        "measurement"
    ]
    assert measurement["method"] == "geodesic"


def test_measurements_can_be_recomputed_with_a_different_strategy(client: TestClient, upload):
    """The stored geometries are reused, so no re-upload is needed."""
    created = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip")

    projected = client.get(f"/api/files/{created['id']}/measurements/").json()
    geodesic = client.get(
        f"/api/files/{created['id']}/measurements/", params={"strategy": "geodesic"}
    ).json()

    assert projected["measurement_strategy"] == "auto"
    assert geodesic["measurement_strategy"] == "geodesic"
    assert geodesic["measurements"][0]["measurement"]["method"] == "geodesic"
    # Same parcel, so the two answers must agree to well under a percent.
    assert projected["measurements"][0]["measurement"]["area_sq_m"] == pytest.approx(
        geodesic["measurements"][0]["measurement"]["area_sq_m"], rel=0.005
    )
    # Recomputing does not overwrite how the file was processed.
    assert client.get(f"/api/files/{created['id']}/").json()["measurement_strategy"] == "auto"


def test_reading_measurements_without_a_strategy_honours_how_the_file_was_processed(
    client: TestClient, upload
):
    """Regression: an omitted ?strategy must not re-measure with the server default."""
    created = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip", strategy="geodesic")

    body = client.get(f"/api/files/{created['id']}/measurements/").json()

    assert body["measurement_strategy"] == "geodesic"
    assert body["measurements"][0]["measurement"]["method"] == "geodesic"


def test_wide_feature_falls_back_to_an_azimuthal_projection(client: TestClient, upload):
    created = upload(builders.sample_kml_large_extent(), "wide.kml")

    measurement = client.get(f"/api/files/{created['id']}/measurements/").json()["measurements"][0][
        "measurement"
    ]

    assert measurement["method"] == "azimuthal"
    assert "+proj=laea" in measurement["measurement_crs"]
    assert measurement["measurable"] is True


def test_unknown_strategy_is_a_400(client: TestClient, upload):
    created = upload(builders.sample_kml(), "survey.kml")

    response = client.get(
        f"/api/files/{created['id']}/measurements/", params={"strategy": "mercator"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"


# -------------------------------------------------------------- other endpoints


def test_features_endpoint_returns_geometry_crs_and_properties(client: TestClient, upload):
    created = upload(builders.sample_kml(), "survey.kml")

    body = client.get(f"/api/files/{created['id']}/features/").json()

    first = body["features"][0]
    assert first["feature_index"] == 0
    assert first["feature_id"] == "block-a"
    assert first["geometry_type"] == "Polygon"
    assert first["source_crs"] == "EPSG:4326"
    assert first["geometry"]["type"] == "Polygon"
    assert first["properties"]["owner"] == "City"
    assert first["measurement"]["kind"] == "AREA"


def test_geometry_can_be_omitted_from_the_features_response(client: TestClient, upload):
    created = upload(builders.sample_kml(), "survey.kml")

    body = client.get(
        f"/api/files/{created['id']}/features/", params={"include_geometry": "false"}
    ).json()

    assert body["features"][0]["geometry"] is None
    assert body["features"][0]["properties"] is not None


def test_measurements_can_include_geometry_and_properties(client: TestClient, upload):
    created = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip")

    body = client.get(
        f"/api/files/{created['id']}/measurements/",
        params={"include_geometry": "true", "include_properties": "true"},
    ).json()

    assert body["measurements"][0]["geometry"]["type"] == "Polygon"
    assert body["measurements"][0]["properties"]["name"] == "North block"


def test_geojson_endpoint_returns_a_feature_collection(client: TestClient, upload):
    created = upload(builders.sample_polygon_shapefile_4326(), "parcels.zip")

    body = client.get(f"/api/files/{created['id']}/geojson/").json()

    assert body["type"] == "FeatureCollection"
    assert body["crs"] == "EPSG:4326"
    assert len(body["features"]) == 2
    assert body["features"][0]["geometry"]["type"] == "Polygon"
    assert body["features"][0]["properties"]["_measurement"]["area_sq_m"] > 0


def test_files_can_be_listed_newest_first(client: TestClient, upload):
    first = upload(builders.sample_kml(), "first.kml")
    second = upload(builders.sample_polygon_shapefile_4326(), "second.zip")

    body = client.get("/api/files/").json()

    assert body["page"]["total"] == 2
    assert [item["id"] for item in body["files"]] == [second["id"], first["id"]]


def test_pagination_windows_the_measurements(client: TestClient, upload):
    created = upload(builders.sample_kml(), "survey.kml")

    body = client.get(
        f"/api/files/{created['id']}/measurements/", params={"limit": 2, "offset": 1}
    ).json()

    assert body["page"] == {"total": 6, "count": 2, "limit": 2, "offset": 1}
    assert [item["feature_index"] for item in body["measurements"]] == [1, 2]


def test_limit_above_the_maximum_is_rejected(client: TestClient, upload):
    created = upload(builders.sample_kml(), "survey.kml")

    response = client.get(f"/api/files/{created['id']}/measurements/", params={"limit": 10_000})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_delete_removes_the_file_and_its_stored_bytes(client: TestClient, upload, settings):
    created = upload(builders.sample_kml(), "survey.kml")
    stored = Path(settings.storage_dir) / created["id"]
    assert stored.exists()

    response = client.delete(f"/api/files/{created['id']}/")

    assert response.status_code == 200
    assert response.json() == {"id": created["id"], "deleted": True}
    assert client.get(f"/api/files/{created['id']}/").status_code == 404
    assert not stored.exists()


def test_health_advertises_capabilities(client: TestClient):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert ".kml" in body["supported_uploads"]
    assert "Polygon" in body["supported_geometry_types"]
    assert body["measurement_strategies"] == ["auto", "utm", "geodesic"]


def test_openapi_document_is_generated(client: TestClient):
    body = client.get("/openapi.json").json()

    assert body["info"]["title"] == "Geospatial File Measurement API"
    assert "/api/files/" in body["paths"]
    assert "/api/files/{file_id}/measurements/" in body["paths"]


def test_request_id_is_echoed(client: TestClient):
    response = client.get("/health", headers={"X-Request-ID": "trace-me"})

    assert response.headers["X-Request-ID"] == "trace-me"


# ----------------------------------------------------------------- error paths


def test_unknown_file_id_is_404(client: TestClient):
    response = client.get("/api/files/does-not-exist/")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unsupported_file_type_is_400(client: TestClient):
    response = client.post(
        "/api/files/", files={"file": ("image.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_file_type"


def test_bare_shp_upload_explains_what_is_missing(client: TestClient):
    response = client.post(
        "/api/files/", files={"file": ("parcels.shp", b"\x00\x00\x27\x0a" + b"\x00" * 60)}
    )

    assert response.status_code == 400
    assert "whole shapefile set" in response.json()["error"]["message"]


def test_empty_upload_is_400(client: TestClient):
    response = client.post("/api/files/", files={"file": ("empty.kml", b"")})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_upload"


def test_corrupt_kml_is_422(client: TestClient):
    response = client.post(
        "/api/files/", files={"file": ("broken.kml", b"<kml><Document>" + b"\x00\x01\x02")}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_geospatial_file"


def test_upload_above_the_size_limit_is_413(client: TestClient, settings):
    oversized = b"<kml>" + b"x" * (settings.max_upload_bytes + 1)

    response = client.post("/api/files/", files={"file": ("huge.kml", oversized)})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


def test_zip_without_geospatial_content_is_400(client: TestClient):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "nothing here")

    response = client.post("/api/files/", files={"file": ("data.zip", buffer.getvalue())})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_file_type"


def test_missing_file_field_is_a_validation_error(client: TestClient):
    response = client.post("/api/files/", data={"nothing": "here"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_failed_upload_is_not_stored(client: TestClient):
    """A rejected upload must not leave a record behind."""
    client.post("/api/files/", files={"file": ("image.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)})

    assert client.get("/api/files/").json()["page"]["total"] == 0
