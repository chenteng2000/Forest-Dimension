const state = {
  allEvents: [],
  visibleEvents: [],
  selectedId: null,
  mode: "core",
  preMap: null,
  postMap: null,
  preBase: null,
  postBase: null,
  preImage: null,
  postImage: null,
  changeMaskTile: null,
  changeVector: null,
  heatLayer: null,
  boundaryLayers: [],
  changeGeoJSON: null,
  yearExportRunning: false,
};

const $ = (selector) => document.querySelector(selector);

function fmt(value, digits = 2) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function eventLabel(feature) {
  const p = feature.properties;
  return `${p.event_id} · ${p.event_date || "无日期"}`;
}

function reviewLabel(status) {
  return { pending: "待复核", needs_review: "需补充", confirmed: "已确认", rejected: "已驳回" }[status] || "待复核";
}

function setHealth(ok, text) {
  const dot = $("#health-dot");
  dot.classList.remove("pending", "ok", "error");
  dot.classList.add(ok ? "ok" : "error");
  $("#health-text").textContent = text;
}

function initMaps() {
  const center = [29.61, 119.0];
  state.preMap = L.map("map-pre", { zoomControl: true, preferCanvas: true }).setView(center, 10);
  state.postMap = L.map("map-post", { zoomControl: false, preferCanvas: true }).setView(center, 10);
  const osm = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  state.preBase = L.tileLayer(osm, { attribution: "© OpenStreetMap contributors", maxZoom: 19 });
  state.postBase = L.tileLayer(osm, { attribution: "© OpenStreetMap contributors", maxZoom: 19 });
  state.preBase.addTo(state.preMap);
  state.postBase.addTo(state.postMap);

  let syncing = false;
  state.preMap.on("move", () => {
    if (syncing) return;
    syncing = true;
    state.postMap.setView(state.preMap.getCenter(), state.preMap.getZoom(), { animate: false });
    syncing = false;
  });
  state.postMap.on("move", () => {
    if (syncing) return;
    syncing = true;
    state.preMap.setView(state.postMap.getCenter(), state.postMap.getZoom(), { animate: false });
    syncing = false;
  });
}

function clearDynamicLayers() {
  for (const layer of [state.preImage, state.postImage, state.changeMaskTile, state.changeVector, state.heatLayer, ...state.boundaryLayers]) {
    if (layer) {
      state.preMap.removeLayer(layer);
      state.postMap.removeLayer(layer);
    }
  }
  state.preImage = null;
  state.postImage = null;
  state.changeMaskTile = null;
  state.changeVector = null;
  state.heatLayer = null;
  state.boundaryLayers = [];
  state.changeGeoJSON = null;
  $("#download-change").disabled = true;
}

function renderEventList() {
  const list = $("#event-list");
  list.innerHTML = "";
  $("#event-count").textContent = state.visibleEvents.length;
  if (!state.visibleEvents.length) {
    list.innerHTML = '<div class="detail-content empty" style="padding:20px">当前筛选没有事件。</div>';
    return;
  }
  for (const feature of state.visibleEvents) {
    const p = feature.properties;
    const button = document.createElement("button");
    button.className = `event-row ${state.selectedId === p.event_id ? "active" : ""}`;
    const reviewStatus = p.review_status || "pending";
    button.innerHTML = `
      <div class="row-top"><span class="event-id">${escapeHtml(p.event_id)}</span><span class="event-date">${escapeHtml(p.event_date || "—")}</span></div>
      <div class="event-location">${escapeHtml(p.location || p.township || "未填写位置")}</div>
      <span class="event-tag ${p.has_search_anchor ? "" : "pending"}">${p.has_search_anchor ? (p.anchor_source === "营造林项目矢量中心（仅定位）" ? "营造林矢量近似定位" : "实测点定位") : "待补定位坐标"}</span>
      <span class="review-pill ${reviewStatus}">${reviewLabel(reviewStatus)}</span>`;
    button.addEventListener("click", () => selectEvent(p.event_id));
    list.appendChild(button);
  }
}

