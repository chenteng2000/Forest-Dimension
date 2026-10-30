import json
import sys
from pathlib import Path
sys.path.insert(0, r"D:\Workspace\Forest Dimension\gee_inputs\python_packages")
import ee
from run_gee_extract import feature_collection, make_collection

ee.Initialize(project="mimetic-cooler-496216-t4")
events = feature_collection()
f = ee.Feature(events.first())
prepared = make_collection(events)
date = ee.Date(f.get("event_date"))
month = ee.Number(date.get("month"))
pre_col = prepared.filterDate(date.advance(-3, "year"), date.advance(-30, "day")).filter(
    ee.Filter.calendarRange(month.subtract(1).max(1), month.add(1).min(12), "month")
)
post_center = date.advance(30, "day")
post_col = prepared.filterDate(post_center, post_center.advance(90, "day"))
print("event", f.get("operation_id").getInfo())
print("pre_count", pre_col.size().getInfo(), "post_count", post_col.size().getInfo())
reducer = ee.Reducer.median().combine(reducer2=ee.Reducer.count(), sharedInputs=True)
empty = ee.Image.constant([0, 0, 0]).rename(["EVI", "NBR", "NDMI"]).updateMask(ee.Image.constant(0))
pre_img = ee.Image(ee.Algorithms.If(pre_col.size().gt(0), pre_col.median(), empty))
post_img = ee.Image(ee.Algorithms.If(post_col.size().gt(0), post_col.median(), empty))
pre = pre_img.select(["EVI", "NBR", "NDMI"]).reduceRegion(
    reducer=reducer, geometry=f.geometry(), scale=30, maxPixels=1e8, tileScale=4
)
post = post_img.select(["EVI", "NBR", "NDMI"]).reduceRegion(
    reducer=reducer, geometry=f.geometry(), scale=30, maxPixels=1e8, tileScale=4
)
print("pre", pre.getInfo())
print("post", post.getInfo())
