"""Create small demo datasets to try the API with.

Run from the project root:

    python sample_data/generate_samples.py

Everything is built from the same factories the test suite uses, so the
expected values below are exact and independently known.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
from shapely.geometry import MultiPolygon, Point, Polygon  # noqa: E402

from tests import factories as f  # noqa: E402

OUT = Path(__file__).resolve().parent


def write(name: str, content: bytes) -> None:
    (OUT / name).write_bytes(content)
    print(f"  wrote sample_data/{name}")


def main() -> None:
    print("Generating sample datasets...")

    # 1. Mixed KML: polygon, line, point, multi-geometry and two deliberately bad features.
    bowtie = [(77.70, 11.30), (77.71, 11.31), (77.71, 11.30), (77.70, 11.31), (77.70, 11.30)]
    write("survey.kml", f.kml_document(
        f.kml_placemark("Field A", f.kml_polygon(f.lonlat_rect(1000, 2000)), {"crop": "rice"}),   # 2.0 km2
        f.kml_placemark("Canal", f.kml_line(f.lonlat_line(3000)), {"kind": "irrigation"}),        # 3.0 km
        f.kml_placemark("Well 1", f.kml_point(77.70, 11.30)),                                      # not measured
        f.kml_placemark("Two ponds", f.kml_multi(
            f.kml_polygon(f.lonlat_rect(100, 100, dx=4000)), f.kml_polygon(f.lonlat_rect(100, 200, dx=5000)))),
        f.kml_placemark("Bad bowtie", f.kml_polygon(bowtie)),                                      # invalid -> FAILED
        f.kml_placemark("No geometry", None),                                                      # null -> FAILED
    ))

    # 2. Zipped Shapefile of polygons in UTM 43N (1 km x 2 km and 500 m x 500 m).
    parcels = f.polygon_frame(sizes=[(1000, 2000), (500, 500)], names=["North field", "South field"])
    write("parcels.zip", f.shapefile_zip(parcels, stem="parcels"))

    # 3. Same data nested in sub-folders, as many GIS exports are.
    write("parcels_nested.zip", f.shapefile_zip(parcels, stem="parcels", folder="export/2026/final"))

    # 4. Lines in EPSG:4326 (geographic input) and a MultiPolygon + Point shapefile.
    write("roads_wgs84.zip", f.shapefile_zip(f.line_frame(lengths=[1500, 4000]).to_crs(4326), stem="roads"))
    multi = MultiPolygon([Polygon(f.utm_rect(100, 100)), Polygon(f.utm_rect(200, 300, dx=5000))])
    write("multipolygon.zip", f.shapefile_zip(
        gpd.GeoDataFrame({"id": [1]}, geometry=[multi], crs=f.UTM_EPSG), stem="multi"))
    write("points.zip", f.shapefile_zip(
        gpd.GeoDataFrame({"label": ["well-7"]}, geometry=[Point(f.ORIGIN_X, f.ORIGIN_Y)], crs=f.UTM_EPSG), stem="points"))

    # 5. A Shapefile without a CRS: accepted, but never measured.
    write("no_crs.zip", f.shapefile_zip(f.polygon_frame(crs=None), stem="no_crs"))

    print("Done.")


if __name__ == "__main__":
    main()
