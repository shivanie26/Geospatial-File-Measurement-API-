"""JSON-file persistence.

Layout (all below ``settings.storage_dir``)::

    uploads/<id>.kml|.zip   original upload, named only by the generated id
    results/<id>.json       {"record": FileRecord, "features": [FeatureRecord, ...]}
    metadata.json           {"version": 1, "files": {<id>: FileRecord}}   (index)
    tmp/                    per-request scratch directories, always removed

Safety properties
* Every JSON write is atomic (temp file + fsync + ``os.replace``).
* The index is updated read-modify-write under a lock (thread lock plus an
  ``O_EXCL`` lock file so several worker processes are also protected).
* Write order is original -> results -> index, so the index never points at
  data that was not fully written.
* The results file embeds the file record, so a missing/corrupt index can be
  rebuilt from ``results/``.
* File ids are validated against a strict pattern, so they can never be used
  for path traversal.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from app.models.schemas import FeatureRecord, FileRecord
from app.utils.exceptions import StorageError

logger = logging.getLogger("geo_api.storage")

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_ALLOWED_SUFFIXES = {".kml", ".zip"}
_LOCK_TIMEOUT_S = 10.0
_LOCK_STALE_S = 30.0
INDEX_VERSION = 1


def is_valid_file_id(file_id: str) -> bool:
    return bool(_ID_PATTERN.match(file_id))


class StorageService:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.uploads_dir = self.root / "uploads"
        self.results_dir = self.root / "results"
        self.tmp_dir = self.root / "tmp"
        self.index_path = self.root / "metadata.json"
        self._lock_path = self.root / ".metadata.lock"
        self._thread_lock = threading.Lock()

    # ------------------------------------------------------------ plumbing
    def ensure_dirs(self) -> None:
        for directory in (self.uploads_dir, self.results_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_file_id() -> str:
        return uuid.uuid4().hex

    @contextmanager
    def workdir(self) -> Iterator[Path]:
        """Scratch directory for one request; always removed afterwards."""
        self.ensure_dirs()
        with tempfile.TemporaryDirectory(dir=self.tmp_dir, prefix="upload-") as name:
            yield Path(name)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.ensure_dirs()
        with self._thread_lock:
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            while True:
                try:
                    fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    try:
                        if time.time() - self._lock_path.stat().st_mtime > _LOCK_STALE_S:
                            self._lock_path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    if time.monotonic() > deadline:
                        raise StorageError("Storage is busy, please retry.", code="STORAGE_BUSY")
                    time.sleep(0.01)
            try:
                yield
            finally:
                os.close(fd)
                self._lock_path.unlink(missing_ok=True)

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, allow_nan=False, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except (OSError, ValueError, TypeError) as exc:
            tmp.unlink(missing_ok=True)
            logger.error("Atomic write failed for %s: %s", path.name, exc)
            raise StorageError("Could not persist data.") from exc

    # --------------------------------------------------------------- index
    def _rebuild_index(self) -> dict[str, dict[str, Any]]:
        """Reconstruct the index from the record embedded in every results file."""
        files: dict[str, dict[str, Any]] = {}
        if self.results_dir.is_dir():
            for path in sorted(self.results_dir.glob("*.json")):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))["record"]
                    FileRecord.model_validate(record)
                    files[path.stem] = record
                except (OSError, ValueError, KeyError, TypeError, ValidationError):
                    logger.error("Skipping unreadable results file %s", path.name)
        return files

    def _read_index(self, *, repair: bool = False) -> dict[str, dict[str, Any]]:
        if not self.index_path.exists():
            return self._rebuild_index() if self.results_dir.is_dir() and any(self.results_dir.glob("*.json")) else {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            files = data["files"]
            if not isinstance(files, dict):
                raise TypeError("'files' must be an object")
            return files
        except (OSError, ValueError, KeyError, TypeError):
            logger.error("metadata.json is malformed; rebuilding index from results/")
            files = self._rebuild_index()
            if repair:
                backup = self.index_path.with_name(f"metadata.json.corrupt-{int(time.time())}")
                try:
                    os.replace(self.index_path, backup)
                except OSError:
                    pass
            return files

    def _write_index(self, files: dict[str, dict[str, Any]]) -> None:
        self._atomic_write_json(self.index_path, {"version": INDEX_VERSION, "files": files})

    # ----------------------------------------------------------------- API
    def save(
        self,
        record: FileRecord,
        features: list[FeatureRecord],
        original: Path,
        suffix: str,
    ) -> None:
        """Persist the original upload, the results and the index entry."""
        if suffix not in _ALLOWED_SUFFIXES:
            raise StorageError("Unsupported storage suffix.")
        if not is_valid_file_id(record.id):
            raise StorageError("Invalid file id.")
        self.ensure_dirs()
        original_dest = self.uploads_dir / f"{record.id}{suffix}"
        results_dest = self.results_dir / f"{record.id}.json"
        record_json = record.model_dump(mode="json")
        try:
            shutil.copyfile(original, original_dest)
            self._atomic_write_json(
                results_dest,
                {"record": record_json, "features": [f.model_dump(mode="json") for f in features]},
            )
            with self._locked():
                files = self._read_index(repair=True)
                files[record.id] = record_json
                self._write_index(files)
        except BaseException:
            original_dest.unlink(missing_ok=True)
            results_dest.unlink(missing_ok=True)
            raise

    def get_record(self, file_id: str) -> FileRecord | None:
        if not is_valid_file_id(file_id):
            return None
        raw = self._read_index().get(file_id)
        if raw is None:
            return None
        try:
            return FileRecord.model_validate(raw)
        except ValidationError as exc:
            logger.error("Stored record %s is invalid: %s", file_id, exc.error_count())
            raise StorageError("Stored metadata for this file is unreadable.") from exc

    def load_feature_page(self, file_id: str, offset: int, limit: int) -> tuple[int, list[FeatureRecord]]:
        """Return ``(total_features, features[offset:offset+limit])``."""
        if not is_valid_file_id(file_id):
            raise StorageError("Invalid file id.")
        path = self.results_dir / f"{file_id}.json"
        try:
            features = json.loads(path.read_text(encoding="utf-8"))["features"]
            page = [FeatureRecord.model_validate(f) for f in features[offset : offset + limit]]
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            logger.error("Results for %s are unavailable: %s", file_id, type(exc).__name__)
            raise StorageError("Stored results for this file are unavailable.", code="RESULTS_UNAVAILABLE") from exc
        return len(features), page
