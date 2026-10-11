"""Upload handling: validation, safe archive extraction and orchestration.

Security approach for ZIP uploads
* The archive is never extracted wholesale. Members are inspected first
  (names, symlinks, encryption, sizes, ratios) and only the 3-5 whitelisted
  Shapefile components of the selected dataset are copied out.
* Extracted files are written under FIXED names (``dataset.shp`` ...) inside a
  private temporary directory, so no archive-controlled name ever reaches the
  filesystem.
* Declared sizes are not trusted: bytes are counted while streaming.
"""
from __future__ import annotations

import logging
import re
import shutil
import stat
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from zlib import error as zlib_error

from app.config import Settings
from app.models.schemas import FileRecord
from app.services import geospatial_service
from app.services.measurement_service import build_file_record, measure_dataset
from app.services.storage_service import StorageService
from app.utils.exceptions import (
    BadRequestError,
    PayloadTooLargeError,
    UnprocessableFileError,
    UnsupportedMediaTypeError,
)

logger = logging.getLogger("geo_api.files")

SUPPORTED_SUFFIXES = {".kml", ".zip"}
_CHUNK = 1024 * 1024
_RATIO_CHECK_MIN_SIZE = 1024 * 1024
_REQUIRED = (".shp", ".shx", ".dbf")
_OPTIONAL = (".prj", ".cpg")
_JUNK = re.compile(r"(^|/)(__macosx|\.ds_store|thumbs\.db)(/|$)|(^|/)\._", re.IGNORECASE)
_DOCTYPE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_SHP_MAGIC = b"\x00\x00\x27\x0a"  # file code 9994, big-endian (shared by .shp and .shx)
# First byte of a dBASE file: the format version (dBASE III/IV/5, FoxPro, Visual FoxPro ...).
_DBF_VERSIONS = {0x02, 0x03, 0x04, 0x05, 0x30, 0x31, 0x32, 0x43, 0x63, 0x83, 0x8B, 0xCB, 0xF5, 0xFB}
_DBF_MIN_HEADER = 32


# --------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------
def sanitize_filename(name: str | None) -> str:
    """Return a display-only filename (no directories, no control characters)."""
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    if len(base) > 255:
        suffix = Path(base).suffix[:20]
        base = base[: 255 - len(suffix)] + suffix
    return base or "upload"


def _copy_limited(stream: BinaryIO, dest: Path, limit: int) -> int:
    total = 0
    with open(dest, "wb") as out:
        while chunk := stream.read(_CHUNK):
            total += len(chunk)
            if total > limit:
                raise PayloadTooLargeError(
                    f"The uploaded file exceeds the maximum size of {limit} bytes.", code="FILE_TOO_LARGE"
                )
            out.write(chunk)
    return total


# --------------------------------------------------------------------------
# KML validation
# --------------------------------------------------------------------------
def validate_kml(path: Path) -> None:
    with open(path, "rb") as handle:
        head = handle.read(64 * 1024)
    if head.startswith(b"PK\x03\x04"):
        raise BadRequestError(
            "The file is a ZIP/KMZ container, not a KML document. Upload a plain .kml file "
            "or a zipped Shapefile.",
            code="INVALID_KML",
        )
    if _DOCTYPE.search(head):
        raise BadRequestError("DOCTYPE and ENTITY declarations are not allowed in KML files.", code="INVALID_KML")
    try:
        root_checked = False
        for _event, element in ET.iterparse(path, events=("start",)):
            if not root_checked:
                root_checked = True
                if element.tag.rsplit("}", 1)[-1].lower() != "kml":
                    raise BadRequestError("The XML document is not a KML file (root element is not <kml>).", code="INVALID_KML")
    except ET.ParseError as exc:
        raise BadRequestError("The KML file is malformed or corrupted and cannot be parsed.", code="INVALID_KML") from exc
    if not root_checked:
        raise BadRequestError("The KML file contains no XML content.", code="INVALID_KML")


# --------------------------------------------------------------------------
# ZIP / Shapefile extraction
# --------------------------------------------------------------------------
@dataclass
class _Dataset:
    directory: str
    stem: str
    members: dict[str, zipfile.ZipInfo] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return f"{self.directory}/{self.stem}" if self.directory else self.stem


def _normalise_member_name(name: str) -> str:
    return name.replace("\\", "/")


def _check_member_safety(info: zipfile.ZipInfo) -> str:
    name = info.filename
    normalised = _normalise_member_name(name)
    parts = PurePosixPath(normalised).parts
    if (
        "\x00" in name
        or normalised.startswith("/")
        or re.match(r"^[A-Za-z]:", normalised)
        or ".." in parts
    ):
        raise BadRequestError("The archive contains unsafe file paths and was rejected.", code="UNSAFE_ARCHIVE")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise BadRequestError("The archive contains symbolic links and was rejected.", code="UNSAFE_ARCHIVE")
    if info.flag_bits & 0x1:
        raise BadRequestError("Encrypted ZIP archives are not supported.", code="ENCRYPTED_ARCHIVE")
    return normalised


