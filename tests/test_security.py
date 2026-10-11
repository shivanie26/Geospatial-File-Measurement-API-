"""Hostile input: unsafe archives, zip bombs, size limits, XML entities, internal errors."""
from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from tests import factories as f
from tests.helpers import assert_error, upload, upload_ok

PARTS = f.shapefile_parts(f.polygon_frame())


def zip_with(extra: dict[str, bytes], base: dict[str, bytes] | None = None) -> bytes:
    return f.make_zip({**(PARTS if base is None else base), **extra})


def tree(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")}


# ------------------------------------------------------------ path traversal
@pytest.mark.parametrize(
    "member",
    ["../evil.txt", "a/../../evil.shp", "..\\evil.txt", "/etc/evil.txt", "C:/Windows/evil.txt", "sub/../../../evil.dbf"],
)
def test_zip_path_traversal_is_rejected_and_nothing_is_written_outside_storage(client, tmp_path, storage_dir, member):
    before = tree(tmp_path)
    assert_error(upload(client, "evil.zip", zip_with({member: b"pwned"})), 400, "UNSAFE_ARCHIVE")

    assert not any("evil" in name for name in tree(tmp_path) - before)
    assert not (tmp_path.parent / "evil.txt").exists()
    assert list((storage_dir / "uploads").iterdir()) == []


def test_unsafe_member_is_rejected_even_when_it_is_not_a_shapefile_part(client):
    # Every entry is inspected, not just the ones that would be extracted.
    assert_error(upload(client, "evil.zip", zip_with({"../harmless-looking.txt": b"x"})), 400, "UNSAFE_ARCHIVE")


def test_symlink_entries_are_rejected(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in PARTS.items():
            archive.writestr(name, data)
        link = zipfile.ZipInfo("link.shp")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    assert_error(upload(client, "link.zip", buffer.getvalue()), 400, "UNSAFE_ARCHIVE")


def test_encrypted_entries_are_rejected(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in PARTS.items():
            archive.writestr(name, data)
        archive.writestr("secret.txt", b"x")
    raw = bytearray(buffer.getvalue())
    # zipfile clears the encryption flag when writing, so set it directly in the
    # central directory record of the last entry (general-purpose flag at offset 8).
    central = raw.rfind(b"PK\x01\x02")
    raw[central + 8] |= 0x01
    assert_error(upload(client, "enc.zip", bytes(raw)), 400, "ENCRYPTED_ARCHIVE")


def test_duplicate_entry_names_are_rejected(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in PARTS.items():
            archive.writestr(name, data)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("data.shp", PARTS["data.shp"])
    assert_error(upload(client, "dup.zip", buffer.getvalue()), 400, "UNSAFE_ARCHIVE")


# ------------------------------------------------------- expansion / zip bombs
def test_high_compression_ratio_member_is_rejected_as_a_zip_bomb(client):
    bomb = zip_with({"padding.bin": b"\x00" * (8 * 1024 * 1024)})     # ~8 MiB of zeros -> a few KiB
    assert_error(upload(client, "bomb.zip", bomb), 413, "ARCHIVE_LIMIT_EXCEEDED")


def test_total_uncompressed_size_limit(make_client):
    client = make_client(max_archive_uncompressed_bytes=5_000)
    error = assert_error(upload(client, "big.zip", zip_with({"padding.txt": b"abcd" * 2_000})), 413, "ARCHIVE_LIMIT_EXCEEDED")
    assert "uncompressed" in error["message"]


def test_too_many_archive_entries(make_client):
    client = make_client(max_archive_entries=5)
    many = {f"filler{i}.txt": b"x" for i in range(10)}
    assert_error(upload(client, "many.zip", zip_with(many)), 413, "ARCHIVE_LIMIT_EXCEEDED")


def test_feature_count_limit(make_client):
    client = make_client(max_features=5)
    content = f.kml_document(*[f.kml_placemark(str(i), f.kml_point(77.7, 11.3)) for i in range(10)])
    assert_error(upload(client, "many.kml", content), 422, "TOO_MANY_FEATURES")


# ----------------------------------------------------------- upload size limit
def test_upload_over_limit_is_rejected_while_streaming(make_client):
    client = make_client(max_upload_bytes=2_000)
    content = f.kml_document(f.kml_placemark("a", f.kml_point(77.7, 11.3))) + b" " * 3_000
    error = assert_error(upload(client, "big.kml", content), 413, "FILE_TOO_LARGE")
    assert "2000" in error["message"]


def test_oversized_request_is_rejected_before_the_body_is_parsed(make_client):
    client = make_client(max_upload_bytes=1_000)
    assert_error(upload(client, "huge.kml", b" " * (2 * 1024 * 1024)), 413, "FILE_TOO_LARGE")


def test_upload_exactly_at_the_limit_is_accepted(make_client):
    content = f.kml_document(f.kml_placemark("a", f.kml_point(77.7, 11.3)))
    client = make_client(max_upload_bytes=len(content))
    assert upload(client, "edge.kml", content).status_code == 201


# ----------------------------------------------------------------- XML attacks
def test_kml_with_external_entity_is_rejected(client):
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<kml><Document><Placemark><name>&x;</name></Placemark></Document></kml>'
    )
    error = assert_error(upload(client, "xxe.kml", xxe), 400, "INVALID_KML")
    assert "passwd" not in error["message"]


def test_kml_entity_expansion_bomb_is_rejected(client):
    lol = (
        b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b"<kml><Document><name>&b;</name></Document></kml>"
    )
    assert_error(upload(client, "lol.kml", lol), 400, "INVALID_KML")


# -------------------------------------------------------------- filenames, ids
def test_hostile_filenames_never_control_where_files_are_stored(client, tmp_path, storage_dir):
    content = f.kml_document(f.kml_placemark("a", f.kml_point(77.7, 11.3)))
    for name in ["../../outside.kml", "..\\..\\outside.kml", "/abs/outside.kml", "a/b/c/../../../x.kml", "nul\x00.kml"]:
        response = upload(client, name, content)
        if response.status_code == 201:
            assert "/" not in response.json()["filename"] and "\\" not in response.json()["filename"]

    created = tree(tmp_path)
    assert not any(p.endswith("outside.kml") for p in created)
    for path in (storage_dir / "uploads").iterdir():
        assert len(path.stem) == 32 and path.suffix == ".kml"        # generated id only


def test_file_ids_cannot_be_used_for_path_traversal(client, storage_dir):
    secret = storage_dir.parent / "secret.json"
    secret.write_text('{"record": {}, "features": []}')
    for bad in ["..%2Fsecret", "%2e%2e%2fsecret", "....//secret", "secret"]:
        assert client.get(f"/api/files/{bad}/").status_code == 404
        assert client.get(f"/api/files/{bad}/measurements/").status_code == 404


# ---------------------------------------------------------- internal failures
def test_unexpected_errors_return_a_generic_500_without_internals(make_client, monkeypatch):
    client = make_client(raise_server_exceptions=False)

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail /srv/app/private.py")

    monkeypatch.setattr("app.services.file_service.measure_dataset", boom)
    response = upload(client, "a.kml", f.kml_document(f.kml_placemark("a", f.kml_point(77.7, 11.3))))

    error = assert_error(response, 500, "INTERNAL_ERROR")
    assert "secret" not in response.text and "private.py" not in response.text
    assert error["message"] == "An internal error occurred."


def test_failed_save_is_cleaned_up_and_does_not_report_success(make_client, storage_dir, monkeypatch):
    client = make_client(raise_server_exceptions=False)
    monkeypatch.setattr("app.services.storage_service.StorageService._write_index", lambda self, files: (_ for _ in ()).throw(OSError("disk full")))
    response = upload(client, "a.kml", f.kml_document(f.kml_placemark("a", f.kml_point(77.7, 11.3))))

    assert response.status_code == 500
    assert list((storage_dir / "uploads").iterdir()) == []
    assert list((storage_dir / "results").iterdir()) == []
