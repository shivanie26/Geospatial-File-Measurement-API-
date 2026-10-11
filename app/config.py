"""Application settings.

Every setting can be overridden with an environment variable prefixed with
``GEO_`` (for example ``GEO_MAX_UPLOAD_BYTES=1048576``) or through a local
``.env`` file. See ``.env.example``.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pyproj import CRS
from pyproj.exceptions import CRSError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GEO_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- storage -----------------------------------------------------------
    storage_dir: Path = Path("storage")

    # --- upload and processing limits -------------------------------------
    max_upload_bytes: int = Field(50 * 1024 * 1024, gt=0)
    max_archive_uncompressed_bytes: int = Field(250 * 1024 * 1024, gt=0)
    max_archive_entries: int = Field(500, gt=0)
    # Maximum compression ratio accepted for any single archive member that is
    # larger than 1 MiB (zip-bomb protection).
    max_archive_compression_ratio: int = Field(200, gt=1)
    max_features: int = Field(50_000, gt=0)

    # --- pagination --------------------------------------------------------
    default_page_size: int = Field(100, gt=0)
    max_page_size: int = Field(1000, gt=0)

    # --- CRS ---------------------------------------------------------------
    # By default a dataset without CRS metadata is NOT measured. Set this to an
    # explicit CRS (e.g. "EPSG:4326") to opt in to assuming it; every such
    # assumption is reported in the API response.
    assume_crs_if_missing: str | None = None

    log_level: str = "INFO"

    @field_validator("assume_crs_if_missing", mode="before")
    @classmethod
    def _validate_assumed_crs(cls, value: object) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            CRS.from_user_input(value)
        except CRSError as exc:
            raise ValueError(f"GEO_ASSUME_CRS_IF_MISSING is not a valid CRS: {value!r}") from exc
        return str(value).strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
