"""GET /api/files/{id}/measurements/ - values, units, statuses and pagination."""
from __future__ import annotations

import json
import math

import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon

from app.config import Settings
from app.models.schemas import FeatureStatus
from app.services.geospatial_service import SourceDataset, SourceFeature
from app.services.measurement_service import measure_dataset
from pyproj import CRS
from tests import factories as f
from tests.helpers import assert_error, by_index, get_measurements, upload, upload_ok


# ------------------------------------------------------------------ areas
def test_polygon_area_matches_known_value_from_a_kml(client):
    ring = f.lonlat_rect(1000, 2000)  # 1 km x 2 km, defined in UTM metres
    result = get_measurements(client, upload_ok(client, "p.kml", f.kml_document(f.kml_placemark("p", f.kml_polygon(ring))))["id"])
    feature = result["features"][0]

    assert feature["geometry_type"] == "Polygon"
    assert feature["status"] == "MEASURED"
    assert feature["measurements"]["area_sq_m"] == pytest.approx(2_000_000.0, abs=0.01)
    assert feature["measurements"]["area_sq_km"] == pytest.approx(2.0, abs=1e-8)
    assert feature["measurements"]["length_m"] is None
    # Independent cross-check on the ellipsoid (no projection). UTM is conformal, not equal-area:
    # its area error is ~0.14 % at this site and at most ~0.2 % at a zone edge, hence rel=3e-3.
    assert feature["measurements"]["area_sq_m"] == pytest.approx(f.geodesic_area_m2(ring), rel=3e-3)


def test_area_is_not_computed_from_degrees(client):
    ring = f.lonlat_rect(1000, 1000)
    result = get_measurements(client, upload_ok(client, "p.kml", f.kml_document(f.kml_placemark("p", f.kml_polygon(ring))))["id"])
    area = result["features"][0]["measurements"]["area_sq_m"]
    # In degrees the same polygon would be ~8e-5 "square degrees"; metres must be ~1e6.
    assert area > 900_000


def test_polygon_hole_is_subtracted(client):
    outer = f.lonlat_rect(1000, 1000)
    hole = f.lonlat_rect(200, 200, dx=400, dy=400)
    content = f.kml_document(f.kml_placemark("donut", f.kml_polygon(outer, holes=[hole])))
    area = get_measurements(client, upload_ok(client, "d.kml", content)["id"])["features"][0]["measurements"]["area_sq_m"]
    assert area == pytest.approx(1_000_000 - 40_000, abs=0.01)


def test_third_coordinate_is_ignored_for_area(client):
    ring3d = [(x, y, 250.0) for x, y in f.lonlat_rect(100, 100)]
    content = f.kml_document(f.kml_placemark("z", f.kml_polygon(ring3d)))
    feature = get_measurements(client, upload_ok(client, "z.kml", content)["id"])["features"][0]
    assert feature["measurements"]["area_sq_m"] == pytest.approx(10_000.0, abs=0.01)


# ---------------------------------------------------------------- lengths
def test_line_length_matches_known_value_from_a_kml(client):
    line = f.lonlat_line(3000)
    result = get_measurements(client, upload_ok(client, "l.kml", f.kml_document(f.kml_placemark("l", f.kml_line(line))))["id"])
    feature = result["features"][0]

    assert feature["geometry_type"] == "LineString"
    assert feature["measurements"]["length_m"] == pytest.approx(3000.0, abs=0.01)
    assert feature["measurements"]["length_km"] == pytest.approx(3.0, abs=1e-6)
    assert feature["measurements"]["area_sq_m"] is None
    assert feature["measurements"]["length_m"] == pytest.approx(f.geodesic_length_m(line), rel=3e-3)  # UTM scale error, see area test


def test_line_length_from_a_shapefile_in_geographic_crs(client):
    gdf = f.line_frame(lengths=[1500.0, 4000.0]).to_crs(4326)
    result = get_measurements(client, upload_ok(client, "roads.zip", f.shapefile_zip(gdf))["id"])
    lengths = [feat["measurements"]["length_m"] for feat in result["features"]]
    assert lengths == [pytest.approx(1500.0, abs=0.01), pytest.approx(4000.0, abs=0.01)]


