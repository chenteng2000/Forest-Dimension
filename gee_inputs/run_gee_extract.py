import json
import sys
import csv
from pathlib import Path

sys.path.insert(0, r"D:\Workspace\Forest Dimension\gee_inputs\python_packages")
import ee

ROOT = Path(r"D:\Workspace\Forest Dimension")
GEO = ROOT / "gee_inputs/priority30_event_polygons.geojson"
PROJECT = "mimetic-cooler-496216-t4"


def feature_collection():
    data = json.loads(GEO.read_text(encoding="utf-8"))
    feats = []
    for f in data["features"]:
        props = f.get("properties", {})
        clean = {}
        for k, v in props.items():
            if v is None:
                continue
            clean[k] = v if isinstance(v, (int, float, str, bool)) else str(v)
        feats.append(ee.Feature(ee.Geometry(f["geometry"]), clean))
    return ee.FeatureCollection(feats)


def scale_reflectance(img):
    # HLS v002 imagery in Earth Engine is already returned in reflectance
    # units.  The per-band *_scale metadata describes the source encoding;
    # multiplying again by 0.0001 would shrink EVI/NBR/NDMI by roughly four
    # orders of magnitude.
    return img


def mask_hls(img):
    qa = img.select("Fmask")
    clear = (
        qa.bitwiseAnd(2).eq(0)
        .And(qa.bitwiseAnd(4).eq(0))
        .And(qa.bitwiseAnd(8).eq(0))
        .And(qa.bitwiseAnd(16).eq(0))
        .And(qa.bitwiseAnd(32).eq(0))
    )
    return img.updateMask(clear)


def add_indices(img):
    blue = img.select("blue")
    red = img.select("red")
    nir = img.select("nir")
    swir1 = img.select("swir1")
    swir2 = img.select("swir2")
    evi = nir.subtract(red).multiply(2.5).divide(
        nir.add(red.multiply(6)).subtract(blue.multiply(7.5)).add(1)
    ).rename("EVI")
    nbr = nir.subtract(swir2).divide(nir.add(swir2)).rename("NBR")
    ndmi = nir.subtract(swir1).divide(nir.add(swir1)).rename("NDMI")
    return img.addBands([evi, nbr, ndmi])


def prep_l30(img):
    sr = scale_reflectance(img.select(["B2", "B4", "B5", "B6", "B7"], ["blue", "red", "nir", "swir1", "swir2"]))
    return add_indices(mask_hls(sr.addBands(img.select("Fmask")))).copyProperties(
        img, ["system:time_start", "CLOUD_COVERAGE"]
    )


def prep_s30(img):
    sr = scale_reflectance(img.select(["B2", "B4", "B8A", "B11", "B12"], ["blue", "red", "nir", "swir1", "swir2"]))
    return add_indices(mask_hls(sr.addBands(img.select("Fmask")))).copyProperties(
        img, ["system:time_start", "CLOUD_COVERAGE"]
    )


def make_collection(events):
    aoi = events.geometry().bounds()
    l30 = (
        ee.ImageCollection("NASA/HLS/HLSL30/v002")
        .filterBounds(aoi)
        .filterDate("2018-01-01", "2026-01-01")
        .filter(ee.Filter.lt("CLOUD_COVERAGE", 70))
        .map(prep_l30)
    )
    s30 = (
        ee.ImageCollection("NASA/HLS/HLSS30/v002")
        .filterBounds(aoi)
        .filterDate("2018-01-01", "2026-01-01")
        .filter(ee.Filter.lt("CLOUD_COVERAGE", 70))
        .map(prep_s30)
    )
    return l30.merge(s30)


