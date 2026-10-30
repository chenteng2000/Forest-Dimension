// 淳安国储林森林生态韧性：HLS 事件级提取
//
// 使用前：先把 priority30_event_polygons_upload.zip 解压后上传为 GEE
// Table asset，并把 EVENTS_ASSET 改成实际资产路径。推荐路径为：
// projects/mimetic-cooler-496216-t4/assets/priority30_event_polygons
//
// 事件图层只包含目前能够由项目矢量或实测点定位的事件；
// point_fallback_measured 仅用于定位辅助，不用于遥感面积统计。

var EVENTS_ASSET = 'projects/mimetic-cooler-496216-t4/assets/priority30_event_polygons';
var events = ee.FeatureCollection(EVENTS_ASSET);
var AOI_ASSET = '';
var aoi = AOI_ASSET ? ee.FeatureCollection(AOI_ASSET).geometry() : events.geometry().bounds();

// HLS v002 在 Earth Engine 中已经以反射率单位提供波段值。不要再次乘
// 0.0001，否则会把 EVI/NBR/NDMI 压低约四个数量级。
function scaleReflectance(img) {
  return img;
}

function maskHls(img) {
  var qa = img.select('Fmask');
  var clear = qa.bitwiseAnd(2).eq(0)
      .and(qa.bitwiseAnd(4).eq(0))
      .and(qa.bitwiseAnd(8).eq(0))
      .and(qa.bitwiseAnd(16).eq(0))
      .and(qa.bitwiseAnd(32).eq(0));
  return img.updateMask(clear);
}

function addIndices(img) {
  var blue = img.select('blue');
  var red = img.select('red');
  var nir = img.select('nir');
  var swir1 = img.select('swir1');
  var swir2 = img.select('swir2');
  var evi = nir.subtract(red).multiply(2.5)
      .divide(nir.add(red.multiply(6)).subtract(blue.multiply(7.5)).add(1))
      .rename('EVI');
  var nbr = nir.subtract(swir2).divide(nir.add(swir2)).rename('NBR');
  var ndmi = nir.subtract(swir1).divide(nir.add(swir1)).rename('NDMI');
  return img.addBands([evi, nbr, ndmi]);
}

function prepL30(img) {
  var sr = scaleReflectance(img.select(
      ['B2', 'B4', 'B5', 'B6', 'B7'],
      ['blue', 'red', 'nir', 'swir1', 'swir2']));
  return addIndices(maskHls(sr.addBands(img.select('Fmask'))))
      .copyProperties(img, ['system:time_start', 'CLOUD_COVERAGE']);
}

function prepS30(img) {
  var sr = scaleReflectance(img.select(
      ['B2', 'B4', 'B8A', 'B11', 'B12'],
      ['blue', 'red', 'nir', 'swir1', 'swir2']));
  return addIndices(maskHls(sr.addBands(img.select('Fmask'))))
      .copyProperties(img, ['system:time_start', 'CLOUD_COVERAGE']);
}

var l30 = ee.ImageCollection('NASA/HLS/HLSL30/v002')
    .filterBounds(aoi)
    .filterDate('2018-01-01', '2026-01-01')
    .filter(ee.Filter.lt('CLOUD_COVERAGE', 70))
    .map(prepL30);
var s30 = ee.ImageCollection('NASA/HLS/HLSS30/v002')
    .filterBounds(aoi)
    .filterDate('2018-01-01', '2026-01-01')
    .filter(ee.Filter.lt('CLOUD_COVERAGE', 70))
    .map(prepS30);
var prepared = l30.merge(s30);

// A zero-band median occurs when a date window has no scenes at all. This
// masked, fixed-band image keeps the mapped function valid and produces zero
// valid pixels for that event instead of failing the whole export.
var EMPTY_INDICES = ee.Image.constant([0, 0, 0])
    .rename(['EVI', 'NBR', 'NDMI'])
    .updateMask(ee.Image.constant(0));

function composite(col) {
  return ee.Image(ee.Algorithms.If(col.size().gt(0), col.median(), EMPTY_INDICES));
}

