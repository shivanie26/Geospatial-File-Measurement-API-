"""Pydantic models: persisted records and API responses.

Units used throughout the API
-----------------------------
* ``area_sq_m``  - square metres        * ``area_sq_km`` - square kilometres
* ``length_m``   - metres               * ``length_km``  - kilometres
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FileStatus(str, Enum):
    COMPLETED = "COMPLETED"  # every feature processed
    PARTIAL = "PARTIAL"  # at least one feature succeeded, at least one failed/unsupported
    FAILED = "FAILED"  # no feature could be processed (e.g. missing CRS)


class FeatureStatus(str, Enum):
    MEASURED = "MEASURED"  # area and/or length computed
    NOT_APPLICABLE = "NOT_APPLICABLE"  # points: nothing to measure
    UNSUPPORTED = "UNSUPPORTED"  # geometry type with no implemented measurement
    FAILED = "FAILED"  # null / empty / invalid geometry, missing CRS, projection failure


class Issue(BaseModel):
    code: str
    message: str


# ---------------------------------------------------------------- CRS models
class CrsInfo(BaseModel):
    identifier: str = Field(description="Authority code such as 'EPSG:4326', or the CRS name.")
    name: str | None = None
    type: Literal["geographic", "projected", "other"]
    assumed: bool = Field(False, description="True if the CRS was supplied by configuration, not the file.")


class MeasurementCrsInfo(BaseModel):
    method: Literal["projected", "geodesic"]
    strategy: str
    identifier: str | None = Field(None, description="CRS used for the calculation (or PROJ definition).")
    name: str | None = None
    units: str = "metre"
    description: str


class MeasurementCrsSet(BaseModel):
    area: MeasurementCrsInfo | None = None
    length: MeasurementCrsInfo | None = None


# ----------------------------------------------------------- stored records
class ProcessingSummary(BaseModel):
    by_status: dict[str, int] = Field(default_factory=dict)
    by_geometry_type: dict[str, int] = Field(default_factory=dict)
    total_area_sq_m: float | None = None
    total_area_sq_km: float | None = None
    total_length_m: float | None = None
    total_length_km: float | None = None


class FeatureRecord(BaseModel):
    """One processed feature as persisted. Measurements are stored at full precision."""

    feature_index: int
    layer: str | None = None
    geometry_type: str | None = None
    geometry: dict[str, Any] | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    status: FeatureStatus
    area_sq_m: float | None = None
    length_m: float | None = None
    errors: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)


class FileInfo(BaseModel):
    id: str
    filename: str
    file_type: Literal["kml", "shapefile"]
    upload_timestamp: datetime
    feature_count: int
    successful_feature_count: int
    failed_feature_count: int
    unsupported_feature_count: int
    crs: str | None = Field(None, description="Source CRS of the dataset, null if the file has none.")
    status: FileStatus
    warnings: list[str] = Field(default_factory=list)


class FileRecord(FileInfo):
    """Persisted metadata: FileInfo plus detail used by the measurements endpoint."""

    source_crs: CrsInfo | None = None
    measurement_crs: MeasurementCrsSet = Field(default_factory=MeasurementCrsSet)
    summary: ProcessingSummary = Field(default_factory=ProcessingSummary)

    def to_info(self) -> FileInfo:
        return FileInfo(**{name: getattr(self, name) for name in FileInfo.model_fields})


# ------------------------------------------------------------ API responses
class Measurements(BaseModel):
    area_sq_m: float | None = None
    area_sq_km: float | None = None
    length_m: float | None = None
    length_km: float | None = None


class FeatureMeasurement(BaseModel):
    feature_index: int
    layer: str | None
    geometry_type: str | None
    geometry: dict[str, Any] | None
    source_crs: str | None
    properties: dict[str, Any]
    status: FeatureStatus
    measurements: Measurements
    errors: list[Issue]
    warnings: list[Issue]


class Pagination(BaseModel):
    total: int
    limit: int
    offset: int
    returned: int
    has_more: bool
    next_offset: int | None


class Units(BaseModel):
    area_sq_m: str = "square metres"
    area_sq_km: str = "square kilometres"
    length_m: str = "metres"
    length_km: str = "kilometres"


class MeasurementsResponse(BaseModel):
    file_id: str
    filename: str
    status: FileStatus
    feature_count: int
    successful_feature_count: int
    failed_feature_count: int
    unsupported_feature_count: int
    source_crs: CrsInfo | None
    measurement_crs: MeasurementCrsSet
    units: Units
    processing_summary: ProcessingSummary
    warnings: list[str]
    pagination: Pagination
    features: list[FeatureMeasurement]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