def _group_datasets(zf: zipfile.ZipFile, settings: Settings) -> list[_Dataset]:
    infos = zf.infolist()
    if len(infos) > settings.max_archive_entries:
        raise PayloadTooLargeError(
            f"The archive contains more than {settings.max_archive_entries} entries.", code="ARCHIVE_LIMIT_EXCEEDED"
        )
    if sum(i.file_size for i in infos) > settings.max_archive_uncompressed_bytes:
        raise PayloadTooLargeError(
            "The archive would expand beyond the allowed uncompressed size.", code="ARCHIVE_LIMIT_EXCEEDED"
        )

    seen: set[str] = set()
    groups: dict[tuple[str, str], _Dataset] = {}
    for info in infos:
        normalised = _check_member_safety(info)  # inspects EVERY entry, even ones we ignore
        if info.is_dir() or _JUNK.search(normalised):
            continue
        if normalised.lower() in seen:
            raise BadRequestError("The archive contains duplicate entry names.", code="UNSAFE_ARCHIVE")
        seen.add(normalised.lower())

        if info.compress_size > 0 and info.file_size >= _RATIO_CHECK_MIN_SIZE:
            if info.file_size / info.compress_size > settings.max_archive_compression_ratio:
                raise PayloadTooLargeError(
                    "The archive has a suspiciously high compression ratio and was rejected.",
                    code="ARCHIVE_LIMIT_EXCEEDED",
                )

        path = PurePosixPath(normalised)
        suffix = path.suffix.lower()
        if suffix not in _REQUIRED + _OPTIONAL:
            continue
        key = (str(path.parent).lower(), path.stem.lower())
        dataset = groups.setdefault(key, _Dataset(directory=str(path.parent).strip("."), stem=path.stem))
        dataset.members[suffix] = info
    return list(groups.values())


def _stream_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, remaining: list[int]) -> None:
    try:
        with zf.open(info) as src, open(dest, "wb") as out:
            while chunk := src.read(_CHUNK):
                remaining[0] -= len(chunk)
                if remaining[0] < 0:
                    raise PayloadTooLargeError(
                        "The archive expands beyond the allowed uncompressed size.", code="ARCHIVE_LIMIT_EXCEEDED"
                    )
                out.write(chunk)
    except (zipfile.BadZipFile, zlib_error, EOFError, NotImplementedError, RuntimeError) as exc:
        raise BadRequestError("The ZIP archive is corrupted and could not be read.", code="INVALID_ZIP") from exc


def extract_shapefile(zip_path: Path, dest_dir: Path, settings: Settings) -> Path:
    """Validate the archive and extract ONE Shapefile dataset. Returns the path of the .shp."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise BadRequestError("The file is not a valid ZIP archive.", code="INVALID_ZIP") from exc

    with zf:
        datasets = _group_datasets(zf, settings)
        with_shp = [d for d in datasets if ".shp" in d.members]
        if not with_shp:
            if datasets:
                names = ", ".join(sorted({d.display_name for d in datasets}))
                raise UnprocessableFileError(
                    f"No .shp file was found for the Shapefile components present ({names}).",
                    code="MISSING_SHAPEFILE_COMPONENTS",
                )
            raise UnprocessableFileError("The ZIP archive does not contain a Shapefile (.shp).", code="NO_SHAPEFILE")
        if len(with_shp) > 1:
            names = sorted(d.display_name for d in with_shp)
            raise UnprocessableFileError(
                "The ZIP archive contains multiple Shapefile datasets "
                f"({', '.join(names)}). Upload one Shapefile per ZIP.",
                code="MULTIPLE_SHAPEFILES",
                details={"datasets": names},
            )

        dataset = with_shp[0]
        missing = [ext for ext in (".shx", ".dbf") if ext not in dataset.members]
        if missing:
            raise UnprocessableFileError(
                f"The Shapefile '{dataset.display_name}' is missing required component(s): "
                f"{', '.join(missing)}.",
                code="MISSING_SHAPEFILE_COMPONENTS",
                details={"missing": missing},
            )

        dest_dir.mkdir(parents=True, exist_ok=True)
        remaining = [settings.max_archive_uncompressed_bytes]
        for suffix, info in dataset.members.items():
            _stream_member(zf, info, dest_dir / f"dataset{suffix}", remaining)

    shp_path = dest_dir / "dataset.shp"
    for suffix in (".shp", ".shx"):
        with open(dest_dir / f"dataset{suffix}", "rb") as handle:
            if handle.read(4) != _SHP_MAGIC:
                raise BadRequestError(
                    f"The {suffix} file is corrupted (invalid file header).", code="INVALID_SHAPEFILE"
                )
    with open(dest_dir / "dataset.dbf", "rb") as handle:
        header = handle.read(_DBF_MIN_HEADER)
    if len(header) < _DBF_MIN_HEADER or header[0] not in _DBF_VERSIONS:
        raise BadRequestError("The .dbf file is corrupted (invalid file header).", code="INVALID_SHAPEFILE")
    return shp_path


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def handle_upload(
    *,
    upload_name: str | None,
    stream: BinaryIO,
    storage: StorageService,
    settings: Settings,
) -> FileRecord:
    filename = sanitize_filename(upload_name)
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedMediaTypeError(
            "Unsupported file type. Upload a .kml file or a .zip containing a Shapefile.",
            code="UNSUPPORTED_FILE_TYPE",
        )

    with storage.workdir() as work:
        raw = work / "upload.bin"
        size = _copy_limited(stream, raw, settings.max_upload_bytes)
        if size == 0:
            raise BadRequestError("The uploaded file is empty.", code="EMPTY_FILE")

        if suffix == ".kml":
            validate_kml(raw)
            kml_path = work / "_source.kml"
            shutil.copyfile(raw, kml_path)
            dataset = geospatial_service.read_kml(kml_path, settings.max_features)
        else:
            shp_path = extract_shapefile(raw, work / "shapefile", settings)
            dataset = geospatial_service.read_shapefile(shp_path, settings.max_features)

        outcome = measure_dataset(dataset, settings)
        record = build_file_record(
            file_id=storage.new_file_id(),
            filename=filename,
            file_type=dataset.file_type,
            uploaded_at=datetime.now(timezone.utc),
            outcome=outcome,
        )
        storage.save(record, outcome.features, raw, suffix)

    logger.info(
        "Processed upload id=%s type=%s features=%d status=%s",
        record.id, record.file_type, record.feature_count, record.status.value,
    )
    return record
