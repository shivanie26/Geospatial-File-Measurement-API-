"""Reading KML and Shapefile datasets into plain Python structures."""
from __future__ import annotations

import datetime as dt
import logging
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyogrio.errors import DataLayerError, DataSourceError, FeatureError, FieldError, GeometryError
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry.base import BaseGeometry

from app.utils.exceptions import BadRequestError, DriverUnavailableError, UnprocessableFileError

logger = logging.getLogger("geo_api.geospatial")

_READ_ERRORS = (DataSourceError, DataLayerError, FeatureError, FieldError, GeometryError, CRSError, ValueError, OSError)

# Fields the GDAL KML driver adds to every layer regardless of file content.
# They are dropped when null/unset so that only real attributes are reported.
_KML_DRIVER_FIELDS = {
    "id", "timestamp", "begin", "end", "altitudeMode", "tessellate",
    "extrude", "visibility", "drawOrder", "icon",
}
_KML_UNSET_VALUES = (-1, 0)


@dataclass
class SourceFeature:
    index: int
    layer: str | None
    geometry: BaseGeometry | None
    properties: dict[str, Any]


@dataclass
class SourceDataset:
    file_type: str  # "kml" | "shapefile"
    features: list[SourceFeature]
    crs: CRS | None
    warnings: list[str] = field(default_factory=list)


def kml_driver_available() -> bool:
    return "r" in pyogrio.list_drivers().get("KML", "")


def json_safe(value: Any) -> Any:
    """Convert pandas/numpy scalars to plain JSON-compatible Python values."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (dt.datetime, dt.date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _clean_kml_properties(props: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in props.items():
        if value is None:
            continue
        if key in _KML_DRIVER_FIELDS and (value == "" or (isinstance(value, (int, float)) and value in _KML_UNSET_VALUES)):
            continue
        cleaned[key] = value
    return cleaned


def _read_layers(path: Path, layers: list[str], file_type: str, max_features: int) -> SourceDataset:
    features: list[SourceFeature] = []
    crs: CRS | None = None
    crs_set = False
    notes: list[str] = []

    for layer in layers:
        info = pyogrio.read_info(path, layer=layer)
        declared = int(info.get("features", -1))
        if declared > 0 and len(features) + declared > max_features:
            raise UnprocessableFileError(
                f"The dataset has more than the allowed {max_features} features.", code="TOO_MANY_FEATURES"
            )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frame = gpd.read_file(path, layer=layer, engine="pyogrio")

        layer_crs = frame.crs
        if not crs_set:
            crs, crs_set = layer_crs, True
        elif (layer_crs is None) != (crs is None) or (layer_crs is not None and crs is not None and not layer_crs.equals(crs)):
            raise UnprocessableFileError(
                "Layers in the file use different coordinate reference systems, which is not supported.",
                code="MIXED_CRS",
            )

        geom_name = frame.geometry.name
        attributes = frame.drop(columns=geom_name).to_dict("records")
        for geom, props in zip(frame.geometry, attributes):
            safe_props = {str(k): json_safe(v) for k, v in props.items()}
            if file_type == "kml":
                safe_props = _clean_kml_properties(safe_props)
            features.append(
                SourceFeature(
                    index=len(features),
                    # GDAL names a folder-less KML layer after the file stem; hide that internal name.
                    layer=layer if file_type == "kml" and layer != path.stem else None,
                    geometry=geom if isinstance(geom, BaseGeometry) else None,
                    properties=safe_props,
                )
            )
        if len(features) > max_features:
            raise UnprocessableFileError(
                f"The dataset has more than the allowed {max_features} features.", code="TOO_MANY_FEATURES"
            )

    if not features:
        raise UnprocessableFileError("The file does not contain any features.", code="NO_FEATURES")
    return SourceDataset(file_type=file_type, features=features, crs=crs, warnings=notes)


def read_kml(path: Path, max_features: int) -> SourceDataset:
    if not kml_driver_available():
        raise DriverUnavailableError(
            "KML reading is not available on this server (GDAL KML driver missing).",
            code="KML_DRIVER_UNAVAILABLE",
        )
    try:
        layer_names = [str(name) for name, _ in pyogrio.list_layers(path)]
        if not layer_names:
            raise UnprocessableFileError("The KML file does not contain any features.", code="NO_FEATURES")
        dataset = _read_layers(path, layer_names, "kml", max_features)
    except _READ_ERRORS as exc:
        logger.warning("KML read failed: %s: %s", type(exc).__name__, exc)
        raise BadRequestError("The KML file could not be read as a geospatial dataset.", code="UNREADABLE_FILE") from exc
    # The KML specification fixes the CRS to WGS 84 lon/lat; GDAL reports EPSG:4326.
    return dataset


def read_shapefile(path: Path, max_features: int) -> SourceDataset:
    try:
        dataset = _read_layers(path, [path.stem], "shapefile", max_features)
    except _READ_ERRORS as exc:
        logger.warning("Shapefile read failed: %s: %s", type(exc).__name__, exc)
        raise BadRequestError(
            "The Shapefile could not be read; its files may be corrupted or inconsistent.",
            code="UNREADABLE_FILE",
        ) from exc
    if dataset.crs is None:
        dataset.warnings.append("The Shapefile has no usable CRS (.prj file missing or unreadable).")
    return dataset