function stat(image, geom) {
  return image.select(['EVI', 'NBR', 'NDMI']).reduceRegion({
    reducer: ee.Reducer.median().combine({reducer2: ee.Reducer.count(), sharedInputs: true}),
    geometry: geom,
    scale: 30,
    maxPixels: 1e8,
    tileScale: 4
  });
}

function summarizeEvent(f) {
  var geom = f.geometry();
  var date = ee.Date(f.get('event_date'));
  var month = ee.Number(date.get('month'));
  var preCol = prepared
      .filterDate(date.advance(-3, 'year'), date.advance(-30, 'day'))
      .filter(ee.Filter.calendarRange(month.subtract(1).max(1), month.add(1).min(12), 'month'));
  var postCenter = date.advance(30, 'day');
  var postCol = prepared
      .filterDate(postCenter, postCenter.advance(90, 'day'));

  var pre = stat(composite(preCol), geom);
  var post = stat(composite(postCol), geom);
  var preCount = ee.Number(ee.Algorithms.If(pre.get('EVI_count'), pre.get('EVI_count'), 0));
  var postCount = ee.Number(ee.Algorithms.If(post.get('EVI_count'), post.get('EVI_count'), 0));
  var valid = preCount.gt(0).and(postCount.gt(0));
  var eviPre = ee.Number(ee.Algorithms.If(preCount.gt(0), pre.get('EVI_median'), 0));
  var eviPost = ee.Number(ee.Algorithms.If(postCount.gt(0), post.get('EVI_median'), 0));
  var nbrPre = ee.Number(ee.Algorithms.If(preCount.gt(0), pre.get('NBR_median'), 0));
  var nbrPost = ee.Number(ee.Algorithms.If(postCount.gt(0), post.get('NBR_median'), 0));
  var ndmiPre = ee.Number(ee.Algorithms.If(preCount.gt(0), pre.get('NDMI_median'), 0));
  var ndmiPost = ee.Number(ee.Algorithms.If(postCount.gt(0), post.get('NDMI_median'), 0));
  var safeEviPre = ee.Number(ee.Algorithms.If(eviPre.abs().lt(1e-9), 1, eviPre));

  return f.set({
    area_rs_ha: geom.area(1).divide(10000),
    evi_pre: eviPre,
    evi_post: eviPost,
    delta_evi_pct: ee.Algorithms.If(
        valid, eviPost.subtract(eviPre).divide(safeEviPre).multiply(100), -9999),
    nbr_pre: nbrPre,
    nbr_post: nbrPost,
    dnbr: ee.Algorithms.If(valid, nbrPre.subtract(nbrPost), -9999),
    ndmi_pre: ndmiPre,
    ndmi_post: ndmiPost,
    r_res_proxy: ee.Algorithms.If(valid, eviPost.divide(safeEviPre), -9999),
    r_rec: 'NA_recovery_target_not_set',
    t_rec: 'NA_observation_or_target_not_set',
    pre_obs: preCol.size(),
    post_obs: postCol.size(),
    valid_pixels_pre: preCount,
    valid_pixels_post: postCount,
    metric_status: ee.Algorithms.If(valid, 'valid_pre_post', 'no_valid_pre_or_post'),
    qa_note: 'HLS L30/S30 v002; Fmask bits 1-5 masked; 30 m; pre uses same-season 3-year baseline; post uses 30-120 day window without month restriction; recovery not evaluated'
  });
}

var results = events.map(summarizeEvent);
print('事件结果', results.limit(5));
Map.centerObject(aoi, 10);
Map.addLayer(events, {color: 'yellow'}, '事件图层');
Map.addLayer(prepared.select('EVI').median(),
    {min: 0, max: 1, palette: ['brown', 'yellow', 'green']}, 'HLS EVI median');
Export.table.toDrive({
  collection: results,
  description: 'chunan_hls_priority30_event_metrics',
  folder: 'chunan_hls_exports',
  fileNamePrefix: 'chunan_hls_priority30_event_metrics',
  fileFormat: 'CSV'
});
