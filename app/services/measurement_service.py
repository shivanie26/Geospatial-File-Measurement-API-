"""Per-feature classification, validation and area / length measurement.

Rules
* A geometry is only measured if it is non-null, non-empty, has finite
  coordinates and is valid (no automatic repair - nothing is fabricated).
* Polygon / MultiPolygon           -> area
* LineString / MultiLineString     -> length
* Point / MultiPoint               -> NOT_APPLICABLE
* GeometryCollection               -> area of its polygons and/or length of its
                                      lines; other members are ignored (warning)
* anything else (e.g. LinearRing)  -> UNSUPPORTED
* One bad feature never aborts the dataset; it is reported on that feature.
* Full precision is stored; rounding happens only in ``present_*`` functions.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np
import shapely
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError
from shapely.errors import ShapelyError
from shapely.geometry import Polygon, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.validation import explain_validity

from app.config import Settings
from app.models.schemas import (
    CrsInfo,
    FeatureMeasurement,
    FeatureRecord,
    FeatureStatus,
    FileRecord,
    FileStatus,
    Issue,
    MeasurementCrsSet,
    Measurements,
    MeasurementsResponse,
    Pagination,
    ProcessingSummary,
    Units,
)
from app.services import crs_service
from app.services.crs_service import Bounds, MeasurementCrs
from app.services.geospatial_service import SourceDataset, SourceFeature

logger = logging.getLogger("geo_api.measurement")

# Presentation precision: ~0.1 mm in every unit.
ROUND_M, ROUND_SQ_M, ROUND_KM, ROUND_SQ_KM = 4, 4, 7, 10

_GEOGRAPHIC_TOLERANCE = 1e-9


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _atomic_parts(geom: BaseGeometry) -> Iterator[BaseGeometry]:
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _atomic_parts(part)
    else:
        yield geom


def _is_finite(geom: BaseGeometry) -> bool:
    coords = shapely.get_coordinates(geom)
    return bool(np.isfinite(coords).all())


def _transform(transformer: Transformer, geom: BaseGeometry) -> BaseGeometry:
    out = shapely_transform(transformer.transform, geom)
    if not _is_finite(out):
        raise ValueError("Transformation produced non-finite coordinates.")
    return out


def _fail(record: FeatureRecord, code: str, message: str) -> None:
    record.status = FeatureStatus.FAILED
    record.area_sq_m = None
    record.length_m = None
    record.errors.append(Issue(code=code, message=message))


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


@dataclass
class _Pending:
    record: FeatureRecord
    geom: BaseGeometry  # 2D copy of the source geometry
    needs_area: bool
    needs_length: bool
    geo: BaseGeometry | None = None  # same geometry in EPSG:4326


@dataclass
class MeasurementOutcome:
    features: list[FeatureRecord]
    source_crs: CrsInfo | None
    measurement_crs: MeasurementCrsSet
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# step 1: classify every feature (independent of CRS)
# --------------------------------------------------------------------------
def _prepare(source: SourceFeature) -> tuple[FeatureRecord, _Pending | None]:
    geom = source.geometry
    record = FeatureRecord(
        feature_index=source.index,
        layer=source.layer,
        properties=source.properties,
        status=FeatureStatus.MEASURED,
    )
    if geom is None:
        _fail(record, "NULL_GEOMETRY", "The feature has no geometry.")
        return record, None

    record.geometry_type = geom.geom_type
    if geom.is_empty:
        _fail(record, "EMPTY_GEOMETRY", "The feature has an empty geometry.")
        return record, None

    if not _is_finite(geom):
        _fail(record, "INVALID_COORDINATES", "The geometry contains NaN or infinite coordinates.")
        return record, None
    record.geometry = mapping(geom)

    parts = list(_atomic_parts(geom))
    has_poly = any(isinstance(p, Polygon) for p in parts)
    has_line = any(p.geom_type == "LineString" for p in parts)
    has_point = any(p.geom_type == "Point" for p in parts)
    has_other = any(p.geom_type not in {"Polygon", "LineString", "Point"} for p in parts)

    if not (has_poly or has_line):
        if has_point and not has_other:
            record.status = FeatureStatus.NOT_APPLICABLE
        else:
            record.status = FeatureStatus.UNSUPPORTED
            record.errors.append(
                Issue(
                    code="UNSUPPORTED_GEOMETRY",
                    message=f"Measurement is not implemented for geometry type '{geom.geom_type}'.",
                )
            )
        return record, None

    if not geom.is_valid:
        _fail(record, "INVALID_GEOMETRY", f"Invalid geometry: {explain_validity(geom)}")
        return record, None

    if geom.geom_type == "GeometryCollection" and (has_point or has_other):
        record.warnings.append(
            Issue(code="MEMBERS_IGNORED", message="Point or ring members of the collection are not measured.")
        )
    return record, _Pending(
        record=record,
        geom=shapely.force_2d(geom),
        needs_area=has_poly,
        needs_length=has_line,
    )


# --------------------------------------------------------------------------
# step 2: CRS-dependent measurement
# --------------------------------------------------------------------------
def _union_bounds(items: list[_Pending]) -> Bounds:
    boxes = [item.geo.bounds for item in items if item.geo is not None]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _polygon_area(geom: BaseGeometry) -> float:
    return math.fsum(p.area for p in _atomic_parts(geom) if isinstance(p, Polygon))


def _lines(geom: BaseGeometry) -> list[BaseGeometry]:
    return [p for p in _atomic_parts(geom) if p.geom_type == "LineString"]


def _measure_area(items: list[_Pending], source_crs: CRS) -> MeasurementCrs | None:
    items = [i for i in items if i.needs_area and i.record.status != FeatureStatus.FAILED]
    if not items:
        return None
    selected = crs_service.select_area_crs(_union_bounds(items))
    try:
        transformer = Transformer.from_crs(source_crs, selected.crs, always_xy=True)
    except (ProjError, CRSError) as exc:
        for item in items:
            _fail(item.record, "PROJECTION_FAILED", f"Cannot transform to the measurement CRS: {exc}")
        return selected
    for item in items:
        try:
            item.record.area_sq_m = _polygon_area(_transform(transformer, item.geom))
        except (ProjError, ValueError, ShapelyError) as exc:
            _fail(item.record, "PROJECTION_FAILED", f"Could not project the geometry for area calculation: {exc}")
    return selected


def _measure_length(items: list[_Pending], source_crs: CRS) -> MeasurementCrs | None:
    items = [i for i in items if i.needs_length and i.record.status != FeatureStatus.FAILED]
    if not items:
        return None
    selected = crs_service.select_length_crs(_union_bounds(items))
    transformer = None
    if selected.crs is not None:
        try:
            transformer = Transformer.from_crs(source_crs, selected.crs, always_xy=True)
        except (ProjError, CRSError) as exc:
            for item in items:
                _fail(item.record, "PROJECTION_FAILED", f"Cannot transform to the measurement CRS: {exc}")
            return selected
    for item in items:
        try:
            if transformer is not None:
                item.record.length_m = math.fsum(line.length for line in _lines(_transform(transformer, item.geom)))
            else:
                assert selected.geod is not None and item.geo is not None
                item.record.length_m = math.fsum(selected.geod.geometry_length(line) for line in _lines(item.geo))
        except (ProjError, ValueError, ShapelyError) as exc:
            _fail(item.record, "PROJECTION_FAILED", f"Could not compute length: {exc}")
    return selected


def _to_geographic(pending: list[_Pending], source_crs: CRS) -> None:
    """Fill ``item.geo`` (EPSG:4326) and reject features outside valid lon/lat ranges."""
    try:
        to_geo = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    except (ProjError, CRSError) as exc:
        for item in pending:
            _fail(item.record, "CRS_TRANSFORMATION_UNSUPPORTED", f"The source CRS cannot be transformed to WGS 84: {exc}")
        return
    for item in pending:
        try:
            geo = _transform(to_geo, item.geom)
        except (ProjError, ValueError, ShapelyError) as exc:
            _fail(item.record, "PROJECTION_FAILED", f"Could not transform the geometry to WGS 84: {exc}")
            continue
        min_x, min_y, max_x, max_y = geo.bounds
        tol = _GEOGRAPHIC_TOLERANCE
        if min_x < -180 - tol or max_x > 180 + tol or min_y < -90 - tol or max_y > 90 + tol:
            _fail(
                item.record,
                "COORDINATES_OUT_OF_RANGE",
                "Coordinates fall outside valid longitude (-180..180) / latitude (-90..90) ranges.",
            )
            continue
        item.geo = geo


def measure_dataset(dataset: SourceDataset, settings: Settings) -> MeasurementOutcome:
    notes: list[str] = list(dataset.warnings)
    source_crs = dataset.crs
    assumed = False
    if source_crs is None and settings.assume_crs_if_missing:
        source_crs = CRS.from_user_input(settings.assume_crs_if_missing)
        assumed = True
        notes.append(
            f"The file has no CRS; {settings.assume_crs_if_missing} was assumed because "
            "GEO_ASSUME_CRS_IF_MISSING is configured. Measurements are only as valid as that assumption."
        )

    records: list[FeatureRecord] = []
    pending: list[_Pending] = []
    for feature in dataset.features:
        record, item = _prepare(feature)
        records.append(record)
        if item is not None:
            pending.append(item)

    area_crs: MeasurementCrs | None = None
    length_crs: MeasurementCrs | None = None
    if pending:
        if source_crs is None:
            for item in pending:
                _fail(
                    item.record,
                    "CRS_MISSING",
                    "The dataset has no coordinate reference system, so area/length cannot be computed reliably. "
                    "Provide a Shapefile with a .prj file, or configure GEO_ASSUME_CRS_IF_MISSING.",
                )
            notes.append("No CRS was found: polygon and line features were not measured.")
        else:
            _to_geographic(pending, source_crs)
            ok = [i for i in pending if i.record.status != FeatureStatus.FAILED]
            area_crs = _measure_area(ok, source_crs)
            length_crs = _measure_length(ok, source_crs)

    return MeasurementOutcome(
        features=records,
        source_crs=crs_service.describe_crs(source_crs, assumed=assumed) if source_crs is not None else None,
        measurement_crs=MeasurementCrsSet(
            area=area_crs.info if area_crs else None,
            length=length_crs.info if length_crs else None,
        ),
        warnings=notes,
    )


# --------------------------------------------------------------------------
# summaries and presentation
# --------------------------------------------------------------------------
def summarise(features: list[FeatureRecord]) -> ProcessingSummary:
    areas = [f.area_sq_m for f in features if f.area_sq_m is not None]
    lengths = [f.length_m for f in features if f.length_m is not None]
    total_area = math.fsum(areas) if areas else None
    total_length = math.fsum(lengths) if lengths else None
    return ProcessingSummary(
        by_status=dict(Counter(f.status.value for f in features)),
        by_geometry_type=dict(Counter(f.geometry_type or "None" for f in features)),
        total_area_sq_m=total_area,
        total_area_sq_km=None if total_area is None else total_area / 1e6,
        total_length_m=total_length,
        total_length_km=None if total_length is None else total_length / 1000.0,
    )


def build_file_record(
    *,
    file_id: str,
    filename: str,
    file_type: str,
    uploaded_at,
    outcome: MeasurementOutcome,
) -> FileRecord:
    features = outcome.features
    failed = sum(f.status == FeatureStatus.FAILED for f in features)
    unsupported = sum(f.status == FeatureStatus.UNSUPPORTED for f in features)
    succeeded = len(features) - failed - unsupported
    if failed == 0 and unsupported == 0:
        status = FileStatus.COMPLETED
    elif succeeded > 0:
        status = FileStatus.PARTIAL
    else:
        status = FileStatus.FAILED
    return FileRecord(
        id=file_id,
        filename=filename,
        file_type=file_type,
        upload_timestamp=uploaded_at,
        feature_count=len(features),
        successful_feature_count=succeeded,
        failed_feature_count=failed,
        unsupported_feature_count=unsupported,
        crs=outcome.source_crs.identifier if outcome.source_crs else None,
        status=status,
        warnings=outcome.warnings,
        source_crs=outcome.source_crs,
        measurement_crs=outcome.measurement_crs,
        summary=summarise(features),
    )


def _present_summary(summary: ProcessingSummary) -> ProcessingSummary:
    return summary.model_copy(
        update={
            "total_area_sq_m": _round(summary.total_area_sq_m, ROUND_SQ_M),
            "total_area_sq_km": _round(summary.total_area_sq_km, ROUND_SQ_KM),
            "total_length_m": _round(summary.total_length_m, ROUND_M),
            "total_length_km": _round(summary.total_length_km, ROUND_KM),
        }
    )


def present_feature(record: FeatureRecord, source_crs: str | None) -> FeatureMeasurement:
    area, length = record.area_sq_m, record.length_m
    return FeatureMeasurement(
        feature_index=record.feature_index,
        layer=record.layer,
        geometry_type=record.geometry_type,
        geometry=record.geometry,
        source_crs=source_crs,
        properties=record.properties,
        status=record.status,
        measurements=Measurements(
            area_sq_m=_round(area, ROUND_SQ_M),
            area_sq_km=None if area is None else _round(area / 1e6, ROUND_SQ_KM),
            length_m=_round(length, ROUND_M),
            length_km=None if length is None else _round(length / 1000.0, ROUND_KM),
        ),
        errors=record.errors,
        warnings=record.warnings,
    )


def present_measurements(
    record: FileRecord,
    total: int,
    page: list[FeatureRecord],
    *,
    limit: int,
    offset: int,
) -> MeasurementsResponse:
    end = offset + len(page)
    info = record.to_info()
    return MeasurementsResponse(
        file_id=record.id,
        filename=record.filename,
        status=record.status,
        feature_count=info.feature_count,
        successful_feature_count=info.successful_feature_count,
        failed_feature_count=info.failed_feature_count,
        unsupported_feature_count=info.unsupported_feature_count,
        source_crs=record.source_crs,
        measurement_crs=record.measurement_crs,
        units=Units(),
        processing_summary=_present_summary(record.summary),
        warnings=record.warnings,
        pagination=Pagination(
            total=total,
            limit=limit,
            offset=offset,
            returned=len(page),
            has_more=end < total,
            next_offset=end if end < total else None,
        ),
        features=[present_feature(f, record.crs) for f in page],
    )
