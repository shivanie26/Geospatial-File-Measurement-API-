"""Local JSON persistence: restart survival, atomic writes, corruption recovery, concurrency."""
from __future__ import annotations

import json
import threading

import pytest

from tests import factories as f
from tests.helpers import assert_error, get_measurements, upload_ok


def sample_kml(n_points: int = 3) -> bytes:
    return f.kml_document(
        f.kml_placemark("field", f.kml_polygon(f.lonlat_rect(1000, 2000)), {"crop": "rice"}),
        f.kml_placemark("road", f.kml_line(f.lonlat_line(3000))),
        *[f.kml_placemark(f"p{i}", f.kml_point(77.7 + i / 1000, 11.3)) for i in range(n_points)],
    )


def test_storage_directories_are_created_automatically(make_client, tmp_path):
    nested = tmp_path / "does" / "not" / "exist" / "yet"
    client = make_client(storage_dir=nested)
    upload_ok(client, "a.kml", sample_kml())

    assert (nested / "uploads").is_dir() and (nested / "results").is_dir()
    assert (nested / "metadata.json").is_file()


def test_data_survives_an_application_restart(make_client):
    first = make_client()
    body = upload_ok(first, "survey.kml", sample_kml())
    info_before = first.get(f"/api/files/{body['id']}/").json()
    measurements_before = get_measurements(first, body["id"])

    restarted = make_client()                                   # brand-new app and StorageService, same directory
    assert restarted.app is not first.app
    assert restarted.get(f"/api/files/{body['id']}/").json() == info_before
    assert get_measurements(restarted, body["id"]) == measurements_before


def test_restart_preserves_all_files_and_new_uploads_keep_working(make_client):
    first = make_client()
    ids = [upload_ok(first, f"s{i}.kml", sample_kml(i + 1))["id"] for i in range(3)]

    restarted = make_client()
    new_id = upload_ok(restarted, "later.kml", sample_kml())["id"]
    for file_id in ids + [new_id]:
        assert restarted.get(f"/api/files/{file_id}/").status_code == 200
    assert {s for s in ids} <= set(json.loads((restarted.app.state.storage.index_path).read_text())["files"])


def test_metadata_json_is_valid_and_has_no_leftover_temp_files(client, storage_dir):
    body = upload_ok(client, "a.kml", sample_kml())
    index = json.loads((storage_dir / "metadata.json").read_text())

    assert index["version"] == 1
    assert index["files"][body["id"]]["filename"] == "a.kml"
    leftovers = [p.name for p in storage_dir.rglob("*") if p.suffix == ".tmp" or p.name == ".metadata.lock"]
    assert leftovers == []


def test_results_file_contains_full_feature_data(client, storage_dir):
    body = upload_ok(client, "a.kml", sample_kml(2))
    stored = json.loads((storage_dir / "results" / f"{body['id']}.json").read_text())

    assert stored["record"]["id"] == body["id"]
    assert len(stored["features"]) == 4
    assert stored["features"][0]["area_sq_m"] == pytest.approx(2_000_000.0, abs=0.01)


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"version": 1}', '{"files": "oops"}', "\x00\x01\x02"])
def test_malformed_metadata_is_rebuilt_from_results_not_fatal(make_client, storage_dir, content):
    client = make_client()
    body = upload_ok(client, "a.kml", sample_kml())
    (storage_dir / "metadata.json").write_text(content)

    restarted = make_client()
    assert restarted.get(f"/api/files/{body['id']}/").status_code == 200
    assert get_measurements(restarted, body["id"])["feature_count"] == 5


def test_next_upload_repairs_a_corrupt_index_without_losing_old_files(make_client, storage_dir):
    client = make_client()
    old_id = upload_ok(client, "old.kml", sample_kml())["id"]
    (storage_dir / "metadata.json").write_text("{corrupt")

    new_id = upload_ok(make_client(), "new.kml", sample_kml())["id"]
    index = json.loads((storage_dir / "metadata.json").read_text())

    assert {old_id, new_id} <= set(index["files"])
    assert list(storage_dir.glob("metadata.json.corrupt-*"))          # the damaged copy is kept for inspection


def test_missing_metadata_file_with_existing_results_is_recovered(make_client, storage_dir):
    client = make_client()
    body = upload_ok(client, "a.kml", sample_kml())
    (storage_dir / "metadata.json").unlink()

    assert make_client().get(f"/api/files/{body['id']}/").json()["id"] == body["id"]


def test_empty_storage_is_handled(client):
    assert client.get("/api/files/0123456789abcdef0123456789abcdef/").status_code == 404


def test_missing_results_file_gives_a_safe_error_not_a_crash(make_client, storage_dir):
    client = make_client(raise_server_exceptions=False)
    body = upload_ok(client, "a.kml", sample_kml())
    (storage_dir / "results" / f"{body['id']}.json").unlink()

    assert client.get(f"/api/files/{body['id']}/").status_code == 200           # index still answers
    error = assert_error(client.get(f"/api/files/{body['id']}/measurements/"), 500, "RESULTS_UNAVAILABLE")
    assert str(storage_dir) not in error["message"]


def test_corrupt_results_file_gives_a_safe_error(make_client, storage_dir):
    client = make_client(raise_server_exceptions=False)
    body = upload_ok(client, "a.kml", sample_kml())
    (storage_dir / "results" / f"{body['id']}.json").write_text("{truncated")

    assert_error(client.get(f"/api/files/{body['id']}/measurements/"), 500, "RESULTS_UNAVAILABLE")


def test_concurrent_uploads_from_separate_app_instances_do_not_lose_index_entries(make_client, storage_dir):
    """Separate apps = separate in-process locks, so this exercises the cross-process file lock."""
    workers = 8
    clients = [make_client() for _ in range(workers)]
    ids: list[str | None] = [None] * workers
    failures: list[str] = []

    def run(i: int) -> None:
        try:
            ids[i] = upload_ok(clients[i], f"c{i}.kml", sample_kml(i % 3))["id"]
        except BaseException as exc:                                              # noqa: BLE001 - report in main thread
            failures.append(repr(exc))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert failures == []
    index = json.loads((storage_dir / "metadata.json").read_text())["files"]
    assert set(ids) <= set(index) and len(index) == workers                       # every upload is in the index
    reader = make_client()
    assert all(reader.get(f"/api/files/{file_id}/").status_code == 200 for file_id in ids)