def test_polyline_with_several_vertices_sums_its_segments(client):
    pts = [(f.ORIGIN_X, f.ORIGIN_Y), (f.ORIGIN_X + 300, f.ORIGIN_Y), (f.ORIGIN_X + 300, f.ORIGIN_Y + 400)]
    content = f.kml_document(f.kml_placemark("bend", f.kml_line(f.lonlat_from_utm(pts))))
    length = get_measurements(client, upload_ok(client, "b.kml", content)["id"])["features"][0]["measurements"]["length_m"]
    assert length == pytest.approx(700.0, abs=0.01)


# ----------------------------------------------------------------- points
def test_points_are_not_measured_and_return_null_not_zero(client):
    content = f.kml_document(
        f.kml_placemark("one", f.kml_point(77.7, 11.3)),
        f.kml_placemark("many", f.kml_multi(f.kml_point(77.7, 11.3), f.kml_point(77.8, 11.3))),
    )
    result = get_measurements(client, upload_ok(client, "pts.kml", content)["id"])

    assert [feat["geometry_type"] for feat in result["features"]] == ["Point", "MultiPoint"]
    for feature in result["features"]:
        assert feature["status"] == "NOT_APPLICABLE"
        assert feature["measurements"] == {"area_sq_m": None, "area_sq_km": None, "length_m": None, "length_km": None}
        assert feature["errors"] == []
        assert feature["geometry"] is not None
    assert result["status"] == "COMPLETED"  # points are not failures
    assert result["measurement_crs"] == {"area": None, "length": None}


def test_point_geometry_and_properties_are_preserved(client):
    gdf = gpd.GeoDataFrame({"label": ["well-7"], "depth": [42.5]}, geometry=[Point(f.ORIGIN_X, f.ORIGIN_Y)], crs=f.UTM_EPSG)
    feature = get_measurements(client, upload_ok(client, "pts.zip", f.shapefile_zip(gdf))["id"])["features"][0]

    assert feature["geometry"]["type"] == "Point"
    assert feature["geometry"]["coordinates"] == pytest.approx([f.ORIGIN_X, f.ORIGIN_Y])
    assert feature["properties"] == {"label": "well-7", "depth": 42.5}
    assert feature["source_crs"] == f"EPSG:{f.UTM_EPSG}"


# ------------------------------------------------------------ multi types
def test_multipolygon_area_is_the_sum_of_its_parts(client):
    multi = MultiPolygon([Polygon(f.utm_rect(100, 100)), Polygon(f.utm_rect(200, 300, dx=5000))])
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[multi], crs=f.UTM_EPSG)
    feature = get_measurements(client, upload_ok(client, "m.zip", f.shapefile_zip(gdf))["id"])["features"][0]

    assert feature["geometry_type"] == "MultiPolygon"
    assert feature["measurements"]["area_sq_m"] == pytest.approx(10_000 + 60_000, abs=0.01)


def test_multilinestring_length_is_the_sum_of_its_parts(client):
    multi = MultiLineString([LineString(f.utm_line(1000)), LineString(f.utm_line(2500, dy=500))])
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[multi], crs=f.UTM_EPSG)
    feature = get_measurements(client, upload_ok(client, "m.zip", f.shapefile_zip(gdf))["id"])["features"][0]

    assert feature["geometry_type"] == "MultiLineString"
    assert feature["measurements"]["length_m"] == pytest.approx(3500.0, abs=0.01)


def test_kml_multigeometry_polygons_and_lines(client):
    content = f.kml_document(
        f.kml_placemark("mp", f.kml_multi(f.kml_polygon(f.lonlat_rect(500, 500)), f.kml_polygon(f.lonlat_rect(500, 500, dx=5000)))),
        f.kml_placemark("ml", f.kml_multi(f.kml_line(f.lonlat_line(1000)), f.kml_line(f.lonlat_line(2000, dy=500)))),
    )
    features = get_measurements(client, upload_ok(client, "multi.kml", content)["id"])["features"]

    assert features[0]["geometry_type"] == "MultiPolygon"
    assert features[0]["measurements"]["area_sq_m"] == pytest.approx(500_000.0, abs=0.01)
    assert features[1]["geometry_type"] == "MultiLineString"
    assert features[1]["measurements"]["length_m"] == pytest.approx(3000.0, abs=0.01)