function applyFilters() {
  const year = $("#year-filter").value;
  const type = $("#type-filter").value;
  const search = $("#search-input").value.trim().toLowerCase();
  state.visibleEvents = state.allEvents.filter((feature) => {
    const p = feature.properties;
    const text = [p.event_id, p.location, p.township, p.village, p.plot_text].join(" ").toLowerCase();
    return (!year || String(p.event_date || "").startsWith(year)) && (!type || p.event_type === type) && (!search || text.includes(search));
  });
  $("#export-year").disabled = !year || !state.visibleEvents.length || state.yearExportRunning;
  if (!state.yearExportRunning) $("#export-year").textContent = year
    ? `一键导出左侧 ${year} 年全部 ${state.visibleEvents.length} 条`
    : "请选择年份后导出左侧事件";
  if (!state.visibleEvents.length && year) {
    $("#year-export-status").textContent = `${year} 年左侧暂无采伐事件，当前没有可导出的事件。`;
  } else if (!state.yearExportRunning && $("#year-export-status").textContent.includes("暂无采伐事件")) {
    $("#year-export-status").textContent = "";
  }
  renderEventList();
  if (state.selectedId && !state.visibleEvents.some((feature) => feature.properties.event_id === state.selectedId)) {
    state.selectedId = null;
    clearDynamicLayers();
    $("#viewer-title").textContent = "当前年份暂无选中事件";
    $("#detail-title").textContent = "未选择事件";
    $("#detail-content").textContent = "从左侧事件表选择一条记录。";
    $("#detection-content").textContent = "当前没有可显示的识别结果。";
    $("#timeseries-chart").textContent = "当前没有可显示的时序。";
  }
  if (!state.selectedId && state.visibleEvents.length) {
    const first = state.visibleEvents.find((feature) => feature.properties.has_search_anchor) || state.visibleEvents[0];
    selectEvent(first.properties.event_id);
  }
}

function loadYearOptions() {
  const select = $("#year-filter");
  const years = [...new Set(["2023", "2024", "2025", ...state.allEvents.map((feature) => String(feature.properties.event_date || "").slice(0, 4)).filter((year) => /^\d{4}$/.test(year))])].sort();
  for (const year of years) {
    const option = document.createElement("option");
    option.value = year;
    const count = state.allEvents.filter((feature) => String(feature.properties.event_date || "").startsWith(year)).length;
    option.textContent = count ? `${year}（${count} 条）` : `${year}（暂无采伐事件）`;
    select.appendChild(option);
  }
  select.value = years.includes("2024") ? "2024" : years[years.length - 1] || "";
}

function loadTypeOptions() {
  const types = [...new Set(state.allEvents.map((feature) => feature.properties.event_type).filter(Boolean))];
  const select = $("#type-filter");
  for (const type of types) {
    const option = document.createElement("option");
    option.value = type;
    option.textContent = type === "harvest" ? "采伐" : type;
    select.appendChild(option);
  }
}

function fitFeature(feature) {
  if (!feature || !feature.geometry) return;
  const layer = L.geoJSON(feature);
  const bounds = layer.getBounds();
  if (bounds.isValid()) {
    state.preMap.fitBounds(bounds.pad(0.35), { animate: false });
    state.postMap.fitBounds(bounds.pad(0.35), { animate: false });
  }
}

function renderBoundary(feature) {
  if (!feature.geometry) return;
  const boundary = L.geoJSON(feature, { style: { color: "#ffcf33", weight: 3, fill: false, opacity: 0.95 } });
  boundary.addTo(state.preMap);
  const boundary2 = L.geoJSON(feature, { style: { color: "#ffcf33", weight: 3, fill: false, opacity: 0.95 } });
  boundary2.addTo(state.postMap);
  state.boundaryLayers = [boundary, boundary2];
}

