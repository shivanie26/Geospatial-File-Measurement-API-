"""Coordinate reference system handling: detection, selection strategy and failure modes."""
from __future__ import annotations

import numpy as np
import pytest
from pyproj import CRS, Geod
from shapely.geometry import Polygon

from app.config import Settings
from app.models.schemas import FeatureStatus
from app.services import crs_service
from app.services.geospatial_service import SourceDataset, SourceFeature
from app.services.measurement_service import measure_dataset
from tests import factories as f
from tests.helpers import get_measurements, upload_ok


def kml_of(*geometries: str) -> bytes:
    return f.kml_document(*[f.kml_placemark(f"g{i}", g) for i, g in enumerate(geometries)])


# ---------------------------------------------------- geographic input (4326)
def test_epsg4326_kml_is_transformed_to_utm_before_measuring(client):
    ring = f.lonlat_rect(1000, 2000)
    result = get_measurements(client, upload_ok(client, "a.kml", kml_of(f.kml_polygon(ring)))["id"])

    assert result["source_crs"]["identifier"] == "EPSG:4326"
    assert result["source_crs"]["type"] == "geographic"
    area_crs = result["measurement_crs"]["area"]
    assert area_crs["method"] == "projected"
    assert area_crs["strategy"] == "utm_single_zone"
    assert area_crs["identifier"] == "EPSG:32643"
    assert area_crs["units"] == "metre"
    assert result["features"][0]["source_crs"] == "EPSG:4326"       # original CRS is preserved on the feature


def test_southern_hemisphere_uses_a_327xx_utm_zone(client):
    lon0, lat0 = 151.20, -33.87                                      # Sydney, UTM zone 56 south
    d = 0.005
    ring = [(lon0, lat0), (lon0 + d, lat0), (lon0 + d, lat0 + d), (lon0, lat0 + d), (lon0, lat0)]
    result = get_measurements(client, upload_ok(client, "s.kml", kml_of(f.kml_polygon(ring)))["id"])

    assert result["measurement_crs"]["area"]["identifier"] == "EPSG:32756"
    area = result["features"][0]["measurements"]["area_sq_m"]
    assert area == pytest.approx(f.geodesic_area_m2(ring), rel=3e-3)


# --------------------------------------------------- projected / other input
@pytest.mark.parametrize("epsg", [3857, 32644, 4326])
def test_input_in_other_crs_gives_true_ground_area(client, epsg):
    """Web Mercator inflates area ~8 % at this latitude; measuring must undo that."""
    gdf = f.polygon_frame(sizes=[(1000, 2000)]).to_crs(epsg)
    result = get_measurements(client, upload_ok(client, "p.zip", f.shapefile_zip(gdf))["id"])

    assert result["source_crs"]["identifier"] == f"EPSG:{epsg}"
    assert result["measurement_crs"]["area"]["identifier"] == "EPSG:32643"
    assert result["features"][0]["measurements"]["area_sq_m"] == pytest.approx(2_000_000.0, rel=2e-4)


def test_projected_source_crs_is_reported_with_its_type(client):
    result = get_measurements(client, upload_ok(client, "p.zip", f.shapefile_zip(f.polygon_frame()))["id"])
    assert result["source_crs"]["type"] == "projected"
    assert result["source_crs"]["assumed"] is False


# ------------------------------------------- datasets spanning several zones
def test_polygon_spanning_two_utm_zones_uses_local_equal_area(client):
    ring = [(71.90, 11.0), (72.10, 11.0), (72.10, 11.2), (71.90, 11.2), (71.90, 11.0)]  # zone 42 | 43 boundary
    result = get_measurements(client, upload_ok(client, "z.kml", kml_of(f.kml_polygon(ring)))["id"])

    area_crs = result["measurement_crs"]["area"]
    assert area_crs["strategy"] == "lambert_azimuthal_equal_area"
    assert "laea" in area_crs["identifier"]
    assert result["features"][0]["measurements"]["area_sq_m"] == pytest.approx(f.geodesic_area_m2(ring), rel=1e-3)


def test_line_spanning_two_utm_zones_uses_geodesic_length(client):
    line = [(71.0, 11.0), (72.0, 11.5), (73.0, 11.0)]
    result = get_measurements(client, upload_ok(client, "l.kml", kml_of(f.kml_line(line)))["id"])

    length_crs = result["measurement_crs"]["length"]
    assert length_crs["method"] == "geodesic"
    assert length_crs["strategy"] == "geodesic_wgs84"
    assert result["features"][0]["measurements"]["length_m"] == pytest.approx(f.geodesic_length_m(line), rel=1e-9)


def test_continental_polygon_uses_global_equal_area_projection(client):
    # A lon/lat box 110 degrees wide: equal-area answer is the exact band-zone area on the ellipsoid.
    lons = np.linspace(20, 130, 221)
    ring = [(x, -10.0) for x in lons] + [(130.0, 10.0)] + [(x, 10.0) for x in lons[::-1]] + [(20.0, -10.0)]
    result = get_measurements(client, upload_ok(client, "c.kml", kml_of(f.kml_polygon(ring)))["id"])

    area_crs = result["measurement_crs"]["area"]
    assert area_crs["strategy"] == "global_cylindrical_equal_area"
    assert area_crs["identifier"] == "EPSG:6933"
    lats = [lat for _, lat in ring]
    reference, _ = Geod(ellps="WGS84").polygon_area_perimeter([x for x, _ in ring], lats)
    assert result["features"][0]["measurements"]["area_sq_m"] == pytest.approx(abs(reference), rel=5e-3)