def test_geometry_collection_measures_polygon_part_and_warns_about_the_rest(client):
    content = f.kml_document(
        f.kml_placemark("gc", f.kml_multi(f.kml_polygon(f.lonlat_rect(500, 500)), f.kml_point(77.7, 11.3)))
    )
    feature = get_measurements(client, upload_ok(client, "gc.kml", content)["id"])["features"][0]

    assert feature["geometry_type"] == "GeometryCollection"
    assert feature["status"] == "MEASURED"
    assert feature["measurements"]["area_sq_m"] == pytest.approx(250_000.0, abs=0.01)
    assert [w["code"] for w in feature["warnings"]] == ["MEMBERS_IGNORED"]


# ------------------------------------------------- mixed types and failures
def mixed_kml() -> bytes:
    bowtie = [(77.70, 11.30), (77.71, 11.31), (77.71, 11.30), (77.70, 11.31), (77.70, 11.30)]
    return f.kml_document(
        f.kml_placemark("field", f.kml_polygon(f.lonlat_rect(1000, 2000)), {"crop": "rice"}),  # 0
        f.kml_placemark("road", f.kml_line(f.lonlat_line(3000))),                                # 1
        f.kml_placemark("well", f.kml_point(77.7, 11.3)),                                        # 2
        f.kml_placemark("bowtie", f.kml_polygon(bowtie)),                                        # 3  invalid
        f.kml_placemark("empty", None),                                                          # 4  no geometry
        f.kml_placemark("pond", f.kml_polygon(f.lonlat_rect(100, 100, dx=9000))),                # 5
    )


def test_mixed_geometry_file_keeps_order_and_measures_each_type(client):
    result = get_measurements(client, upload_ok(client, "mixed.kml", mixed_kml())["id"])

    assert [feat["feature_index"] for feat in result["features"]] == [0, 1, 2, 3, 4, 5]
    assert [feat["geometry_type"] for feat in result["features"]] == [
        "Polygon", "LineString", "Point", "Polygon", None, "Polygon",
    ]
    assert [feat["status"] for feat in result["features"]] == [
        "MEASURED", "MEASURED", "NOT_APPLICABLE", "FAILED", "FAILED", "MEASURED",
    ]
    assert result["processing_summary"]["by_geometry_type"] == {"Polygon": 3, "LineString": 1, "Point": 1, "None": 1}
    assert result["processing_summary"]["total_area_sq_m"] == pytest.approx(2_010_000.0, abs=0.01)
    assert result["processing_summary"]["total_length_m"] == pytest.approx(3000.0, abs=0.01)


def test_one_bad_feature_does_not_fail_the_file(client):
    result = get_measurements(client, upload_ok(client, "mixed.kml", mixed_kml())["id"])
    features = by_index(result)

    assert result["status"] == "PARTIAL"
    assert result["feature_count"] == 6
    assert result["failed_feature_count"] == 2
    assert result["successful_feature_count"] == 4

    assert features[3]["errors"][0]["code"] == "INVALID_GEOMETRY"
    assert "Self-intersection" in features[3]["errors"][0]["message"]
    assert features[4]["errors"][0]["code"] == "NULL_GEOMETRY"
    for failed in (features[3], features[4]):
        assert failed["measurements"]["area_sq_m"] is None   # never a fabricated 0
        assert failed["measurements"]["length_m"] is None
    assert features[0]["errors"] == [] and features[5]["errors"] == []


def test_invalid_shapefile_geometry_is_reported_per_feature(client):
    gdf = gpd.GeoDataFrame(
        {"n": ["ok", "null", "bowtie"]},
        geometry=[Polygon(f.utm_rect(1000, 2000)), None, Polygon([(0, 0), (10, 10), (10, 0), (0, 10), (0, 0)])],
        crs=f.UTM_EPSG,
    )
    result = get_measurements(client, upload_ok(client, "x.zip", f.shapefile_zip(gdf))["id"])
    codes = [[e["code"] for e in feat["errors"]] for feat in result["features"]]

    assert codes == [[], ["NULL_GEOMETRY"], ["INVALID_GEOMETRY"]]
    assert result["features"][0]["measurements"]["area_sq_m"] == pytest.approx(2_000_000.0, abs=0.01)


def _dataset(geometries, crs="EPSG:32643") -> SourceDataset:
    return SourceDataset(
        file_type="shapefile",
        features=[SourceFeature(index=i, layer=None, geometry=g, properties={"i": i}) for i, g in enumerate(geometries)],
        crs=CRS.from_user_input(crs) if crs else None,
    )