function renderChangePolygons(payload) {
  state.changeGeoJSON = payload || null;
  $("#download-change").disabled = !payload || !payload.features || !payload.features.length;
  if (!payload || !payload.features || !payload.features.length) {
    $("#detection-content").innerHTML = '<div class="notice">自动筛查完成：当前阈值下未生成疑似变化单元。候选条件为前期 NDVI ≥ 0.40、NDVI 下降 ≥ 0.15；请结合影像质量与时相核查。</div>';
    return;
  }
  const area = payload.features.reduce((sum, feature) => sum + Number(feature.properties?.area_m2 || 0), 0);
  const labelColor = (assessment) => assessment === "多指标方向一致" ? "#d94b3d" : assessment === "指标方向分歧" ? "#df9229" : "#778986";
  const counts = payload.features.reduce((result, feature) => {
    const label = feature.properties?.assessment || "数据不足";
    result[label] = (result[label] || 0) + 1;
    return result;
  }, {});
  const layer = L.geoJSON(payload, {
    style: (feature) => {
      const color = labelColor(feature.properties?.assessment);
      return { color, weight: 2, fillColor: color, fillOpacity: 0.36 };
    },
    onEachFeature: (feature, vectorLayer) => {
      const properties = feature.properties || {};
      const patchId = properties.patch_id || "识别单元";
      const patchArea = Number(properties.area_m2 || 0);
      vectorLayer.bindPopup(
        `<strong>${escapeHtml(patchId)}</strong><br><strong>${escapeHtml(properties.assessment || "数据不足")}</strong><br>${escapeHtml(properties.assessment_basis || "待计算") }<br>${escapeHtml(properties.review_advice || "请结合原始影像核实") }<br>${Number(properties.pixel_count || 0)} 个识别像元 · ${fmt(patchArea / 10000, 4)} ha<br>中心：${fmt(properties.center_lon, 6)}, ${fmt(properties.center_lat, 6)}`
      );
    },
  });
  layer.addTo(state.postMap);
  state.changeVector = layer;
  const unitRows = payload.features.map((feature) => {
    const p = feature.properties || {};
    const color = labelColor(p.assessment);
    return `<tr><td>${escapeHtml(p.patch_id || "—")}</td><td><span class="assessment-pill" style="--assessment-color:${color}">${escapeHtml(p.assessment || "数据不足")}</span></td><td>${fmt(p.area_ha, 4)}</td><td>${escapeHtml(p.assessment_basis || "—")}</td></tr>`;
  }).join("");
  $("#detection-content").innerHTML = `
    <div class="intelligence-summary"><strong>自动判读：${payload.features.length} 个疑似变化单元</strong><span>多指标方向一致 ${counts["多指标方向一致"] || 0} · 指标方向分歧 ${counts["指标方向分歧"] || 0} · 数据不足 ${counts["数据不足"] || 0}</span></div>
    <div class="detail-table">
      <div class="detail-item"><span>识别单元</span><strong>${payload.features.length} 个</strong></div>
      <div class="detail-item"><span>识别像元总数</span><strong>${payload.features.reduce((sum, item) => sum + Number(item.properties?.pixel_count || 0), 0)} 个</strong></div>
      <div class="detail-item"><span>识别面积</span><strong>${fmt(area / 10000, 3)} ha · ${fmt(area * 0.0015, 2)} 亩</strong></div>
      <div class="detail-item"><span>候选筛查</span><strong>NDVI 下降 ≥ 0.15</strong></div>
      <div class="detail-item"><span>前期植被门槛</span><strong>NDVI ≥ 0.40</strong></div>
    </div>
    <div class="assessment-legend"><span style="color:#d94b3d">● 多指标方向一致</span><span style="color:#df9229">● 指标方向分歧</span><span style="color:#778986">● 数据不足</span></div>
    <div class="assessment-table-wrap"><table class="series-table"><thead><tr><th>识别单元</th><th>辅助判读</th><th>面积 ha</th><th>可追溯依据</th></tr></thead><tbody>${unitRows}</tbody></table></div>
    <p class="muted" style="margin-top:12px">颜色表示指数变化方向的一致性，并非采伐概率。面积按 10 m 识别像元数 × 100 m² 计算。请结合原始影像、HLS 时序和作业资料核实变化原因。</p>`;
}

