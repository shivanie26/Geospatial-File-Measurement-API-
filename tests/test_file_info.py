"""GET /api/files/{id}/ - file metadata and processing counters."""
from __future__ import annotations

from tests import factories as f
from tests.helpers import assert_error, upload_ok

EXPECTED_KEYS = {
    "id", "filename", "file_type", "upload_timestamp", "feature_count",
    "successful_feature_count", "failed_feature_count", "unsupported_feature_count",
    "crs", "status", "warnings",
}


def test_file_info_matches_upload_response(client):
    uploaded = upload_ok(client, "survey.kml", f.kml_document(
        f.kml_placemark("a", f.kml_polygon(f.lonlat_rect(100, 100))),
        f.kml_placemark("b", f.kml_point(77.7, 11.3)),
    ))
    response = client.get(f"/api/files/{uploaded['id']}/")

    assert response.status_code == 200
    assert response.json() == uploaded
    assert set(response.json()) == EXPECTED_KEYS


def test_counts_and_status_for_a_partially_failed_file(client):
    bowtie = [(77.70, 11.30), (77.71, 11.31), (77.71, 11.30), (77.70, 11.31), (77.70, 11.30)]
    content = f.kml_document(
        f.kml_placemark("good", f.kml_polygon(f.lonlat_rect(100, 100))),
        f.kml_placemark("bad", f.kml_polygon(bowtie)),
        f.kml_placemark("none", None),
    )
    info = client.get(f"/api/files/{upload_ok(client, 'mix.kml', content)['id']}/").json()

    assert info["feature_count"] == 3
    assert info["successful_feature_count"] == 1
    assert info["failed_feature_count"] == 2
    assert info["unsupported_feature_count"] == 0
    assert info["status"] == "PARTIAL"


def test_file_without_crs_reports_null_crs_warning_and_failed_status(client):
    archive = f.shapefile_zip(f.polygon_frame(crs=None))
    info = client.get(f"/api/files/{upload_ok(client, 'nocrs.zip', archive)['id']}/").json()

    assert info["crs"] is None
    assert info["status"] == "FAILED"
    assert info["failed_feature_count"] == 1
    assert any("CRS" in warning for warning in info["warnings"])


def test_unknown_id_returns_404(client):
    error = assert_error(client.get("/api/files/0123456789abcdef0123456789abcdef/"), 404, "FILE_NOT_FOUND")
    assert "not found" in error["message"].lower()


def test_malformed_ids_return_404_not_500(client):
    for bad_id in ("not-an-id", "..%2F..%2Fetc%2Fpasswd", "0123", "G" * 32):
        assert client.get(f"/api/files/{bad_id}/").status_code == 404
        assert client.get(f"/api/files/{bad_id}/measurements/").status_code == 404
