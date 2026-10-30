"""淳安森林变化智能辅助判读平台的轻量本地服务。

平台将本地事件台账与 GEE 中的 Sentinel-2 SR Harmonized 10 m 影像连接起来，
为选定事件生成前后影像、NDVI 变化图层和变化斑块 GeoJSON。
"""

from __future__ import annotations

import csv
import json
import math
import mimetypes
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import traceback
from uuid import uuid4
import webbrowser
from calendar import monthrange
from io import BytesIO
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

if getattr(sys, "frozen", False):
    # PyInstaller one-dir builds unpack read-only resources into _MEIPASS.
    # Keep the editable review file beside the executable so it survives
    # restarts and upgrades of the bundled resources.
    APP_HOME = Path(sys.executable).resolve().parent
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", APP_HOME))
    ROOT = RESOURCE_ROOT
    DATA_DIR = RESOURCE_ROOT / "gee_inputs"
    STATIC_DIR = RESOURCE_ROOT / "forest_change_platform" / "static"
    REVIEW_PATH = APP_HOME / "review_state.json"
else:
    APP_HOME = Path(__file__).resolve().parent
    RESOURCE_ROOT = APP_HOME.parent
    ROOT = RESOURCE_ROOT
    DATA_DIR = ROOT / "gee_inputs"
    STATIC_DIR = APP_HOME / "static"
    REVIEW_PATH = APP_HOME / "review_state.json"
PROJECT = "mimetic-cooler-496216-t4"
LOCAL_TZ = timezone(timedelta(hours=8))


def _local_today() -> date:
    return datetime.now(LOCAL_TZ).date()
PY_PACKAGES = DATA_DIR / "python_packages"
if str(PY_PACKAGES) not in sys.path:
    sys.path.insert(0, str(PY_PACKAGES))

import ee  # type: ignore  # noqa: E402
import xlsxwriter  # type: ignore  # noqa: E402


def _ensure_ssl_policy_file() -> None:
    """Make the bundled Python runtime's custom CA policy visible to ssl.py.

    The workspace Python runtime patches ``ssl.SSLContext`` to load a small
    local CA policy file next to ``ssl.py``. In a frozen build, ``ssl.py`` can
    be unpacked into a temporary _MEIPASS directory, so copy the bundled file
    there on startup when needed.
    """
    source = RESOURCE_ROOT / "codex-rejected-cas.pem"
    ssl_module = Path(getattr(ssl, "__file__", ""))
    target = ssl_module.parent / "codex-rejected-cas.pem" if ssl_module else None
    if not source.exists() or target is None or target == source or target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError:
        # If the extraction directory is read-only, ssl.py will report the
        # actual certificate-path error through the normal request handler.
        return


_ensure_ssl_policy_file()


def _debug_runtime(message: str) -> None:
    if os.environ.get("FOREST_CHANGE_DEBUG") != "1":
        return


