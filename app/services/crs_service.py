"""CRS description and measurement-CRS selection.

Strategy (documented in the README)
-----------------------------------
All selection is driven by the dataset's extent expressed in lon/lat
(EPSG:4326), computed separately for the features that need an area and the
features that need a length.

Area  (units: metres)
  1. Whole extent inside ONE UTM zone and between 80S and 84N -> that UTM zone
     (WGS 84 / UTM, EPSG:326xx north, EPSG:327xx south). UTM is conformal, not
     equal-area: its scale factor runs from 0.9996 (central meridian) to about
     1.001 (zone edge), so areas are off by at most ~0.2 %.
  2. Otherwise, extent no larger than 90 degrees in either direction ->
     Lambert Azimuthal Equal Area centred on the extent (exact areas).
  3. Otherwise (continental/global) -> EPSG:6933 (cylindrical equal-area).

Length (units: metres)
  1. Whole extent inside ONE UTM zone and between 80S and 84N -> that UTM zone.
  2. Otherwise -> ellipsoidal geodesic length on WGS 84 (pyproj ``Geod``).
     No single projection preserves distance over several zones, so the
     calculation is done on the ellipsoid instead of a projected plane.
"""
from __future__ import annotations

from dataclasses import dataclass

from pyproj import CRS, Geod

from app.models.schemas import CrsInfo, MeasurementCrsInfo

Bounds = tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)

UTM_MIN_LAT = -80.0
UTM_MAX_LAT = 84.0
LAEA_MAX_SPAN_DEG = 90.0
GLOBAL_EQUAL_AREA_EPSG = 6933


@dataclass(frozen=True)
class MeasurementCrs:
    info: MeasurementCrsInfo
    crs: CRS | None = None  # set for projected strategies
    geod: Geod | None = None  # set for the geodesic strategy


def crs_identifier(crs: CRS) -> str:
    authority = crs.to_authority(min_confidence=70)
    if authority:
        return f"{authority[0]}:{authority[1]}"
    return crs.name or "UNKNOWN"


def describe_crs(crs: CRS, *, assumed: bool = False) -> CrsInfo:
    if crs.is_geographic:
        kind = "geographic"
    elif crs.is_projected:
        kind = "projected"
    else:
        kind = "other"
    return CrsInfo(identifier=crs_identifier(crs), name=crs.name, type=kind, assumed=assumed)


def utm_zone(lon: float) -> int:
    return min(int((lon + 180.0) // 6) + 1, 60)


def _single_utm_epsg(bounds: Bounds) -> int | None:
    min_lon, min_lat, max_lon, max_lat = bounds
    if min_lat < UTM_MIN_LAT or max_lat > UTM_MAX_LAT:
        return None
    zone = utm_zone(min_lon)
    if utm_zone(max_lon) != zone:
        return None
    centre_lat = (min_lat + max_lat) / 2.0
    return (32600 if centre_lat >= 0 else 32700) + zone


def _utm(epsg: int, what: str) -> MeasurementCrs:
    crs = CRS.from_epsg(epsg)
    return MeasurementCrs(
        info=MeasurementCrsInfo(
            method="projected",
            strategy="utm_single_zone",
            identifier=f"EPSG:{epsg}",
            name=crs.name,
            description=f"All {what} features lie in one UTM zone; measured in {crs.name}.",
        ),
        crs=crs,
    )


def select_area_crs(bounds: Bounds) -> MeasurementCrs:
    epsg = _single_utm_epsg(bounds)
    if epsg is not None:
        return _utm(epsg, "polygon")

    min_lon, min_lat, max_lon, max_lat = bounds
    if max(max_lon - min_lon, max_lat - min_lat) <= LAEA_MAX_SPAN_DEG:
        lon0 = round((min_lon + max_lon) / 2.0, 4)
        lat0 = round((min_lat + max_lat) / 2.0, 4)
        definition = f"+proj=laea +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        return MeasurementCrs(
            info=MeasurementCrsInfo(
                method="projected",
                strategy="lambert_azimuthal_equal_area",
                identifier=definition,
                name=f"WGS 84 / Lambert Azimuthal Equal Area centred at lon={lon0}, lat={lat0}",
                description=(
                    "Polygons span several UTM zones (or lie outside UTM limits); "
                    "measured in an equal-area projection centred on the polygon extent."
                ),
            ),
            crs=CRS.from_proj4(definition),
        )

    crs = CRS.from_epsg(GLOBAL_EQUAL_AREA_EPSG)
    return MeasurementCrs(
        info=MeasurementCrsInfo(
            method="projected",
            strategy="global_cylindrical_equal_area",
            identifier=f"EPSG:{GLOBAL_EQUAL_AREA_EPSG}",
            name=crs.name,
            description="Polygons cover a continental/global extent; measured in a global equal-area projection.",
        ),
        crs=crs,
    )


def select_length_crs(bounds: Bounds) -> MeasurementCrs:
    epsg = _single_utm_epsg(bounds)
    if epsg is not None:
        return _utm(epsg, "line")
    return MeasurementCrs(
        info=MeasurementCrsInfo(
            method="geodesic",
            strategy="geodesic_wgs84",
            identifier="EPSG:4326",
            name="WGS 84 (ellipsoidal geodesic distance)",
            description=(
                "Lines span several UTM zones (or lie outside UTM limits); lengths are geodesic "
                "distances on the WGS 84 ellipsoid because no single projection preserves distance."
            ),
        ),
        geod=Geod(ellps="WGS84"),
    )