function renderTimeSeries(payload) {
  const holder = $("#timeseries-chart");
  const records = payload.points || [];
  const points = records.filter((point) => point.evi !== null && point.evi !== undefined && point.nbr !== null && point.nbr !== undefined)
    .map((point) => ({ ...point, time: new Date(`${point.date || point.start}T00:00:00`).getTime() }))
    .filter((point) => Number.isFinite(point.time)).sort((a, b) => a.time - b.time);
  if (!points.length) {
    holder.className = "timeseries-chart empty";
    holder.textContent = "这些窗口内暂无有效 HLS EVI/NBR 观测；请查看影像与观测状态。";
    return;
  }
  const width = 900;
  const height = 260;
  const margin = { top: 28, right: 30, bottom: 45, left: 48 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const values = points.flatMap((point) => [point.evi, point.nbr].map(Number));
  if (payload.baseline?.evi !== null && payload.baseline?.evi !== undefined) values.push(Number(payload.baseline.evi));
  const minValue = Math.max(-1, Math.floor((Math.min(...values) - 0.08) * 10) / 10);
  const maxValue = Math.min(1.5, Math.ceil((Math.max(...values) + 0.08) * 10) / 10);
  const start = points[0].time;
  const end = points[points.length - 1].time;
  const x = (time) => margin.left + ((time - start) / Math.max(1, end - start)) * plotW;
  const y = (value) => margin.top + (1 - (value - minValue) / Math.max(0.01, maxValue - minValue)) * plotH;
  const pathFor = (key) => points.map((point, index) => `${index ? "L" : "M"}${x(point.time).toFixed(1)},${y(Number(point[key])).toFixed(1)}`).join(" ");
  const grid = [];
  for (let i = 0; i <= 4; i += 1) {
    const value = minValue + ((maxValue - minValue) * i) / 4;
    const yy = y(value);
    grid.push(`<line class="chart-grid" x1="${margin.left}" x2="${width - margin.right}" y1="${yy}" y2="${yy}"/><text class="chart-label" x="8" y="${yy + 3}">${value.toFixed(2)}</text>`);
  }
  const ticks = [points[0], points[Math.floor(points.length / 2)], points[points.length - 1]];
  const tickLabels = ticks.map((point) => `<text class="chart-label" text-anchor="middle" x="${x(point.time)}" y="${height - 12}">${escapeHtml((point.date || point.start).slice(0, 7))}</text>`).join("");
  const eventTime = new Date(`${payload.reference_date}T00:00:00`).getTime();
  const eventX = eventTime >= start && eventTime <= end ? x(eventTime) : null;
  const eventMarker = eventX === null ? "" : `<line class="chart-event-line" x1="${eventX}" x2="${eventX}" y1="${margin.top}" y2="${height - margin.bottom}"/><text class="chart-event-label" x="${eventX + 5}" y="${margin.top + 11}">${payload.reviewed_actual_date ? "核验日期" : "许可起始"}</text>`;
  const baselineLine = payload.baseline?.evi === null || payload.baseline?.evi === undefined ? "" : `<line x1="${margin.left}" x2="${width - margin.right}" y1="${y(Number(payload.baseline.evi))}" y2="${y(Number(payload.baseline.evi))}" stroke="#d49a2a" stroke-dasharray="5 4"/><text x="${width - 125}" y="${y(Number(payload.baseline.evi)) - 5}" fill="#a87314" font-size="12">前 3 年 EVI 基线</text>`;
  const circles = points.map((point) => `<circle cx="${x(point.time)}" cy="${y(Number(point.evi))}" r="3.5" fill="#208163"/><circle cx="${x(point.time)}" cy="${y(Number(point.nbr))}" r="3.5" fill="#3379b8"/>`).join("");
  holder.className = "timeseries-chart";
  const row = (point) => `<tr><td>${escapeHtml(point.label || "—")}</td><td>${escapeHtml(point.start || "—")} 至 ${escapeHtml(point.end || "—")}</td><td>${fmt(point.evi, 3)}</td><td>${fmt(point.nbr, 3)}</td><td>${fmt(point.ndmi, 3)}</td><td>${point.scene_count ?? 0}</td><td>${fmt(point.mean_valid_observations_per_pixel, 1)}</td><td>${escapeHtml(point.status || "—")}</td></tr>`;
  const recovery = payload.recovery || [];
  holder.innerHTML = `<div class="muted" style="margin-bottom:8px">${escapeHtml(payload.sensor || "HLS")} · ${payload.resolution_m || 30} m · ${escapeHtml(payload.unit_scope || "")}</div><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="HLS EVI 和 NBR 时序图">
    ${grid.join("")}
    <line class="chart-axis" x1="${margin.left}" x2="${width - margin.right}" y1="${height - margin.bottom}" y2="${height - margin.bottom}"/>
    <line class="chart-axis" x1="${margin.left}" x2="${margin.left}" y1="${margin.top}" y2="${height - margin.bottom}"/>
    ${baselineLine}${eventMarker}<path d="${pathFor("evi")}" fill="none" stroke="#208163" stroke-width="2.5"/><path d="${pathFor("nbr")}" fill="none" stroke="#3379b8" stroke-width="2.5"/>${circles}${tickLabels}
  </svg><div class="muted"><span style="color:#208163">● EVI</span>　<span style="color:#3379b8">● NBR</span>　${escapeHtml(payload.date_basis || "")}；许可期 ${escapeHtml(payload.license_start || "")} 至 ${escapeHtml(payload.license_end || "")}。折线连接相邻有效窗口，横向间隔按日期绘制。</div>
  <div class="muted" style="margin-top:9px">${escapeHtml(payload.valid_observation_note || "")}</div>
  <div class="muted" style="margin-top:9px">前 3 年同季节 EVI 基线：${fmt(payload.baseline?.evi, 3)}；扰动后第 1 / 2 / 3 年 EVI：${recovery.map((item) => `${fmt(item.evi, 3)}（${escapeHtml(item.status || "待观察")}）`).join(" / ") || "待观察"}。恢复程度须结合明确的恢复目标与后续年份判读。</div>
  <div style="overflow-x:auto;margin-top:14px"><table class="series-table"><thead><tr><th>阶段</th><th>窗口</th><th>EVI</th><th>NBR</th><th>NDMI</th><th>影像数</th><th>平均有效观测</th><th>状态</th></tr></thead><tbody>${[...(payload.baseline_annual || []), ...(payload.baseline ? [payload.baseline] : []), ...(payload.operation_series || []), ...recovery].map(row).join("")}</tbody></table></div>`;
}

function renderDetail(feature, layerData) {
  const p = feature.properties;
  $("#review-status").value = p.review_status || "pending";
  $("#reviewer-name").value = p.review_reviewer || "";
  $("#actual-disturbance-date").value = p.review_actual_disturbance_date || "";
  $("#review-note").value = p.review_note || "";
  $("#review-feedback").textContent = p.review_updated_at ? `上次保存：${p.review_updated_at}` : "尚未保存";
  $("#detail-title").textContent = eventLabel(feature);
  $("#detail-content").innerHTML = `
    <div class="detail-table">
      <div class="detail-item"><span>许可/台账期</span><strong>${escapeHtml(p.date_start || p.event_date || "—")} 至 ${escapeHtml(p.date_end || "—")}</strong></div>
      <div class="detail-item"><span>实际扰动日期</span><strong>${escapeHtml(p.review_actual_disturbance_date || "待人工核验")}</strong></div>
      <div class="detail-item"><span>地点</span><strong>${escapeHtml(p.location || "—")}</strong></div>
      <div class="detail-item"><span>小班号</span><strong>${escapeHtml(p.plot_tokens || p.plot_text || "—")}</strong></div>
      <div class="detail-item"><span>采伐强度</span><strong>${escapeHtml(p.intensity_level || "—")} / ${fmt(p.intensity_pct, 1)}%</strong></div>
      <div class="detail-item"><span>台账面积</span><strong>${fmt(p.area_record_ha, 3)} ha</strong></div>
      <div class="detail-item"><span>定位方式</span><strong>${escapeHtml(p.anchor_source || "待补坐标")}</strong></div>
    </div>`;
  if (!p.has_search_anchor) {
    $("#detection-content").innerHTML = '<div class="notice">该事件目前没有可用定位坐标。平台保留原始采伐记录，补充坐标后即可运行影像识别。</div>';
    return;
  }
  $("#pre-period").textContent = `${layerData.pre_period.start} — ${layerData.pre_period.end}`;
  $("#post-period").textContent = `${layerData.post_period.start} — ${layerData.post_period.end}`;
  $("#viewer-meta").textContent = `${layerData.period_label} · ${layerData.sensor} · ${layerData.resolution_m} m · 前 ${layerData.pre_period.scene_count} 景 / 后 ${layerData.post_period.scene_count} 景`;
  const quality = layerData.quality || {};
  const sourceLabels = {
    strict_measured_plot_match: "严格配准实测点",
    measured_point: "实测点坐标",
    vector_geometry_centroid: "营造林项目矢量中心（仅定位）",
    vector_point_anchor: "营造林项目矢量点位（仅定位）",
    vector_point_anchors: "营造林项目矢量点位（仅定位）",
  };
  $("#detail-content").innerHTML += `
    <div class="quality-strip">
      <span>搜索窗口 NDVI 均值 前：<strong>${fmt(quality.pre_ndvi_mean, 3)}</strong></span>
      <span>后：<strong>${fmt(quality.post_ndvi_mean, 3)}</strong></span>
      <span>差值：<strong>${fmt(quality.ndvi_change, 3)}</strong></span>
      <span>有效像元：<strong>${quality.pre_valid_pixels ?? "—"} / ${quality.post_valid_pixels ?? "—"}</strong></span>
    </div>`;
  $("#layer-note").textContent = `${sourceLabels[layerData.anchor_source] || "定位锚点"} · 搜索半径 ${Math.round(layerData.search_radius_m)} m；营造林项目矢量边界不参与识别。`;
}

async function selectEvent(eventId) {
  state.selectedId = eventId;
  renderEventList();
  const feature = state.allEvents.find((item) => item.properties.event_id === eventId);
  if (!feature) return;
  $("#viewer-title").textContent = eventLabel(feature);
  clearDynamicLayers();
  if (!feature.properties.has_search_anchor) {
    renderDetail(feature, {});
    renderTimeSeries({ points: [] });
    return;
  }
  $("#timeseries-chart").className = "timeseries-chart empty";
  $("#timeseries-chart").textContent = "正在加载 HLS EVI、NBR、NDMI 同季节时序…";
  $("#loading").classList.remove("hidden");
  $("#layer-note").textContent = "正在按定位锚点搜索 Sentinel-2 影像并识别变化像元…";
  try {
    const response = await fetch(`/api/event/${encodeURIComponent(eventId)}/layers?mode=${state.mode}`);
    const payload = await response.json();
    if (state.selectedId !== eventId) return;
    if (!response.ok) throw new Error(payload.error || "影像请求失败");
    setHealth(true, "Earth Engine 已连接 · mimetic-cooler-496216-t4");
    state.preImage = L.tileLayer(payload.tiles.pre_rgb, { opacity: 1, attribution: "Google Earth Engine / Sentinel-2" }).addTo(state.preMap);
    state.postImage = L.tileLayer(payload.tiles.post_rgb, { opacity: 1, attribution: "Google Earth Engine / Sentinel-2" }).addTo(state.postMap);
    state.changeMaskTile = L.tileLayer(payload.tiles.change_mask, { opacity: 0.82, attribution: "NDVI change" }).addTo(state.postMap);
    state.heatLayer = L.tileLayer(payload.tiles.change_heat, { opacity: 0.55, attribution: "NDVI difference" });
    const detectedBounds = L.geoJSON(payload.detected_change).getBounds();
    if (detectedBounds.isValid()) {
      state.preMap.fitBounds(detectedBounds.pad(0.5), { animate: false });
      state.postMap.fitBounds(detectedBounds.pad(0.5), { animate: false });
    } else if (payload.map_center) {
      const center = [payload.map_center.latitude, payload.map_center.longitude];
      state.preMap.setView(center, 14, { animate: false });
      state.postMap.setView(center, 14, { animate: false });
    }
    renderDetail(feature, payload);
    renderChangePolygons(payload.detected_change);
    try {
      const tsResponse = await fetch(`/api/event/${encodeURIComponent(eventId)}/timeseries?mode=${state.mode}`);
      const tsPayload = await tsResponse.json();
      if (!tsResponse.ok) throw new Error(tsPayload.error || "时序请求失败");
      if (state.selectedId === eventId) renderTimeSeries(tsPayload);
    } catch (seriesError) {
      if (state.selectedId === eventId) {
        $("#timeseries-chart").className = "timeseries-chart empty";
        $("#timeseries-chart").textContent = `HLS 时序暂不可用：${seriesError.message}`;
      }
    }
    $("#toggle-change").checked = true;
    $("#toggle-heat").checked = false;
    const sourceLabels = {
      strict_measured_plot_match: "严格配准实测点",
      measured_point: "实测点坐标",
      vector_geometry_centroid: "营造林项目矢量中心（仅定位）",
      vector_point_anchor: "营造林项目矢量点位（仅定位）",
      vector_point_anchors: "营造林项目矢量点位（仅定位）",
    };
    $("#layer-note").textContent = `${payload.detected_change.features.length} 个疑似变化单元 · 多指标辅助判读 · ${sourceLabels[payload.anchor_source] || "定位锚点"} · 搜索半径 ${Math.round(payload.search_radius_m)} m；范围由变化像元生成。`;
  } catch (error) {
    if (state.selectedId !== eventId) return;
    $("#detection-content").innerHTML = `<div class="notice">${escapeHtml(error.message)}</div>`;
    $("#layer-note").textContent = "影像请求失败，请查看平台终端日志。";
  } finally {
    if (state.selectedId === eventId) $("#loading").classList.add("hidden");
  }
}

async function saveReview() {
  if (!state.selectedId) return;
  const button = $("#save-review");
  const feedback = $("#review-feedback");
  button.disabled = true;
  feedback.textContent = "正在保存…";
  try {
    const response = await fetch(`/api/event/${encodeURIComponent(state.selectedId)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        status: $("#review-status").value,
        reviewer: $("#reviewer-name").value,
        note: $("#review-note").value,
        actual_disturbance_date: $("#actual-disturbance-date").value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "保存失败");
    const feature = state.allEvents.find((item) => item.properties.event_id === state.selectedId);
    if (feature) {
      feature.properties.review_status = payload.status;
      feature.properties.review_reviewer = payload.reviewer;
      feature.properties.review_note = payload.note;
      feature.properties.review_actual_disturbance_date = payload.actual_disturbance_date;
      feature.properties.review_updated_at = payload.updated_at;
    }
    feedback.textContent = `已保存：${payload.updated_at}`;
    renderEventList();
    if (feature) selectEvent(state.selectedId);
  } catch (error) {
    feedback.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function downloadChangeGeoJSON() {
  if (!state.changeGeoJSON || !state.selectedId) return;
  const blob = new Blob([JSON.stringify(state.changeGeoJSON, null, 2)], { type: "application/geo+json;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${state.selectedId}_sentinel2_detected_units.geojson`;
  link.click();
  URL.revokeObjectURL(link.href);
}

async function exportReviewWorkbook() {
  const button = $("#export-review");
  if (button.disabled) return;
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "正在整理工作簿…";
  try {
    const query = new URLSearchParams({ event_id: state.selectedId || "", mode: state.mode });
    const response = await fetch(`/api/export/review.xlsx?${query.toString()}`);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || "核查工作簿导出失败");
    }
    const blob = await response.blob();
    const downloadUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = downloadUrl;
    link.download = `淳安森林变化智能辅助判读_${state.selectedId || "全部事件"}_${state.mode}.xlsx`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
  } catch (error) {
    $("#layer-note").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function exportYearWorkbook() {
  const year = $("#year-filter").value;
  if (!year) return;
  const eventIds = state.visibleEvents.map((feature) => feature.properties.event_id);
  if (!eventIds.length) return;
  const button = $("#export-year");
  const status = $("#year-export-status");
  state.yearExportRunning = true;
  button.disabled = true;
  button.textContent = `正在导出左侧 ${eventIds.length} 条…`;
  status.textContent = `左侧 ${year} 年 ${eventIds.length} 条事件正在排队处理，请保持程序运行。`;
  try {
    const startResponse = await fetch("/api/export/year", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ year, mode: state.mode, event_ids: eventIds }),
    });
    const job = await startResponse.json();
    if (!startResponse.ok) throw new Error(job.error || "年度导出启动失败");
    let latest = job;
    while (latest.status === "running") {
      status.textContent = `左侧 ${year} 年事件：已处理 ${latest.completed}/${latest.total} 条${latest.current_event ? ` · ${latest.current_event}` : ""}。`;
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const response = await fetch(`/api/export/year/${encodeURIComponent(job.id)}/status`);
      latest = await response.json();
      if (!response.ok) throw new Error(latest.error || "无法获取导出进度");
    }
    if (latest.status !== "complete") throw new Error(latest.error || "年度导出失败");
    const response = await fetch(`/api/export/year/${encodeURIComponent(job.id)}/download`);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || "年度工作簿下载失败");
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `淳安森林变化智能辅助判读_${year}_左侧${eventIds.length}条_${state.mode}.xlsx`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    status.textContent = `左侧 ${year} 年 ${latest.total} 条记录已整理完成；请查看下载的 Excel 中“批量处理状态”表。`;
  } catch (error) {
    status.textContent = `年度导出失败：${error.message}`;
  } finally {
    state.yearExportRunning = false;
    applyFilters();
  }
}

