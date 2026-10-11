"""Builders for test datasets (KML text, zipped Shapefiles, crafted ZIPs).

Everything is generated in code, so the suite needs no downloaded data. Metric
test geometries are defined in a projected CRS (UTM 43N) and converted to
lon/lat, which gives exact, independently known areas and lengths.
"""
from __future__ import annotations

import io
import tempfile
import warnings
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

import geopandas as gpd
from pyproj import Geod, Transformer
from shapely.geometry import LineString, Polygon

Coord = tuple[float, float]

UTM_EPSG = 32643  # WGS 84 / UTM zone 43N (covers central Tamil Nadu, lon 72E-78E)
_TO_LONLAT = Transformer.from_crs(UTM_EPSG, 4326, always_xy=True)
_TO_UTM = Transformer.from_crs(4326, UTM_EPSG, always_xy=True)
_GEOD = Geod(ellps="WGS84")

# An arbitrary anchor inside UTM zone 43N, in UTM metres.
ORIGIN_X, ORIGIN_Y = _TO_UTM.transform(77.70, 11.30)


# ------------------------------------------------------------ coordinates
def lonlat_from_utm(points: Iterable[Coord]) -> list[Coord]:
    return [tuple(_TO_LONLAT.transform(x, y)) for x, y in points]


def utm_rect(width_m: float, height_m: float, dx: float = 0.0, dy: float = 0.0) -> list[Coord]:
    """Closed ring (in UTM metres) of a width x height rectangle near the origin."""
    x, y = ORIGIN_X + dx, ORIGIN_Y + dy
    return [(x, y), (x + width_m, y), (x + width_m, y + height_m), (x, y + height_m), (x, y)]


def lonlat_rect(width_m: float, height_m: float, dx: float = 0.0, dy: float = 0.0) -> list[Coord]:
    return lonlat_from_utm(utm_rect(width_m, height_m, dx, dy))


def utm_line(length_m: float, dx: float = 0.0, dy: float = 0.0) -> list[Coord]:
    """East-west line of the given length (UTM metres)."""
    x, y = ORIGIN_X + dx, ORIGIN_Y + dy
    return [(x, y), (x + length_m / 2, y), (x + length_m, y)]


def lonlat_line(length_m: float, dx: float = 0.0, dy: float = 0.0) -> list[Coord]:
    return lonlat_from_utm(utm_line(length_m, dx, dy))


def geodesic_area_m2(ring: Sequence[Coord]) -> float:
    """Independent reference: area on the WGS 84 ellipsoid (no projection involved)."""
    lons, lats = zip(*ring)
    area, _ = _GEOD.polygon_area_perimeter(lons, lats)
    return abs(area)


def geodesic_length_m(line: Sequence[Coord]) -> float:
    lons, lats = zip(*line)
    return _GEOD.line_length(lons, lats)


# ------------------------------------------------------------------- KML
def _coords_text(coords: Sequence[Sequence[float]]) -> str:
    return " ".join(",".join(repr(float(v)) for v in c) for c in coords)


def kml_point(lon: float, lat: float) -> str:
    return f"<Point><coordinates>{lon!r},{lat!r}</coordinates></Point>"


def kml_line(coords: Sequence[Sequence[float]]) -> str:
    return f"<LineString><coordinates>{_coords_text(coords)}</coordinates></LineString>"


def kml_polygon(ring: Sequence[Sequence[float]], holes: Sequence[Sequence[Sequence[float]]] = ()) -> str:
    inner = "".join(
        f"<innerBoundaryIs><LinearRing><coordinates>{_coords_text(h)}</coordinates></LinearRing></innerBoundaryIs>"
        for h in holes
    )
    return (
        "<Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{_coords_text(ring)}</coordinates></LinearRing></outerBoundaryIs>{inner}</Polygon>"
    )


def kml_multi(*geometries: str) -> str:
    return f"<MultiGeometry>{''.join(geometries)}</MultiGeometry>"


def kml_placemark(name: str, geometry: str | None, data: dict[str, str] | None = None) -> str:
    extended = ""
    if data:
        items = "".join(f'<Data name="{k}"><value>{v}</value></Data>' for k, v in data.items())
        extended = f"<ExtendedData>{items}</ExtendedData>"
    return f"<Placemark><name>{name}</name>{extended}{geometry or ''}</Placemark>"


def kml_document(*placemarks: str) -> bytes:
    body = "".join(placemarks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>test</name>'
        f"{body}</Document></kml>"
    ).encode("utf-8")


# ------------------------------------------------------------- Shapefiles
def shapefile_parts(gdf: gpd.GeoDataFrame, stem: str = "data") -> dict[str, bytes]:
    """Write a GeoDataFrame as a Shapefile and return {filename: bytes} for every component."""
    with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # pyogrio warns when deliberately writing without a CRS
        gdf.to_file(Path(tmp) / f"{stem}.shp", driver="ESRI Shapefile", engine="pyogrio")
        return {p.name: p.read_bytes() for p in sorted(Path(tmp).iterdir())}


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def shapefile_zip(
    gdf: gpd.GeoDataFrame,
    *,
    stem: str = "data",
    folder: str = "",
    drop: Sequence[str] = (),
) -> bytes:
    """ZIP of a Shapefile; ``folder`` nests it in a directory, ``drop`` removes extensions."""
    parts = shapefile_parts(gdf, stem)
    prefix = f"{folder.strip('/')}/" if folder else ""
    return make_zip(
        {f"{prefix}{name}": data for name, data in parts.items() if Path(name).suffix.lower() not in drop}
    )


def polygon_frame(
    *,
    crs: str | int | None = UTM_EPSG,
    sizes: Sequence[tuple[float, float]] = ((1000.0, 2000.0),),
    names: Sequence[str] | None = None,
) -> gpd.GeoDataFrame:
    """Rectangles laid out side by side, in UTM metres (so areas are exactly w * h)."""
    geoms = [Polygon(utm_rect(w, h, dx=i * 10_000.0)) for i, (w, h) in enumerate(sizes)]
    names = names or [f"parcel-{i}" for i in range(len(geoms))]
    return gpd.GeoDataFrame({"name": list(names), "seq": list(range(len(geoms)))}, geometry=geoms, crs=crs)


def line_frame(*, crs: str | int | None = UTM_EPSG, lengths: Sequence[float] = (3000.0,)) -> gpd.GeoDataFrame:
    geoms = [LineString(utm_line(n, dy=i * 1000.0)) for i, n in enumerate(lengths)]
    return gpd.GeoDataFrame(
        {"name": [f"road-{i}" for i in range(len(geoms))]}, geometry=geoms, crs=crs
    )