def summarize_event(f, prepared):
    geom = f.geometry()
    date = ee.Date(f.get("event_date"))
    month = ee.Number(date.get("month"))
    pre_col = (
        prepared.filterDate(date.advance(-3, "year"), date.advance(-30, "day"))
        .filter(ee.Filter.calendarRange(month.subtract(1).max(1), month.add(1).min(12), "month"))
    )
    post_center = date.advance(30, "day")
    post_col = (
        prepared.filterDate(post_center, post_center.advance(90, "day"))
    )
    empty_indices = ee.Image.constant([0, 0, 0]).rename(["EVI", "NBR", "NDMI"]).updateMask(ee.Image.constant(0))
    pre_img = ee.Image(ee.Algorithms.If(pre_col.size().gt(0), pre_col.median(), empty_indices))
    post_img = ee.Image(ee.Algorithms.If(post_col.size().gt(0), post_col.median(), empty_indices))
    reducer = ee.Reducer.median().combine(reducer2=ee.Reducer.count(), sharedInputs=True)
    pre = pre_img.select(["EVI", "NBR", "NDMI"]).reduceRegion(
        reducer=reducer, geometry=geom, scale=30, maxPixels=1e8, tileScale=4
    )
    post = post_img.select(["EVI", "NBR", "NDMI"]).reduceRegion(
        reducer=reducer, geometry=geom, scale=30, maxPixels=1e8, tileScale=4
    )
    # `reduceRegion` returns dictionary keys with null values when an image
    # has no valid pixels.  Do not use those nulls in arithmetic: Earth Engine
    # can evaluate both branches of an `If` while building the server graph.
    # Counts are always numeric for the selected bands, so use them as the
    # validity test and replace missing medians with harmless numeric values.
    pre_count = ee.Number(ee.Algorithms.If(pre.get("EVI_count"), pre.get("EVI_count"), 0))
    post_count = ee.Number(ee.Algorithms.If(post.get("EVI_count"), post.get("EVI_count"), 0))
    valid = pre_count.gt(0).And(post_count.gt(0))
    evi_pre = ee.Number(ee.Algorithms.If(pre_count.gt(0), pre.get("EVI_median"), 0))
    evi_post = ee.Number(ee.Algorithms.If(post_count.gt(0), post.get("EVI_median"), 0))
    nbr_pre = ee.Number(ee.Algorithms.If(pre_count.gt(0), pre.get("NBR_median"), 0))
    nbr_post = ee.Number(ee.Algorithms.If(post_count.gt(0), post.get("NBR_median"), 0))
    ndmi_pre = ee.Number(ee.Algorithms.If(pre_count.gt(0), pre.get("NDMI_median"), 0))
    ndmi_post = ee.Number(ee.Algorithms.If(post_count.gt(0), post.get("NDMI_median"), 0))
    # The denominator is made nonzero even for an invalid event.  The output
    # is overwritten with -9999 below when `valid` is false.
    safe_evi_pre = ee.Number(ee.Algorithms.If(evi_pre.abs().lt(1e-9), 1, evi_pre))
    delta_evi = ee.Number(ee.Algorithms.If(valid, evi_post.subtract(evi_pre).divide(safe_evi_pre).multiply(100), -9999))
    dnbr = ee.Number(ee.Algorithms.If(valid, nbr_pre.subtract(nbr_post), -9999))
    r_res = ee.Number(ee.Algorithms.If(valid, evi_post.divide(safe_evi_pre), -9999))
    return f.set(
        {
            "area_rs_ha": geom.area(1).divide(10000),
            "evi_pre": evi_pre,
            "evi_post": evi_post,
            "delta_evi_pct": delta_evi,
            "nbr_pre": nbr_pre,
            "nbr_post": nbr_post,
            "dnbr": dnbr,
            "ndmi_pre": ndmi_pre,
            "ndmi_post": ndmi_post,
            "r_res_proxy": r_res,
            "r_rec": "NA_recovery_target_not_set",
            "t_rec": "NA_observation_or_target_not_set",
            "pre_obs": pre_col.size(),
            "post_obs": post_col.size(),
            "valid_pixels_pre": pre_count,
            "valid_pixels_post": post_count,
            "metric_status": ee.Algorithms.If(valid, "valid_pre_post", "no_valid_pre_or_post"),
            "qa_note": "HLS L30/S30 v002; Fmask bits 1-5 masked; 30 m; pre uses same-season 3-year baseline; post uses 30-120 day window without month restriction; recovery not evaluated",
        }
    )


if __name__ == "__main__":
    ee.Initialize(project=PROJECT)
    events = feature_collection()
    prepared = make_collection(events)
    results = events.map(lambda f: summarize_event(f, prepared))
    print("event_count", events.size().getInfo())
    print(
        "hls_l30_count",
        ee.ImageCollection("NASA/HLS/HLSL30/v002")
        .filterBounds(events.geometry())
        .filterDate("2018-01-01", "2026-01-01")
        .size()
        .getInfo(),
    )
    # Materialize the small priority set locally so it can be reviewed and
    # joined back to the event catalog without relying on a Drive export.
    payload = ee.FeatureCollection(results).getInfo()
    out_json = ROOT / "gee_inputs" / "priority30_hls_event_metrics.geojson"
    out_csv = ROOT / "gee_inputs" / "priority30_hls_event_metrics.csv"
    out_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    rows = [feature.get("properties", {}) for feature in payload.get("features", [])]
    fieldnames = sorted({key for row in rows for key in row})
    with out_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    statuses = {}
    for row in rows:
        key = row.get("metric_status", "missing")
        statuses[key] = statuses.get(key, 0) + 1
    print("written_geojson", out_json)
    print("written_csv", out_csv)
    print("metric_status_counts", statuses)
