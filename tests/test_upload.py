"""POST /api/files/ - accepted formats and the shape of a successful response."""
from __future__ import annotations

import re
from datetime import datetime

from tests import factories as f
from tests.helpers import assert_error, upload, upload_ok

HEX_ID = re.compile(r"^[0-9a-f]{32}$")


def simple_kml() -> bytes:
    return f.kml_document(
        f.kml_placemark("field", f.kml_polygon(f.lonlat_rect(1000, 2000)), {"crop": "rice"}),
        f.kml_placemark("road", f.kml_line(f.lonlat_line(3000))),
        f.kml_placemark("well", f.kml_point(77.7, 11.3)),
    )


def test_kml_upload_succeeds(client):
    body = upload_ok(client, "survey.kml", simple_kml())

    assert HEX_ID.match(body["id"])
    assert body["filename"] == "survey.kml"
    assert body["file_type"] == "kml"
    assert body["feature_count"] == 3
    assert body["crs"] == "EPSG:4326"
    assert body["status"] == "COMPLETED"
    assert body["successful_feature_count"] == 3
    assert body["failed_feature_count"] == 0
    datetime.fromisoformat(body["upload_timestamp"].replace("Z", "+00:00"))


def test_response_is_created_with_json_content_type(client):
    response = upload(client, "survey.kml", simple_kml())
    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/json")


def test_extension_check_is_case_insensitive(client):
    assert upload_ok(client, "SURVEY.KML", simple_kml())["file_type"] == "kml"


def test_zipped_shapefile_upload_succeeds(client):
    gdf = f.polygon_frame(sizes=[(1000, 2000), (500, 500)])
    body = upload_ok(client, "parcels.zip", f.shapefile_zip(gdf))

    assert body["filename"] == "parcels.zip"
    assert body["file_type"] == "shapefile"
    assert body["feature_count"] == 2
    assert body["crs"] == f"EPSG:{f.UTM_EPSG}"
    assert body["status"] == "COMPLETED"


def test_shapefile_nested_in_zip_directories(client):
    archive = f.shapefile_zip(f.polygon_frame(), folder="deliverables/2026/final", stem="parcels")
    body = upload_ok(client, "nested.zip", archive)

    assert body["feature_count"] == 1
    assert body["status"] == "COMPLETED"


def test_zip_extras_are_ignored_and_optional_cpg_is_accepted(client):
    parts = f.shapefile_parts(f.polygon_frame())
    parts["notes.txt"] = b"not geospatial"
    parts["__MACOSX/._data.shp"] = b"resource fork junk"
    parts[".DS_Store"] = b"junk"
    body = upload_ok(client, "with_extras.zip", f.make_zip(parts))

    assert body["feature_count"] == 1
    assert body["status"] == "COMPLETED"


def test_zip_with_several_shapefiles_is_rejected_with_clear_message(client):
    parts = {}
    for stem in ("north", "south"):
        for name, data in f.shapefile_parts(f.polygon_frame(), stem=stem).items():
            parts[f"{stem}/{name}"] = data
    error = assert_error(upload(client, "two.zip", f.make_zip(parts)), 422, "MULTIPLE_SHAPEFILES")

    assert sorted(error["details"]["datasets"]) == ["north/north", "south/south"]
    assert "one Shapefile per ZIP" in error["message"]


def test_every_upload_gets_a_unique_id(client):
    ids = {upload_ok(client, "survey.kml", simple_kml())["id"] for _ in range(5)}
    assert len(ids) == 5


def test_untrusted_filename_is_reduced_to_a_display_name(client):
    body = upload_ok(client, "../../etc/evil\\path/survey.kml", simple_kml())
    assert body["filename"] == "survey.kml"


def test_original_upload_is_stored_under_the_generated_id_only(client, storage_dir):
    content = simple_kml()
    body = upload_ok(client, "my survey (final).kml", content)

    stored = list((storage_dir / "uploads").iterdir())
    assert [p.name for p in stored] == [f"{body['id']}.kml"]
    assert stored[0].read_bytes() == content


def test_responses_do_not_reveal_server_paths(client, storage_dir):
    body = upload_ok(client, "survey.kml", simple_kml())
    for response in (client.get(f"/api/files/{body['id']}/"), client.get(f"/api/files/{body['id']}/measurements/")):
        assert str(storage_dir) not in response.text
        assert "/uploads/" not in response.text and "\\uploads\\" not in response.text
        assert "metadata.json" not in response.text


def test_scratch_space_is_cleaned_after_success_and_failure(client, storage_dir):
    upload_ok(client, "survey.kml", simple_kml())
    upload(client, "broken.kml", b"<kml><oops")
    upload(client, "broken.zip", b"definitely not a zip")

    assert list((storage_dir / "tmp").iterdir()) == []


def test_root_redirects_to_the_swagger_docs(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/docs"