def _measure(geometries, **kwargs):
    return measure_dataset(_dataset(geometries, **kwargs), Settings(_env_file=None, assume_crs_if_missing=None))


def test_empty_geometries_fail_cleanly():
    outcome = _measure([Polygon(), LineString(), Point()])
    assert [feat.status for feat in outcome.features] == [FeatureStatus.FAILED] * 3
    assert [feat.errors[0].code for feat in outcome.features] == ["EMPTY_GEOMETRY"] * 3
    assert all(feat.area_sq_m is None and feat.length_m is None for feat in outcome.features)


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_non_finite_coordinates_fail_cleanly():
    outcome = _measure([Polygon([(0, 0), (math.nan, 1), (1, 1), (0, 0)])])
    assert outcome.features[0].status == FeatureStatus.FAILED
    assert outcome.features[0].errors[0].code == "INVALID_COORDINATES"


def test_unsupported_geometry_type_is_labelled_unsupported_not_zero():
    from shapely.geometry import LinearRing

    outcome = _measure([LinearRing([(0, 0), (1, 0), (1, 1), (0, 0)])])
    feature = outcome.features[0]
    assert feature.status == FeatureStatus.UNSUPPORTED
    assert feature.errors[0].code == "UNSUPPORTED_GEOMETRY"
    assert feature.area_sq_m is None and feature.length_m is None


def test_unsupported_features_are_counted_separately_in_the_file_record():
    from shapely.geometry import LinearRing

    from app.services.measurement_service import build_file_record
    from datetime import datetime, timezone

    outcome = _measure([Polygon(f.utm_rect(10, 10)), LinearRing([(0, 0), (1, 0), (1, 1), (0, 0)])])
    record = build_file_record(
        file_id="a" * 32, filename="x.zip", file_type="shapefile", uploaded_at=datetime.now(timezone.utc), outcome=outcome
    )
    assert (record.successful_feature_count, record.failed_feature_count, record.unsupported_feature_count) == (1, 0, 1)
    assert record.status.value == "PARTIAL"


# --------------------------------------------- original data is preserved
def test_original_geometry_properties_and_crs_are_preserved(client):
    gdf = f.polygon_frame(sizes=[(1000, 2000), (50, 60)], names=["alpha", "beta"])
    result = get_measurements(client, upload_ok(client, "p.zip", f.shapefile_zip(gdf))["id"])

    for feature, (_, row) in zip(result["features"], gdf.iterrows()):
        assert feature["source_crs"] == f"EPSG:{f.UTM_EPSG}"          # original CRS, not the measurement CRS
        assert feature["properties"] == {"name": row["name"], "seq": int(row["seq"])}
        returned = Polygon(feature["geometry"]["coordinates"][0])
        # Shapefile writers re-orient rings, so compare normalised geometry, not vertex order.
        assert returned.normalize().equals_exact(row.geometry.normalize(), 1e-6)


def test_kml_properties_are_extracted(client):
    content = f.kml_document(f.kml_placemark("Field A", f.kml_polygon(f.lonlat_rect(10, 10)), {"crop": "wheat", "owner": "R"}))
    properties = get_measurements(client, upload_ok(client, "p.kml", content)["id"])["features"][0]["properties"]
    assert properties["Name"] == "Field A"
    assert properties["crop"] == "wheat" and properties["owner"] == "R"


# ------------------------------------------------- precision, units, schema
def test_full_precision_is_stored_and_rounding_happens_only_when_presenting(client, storage_dir):
    gdf = f.polygon_frame(sizes=[(123.456789, 987.654321)])
    body = upload_ok(client, "precise.zip", f.shapefile_zip(gdf))
    stored = json.loads((storage_dir / "results" / f"{body['id']}.json").read_text())["features"][0]["area_sq_m"]
    presented = get_measurements(client, body["id"])["features"][0]["measurements"]["area_sq_m"]

    assert stored == pytest.approx(123.456789 * 987.654321, rel=1e-9)
    assert stored != round(stored, 4)            # the stored value has more digits than the API shows
    assert presented == round(stored, 4)