def _open_external_browser(url: str) -> None:
    """Open the local platform in Chrome when it is installed."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            subprocess.Popen(
                [str(candidate), url],
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except OSError:
            continue
    webbrowser.open(url)
    try:
        (APP_HOME / "debug_runtime.txt").open("a", encoding="utf-8").write(message + "\n")
    except OSError:
        return


def _coerce(value):
    if value is None or value == "":
        return value
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    try:
        if "." not in text and "e" not in text.lower():
            return int(text)
        return float(text)
    except ValueError:
        return value


def _read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        text_fields = {"event_id", "operation_id", "plot_tokens", "source_sheet", "geometry_source"}
        return [
            {key: (value if key in text_fields else _coerce(value)) for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _load_data() -> list[dict]:
    catalog = {row["event_id"]: row for row in _read_csv(DATA_DIR / "priority30_event_catalog.csv")}
    metrics = {row["event_id"]: row for row in _read_csv(DATA_DIR / "priority30_hls_event_metrics.csv")}
    geojson = json.loads((DATA_DIR / "priority30_event_polygons.geojson").read_text(encoding="utf-8"))
    geometries = {feature["properties"]["event_id"]: feature["geometry"] for feature in geojson["features"]}

    events = []
    for event_id, base in catalog.items():
        props = dict(base)
        props.update(metrics.get(event_id, {}))
        props["event_id"] = event_id
        props["has_geometry"] = event_id in geometries
        events.append({"type": "Feature", "geometry": geometries.get(event_id), "properties": props})
    return sorted(events, key=lambda feature: (str(feature["properties"].get("event_date", "")), feature["properties"]["event_id"]))


def _load_raw_harvest_records() -> tuple[list[str], dict[str, dict]]:
    """Load the 30 source rows kept from the original priority review sheet."""
    path = DATA_DIR / "priority30_harvest_records.csv"
    if not path.exists():
        return [], {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [field for field in (reader.fieldnames or []) if field != "event_id"]
        records = {row.get("event_id", ""): row for row in reader if row.get("event_id")}
    return fields, records


EVENTS = _load_data()
EVENT_BY_ID = {feature["properties"]["event_id"]: feature for feature in EVENTS}
RAW_HARVEST_FIELDS, RAW_HARVEST_BY_ID = _load_raw_harvest_records()
ANCHOR_ROWS = _read_csv(DATA_DIR / "priority30_event_anchors.csv") if (DATA_DIR / "priority30_event_anchors.csv").exists() else []
ANCHORS_BY_ID: dict[str, list[dict]] = {}
for anchor in ANCHOR_ROWS:
    ANCHORS_BY_ID.setdefault(str(anchor.get("event_id", "")), []).append(anchor)
for event in EVENTS:
    props = event["properties"]
    anchors = ANCHORS_BY_ID.get(props["event_id"], [])
    props["measured_anchors"] = anchors
    props["has_search_anchor"] = bool(anchors or event.get("geometry"))
    if anchors:
        props["anchor_source"] = "严格配准实测点"
    elif (event.get("geometry") or {}).get("type") == "Point" and props.get("geometry_status") == "point_fallback_measured":
        props["anchor_source"] = "实测点坐标"
    elif event.get("geometry"):
        props["anchor_source"] = "营造林项目矢量中心（仅定位）"
    else:
        props["anchor_source"] = "缺少定位坐标"
REVIEW_LOCK = threading.Lock()
try:
    REVIEW_STATE = json.loads(REVIEW_PATH.read_text(encoding="utf-8")) if REVIEW_PATH.exists() else {}
except (OSError, json.JSONDecodeError):
    REVIEW_STATE = {}
EE_READY = False
EE_ERROR = None
CACHE: dict[tuple[str, str], dict] = {}
YEAR_EXPORT_JOBS: dict[str, dict] = {}
YEAR_EXPORT_LOCK = threading.Lock()


def _review_for(event_id: str) -> dict:
    return dict(REVIEW_STATE.get(event_id, {
        "status": "pending", "note": "", "reviewer": "", "updated_at": "",
        "actual_disturbance_date": "",
    }))


def _event_payload(feature: dict) -> dict:
    payload = json.loads(json.dumps(feature, ensure_ascii=False))
    # Source geometries are not exposed to the interface as analysis bounds.
    # GEE uses a single geometry centroid only as an approximate search anchor
    # when no directly matched field coordinate is available.
    payload["geometry"] = None
    payload["properties"].update({f"review_{key}": value for key, value in _review_for(feature["properties"]["event_id"]).items()})
    payload["properties"]["review_status"] = _review_for(feature["properties"]["event_id"]).get("status", "pending")
    return payload


def _save_review(event_id: str, body: dict) -> dict:
    allowed = {"pending", "confirmed", "rejected", "needs_review"}
    status = str(body.get("status", "pending"))
    if status not in allowed:
        raise ValueError(f"不支持的核查状态：{status}")
    actual_date_text = str(body.get("actual_disturbance_date", "")).strip()
    if actual_date_text:
        try:
            actual_date = datetime.strptime(actual_date_text, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("实际扰动日期请填写 YYYY-MM-DD") from exc
        if actual_date > _local_today():
            raise ValueError("人工确认的实际扰动日期不能晚于今天")
    entry = {
        "status": status,
        "note": str(body.get("note", ""))[:2000],
        "reviewer": str(body.get("reviewer", ""))[:100],
        "actual_disturbance_date": actual_date_text,
        "updated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
    }
    with REVIEW_LOCK:
        REVIEW_STATE[event_id] = entry
        REVIEW_PATH.write_text(json.dumps(REVIEW_STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    for cache_key in list(CACHE):
        if cache_key[0] in {event_id, f"timeseries:{event_id}"}:
            CACHE.pop(cache_key, None)
    return entry


def _review_label(status: str) -> str:
    return {"pending": "待复核", "confirmed": "已确认", "rejected": "已驳回", "needs_review": "需补充"}.get(status, "待复核")


def _init_ee() -> None:
    global EE_READY, EE_ERROR
    if EE_READY:
        return
    try:
        ssl_path = Path(getattr(ssl, "__file__", ""))
        _debug_runtime(
            "runtime="
            + repr(
                {
                    "meipass": str(getattr(sys, "_MEIPASS", "")),
                    "resource_root": str(RESOURCE_ROOT),
                    "ssl_file": str(ssl_path),
                    "policy_source_exists": (RESOURCE_ROOT / "codex-rejected-cas.pem").exists(),
                    "policy_target": str(ssl_path.parent / "codex-rejected-cas.pem"),
                    "policy_target_exists": (ssl_path.parent / "codex-rejected-cas.pem").exists(),
                }
            )
        )
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            _debug_runtime("ssl_context=ok")
        except Exception as ssl_exc:
            _debug_runtime("ssl_context_error=" + repr(ssl_exc))
        ee.Initialize(project=PROJECT)
        EE_READY = True
    except Exception as exc:  # pragma: no cover - depends on local credentials
        EE_ERROR = str(exc)
        if os.environ.get("FOREST_CHANGE_DEBUG") == "1":
            traceback.print_exc()
        raise


def _parse_event_date(feature: dict) -> date:
    event_id = feature["properties"]["event_id"]
    reviewed_date = _review_for(event_id).get("actual_disturbance_date")
    props = feature["properties"]
    value = reviewed_date or props.get("date_start") or props.get("event_date")
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _licensed_date_range(feature: dict) -> tuple[date, date]:
    props = feature["properties"]
    event_date = datetime.strptime(str(props.get("event_date") or props.get("date_start"))[:10], "%Y-%m-%d").date()
    start = datetime.strptime(str(props.get("date_start") or event_date)[:10], "%Y-%m-%d").date()
    end = datetime.strptime(str(props.get("date_end") or start)[:10], "%Y-%m-%d").date()
    return (start, max(start, end))


def _same_season_window(reference: date, year: int, half_width_days: int = 30) -> tuple[date, date]:
    day = min(reference.day, monthrange(year, reference.month)[1])
    center = date(year, reference.month, day)
    return center - timedelta(days=half_width_days), center + timedelta(days=half_width_days + 1)


def _date_basis(feature: dict) -> str:
    return "人工核验实际日期" if _review_for(feature["properties"]["event_id"]).get("actual_disturbance_date") else "许可/台账起始日期（实际日期待核）"


def _date_text(value: date) -> str:
    return value.isoformat()


def _periods(event_date: date, mode: str) -> tuple[date, date, date, date, str]:
    # The core view uses public Sentinel-2 imagery in 2023-2024. The extended
    # view allows 2025 imagery for late-2024 events and the 2025 ledger entries.
    pre_center = event_date.replace(year=event_date.year - 1)
    pre_start = pre_center - timedelta(days=60)
    pre_end = pre_center + timedelta(days=60)
    post_start = event_date + timedelta(days=15)
    post_end = event_date + timedelta(days=120)
    if mode == "core":
        dataset_end = date(2025, 1, 1)
        label = "核心时段：2023—2024"
    else:
        dataset_end = _local_today() + timedelta(days=1)
        label = f"扩展时段：2023—{_local_today().year}"
    post_end = min(post_end, dataset_end - timedelta(days=1))
    if post_end < post_start:
        post_end = post_start
    return pre_start, pre_end, post_start, post_end, label


def _mask_s2(image):
    scl = image.select("SCL")
    # Remove saturated, defective, cloud-shadow, medium/high cloud and snow.
    clear = (
        scl.neq(1)
        .And(scl.neq(3))
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return image.updateMask(clear)


def _s2_collection(geometry, start: date, end: date):
    return (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geometry)
        .filterDate(_date_text(start), _date_text(end + timedelta(days=1)))
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
        .map(_mask_s2)
    )


def _empty_s2():
    names = ["B2", "B3", "B4", "B8", "B11", "B12", "SCL"]
    return ee.Image.constant([0, 0, 0, 0, 0, 0, 0]).rename(names).updateMask(ee.Image.constant(0))


def _composite(collection):
    return ee.Image(ee.Algorithms.If(collection.size().gt(0), collection.median(), _empty_s2()))


def _hls_add_indices(image, nir_band: str, swir1_band: str, swir2_band: str):
    qa = image.select("Fmask")
    clear = (
        qa.bitwiseAnd(1).eq(0)
        .And(qa.bitwiseAnd(2).eq(0))
        .And(qa.bitwiseAnd(4).eq(0))
        .And(qa.bitwiseAnd(8).eq(0))
        .And(qa.bitwiseAnd(16).eq(0))
        .And(qa.bitwiseAnd(32).eq(0))
    )
    blue = image.select("B2")
    red = image.select("B4")
    nir = image.select(nir_band)
    swir1 = image.select(swir1_band)
    swir2 = image.select(swir2_band)
    evi = nir.subtract(red).multiply(2.5).divide(nir.add(red.multiply(6)).subtract(blue.multiply(7.5)).add(1)).rename("EVI")
    nbr = nir.subtract(swir2).divide(nir.add(swir2)).rename("NBR")
    ndmi = nir.subtract(swir1).divide(nir.add(swir1)).rename("NDMI")
    return ee.Image.cat([evi, nbr, ndmi]).updateMask(clear).copyProperties(image, ["system:time_start", "CLOUD_COVERAGE"])


def _hls_collection(geometry, start: date, end: date):
    """Return HLS observations normalized to common EVI/NBR/NDMI bands."""
    if end <= start:
        return ee.ImageCollection.fromImages([])
    l30 = (
        ee.ImageCollection("NASA/HLS/HLSL30/v002")
        .filterBounds(geometry)
        .filterDate(_date_text(start), _date_text(end))
        .filter(ee.Filter.lt("CLOUD_COVERAGE", 80))
        .map(lambda image: _hls_add_indices(image, "B5", "B6", "B7"))
    )
    s30 = (
        ee.ImageCollection("NASA/HLS/HLSS30/v002")
        .filterBounds(geometry)
        .filterDate(_date_text(start), _date_text(end))
        .filter(ee.Filter.lt("CLOUD_COVERAGE", 80))
        .map(lambda image: _hls_add_indices(image, "B8A", "B11", "B12"))
    )
    return l30.merge(s30)


def _empty_hls_indices():
    return ee.Image.constant([0, 0, 0]).rename(["EVI", "NBR", "NDMI"]).updateMask(ee.Image.constant(0))


def _hls_period_feature(collection, geometry, start: date, end: date, phase: str, label: str, year=None):
    window = collection.filterDate(_date_text(start), _date_text(end))
    scene_count = window.size()
    composite = ee.Image(ee.Algorithms.If(
        scene_count.gt(0), window.select(["EVI", "NBR", "NDMI"]).median(), _empty_hls_indices()
    ))
    reducer = ee.Reducer.mean().combine(reducer2=ee.Reducer.count(), sharedInputs=True)
    stats = composite.reduceRegion(
        reducer=reducer, geometry=geometry, scale=30, maxPixels=1e7, tileScale=4
    )
    observation_count = ee.Image(ee.Algorithms.If(
        scene_count.gt(0), window.select("EVI").count(), ee.Image.constant(0).rename("EVI")
    ))
    observation_stats = observation_count.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geometry, scale=30, maxPixels=1e7, tileScale=4
    )
    properties = {
        "phase": phase,
        "label": label,
        "year": year if year is not None else -1,
        "start": _date_text(start),
        "end": _date_text(end - timedelta(days=1)),
        "date": _date_text(start + (end - start) / 2),
        "scene_count": scene_count,
        "evi": stats.get("EVI_mean", -9999),
        "nbr": stats.get("NBR_mean", -9999),
        "ndmi": stats.get("NDMI_mean", -9999),
        "valid_pixels": stats.get("EVI_count", 0),
        "mean_valid_observations_per_pixel": observation_stats.get("EVI", 0),
    }
    return ee.Feature(None, properties)


def _hls_baseline_feature(geometry, annual_collections, annual_images, start: date, end: date):
    baseline_image = ee.ImageCollection.fromImages(annual_images).median()
    reducer = ee.Reducer.mean().combine(reducer2=ee.Reducer.count(), sharedInputs=True)
    stats = baseline_image.reduceRegion(
        reducer=reducer, geometry=geometry, scale=30, maxPixels=1e7, tileScale=4
    )
    observation_counts = ee.ImageCollection.fromImages([
        ee.Image(ee.Algorithms.If(
            item.size().gt(0), item.select("EVI").count(), ee.Image.constant(0).rename("EVI")
        )) for item in annual_collections
    ]).sum()
    observation_stats = observation_counts.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geometry, scale=30, maxPixels=1e7, tileScale=4
    )
    scene_count = ee.Number(0)
    for item in annual_collections:
        scene_count = scene_count.add(item.size())
    return ee.Feature(None, {
        "phase": "baseline_median",
        "label": "扰动前 3 年同季节中位数",
        "year": -1,
        "start": _date_text(start),
        "end": _date_text(end - timedelta(days=1)),
        "date": _date_text(start + (end - start) / 2),
        "scene_count": scene_count,
        "evi": stats.get("EVI_mean", -9999),
        "nbr": stats.get("NBR_mean", -9999),
        "ndmi": stats.get("NDMI_mean", -9999),
        "valid_pixels": stats.get("EVI_count", 0),
        "mean_valid_observations_per_pixel": observation_stats.get("EVI", 0),
    })


def _month_windows(start: date, end: date, today: date):
    windows = []
    cursor = date(start.year, start.month, 1)
    end_inclusive = min(end, today)
    while cursor <= end_inclusive:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        window_start = max(cursor, start)
        window_end = min(next_month, end_inclusive + timedelta(days=1))
        if window_start < window_end:
            windows.append((window_start, window_end))
        cursor = next_month
    return windows


def _map_url(image, vis):
    map_id = image.getMapId(vis)
    return map_id["tile_fetcher"].url_format


def _event_search_geometry(feature: dict):
    """Build an image-search zone from points, never from a source polygon area."""
    props = feature["properties"]
    measured = props.get("measured_anchors") or []
    anchors = []
    if measured:
        for item in measured:
            try:
                lon = float(item["longitude"])
                lat = float(item["latitude"])
            except (KeyError, TypeError, ValueError):
                continue
            anchors.append({"geometry": ee.Geometry.Point([lon, lat]), "longitude": lon, "latitude": lat})
        source = "strict_measured_plot_match"
    else:
        source_geometry = feature.get("geometry") or {}
        geometry_type = source_geometry.get("type")
        coordinates = source_geometry.get("coordinates")
        if geometry_type == "Point" and isinstance(coordinates, list) and len(coordinates) >= 2:
            lon, lat = float(coordinates[0]), float(coordinates[1])
            anchors.append({"geometry": ee.Geometry.Point([lon, lat]), "longitude": lon, "latitude": lat})
            source = "measured_point" if props.get("geometry_status") == "point_fallback_measured" else "vector_point_anchor"
        elif geometry_type == "MultiPoint" and isinstance(coordinates, list):
            for coordinate in coordinates:
                lon, lat = float(coordinate[0]), float(coordinate[1])
                anchors.append({"geometry": ee.Geometry.Point([lon, lat]), "longitude": lon, "latitude": lat})
            source = "vector_point_anchors"
        elif geometry_type in {"Polygon", "MultiPolygon"} and coordinates:
            # Use one centroid for the complete old geometry. Its footprint is
            # never used to clip the image search or the detected pixel units.
            source_geometry = ee.Geometry.Polygon(coordinates) if geometry_type == "Polygon" else ee.Geometry.MultiPolygon(coordinates)
            center = source_geometry.centroid(1)
            center_lon, center_lat = center.coordinates().getInfo()
            anchors.append({"geometry": center, "longitude": center_lon, "latitude": center_lat})
            source = "vector_geometry_centroid"
        else:
            source = "missing_anchor"

    if not anchors:
        raise ValueError("该事件缺少可用定位坐标，暂不能运行影像识别")

    try:
        event_area_m2 = max(0.0, float(props.get("area_record_ha") or 0) * 10000)
    except (TypeError, ValueError):
        event_area_m2 = 0.0
    per_anchor_area = event_area_m2 / max(1, len(anchors))
    radius_m = max(120.0, min(900.0, math.sqrt(per_anchor_area / math.pi) * 1.4 if per_anchor_area else 300.0))
    search_zones = [ee.Feature(anchor["geometry"].buffer(radius_m, 1)) for anchor in anchors]
    search_geometry = ee.FeatureCollection(search_zones).geometry(1)
    center = {
        "longitude": sum(anchor["longitude"] for anchor in anchors) / len(anchors),
        "latitude": sum(anchor["latitude"] for anchor in anchors) / len(anchors),
    }
    return search_geometry, {
        "source": source,
        "anchors": [{"longitude": item["longitude"], "latitude": item["latitude"]} for item in anchors],
        "radius_m": radius_m,
        "center": center,
    }


def _feature_collection_geojson(event_geometry, event_id: str, pre_image, post_image):
    geom = event_geometry
    pre_ndvi = pre_image.normalizedDifference(["B8", "B4"]).rename("NDVI_pre")
    post_ndvi = post_image.normalizedDifference(["B8", "B4"]).rename("NDVI_post")
    loss = pre_ndvi.subtract(post_ndvi).rename("NDVI_loss")
    # The source event polygon is not used as a mask. This mask is evaluated
    # across the anchor-based search zone; the returned geometries are the
    # pixels that pass the image-change criteria.
    mask = loss.gte(0.15).And(pre_ndvi.gte(0.4)).selfMask().clip(geom)

    pixel_count_band = ee.Image.constant(1).rename("pixel_count").toUint8().updateMask(mask)
    labeled = mask.rename("change").toUint8().addBands(pixel_count_band)
    vectors = labeled.reduceToVectors(
        reducer=ee.Reducer.sum().setOutputs(["pixel_count"]),
        geometry=geom,
        scale=10,
        geometryType="polygon",
        eightConnected=True,
        labelProperty="change",
        maxPixels=1e7,
        tileScale=4,
    )

    def evi(image):
        blue = image.select("B2").multiply(0.0001)
        red = image.select("B4").multiply(0.0001)
        nir = image.select("B8").multiply(0.0001)
        return nir.subtract(red).multiply(2.5).divide(nir.add(red.multiply(6)).subtract(blue.multiply(7.5)).add(1)).rename("EVI")

    def normalized_swir_index(image, swir_band, name):
        nir = image.select("B8")
        swir = image.select(swir_band).resample("bilinear")
        return nir.subtract(swir).divide(nir.add(swir)).rename(name)

    pre_metrics = ee.Image.cat([
        evi(pre_image).rename("evi_pre"),
        normalized_swir_index(pre_image, "B12", "nbr_pre"),
        normalized_swir_index(pre_image, "B11", "ndmi_pre"),
    ])
    post_metrics = ee.Image.cat([
        evi(post_image).rename("evi_post"),
        normalized_swir_index(post_image, "B12", "nbr_post"),
        normalized_swir_index(post_image, "B11", "ndmi_post"),
    ])
    index_image = pre_metrics.addBands(post_metrics)

    def add_unit_metrics(feature):
        unit_geometry = feature.geometry()
        center = unit_geometry.centroid(1).coordinates()
        bounds = unit_geometry.bounds(1)
        ring = ee.List(bounds.coordinates().get(0))
        longitudes = ring.map(lambda point: ee.List(point).get(0))
        latitudes = ring.map(lambda point: ee.List(point).get(1))
        pixel_count = ee.Number(feature.get("pixel_count")).round()
        mean_indices = index_image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=unit_geometry,
            scale=10,
            maxPixels=1e6,
            tileScale=4,
        )
        return feature.set({
            "pixel_count": pixel_count,
            "area_m2": pixel_count.multiply(100),
            "area_ha": pixel_count.multiply(100).divide(10000),
            "area_mu": pixel_count.multiply(100).multiply(0.0015),
            "center_lon": center.get(0),
            "center_lat": center.get(1),
            "centroid_lon": center.get(0),
            "centroid_lat": center.get(1),
            "bbox": ee.List([
                longitudes.reduce(ee.Reducer.min()),
                latitudes.reduce(ee.Reducer.min()),
                longitudes.reduce(ee.Reducer.max()),
                latitudes.reduce(ee.Reducer.max()),
            ]),
            "evi_pre": mean_indices.get("evi_pre"),
            "evi_post": mean_indices.get("evi_post"),
            "nbr_pre": mean_indices.get("nbr_pre"),
            "nbr_post": mean_indices.get("nbr_post"),
            "ndmi_pre": mean_indices.get("ndmi_pre"),
            "ndmi_post": mean_indices.get("ndmi_post"),
        })

    vectors = vectors.map(add_unit_metrics).filter(ee.Filter.gte("pixel_count", 1))
    payload = vectors.sort("pixel_count", False).getInfo()
    for index, feature in enumerate(payload.get("features", []), start=1):
        properties = feature.setdefault("properties", {})
        properties["event_id"] = event_id
        properties["patch_id"] = f"{event_id}_UNIT_{index:03d}"
        properties.update(_assess_candidate_unit(properties))
    return payload


def _assess_candidate_unit(properties: dict) -> dict:
    """Describe directional index agreement; this is not a trained classifier."""
    labels = {"evi": "EVI", "nbr": "NBR", "ndmi": "NDMI"}
    changes = {}
    for key, label in labels.items():
        before, after = properties.get(f"{key}_pre"), properties.get(f"{key}_post")
        if before is None or after is None:
            changes[label] = None
        else:
            changes[label] = round(float(after) - float(before), 4)
    if any(change is None for change in changes.values()):
        assessment = "数据不足"
        advice = "EVI、NBR 或 NDMI 缺少有效像元，请检查云、阴影和原始影像。"
    elif all(change < 0 for change in changes.values()):
        assessment = "多指标方向一致"
        advice = "NDVI、EVI、NBR、NDMI 均下降；请结合原始影像和作业资料核实原因。"
    else:
        assessment = "指标方向分歧"
        advice = "NDVI 下降，但 EVI、NBR 或 NDMI 未同向下降；优先复核物候、云影和时相。"
    evidence = "；".join(
        f"{label}{'缺测' if delta is None else f'变化 {delta:+.4f}'}"
        for label, delta in changes.items()
    )
    return {"assessment": assessment, "assessment_basis": f"NDVI 阈值候选；{evidence}", "review_advice": advice}


def _workbook_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return value


def _workbook_review_status(status: str) -> str:
    return {"pending": "待复核", "confirmed": "已确认", "rejected": "已驳回", "needs_review": "需补充"}.get(status, "待复核")


def _geojson_bounds(feature: dict) -> tuple[float | None, float | None, float | None, float | None]:
    geometry = feature.get("geometry") or {}
    extent = [float("inf"), float("inf"), float("-inf"), float("-inf")]

    def visit(value):
        if not isinstance(value, list):
            return
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            extent[0] = min(extent[0], float(value[0]))
            extent[1] = min(extent[1], float(value[1]))
            extent[2] = max(extent[2], float(value[0]))
            extent[3] = max(extent[3], float(value[1]))
            return
        for item in value:
            visit(item)

    visit(geometry.get("coordinates", []))
    if not all(abs(value) < float("inf") for value in extent):
        return None, None, None, None
    return tuple(extent)


def _raw_harvest_excel_value(header: str, value):
    if value in (None, ""):
        return None
    if header == "事件日期":
        return _workbook_date(value)
    if header in {"采伐强度（%）", "采伐面积（公顷）", "采伐蓄积（立方米）", "出材量（立方米）", "原表行号", "综合排序分"}:
        try:
            number = float(value)
            return int(number) if number.is_integer() and header == "原表行号" else number
        except (TypeError, ValueError):
            return value
    return value


def build_review_workbook(event_id: str | None, mode: str = "core", year: str | None = None,
                          progress=None, event_ids: list[str] | None = None) -> bytes:
    if mode not in {"core", "extended"}:
        raise ValueError("不支持的影像时段")
    selected = EVENT_BY_ID.get(event_id) if event_id else None
    if event_id and not selected:
        raise ValueError("未找到所选事件")
    if event_id and year:
        raise ValueError("单事件导出和年度导出不能同时指定")
    if year and (len(year) != 4 or not year.isdigit()):
        raise ValueError("年度导出需要四位年份")
    if event_ids is not None:
        if not isinstance(event_ids, list) or not event_ids or len(event_ids) > len(EVENTS):
            raise ValueError("左侧事件列表为空或数量无效")
        if len(set(event_ids)) != len(event_ids) or any(item not in EVENT_BY_ID for item in event_ids):
            raise ValueError("左侧事件编号无效或重复")
        indexed_events = [EVENT_BY_ID[item] for item in event_ids]
        if year and any(not str(event["properties"].get("event_date", "")).startswith(year) for event in indexed_events):
            raise ValueError("左侧事件与所选年份不一致")
    else:
        indexed_events = [selected] if selected else [
            event for event in EVENTS if not year or str(event["properties"].get("event_date", "")).startswith(year)
        ]
    batch = bool(year or event_ids)
    if year and not indexed_events:
        raise ValueError(f"{year} 年没有采伐记录")

    layer_by_id = {}
    series_by_id = {}
    errors_by_id = {}
    for index, event in enumerate(indexed_events, start=1):
        current_id = event["properties"]["event_id"]
        if event["properties"].get("has_search_anchor"):
            try:
                layer_by_id[current_id] = build_layers(current_id, mode)
                if selected or batch:
                    try:
                        series_by_id[current_id] = build_timeseries(current_id, mode)
                    except Exception as exc:
                        if selected:
                            raise
                        errors_by_id[current_id] = f"HLS时序失败：{exc}"
            except Exception as exc:
                if selected:
                    raise
                errors_by_id[current_id] = f"影像识别失败：{exc}"
        if progress:
            progress(index, len(indexed_events), current_id)
    mode_label = "核心时段：2023—2024" if mode == "core" else f"扩展时段：2023—{_local_today().year}"

    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    workbook.set_properties({
        "title": "淳安县森林变化智能辅助判读工作簿",
        "subject": "采伐台账原始记录与 Sentinel-2 识别单元指标",
        "author": "淳安森林变化智能辅助判读平台",
    })
    title_fmt = workbook.add_format({"bold": True, "font_size": 16, "font_color": "#19312D", "bg_color": "#DCEFE6", "valign": "vcenter"})
    note_fmt = workbook.add_format({"font_color": "#49635C", "text_wrap": True, "valign": "vcenter"})
    warning_fmt = workbook.add_format({"font_color": "#8A5B35", "text_wrap": True, "valign": "vcenter"})
    integer_fmt = workbook.add_format({"num_format": "#,##0"})
    decimal_fmt = workbook.add_format({"num_format": "#,##0.000"})
    area_fmt = workbook.add_format({"num_format": "#,##0.00"})
    coordinate_fmt = workbook.add_format({"num_format": "0.000000"})
    percent_fmt = workbook.add_format({"num_format": "0.0%"})
    date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd"})

    # Main output: one row per detected connected pixel unit, joined to its
    # original harvest record. A no-detection row remains for traceability.
    result = workbook.add_worksheet("识别单元结果")
    result_headers = [
        "事件编号", "识别单元编号", "识别状态", "定位锚点来源", "搜索半径（m）", "事件日期", "前时相范围", "后时相范围",
        "识别像元数", "单像元面积（m²）", "识别采伐面积（m²）", "识别采伐面积（ha）", "识别采伐面积（亩）",
        "单元中心经度", "单元中心纬度", "外接框西界", "外接框南界", "外接框东界", "外接框北界",
        "采伐证号", "小班号", "乡镇", "行政村", "自然村", "地点",
        "采伐强度（%）", "采伐强度等级", "采伐面积（公顷）", "作业面积等级", "采伐蓄积（立方米）",
        "出材量（立方米）", "核查优先级", "采伐期限", "来源工作表", "原表行号", "综合排序分",
        "前期平均EVI", "后期平均EVI", "EVI变化量（后-前）", "EVI相对变化（%）",
        "前期平均NBR", "后期平均NBR", "NBR变化量（后-前）",
        "前期平均NDMI", "后期平均NDMI", "NDMI变化量（后-前）",
        "人工核验实际扰动日期", "时序日期依据",
        "多指标辅助判读", "判读依据", "人工核查建议",
    ]
    result.merge_range(0, 0, 0, len(result_headers) - 1, "淳安县疑似森林变化单元与智能辅助判读", title_fmt)
    subtitle = (f"当前事件：{event_id} · {mode_label}" if selected else
                f"左侧列表 {year or ''} 年 {len(indexed_events)} 条采伐记录 · {mode_label}" if batch else
                f"未选择单个事件 · {mode_label} · 保留台账，不逐条运行影像识别")
    result.merge_range(1, 0, 1, len(result_headers) - 1, subtitle, note_fmt)
    result.merge_range(
        2, 0, 2, len(result_headers) - 1,
        "采伐原始字段来自原工作簿“优先核查30条”。识别面积按 10 m Sentinel-2 变化像元数 × 100 m²；判读等级仅表示指数方向一致性，不代表采伐概率或人工确认。指数均值按识别单元内有效像元计算。",
        warning_fmt,
    )
    result.set_row(0, 30)
    result.set_row(1, 23)
    result.set_row(2, 42)

    result_rows = []
    for event in indexed_events:
        props = event["properties"]
        current_id = props["event_id"]
        layer_result = layer_by_id.get(current_id)
        patches = (layer_result or {}).get("detected_change", {}).get("features", [])
        pixel_area_m2 = float((layer_result or {}).get("resolution_m", 10)) ** 2
        raw = RAW_HARVEST_BY_ID.get(current_id, {})

        def raw_value(key, fallback=None):
            value = raw.get(key)
            if value in (None, ""):
                value = fallback
            return _raw_harvest_excel_value(key, value)

        raw_values = [
            raw_value("采伐证号", props.get("operation_id")),
            raw_value("小班号", props.get("plot_text")),
            raw_value("乡镇", props.get("township")),
            raw_value("行政村", props.get("village")),
            raw_value("自然村", props.get("natural_village")),
            raw_value("地点", props.get("location")),
            raw_value("采伐强度（%）", props.get("intensity_pct")),
            raw_value("采伐强度等级", props.get("intensity_level")),
            raw_value("采伐面积（公顷）", props.get("area_record_ha")),
            raw_value("作业面积等级", props.get("area_level")),
            raw_value("采伐蓄积（立方米）", props.get("harvest_volume_m3")),
            raw_value("出材量（立方米）", props.get("output_volume_m3")),
            raw_value("核查优先级", props.get("priority")),
            raw_value("采伐期限", ""),
            raw_value("来源工作表", props.get("source_sheet")),
            raw_value("原表行号", props.get("source_row")),
            raw_value("综合排序分", ""),
        ]
        units = patches if (selected or batch) else []
        if not units:
            if current_id in errors_by_id and current_id not in layer_by_id:
                status = "影像识别失败（见批量处理状态）"
            elif not selected and not batch:
                status = "未运行（未选中事件）"
            elif not props.get("has_search_anchor"):
                status = "待补定位坐标"
            else:
                status = "未检出变化像元"
            record_date = raw_value("事件日期", props.get("event_date"))
            result_rows.append([
                current_id, "", status, props.get("anchor_source", "缺少定位坐标"),
                (layer_result or {}).get("search_radius_m"), record_date, None, None, None, pixel_area_m2,
                None, None, None, None, None, None, None, None, None,
                *raw_values, None, None, None, None, None, None, None, None, None, None,
                _review_for(current_id).get("actual_disturbance_date", ""), _date_basis(event),
                "无候选单元" if not patches else None,
                errors_by_id.get(current_id, "") if current_id in errors_by_id else "未检出可判读斑块或缺少定位坐标",
                "检查定位及影像质量" if props.get("has_search_anchor") else "补充定位坐标",
            ])
            continue

        for unit in units:
            unit_props = unit.get("properties", {})
            count = int(round(float(unit_props.get("pixel_count") or 0)))
            area_m2 = count * pixel_area_m2 if "pixel_count" in unit_props else float(unit_props.get("area_m2") or 0)
            bbox = unit_props.get("bbox")
            if isinstance(bbox, list) and len(bbox) == 4:
                west, south, east, north = bbox
            else:
                west, south, east, north = _geojson_bounds(unit)
            evi_pre, evi_post = unit_props.get("evi_pre"), unit_props.get("evi_post")
            nbr_pre, nbr_post = unit_props.get("nbr_pre"), unit_props.get("nbr_post")
            ndmi_pre, ndmi_post = unit_props.get("ndmi_pre"), unit_props.get("ndmi_post")
            evi_delta = float(evi_post) - float(evi_pre) if evi_pre is not None and evi_post is not None else None
            evi_relative = evi_delta / float(evi_pre) if evi_delta is not None and float(evi_pre) != 0 else None
            nbr_delta = float(nbr_post) - float(nbr_pre) if nbr_pre is not None and nbr_post is not None else None
            ndmi_delta = float(ndmi_post) - float(ndmi_pre) if ndmi_pre is not None and ndmi_post is not None else None
            pre_period = (layer_result or {}).get("pre_period", {})
            post_period = (layer_result or {}).get("post_period", {})
            result_rows.append([
                current_id, unit_props.get("patch_id", ""), "已识别",
                {"strict_measured_plot_match": "严格配准实测坐标", "measured_point": "实测点坐标", "vector_geometry_centroid": "营造林项目矢量中心（仅定位）", "vector_point_anchor": "营造林项目矢量点位（仅定位）", "vector_point_anchors": "营造林项目矢量点位（仅定位）"}.get((layer_result or {}).get("anchor_source"), (layer_result or {}).get("anchor_source", "")),
                (layer_result or {}).get("search_radius_m"), raw_value("事件日期", props.get("event_date")),
                f"{pre_period.get('start', '')} 至 {pre_period.get('end', '')}",
                f"{post_period.get('start', '')} 至 {post_period.get('end', '')}",
                count if "pixel_count" in unit_props else None, pixel_area_m2, area_m2, area_m2 / 10000, area_m2 * 0.0015,
                unit_props.get("center_lon", unit_props.get("centroid_lon")),
                unit_props.get("center_lat", unit_props.get("centroid_lat")),
                west, south, east, north, *raw_values,
                evi_pre, evi_post, evi_delta, evi_relative, nbr_pre, nbr_post, nbr_delta, ndmi_pre, ndmi_post, ndmi_delta,
                _review_for(current_id).get("actual_disturbance_date", ""), _date_basis(event),
                unit_props.get("assessment"), unit_props.get("assessment_basis"), unit_props.get("review_advice"),
            ])

    result_formats = {
        "事件日期": date_fmt, "识别像元数": integer_fmt, "单像元面积（m²）": area_fmt,
        "搜索半径（m）": decimal_fmt,
        "识别采伐面积（m²）": area_fmt, "识别采伐面积（ha）": area_fmt, "识别采伐面积（亩）": area_fmt,
        "单元中心经度": coordinate_fmt, "单元中心纬度": coordinate_fmt,
        "外接框西界": coordinate_fmt, "外接框南界": coordinate_fmt, "外接框东界": coordinate_fmt, "外接框北界": coordinate_fmt,
        "采伐强度（%）": decimal_fmt, "采伐面积（公顷）": area_fmt,
        "采伐蓄积（立方米）": decimal_fmt, "出材量（立方米）": decimal_fmt,
        "原表行号": integer_fmt, "综合排序分": decimal_fmt,
        "前期平均EVI": decimal_fmt, "后期平均EVI": decimal_fmt, "EVI变化量（后-前）": decimal_fmt,
        "EVI相对变化（%）": percent_fmt, "前期平均NBR": decimal_fmt, "后期平均NBR": decimal_fmt,
        "NBR变化量（后-前）": decimal_fmt, "前期平均NDMI": decimal_fmt, "后期平均NDMI": decimal_fmt,
        "NDMI变化量（后-前）": decimal_fmt,
    }
    result.add_table(3, 0, 3 + len(result_rows), len(result_headers) - 1, {
        "name": "DetectedUnitResults", "style": "Table Style Medium 4",
        "columns": [{"header": header, **({"format": result_formats[header]} if header in result_formats else {})} for header in result_headers],
        "data": result_rows,
    })
    result.freeze_panes(4, 3)
    result.set_zoom(85)
    result.set_landscape()
    result.fit_to_pages(1, 0)
    result.repeat_rows(3)
    result.set_column(0, 3, 24)
    result.set_column(4, 4, 16, decimal_fmt)
    result.set_column(5, 7, 23)
    result.set_column(8, 12, 19)
    result.set_column(13, 18, 17, coordinate_fmt)
    result.set_column(19, 35, 24)
    result.set_column(36, 45, 19, decimal_fmt)
    result.set_column(46, 47, 25)
    result.set_column(48, 48, 20)
    result.set_column(49, 50, 58)
    result.set_column(24, 24, 35)
    result.set_column(30, 33, 26)

    if (selected and selected["properties"].get("has_search_anchor")) or batch:
        series_headers = [
            "事件编号", "阶段", "观察年", "窗口起始", "窗口结束", "窗口状态",
            "平均EVI", "平均NBR", "平均NDMI", "窗口影像数", "有效像元数",
            "平均每像元有效观测数", "日期依据", "分析范围",
        ]

        def write_series_sheet(sheet_name, records, table_name, explanation):
            sheet = workbook.add_worksheet(sheet_name)
            sheet.merge_range(0, 0, 0, len(series_headers) - 1, sheet_name, title_fmt)
            sheet.merge_range(1, 0, 1, len(series_headers) - 1, explanation, note_fmt)
            sheet.set_row(0, 30)
            sheet.set_row(1, 42)
            rows = [[
                series_event_id, item.get("label"),
                "前三年汇总" if item.get("phase") == "baseline_median" else item.get("year_after") or item.get("year"),
                item.get("start"), item.get("end"), item.get("status"),
                item.get("evi"), item.get("nbr"), item.get("ndmi"),
                item.get("scene_count"), item.get("valid_pixels"),
                item.get("mean_valid_observations_per_pixel"),
                series["date_basis"], series["unit_scope"],
            ] for series_event_id, series, item in records]
            if not rows:
                rows = [[year or event_id, "无可用HLS序列", None, None, None, "无可用窗口"] + [None] * 6 + [None, None]]
            sheet.add_table(2, 0, 2 + len(rows), len(series_headers) - 1, {
                "name": table_name, "style": "Table Style Medium 4",
                "columns": [{"header": header} for header in series_headers], "data": rows,
            })
            sheet.freeze_panes(3, 2)
            sheet.set_column(0, 5, 23)
            sheet.set_column(6, 8, 18, decimal_fmt)
            sheet.set_column(9, 11, 22)
            sheet.set_column(12, 13, 37)

        baseline_records = []
        monthly_records = []
        for series_event_id, series in series_by_id.items():
            for item in series["baseline_annual"] + ([series["baseline"]] if series["baseline"] else []) + series["recovery"]:
                baseline_records.append((series_event_id, series, item))
            for item in series["operation_series"]:
                monthly_records.append((series_event_id, series, item))
        write_series_sheet(
            "HLS基线与恢复", baseline_records,
            "HLSBaselineRecovery",
            "HLS 30 m：扰动前 3 个同季节窗口逐年合成，再取逐像元中位数作基线；恢复列出扰动后 1—3 年同季节窗口。无人工核验日期时仅以许可起始日暂定窗口。",
        )
        write_series_sheet(
            "HLS许可期逐月核查", monthly_records, "HLSLicenseMonths",
            "HLS 30 m：许可期逐月 EVI、NBR、NDMI 用于辅助判读扰动发生时间；单个指数下降或许可日期均不能独立证明实际采伐。",
        )

    if batch:
        status_sheet = workbook.add_worksheet("批量处理状态")
        status_headers = ["事件编号", "处理状态", "识别单元数", "HLS时序状态", "错误详情"]
        status_rows = []
        for event in indexed_events:
            current_id = event["properties"]["event_id"]
            if not event["properties"].get("has_search_anchor"):
                status = "待补定位坐标"
            elif current_id not in layer_by_id:
                status = "识别失败"
            elif layer_by_id[current_id].get("detected_change", {}).get("features"):
                status = "已识别"
            else:
                status = "未检出变化像元"
            status_rows.append([
                current_id, status,
                len(layer_by_id.get(current_id, {}).get("detected_change", {}).get("features", [])),
                "已计算" if current_id in series_by_id else "未计算",
                errors_by_id.get(current_id, ""),
            ])
        status_sheet.merge_range(0, 0, 0, 4, f"左侧列表 {year or ''} 年 {len(indexed_events)} 条事件处理状态", title_fmt)
        status_sheet.add_table(2, 0, 2 + len(status_rows), 4, {
            "name": "YearExportStatus", "style": "Table Style Medium 2",
            "columns": [{"header": header} for header in status_headers], "data": status_rows,
        })
        status_sheet.freeze_panes(3, 1)
        status_sheet.set_column(0, 3, 24)
        status_sheet.set_column(4, 4, 90)

    # Keep the complete original source fields on a dedicated sheet.
    source_sheet = workbook.add_worksheet("采伐记录原数据")
    fallback_headers = ["事件日期", "采伐证号", "小班号", "乡镇", "行政村", "自然村", "地点", "采伐强度（%）", "采伐强度等级", "采伐面积（公顷）", "作业面积等级", "采伐蓄积（立方米）", "出材量（立方米）", "核查优先级", "采伐期限", "来源工作表", "原表行号", "综合排序分"]
    source_fields = RAW_HARVEST_FIELDS or fallback_headers
    source_headers = ["事件编号"] + source_fields
    source_rows = []
    for event in indexed_events:
        props = event["properties"]
        current_id = props["event_id"]
        raw = RAW_HARVEST_BY_ID.get(current_id, {})
        fallback = {
            "事件日期": props.get("event_date"), "采伐证号": props.get("operation_id"), "小班号": props.get("plot_text"),
            "乡镇": props.get("township"), "行政村": props.get("village"), "自然村": props.get("natural_village"),
            "地点": props.get("location"), "采伐强度（%）": props.get("intensity_pct"), "采伐强度等级": props.get("intensity_level"),
            "采伐面积（公顷）": props.get("area_record_ha"), "作业面积等级": props.get("area_level"),
            "采伐蓄积（立方米）": props.get("harvest_volume_m3"), "出材量（立方米）": props.get("output_volume_m3"),
            "核查优先级": props.get("priority"), "来源工作表": props.get("source_sheet"), "原表行号": props.get("source_row"),
        }
        source_rows.append([current_id] + [_raw_harvest_excel_value(header, raw.get(header, fallback.get(header))) for header in source_fields])
    source_sheet.merge_range(0, 0, 0, len(source_headers) - 1, "采伐核查原始记录", title_fmt)
    source_sheet.merge_range(1, 0, 1, len(source_headers) - 1, "字段和值按原工作簿“优先核查30条”保留；事件编号用于与识别单元结果对应。来源工作表和原表行号指向原始来源记录。", note_fmt)
    source_sheet.set_row(0, 30)
    source_sheet.set_row(1, 34)
    source_sheet.add_table(2, 0, 2 + len(source_rows), len(source_headers) - 1, {
        "name": "RawHarvestRecords", "style": "Table Style Medium 2",
        "columns": [{"header": header, **({"format": date_fmt} if header == "事件日期" else {})} for header in source_headers],
        "data": source_rows,
    })
    source_sheet.freeze_panes(3, 1)
    source_sheet.set_zoom(95)
    source_sheet.set_column(0, 0, 24)
    source_sheet.set_column(1, len(source_headers) - 1, 24)

    methods = workbook.add_worksheet("指标说明")
    methods.merge_range(0, 0, 0, 1, "指标口径与使用说明", title_fmt)
    method_rows = [
        ("识别结果范围", "识别单元几何来自 Sentinel-2 变化像元；以像元连通斑块作为单元，不使用营造林/采伐矢量面面积作为识别面积。"),
        ("采伐面积", "识别像元数 × 单像元面积。Sentinel-2 B2/B3/B4/B8 为 10 m；单像元面积按 10 m × 10 m = 100 m²。"),
        ("定位锚点与搜索范围", "优先使用严格配准的实测点；缺少实测坐标时，营造林项目矢量只取整体中心作为近似定位锚点。该矢量包含采伐、抚育、补植、改培等项目类型，不能证明对应采伐证；矢量边界不用于识别或裁剪。影像搜索缓冲半径按台账面积估算，并限制在 120—900 m。"),
        ("中心坐标与外接框", "中心经纬度和外接框由识别单元像元几何计算；外接框只用于快速定位，不作为面积。"),
        ("前后指数", "EVI、NBR、NDMI 分别对识别单元内前、后时相合成影像计算均值。空值表示对应时相缺少有效像元。"),
        ("时相窗口", "前期和后期日期范围见识别单元结果表。影像使用 Sentinel-2 SR Harmonized，并屏蔽云、云影及雪冰。"),
        ("变化量", "EVI/NBR/NDMI 变化量 = 后期均值 − 前期均值；EVI 相对变化 =（后期 − 前期）÷ 前期。"),
        ("恢复指标", "本表保留前后均值供后续抵抗力/恢复分析。恢复时间或恢复程度还需要明确恢复目标和更晚的观测时相，因此不将单次前后差值标作恢复指标。"),
        ("实际扰动日期", "许可起始日仅用于暂定影像窗口。经作业、验收资料和原始影像人工核验后，可在平台填写实际扰动日期；未填写时不得把许可日期称为实际采伐日期。"),
        ("HLS基线", "前 3 年同季节各取约 61 天窗口，逐年形成合成影像后再取逐像元中位数；无有效像元时留空。"),
        ("HLS有效观测", "窗口影像数是 HLS L30/S30 场景数；有效像元数是合成影像中有值的 30 m 像元数；平均每像元有效观测数来自 Fmask 后逐景计数。"),
        ("HLS许可期核查", "逐月 EVI、NBR、NDMI 与原始影像联合查看。月度变化只能提供候选扰动时间，不代替现场确认。"),
        ("多指标辅助判读", "先用 NDVI 阈值筛出候选像元；逐识别单元比较前后 EVI、NBR、NDMI 的变化方向。三项都下降标为“多指标方向一致”；任一方向不一致标为“指标方向分歧”；任一缺少有效值标为“数据不足”。这不是训练模型或采伐概率。"),
        ("判读边界", "多指标一致仅表示遥感指数变化方向一致，不足以确认采伐原因、实际作业日期或真实采伐面积。请结合原始影像、台账、作业或验收记录人工核实。"),
        ("台账面积", "“采伐面积（公顷）”是原工作簿数值；“识别采伐面积”是遥感阈值像元面积，两者口径不同，不应直接视为相等。"),
        ("数据来源", "采伐记录原始字段来自《淳安县采伐作业_可呈现图表与清单.xlsx》中的“优先核查30条”工作表。"),
    ]
    methods.write_row(2, 0, ["项目", "说明"])
    for row_num, (label, description) in enumerate(method_rows, start=3):
        methods.write(row_num, 0, label)
        methods.write(row_num, 1, description, note_fmt)
        methods.set_row(row_num, 36)
    methods.set_column(0, 0, 24)
    methods.set_column(1, 1, 110)
    methods.freeze_panes(3, 0)

    workbook.close()
    return output.getvalue()

def build_timeseries(event_id: str, mode: str = "core") -> dict:
    cache_key = (f"timeseries:{event_id}", mode)
    if cache_key in CACHE:
        return CACHE[cache_key]
    feature = EVENT_BY_ID.get(event_id)
    if not feature:
        raise ValueError("未找到所选采伐记录")
    layer_result = build_layers(event_id, mode)
    detected = layer_result.get("detected_change", {}).get("features", [])
    if detected:
        units = [ee.Feature(ee.Geometry(item["geometry"])) for item in detected if item.get("geometry")]
        analysis_geometry = ee.FeatureCollection(units).geometry(1)
        analysis_scope = "Sentinel-2 识别变化像元范围"
    else:
        analysis_geometry, _ = _event_search_geometry(feature)
        analysis_scope = "锚点搜索缓冲区（尚无识别单元，日期判读需人工谨慎核实）"

    reference_date = _parse_event_date(feature)
    license_start, license_end = _licensed_date_range(feature)
    today = _local_today()
    baseline_specs = []
    for year in range(reference_date.year - 3, reference_date.year):
        window_start, window_end = _same_season_window(reference_date, year)
        baseline_specs.append((year, window_start, window_end))

    operation_specs = _month_windows(license_start, license_end, today)
    recovery_specs = []
    recovery_not_ready = []
    for years_after in (1, 2, 3):
        window_start, window_end = _same_season_window(reference_date, reference_date.year + years_after)
        if window_start > today:
            recovery_not_ready.append({
                "phase": "recovery",
                "label": f"扰动后第 {years_after} 年",
                "year_after": years_after,
                "start": _date_text(window_start),
                "end": _date_text(window_end - timedelta(days=1)),
                "status": "观察窗口尚未到达",
                "scene_count": 0,
                "evi": None,
                "nbr": None,
                "ndmi": None,
                "valid_pixels": 0,
                "mean_valid_observations_per_pixel": 0,
            })
        elif window_end > today + timedelta(days=1):
            recovery_not_ready.append({
                "phase": "recovery",
                "label": f"扰动后第 {years_after} 年",
                "year_after": years_after,
                "start": _date_text(window_start),
                "end": _date_text(window_end - timedelta(days=1)),
                "status": "同季节观察窗口尚未完整",
                "scene_count": 0,
                "evi": None,
                "nbr": None,
                "ndmi": None,
                "valid_pixels": 0,
                "mean_valid_observations_per_pixel": 0,
            })
        else:
            recovery_specs.append((years_after, window_start, window_end))

    all_windows = [(start, end) for _, start, end in baseline_specs]
    all_windows.extend(operation_specs)
    all_windows.extend((start, end) for _, start, end in recovery_specs)
    collection_start = min(start for start, _ in all_windows)
    collection_end = min(max(end for _, end in all_windows), today + timedelta(days=1))
    collection = _hls_collection(analysis_geometry, collection_start, collection_end)

    output_features = []
    annual_collections = []
    annual_images = []
    for year, window_start, window_end in baseline_specs:
        annual_collection = collection.filterDate(_date_text(window_start), _date_text(window_end))
        annual_collections.append(annual_collection)
        annual_image = ee.Image(ee.Algorithms.If(
            annual_collection.size().gt(0),
            annual_collection.select(["EVI", "NBR", "NDMI"]).median(),
            _empty_hls_indices(),
        ))
        annual_images.append(annual_image)
        record = _hls_period_feature(
            collection, analysis_geometry, window_start, window_end,
            "baseline_year", f"扰动前同季节基线 {year}", year,
        )
        output_features.append(record)

    baseline_summary = _hls_baseline_feature(
        analysis_geometry, annual_collections, annual_images,
        baseline_specs[0][1], baseline_specs[-1][2],
    )
    output_features.append(baseline_summary)

    for window_start, window_end in operation_specs:
        month_label = f"许可期月合成 {window_start:%Y-%m}"
        output_features.append(_hls_period_feature(
            collection, analysis_geometry, window_start, window_end,
            "license_month", month_label, window_start.year,
        ))

    for years_after, window_start, window_end in recovery_specs:
        output_features.append(_hls_period_feature(
            collection, analysis_geometry, window_start, window_end,
            "recovery", f"扰动后第 {years_after} 年", reference_date.year + years_after,
        ))

    raw_rows = ee.FeatureCollection(output_features).getInfo().get("features", [])

    def clean_record(raw_feature):
        props = dict(raw_feature.get("properties", {}))
        for key in ("evi", "nbr", "ndmi"):
            value = props.get(key)
            props[key] = None if value is None or float(value) <= -9999 else float(value)
        props["scene_count"] = int(props.get("scene_count") or 0)
        props["valid_pixels"] = int(props.get("valid_pixels") or 0)
        props["mean_valid_observations_per_pixel"] = float(props.get("mean_valid_observations_per_pixel") or 0)
        if props.get("phase") == "recovery":
            props["year_after"] = int(props.get("year") or 0) - reference_date.year
        props["status"] = "有有效观测" if props["scene_count"] and props["valid_pixels"] else "窗口内无有效像元"
        return props

    records = [clean_record(item) for item in raw_rows]
    baseline_annual = [item for item in records if item.get("phase") == "baseline_year"]
    baseline = next((item for item in records if item.get("phase") == "baseline_median"), None)
    operation_series = [item for item in records if item.get("phase") == "license_month"]
    recovery = [item for item in records if item.get("phase") == "recovery"] + recovery_not_ready
    points = sorted(
        baseline_annual + operation_series + recovery,
        key=lambda item: item.get("date") or item.get("start", ""),
    )
    event_date = _licensed_date_range(feature)[0]
    result = {
        "event_id": event_id,
        "period_label": "扰动前 3 年基线 · 许可期月序列 · 扰动后 1—3 年同季节恢复观察",
        "sensor": "HLS L30/S30 v002",
        "resolution_m": 30,
        "unit_scope": analysis_scope,
        "unit_count": len(detected),
        "reference_date": _date_text(reference_date),
        "license_start": _date_text(license_start),
        "license_end": _date_text(license_end),
        "date_precision": feature["properties"].get("date_precision", "未注明"),
        "date_basis": _date_basis(feature),
        "reviewed_actual_date": _review_for(event_id).get("actual_disturbance_date", ""),
        "baseline": baseline,
        "baseline_annual": baseline_annual,
        "operation_series": operation_series,
        "recovery": sorted(recovery, key=lambda item: item.get("year_after", item.get("year", 0))),
        "points": points,
        "valid_observation_note": "场景数为窗口内HLS L30/S30影像数；平均有效观测数为研究范围内每个像元的平均QA后有效观测数。",
    }
    CACHE[cache_key] = result
    return result


def build_layers(event_id: str, mode: str = "core") -> dict:
    cache_key = (event_id, mode)
    if cache_key in CACHE:
        return CACHE[cache_key]
    feature = EVENT_BY_ID.get(event_id)
    if not feature:
        raise ValueError("未找到所选采伐记录")
    _init_ee()

    event_date = _parse_event_date(feature)
    pre_start, pre_end, post_start, post_end, period_label = _periods(event_date, mode)
    search_geometry, anchor_info = _event_search_geometry(feature)
    pre_col = _s2_collection(search_geometry, pre_start, pre_end)
    post_col = _s2_collection(search_geometry, post_start, post_end)
    pre_image = _composite(pre_col)
    post_image = _composite(post_col)

    pre_count = int(pre_col.size().getInfo())
    post_count = int(post_col.size().getInfo())
    pre_rgb = _map_url(pre_image, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000, "gamma": 1.1})
    post_rgb = _map_url(post_image, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000, "gamma": 1.1})

    pre_ndvi = pre_image.normalizedDifference(["B8", "B4"]).rename("NDVI")
    post_ndvi = post_image.normalizedDifference(["B8", "B4"]).rename("NDVI")
    ndvi_reducer = ee.Reducer.mean().combine(reducer2=ee.Reducer.count(), sharedInputs=True)
    pre_ndvi_stats = pre_ndvi.reduceRegion(
        reducer=ndvi_reducer, geometry=search_geometry, scale=10, maxPixels=1e7, tileScale=4
    ).getInfo()
    post_ndvi_stats = post_ndvi.reduceRegion(
        reducer=ndvi_reducer, geometry=search_geometry, scale=10, maxPixels=1e7, tileScale=4
    ).getInfo()
    change = pre_ndvi.subtract(post_ndvi).rename("NDVI_loss")
    change_mask = change.gte(0.15).And(pre_ndvi.gte(0.4)).selfMask().clip(search_geometry)
    change_url = _map_url(change_mask, {"min": 0, "max": 1, "palette": ["ff3b30"]})
    change_heat_url = _map_url(change.clip(search_geometry), {"min": -0.3, "max": 0.3, "palette": ["2166ac", "f7f7f7", "b2182b"]})
    vectors = _feature_collection_geojson(search_geometry, event_id, pre_image, post_image)
    assessment_counts = {"多指标方向一致": 0, "指标方向分歧": 0, "数据不足": 0}
    for unit in vectors.get("features", []):
        label = unit.get("properties", {}).get("assessment", "数据不足")
        assessment_counts[label] = assessment_counts.get(label, 0) + 1

    quality_pre = pre_ndvi_stats.get("NDVI_mean")
    quality_post = post_ndvi_stats.get("NDVI_mean")
    result = {
        "event_id": event_id,
        "mode": mode,
        "period_label": period_label,
        "sensor": "Sentinel-2 SR Harmonized",
        "resolution_m": 10,
        "pre_period": {"start": _date_text(pre_start), "end": _date_text(pre_end), "scene_count": pre_count},
        "post_period": {"start": _date_text(post_start), "end": _date_text(post_end), "scene_count": post_count},
        "quality": {
            "pre_ndvi_mean": quality_pre,
            "post_ndvi_mean": quality_post,
            "ndvi_change": quality_pre - quality_post if quality_pre is not None and quality_post is not None else None,
            "pre_valid_pixels": pre_ndvi_stats.get("NDVI_count", 0),
            "post_valid_pixels": post_ndvi_stats.get("NDVI_count", 0),
        },
        "anchor_source": anchor_info["source"],
        "anchor_count": len(anchor_info["anchors"]),
        "search_radius_m": anchor_info["radius_m"],
        "map_center": anchor_info["center"],
        "threshold": {"ndvi_loss": 0.15, "pre_ndvi_min": 0.4, "pixel_area_m2": 100, "min_pixel_count": 1},
        "tiles": {"pre_rgb": pre_rgb, "post_rgb": post_rgb, "change_mask": change_url, "change_heat": change_heat_url},
        "detected_change": vectors,
        "assessment_counts": assessment_counts,
        "assessment_method": "NDVI 阈值筛查；逐斑块比较 EVI、NBR、NDMI 前后变化方向；结果是规则辅助判读，须人工核实。",
    }
    CACHE[cache_key] = result
    return result


def _start_year_export(year: str, mode: str, event_ids: list[str]) -> dict:
    if len(year) != 4 or not year.isdigit():
        raise ValueError("请选择有效的四位年份")
    if mode not in {"core", "extended"}:
        raise ValueError("不支持的影像时段")
    if mode == "core" and int(year) > 2024:
        raise ValueError("2025 年及以后记录请切换到扩展影像时段")
    if not isinstance(event_ids, list) or not event_ids or len(event_ids) > len(EVENTS):
        raise ValueError("左侧列表没有可导出的事件")
    if any(not isinstance(item, str) for item in event_ids) or len(set(event_ids)) != len(event_ids):
        raise ValueError("左侧事件编号无效或重复")
    events = [EVENT_BY_ID.get(item) for item in event_ids]
    if any(event is None or not str(event["properties"].get("event_date", "")).startswith(year) for event in events):
        raise ValueError("左侧事件与所选年份不一致")
    job_id = uuid4().hex
    job = {"id": job_id, "year": year, "mode": mode, "status": "running", "completed": 0,
           "total": len(events), "current_event": "", "error": "", "body": None}
    with YEAR_EXPORT_LOCK:
        YEAR_EXPORT_JOBS[job_id] = job

    def progress(completed, total, current_event):
        with YEAR_EXPORT_LOCK:
            job.update(completed=completed, total=total, current_event=current_event)

    def worker():
        try:
            body = build_review_workbook(None, mode, year=year, progress=progress, event_ids=event_ids)
            with YEAR_EXPORT_LOCK:
                job.update(status="complete", body=body, current_event="")
        except Exception as exc:
            print("[platform] year export failed:", repr(exc))
            with YEAR_EXPORT_LOCK:
                job.update(status="failed", error=str(exc), current_event="")

    threading.Thread(target=worker, name=f"year-export-{year}-{job_id[:8]}", daemon=True).start()
    return {key: value for key, value in job.items() if key != "body"}


def _year_export_status(job_id: str) -> dict | None:
    with YEAR_EXPORT_LOCK:
        job = YEAR_EXPORT_JOBS.get(job_id)
        return {key: value for key, value in job.items() if key != "body"} if job else None

class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = "ForestChangePlatform/0.1"

    def log_message(self, format, *args):  # noqa: A002
        print("[platform] " + format % args)

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, rows: list[dict], filename: str):
        fieldnames = sorted({key for row in rows for key in row})
        output = __import__("io").StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        body = output.getvalue().encode("utf-8-sig")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_attachment(self, body: bytes, filename: str, content_type: str):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self._send_json({"ok": True, "earth_engine_ready": EE_READY, "earth_engine_error": EE_ERROR, "project": PROJECT})
                return
            if path == "/api/events":
                year = query.get("year", [""])[0]
                event_type = query.get("event_type", [""])[0]
                result = EVENTS
                if year:
                    result = [f for f in result if str(f["properties"].get("event_date", "")).startswith(year)]
                if event_type:
                    result = [f for f in result if f["properties"].get("event_type") == event_type]
                self._send_json({"type": "FeatureCollection", "features": [_event_payload(feature) for feature in result]})
                return
            if path == "/api/export/events.csv":
                rows = []
                for feature in EVENTS:
                    row = dict(feature["properties"])
                    row.update({f"review_{key}": value for key, value in _review_for(row["event_id"]).items()})
                    rows.append(row)
                self._send_csv(rows, "forest_change_review_events.csv")
                return
            if path == "/api/export/review.xlsx":
                event_id = query.get("event_id", [""])[0]
                mode = query.get("mode", ["core"])[0]
                body = build_review_workbook(event_id or None, mode)
                safe_event_id = event_id if event_id in EVENT_BY_ID else "all"
                filename = f"ForestChangeReview_{safe_event_id}_{mode}.xlsx"
                self._send_attachment(
                    body,
                    filename,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                return
            if path.startswith("/api/export/year/"):
                parts = path.strip("/").split("/")
                if len(parts) != 5 or parts[-1] not in {"status", "download"}:
                    self._send_json({"error": "endpoint_not_found"}, HTTPStatus.NOT_FOUND)
                    return
                job_id = parts[-2]
                public = _year_export_status(job_id)
                if public is None:
                    self._send_json({"error": "导出任务不存在或程序已重启"}, HTTPStatus.NOT_FOUND)
                    return
                if parts[-1] == "status":
                    self._send_json(public)
                    return
                if public["status"] != "complete":
                    self._send_json({"error": "年度工作簿尚未生成"}, HTTPStatus.CONFLICT)
                    return
                with YEAR_EXPORT_LOCK:
                    body = YEAR_EXPORT_JOBS[job_id]["body"]
                self._send_attachment(body, f"ForestChangeReview_{public['year']}_{public['mode']}.xlsx",
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                return
            if path.startswith("/api/event/") and path.endswith("/layers"):
                parts = path.split("/")
                event_id = parts[-2]
                mode = query.get("mode", ["core"])[0]
                self._send_json(build_layers(event_id, mode))
                return
            if path.startswith("/api/event/") and path.endswith("/timeseries"):
                parts = path.split("/")
                event_id = parts[-2]
                mode = query.get("mode", ["core"])[0]
                self._send_json(build_timeseries(event_id, mode))
                return
            if path.startswith("/api/event/") and path.endswith("/review"):
                event_id = path.split("/")[-2]
                if event_id not in EVENT_BY_ID:
                    self._send_json({"error": "event_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json({"event_id": event_id, **_review_for(event_id)})
                return
            if path.startswith("/api/event/"):
                event_id = path.split("/")[-1]
                feature = EVENT_BY_ID.get(event_id)
                if feature is None:
                    self._send_json({"error": "event_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(_event_payload(feature))
                return
            if path == "/" or path == "/index.html":
                self._send_file(STATIC_DIR / "index.html")
                return
            static_path = (STATIC_DIR / path.lstrip("/")).resolve()
            if STATIC_DIR.resolve() not in static_path.parents:
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self._send_file(static_path)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - runtime GEE failures
            print("[platform] request failed:", repr(exc))
            if os.environ.get("FOREST_CHANGE_DEBUG") == "1":
                traceback.print_exc()
            self._send_json({"error": str(exc), "type": type(exc).__name__}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/export/year":
                body = self._read_json_body()
                self._send_json(_start_year_export(str(body.get("year", "")), str(body.get("mode", "core")), body.get("event_ids")))
                return
            if path.startswith("/api/event/") and path.endswith("/review"):
                event_id = path.split("/")[-2]
                if event_id not in EVENT_BY_ID:
                    self._send_json({"error": "event_not_found"}, HTTPStatus.NOT_FOUND)
                    return
                entry = _save_review(event_id, self._read_json_body())
                self._send_json({"event_id": event_id, **entry})
                return
            self._send_json({"error": "endpoint_not_found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            print("[platform] POST failed:", repr(exc))
            self._send_json({"error": str(exc), "type": type(exc).__name__}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    host = "127.0.0.1"
    port = 8765
    try:
        server = ExclusiveThreadingHTTPServer((host, port), Handler)
    except OSError:
        server = ExclusiveThreadingHTTPServer((host, 0), Handler)
        port = server.server_port
        print(f"默认端口 8765 已占用，改用本机端口 {port}")
    print(f"森林变化智能辅助判读平台: http://{host}:{port}")
    print(f"Earth Engine project: {PROJECT}")
    if os.environ.get("FOREST_CHANGE_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: _open_external_browser(f"http://{host}:{port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n平台已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