def test_different_geometry_kinds_may_use_different_measurement_crs(client):
    # Polygon sits in one zone; the line crosses a zone boundary. Each kind gets its own CRS.
    content = kml_of(
        f.kml_polygon(f.lonlat_rect(500, 500)),
        f.kml_line([(71.0, 11.0), (73.0, 11.0)]),
    )
    result = get_measurements(client, upload_ok(client, "mix.kml", content)["id"])
    assert result["measurement_crs"]["area"]["strategy"] == "utm_single_zone"
    assert result["measurement_crs"]["length"]["strategy"] == "geodesic_wgs84"


# ----------------------------------------------------------- selection units
def test_zone_boundaries():
    assert crs_service.utm_zone(-180.0) == 1
    assert crs_service.utm_zone(77.7) == 43
    assert crs_service.utm_zone(179.999) == 60
    assert crs_service.utm_zone(180.0) == 60


def test_extent_near_the_pole_falls_back_from_utm():
    chosen = crs_service.select_area_crs((10.0, 85.0, 11.0, 86.0))
    assert chosen.info.strategy == "lambert_azimuthal_equal_area"


def test_extent_straddling_the_equator_gets_a_valid_utm_zone():
    chosen = crs_service.select_area_crs((77.0, -0.5, 77.5, 0.5))
    assert chosen.info.strategy == "utm_single_zone"
    assert chosen.crs.is_projected


# ------------------------------------------------------------- missing CRS
def test_missing_crs_is_never_assumed_and_nothing_is_fabricated(client):
    archive = f.shapefile_zip(f.polygon_frame(crs=None, sizes=[(1000, 2000)]))
    body = upload_ok(client, "nocrs.zip", archive)
    result = get_measurements(client, body["id"])

    assert body["crs"] is None and body["status"] == "FAILED"
    assert result["source_crs"] is None
    assert result["measurement_crs"] == {"area": None, "length": None}
    feature = result["features"][0]
    assert feature["status"] == "FAILED"
    assert feature["errors"][0]["code"] == "CRS_MISSING"
    assert "coordinate reference system" in feature["errors"][0]["message"]
    assert feature["measurements"]["area_sq_m"] is None            # not 0, not a guess
    assert feature["source_crs"] is None


def test_missing_crs_still_returns_points_and_geometry(client):
    from shapely.geometry import Point
    import geopandas as gpd

    gdf = gpd.GeoDataFrame({"n": ["p"]}, geometry=[Point(1.0, 2.0)], crs=None)
    feature = get_measurements(client, upload_ok(client, "pts.zip", f.shapefile_zip(gdf))["id"])["features"][0]
    assert feature["status"] == "NOT_APPLICABLE"
    assert feature["geometry"]["coordinates"] == pytest.approx([1.0, 2.0])


def test_assumed_crs_is_opt_in_configured_and_flagged(make_client):
    client = make_client(assume_crs_if_missing="EPSG:32643")
    archive = f.shapefile_zip(f.polygon_frame(crs=None, sizes=[(1000, 2000)]))
    body = upload_ok(client, "nocrs.zip", archive)
    result = get_measurements(client, body["id"])

    assert body["status"] == "COMPLETED"
    assert result["source_crs"]["identifier"] == "EPSG:32643"
    assert result["source_crs"]["assumed"] is True
    assert result["features"][0]["measurements"]["area_sq_m"] == pytest.approx(2_000_000.0, abs=0.01)
    assert any("assumed" in w for w in result["warnings"])


def test_invalid_assumed_crs_setting_is_rejected_at_startup():
    with pytest.raises(ValueError):
        Settings(_env_file=None, assume_crs_if_missing="EPSG:not-a-crs")


# ----------------------------------------------------------- failure modes
def _dataset(geometries, crs) -> SourceDataset:
    return SourceDataset(
        file_type="shapefile",
        features=[SourceFeature(index=i, layer=None, geometry=g, properties={}) for i, g in enumerate(geometries)],
        crs=crs,
    )


def test_crs_that_cannot_be_transformed_fails_features_not_the_run():
    engineering = CRS.from_wkt(
        'ENGCRS["site grid",EDATUM["site datum"],CS[Cartesian,2],'
        'AXIS["x",east,ORDER[1]],AXIS["y",north,ORDER[2]],LENGTHUNIT["metre",1]]'
    )
    outcome = measure_dataset(
        _dataset([Polygon([(0, 0), (10, 0), (10, 10), (0, 0)])], engineering),
        Settings(_env_file=None, assume_crs_if_missing=None),
    )
    feature = outcome.features[0]
    assert feature.status == FeatureStatus.FAILED
    assert feature.errors[0].code == "CRS_TRANSFORMATION_UNSUPPORTED"
    assert feature.area_sq_m is None


def test_out_of_range_geographic_coordinates_fail_the_feature(client):
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(
        {"n": ["bad", "good"]},
        geometry=[
            Polygon([(200, 10), (201, 10), (201, 11), (200, 11), (200, 10)]),
            Polygon(f.lonlat_rect(500, 500)),
        ],
        crs=4326,
    )
    features = get_measurements(client, upload_ok(client, "oor.zip", f.shapefile_zip(gdf))["id"])["features"]
    assert features[0]["status"] == "FAILED"
    assert features[0]["errors"][0]["code"] == "COORDINATES_OUT_OF_RANGE"
    assert features[1]["status"] == "MEASURED"