def test_response_schema_and_units(client):
    body = upload_ok(client, "mixed.kml", mixed_kml())
    result = get_measurements(client, body["id"])

    assert set(result) == {
        "file_id", "filename", "status", "feature_count", "successful_feature_count", "failed_feature_count",
        "unsupported_feature_count", "source_crs", "measurement_crs", "units", "processing_summary", "warnings",
        "pagination", "features",
    }
    assert result["file_id"] == body["id"] and result["filename"] == "mixed.kml"
    assert result["units"] == {
        "area_sq_m": "square metres", "area_sq_km": "square kilometres", "length_m": "metres", "length_km": "kilometres",
    }
    assert result["source_crs"] == {"identifier": "EPSG:4326", "name": "WGS 84", "type": "geographic", "assumed": False}
    assert result["measurement_crs"]["area"]["identifier"] == f"EPSG:{f.UTM_EPSG}"
    assert result["measurement_crs"]["area"]["units"] == "metre"

    for feature in result["features"]:
        assert set(feature) == {
            "feature_index", "layer", "geometry_type", "geometry", "source_crs", "properties",
            "status", "measurements", "errors", "warnings",
        }
        m = feature["measurements"]
        assert set(m) == {"area_sq_m", "area_sq_km", "length_m", "length_km"}
        if m["area_sq_m"] is not None:
            assert m["area_sq_km"] == pytest.approx(m["area_sq_m"] / 1e6, abs=1e-9)
        if m["length_m"] is not None:
            assert m["length_km"] == pytest.approx(m["length_m"] / 1000, abs=1e-6)


# --------------------------------------------------------------- pagination
@pytest.fixture
def points_file(client) -> str:
    placemarks = [f.kml_placemark(f"p{i}", f.kml_point(77.7 + i * 0.001, 11.3), {"n": str(i)}) for i in range(25)]
    return upload_ok(client, "many.kml", f.kml_document(*placemarks))["id"]


def test_pagination_returns_every_feature_exactly_once(client, points_file):
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = get_measurements(client, points_file, limit=10, offset=offset)
        pagination = page["pagination"]
        assert pagination["total"] == 25 and pagination["limit"] == 10 and pagination["offset"] == offset
        assert pagination["returned"] == len(page["features"])
        seen += [feat["feature_index"] for feat in page["features"]]
        offset, pages = pagination["next_offset"], pages + 1
        assert pagination["has_more"] == (offset is not None)

    assert pages == 3
    assert seen == list(range(25))                         # nothing omitted, nothing repeated, order preserved


def test_totals_are_for_the_whole_file_not_the_page(client, points_file):
    page = get_measurements(client, points_file, limit=5, offset=20)
    assert page["feature_count"] == 25
    assert page["successful_feature_count"] == 25
    assert page["processing_summary"]["by_geometry_type"] == {"Point": 25}
    assert len(page["features"]) == 5 and page["pagination"]["has_more"] is False


def test_offset_past_the_end_returns_an_empty_page(client, points_file):
    page = get_measurements(client, points_file, limit=10, offset=100)
    assert page["features"] == []
    assert page["pagination"] == {"total": 25, "limit": 10, "offset": 100, "returned": 0, "has_more": False, "next_offset": None}


def test_default_page_size_applies_and_is_reported(make_client):
    client = make_client(default_page_size=7)
    file_id = upload_ok(client, "many.kml", f.kml_document(*[f.kml_placemark(f"p{i}", f.kml_point(77.7, 11.3)) for i in range(10)]))["id"]
    page = get_measurements(client, file_id)
    assert page["pagination"]["limit"] == 7 and len(page["features"]) == 7 and page["pagination"]["next_offset"] == 7


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": -1}, {"offset": -1}, {"limit": "abc"}])
def test_invalid_pagination_parameters_are_rejected(client, points_file, params):
    assert_error(client.get(f"/api/files/{points_file}/measurements/", params=params), 422, "VALIDATION_ERROR")


def test_limit_above_the_configured_maximum_is_rejected_not_silently_clamped(make_client):
    client = make_client(max_page_size=50)
    file_id = upload_ok(client, "p.kml", f.kml_document(f.kml_placemark("p", f.kml_point(77.7, 11.3))))["id"]
    assert_error(client.get(f"/api/files/{file_id}/measurements/", params={"limit": 51}), 422, "INVALID_PAGINATION")
    assert client.get(f"/api/files/{file_id}/measurements/", params={"limit": 50}).status_code == 200


def test_unknown_file_id_returns_404(client):
    assert_error(client.get("/api/files/0123456789abcdef0123456789abcdef/measurements/"), 404, "FILE_NOT_FOUND")