async function loadInitialData() {
  const health = await fetch("/api/health").then((response) => response.json());
  if (health.ok && health.earth_engine_ready) setHealth(true, `Earth Engine 已连接 · ${health.project}`);
  else if (health.ok) setHealth(true, `平台已启动 · 选择事件后连接 Earth Engine`);
  else setHealth(false, "Earth Engine 未连接");
  const response = await fetch("/api/events");
  const payload = await response.json();
  state.allEvents = payload.features || [];
  loadYearOptions();
  loadTypeOptions();
  applyFilters();
}

$("#year-filter").addEventListener("change", () => {
  const year = $("#year-filter").value;
  if (Number(year) > 2024 && state.mode === "core") {
    state.mode = "extended";
    document.querySelectorAll(".mode").forEach((item) => item.classList.toggle("active", item.dataset.mode === "extended"));
  }
  applyFilters();
});
$("#type-filter").addEventListener("change", applyFilters);
$("#search-input").addEventListener("input", applyFilters);
document.querySelectorAll(".mode").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".mode").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  state.mode = button.dataset.mode;
  if (state.selectedId) selectEvent(state.selectedId);
}));
$("#toggle-change").addEventListener("change", (event) => {
  for (const layer of [state.changeMaskTile, state.changeVector]) {
    if (!layer) continue;
    if (event.target.checked) layer.addTo(state.postMap);
    else state.postMap.removeLayer(layer);
  }
});
$("#toggle-heat").addEventListener("change", (event) => {
  if (!state.heatLayer) return;
  if (event.target.checked) state.heatLayer.addTo(state.postMap);
  else state.postMap.removeLayer(state.heatLayer);
});
$("#save-review").addEventListener("click", saveReview);
$("#download-change").addEventListener("click", downloadChangeGeoJSON);
$("#export-review").addEventListener("click", exportReviewWorkbook);
$("#export-year").addEventListener("click", exportYearWorkbook);

initMaps();
loadInitialData().catch((error) => {
  setHealth(false, "平台初始化失败");
  $("#event-list").innerHTML = `<div class="notice" style="margin:15px">${escapeHtml(error.message)}</div>`;
});
