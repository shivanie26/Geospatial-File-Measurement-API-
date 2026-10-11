"""Rejection of unsupported, empty, corrupted and incomplete uploads."""
from __future__ import annotations

import pytest

from tests import factories as f
from tests.helpers import assert_error, upload


GOOD_KML = f.kml_document(f.kml_placemark("a", f.kml_point(77.7, 11.3)))


def good_parts() -> dict[str, bytes]:
    return f.shapefile_parts(f.polygon_frame())


@pytest.mark.parametrize("filename", ["notes.txt", "data.geojson", "points.csv", "noextension", "layer.shp", "image.png"])
def test_unsupported_extensions_return_415(client, filename):
    assert_error(upload(client, filename, b"some content"), 415, "UNSUPPORTED_FILE_TYPE")


def test_valid_kml_content_with_wrong_extension_is_still_rejected(client):
    assert_error(upload(client, "survey.txt", GOOD_KML), 415, "UNSUPPORTED_FILE_TYPE")


@pytest.mark.parametrize("filename", ["empty.kml", "empty.zip"])
def test_empty_files_return_400(client, filename):
    assert_error(upload(client, filename, b""), 400, "EMPTY_FILE")


def test_truncated_kml_is_reported_as_corrupted(client):
    assert_error(upload(client, "cut.kml", GOOD_KML[:80]), 400, "INVALID_KML")


def test_text_that_is_not_xml_is_rejected_as_kml(client):
    assert_error(upload(client, "fake.kml", b"this is plainly not xml"), 400, "INVALID_KML")


def test_xml_that_is_not_kml_is_rejected(client):
    error = assert_error(upload(client, "track.kml", b"<?xml version='1.0'?><gpx><wpt/></gpx>"), 400, "INVALID_KML")
    assert "not a KML" in error["message"]


def test_kmz_renamed_to_kml_is_rejected_with_a_helpful_message(client):
    error = assert_error(upload(client, "bundle.kml", f.make_zip({"doc.kml": GOOD_KML})), 400, "INVALID_KML")
    assert "KMZ" in error["message"]


def test_valid_kml_without_features_returns_422(client):
    empty = b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>'
    assert_error(upload(client, "empty_doc.kml", empty), 422, "NO_FEATURES")


def test_bytes_that_are_not_a_zip_return_400(client):
    assert_error(upload(client, "data.zip", b"definitely not a zip archive " * 5), 400, "INVALID_ZIP")


def test_truncated_zip_return_400(client):
    archive = f.make_zip(good_parts())
    assert_error(upload(client, "cut.zip", archive[: len(archive) // 2]), 400, "INVALID_ZIP")


def test_zip_without_any_shapefile_returns_422(client):
    assert_error(upload(client, "docs.zip", f.make_zip({"readme.txt": b"hello"})), 422, "NO_SHAPEFILE")


@pytest.mark.parametrize("missing", [".shx", ".dbf"])
def test_zip_missing_required_component_names_the_missing_file(client, missing):
    archive = f.shapefile_zip(f.polygon_frame(), drop=[missing])
    error = assert_error(upload(client, "partial.zip", archive), 422, "MISSING_SHAPEFILE_COMPONENTS")
    assert error["details"] == {"missing": [missing]}
    assert missing in error["message"]


def test_zip_with_only_sidecar_files_and_no_shp_returns_422(client):
    parts = good_parts()
    error = assert_error(upload(client, "sidecars.zip", f.make_zip({"data.dbf": parts["data.dbf"], "data.prj": parts["data.prj"]})), 422)
    assert error["code"] == "MISSING_SHAPEFILE_COMPONENTS"


def test_missing_prj_is_accepted_but_flagged_not_rejected(client):
    body = upload(client, "noprj.zip", f.shapefile_zip(f.polygon_frame(), drop=[".prj"])).json()
    assert body["status"] == "FAILED" and body["crs"] is None
    assert any("CRS" in w for w in body["warnings"])


@pytest.mark.parametrize("component", [".shp", ".shx", ".dbf"])
def test_corrupted_shapefile_component_returns_400(client, component):
    parts = good_parts()
    parts[f"data{component}"] = b"\x00garbage" * 40
    assert_error(upload(client, "corrupt.zip", f.make_zip(parts)), 400, "INVALID_SHAPEFILE")


def test_truncated_shp_returns_400(client):
    parts = good_parts()
    parts["data.shp"] = parts["data.shp"][:60]
    assert_error(upload(client, "short.zip", f.make_zip(parts)), 400, "UNREADABLE_FILE")


def test_missing_file_field_returns_422_validation_error(client):
    assert_error(client.post("/api/files/"), 422, "VALIDATION_ERROR")
    assert_error(client.post("/api/files/", data={"file": "just text"}), 422, "VALIDATION_ERROR")


def test_failed_uploads_leave_nothing_behind(client, storage_dir):
    upload(client, "bad.kml", b"<kml")
    upload(client, "bad.zip", b"nope")
    upload(client, "bad.txt", b"nope")

    assert list((storage_dir / "uploads").iterdir()) == []
    assert list((storage_dir / "results").iterdir()) == []
    assert not (storage_dir / "metadata.json").exists()


def test_error_bodies_never_contain_tracebacks_or_paths(client, storage_dir):
    responses = [
        upload(client, "bad.kml", b"<kml"),
        upload(client, "bad.zip", b"nope"),
        upload(client, "corrupt.zip", f.make_zip({"data.shp": b"\x00" * 100, "data.shx": b"", "data.dbf": b""})),
    ]
    for response in responses:
        assert response.status_code >= 400
        assert "Traceback" not in response.text
        assert str(storage_dir) not in response.text
        assert "/tmp/" not in response.text
