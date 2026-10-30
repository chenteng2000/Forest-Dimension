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
      <span class="event-tag ${p.has_geometry ? "" : "pending"}">${p.has_geometry ? "可对比影像" : "待补空间边界"}</span>
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
  renderEventList();
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
    $("#detection-content").innerHTML = '<div class="notice">当前阈值下未生成疑似变化斑块。请检查影像质量，或切换到扩展时段。</div>';
    return;
  }
  const area = payload.features.reduce((sum, feature) => sum + Number(feature.properties?.area_m2 || 0), 0);
  const layer = L.geoJSON(payload, { style: { color: "#f04438", weight: 2, fillColor: "#f04438", fillOpacity: 0.34 } });
  layer.addTo(state.postMap);
  state.changeVector = layer;
  $("#detection-content").innerHTML = `
    <div class="detail-table">
      <div class="detail-item"><span>疑似变化斑块</span><strong>${payload.features.length} 个</strong></div>
      <div class="detail-item"><span>合计面积</span><strong>${fmt(area / 10000, 3)} ha</strong></div>
      <div class="detail-item"><span>NDVI 下降阈值</span><strong>≥ 0.15</strong></div>
      <div class="detail-item"><span>变化前 NDVI 门槛</span><strong>≥ 0.40</strong></div>
    </div>
    <p class="muted" style="margin-top:12px">红色斑块是候选变化区域，需结合采伐台账和原始影像进行人工核实。</p>`;
}

function renderTimeSeries(payload) {
  const holder = $("#timeseries-chart");
  const points = (payload.points || [])
    .map((point) => ({ date: new Date(point.date), label: point.date, ndvi: Number(point.ndvi), pixels: Number(point.valid_pixels || 0) }))
    .filter((point) => Number.isFinite(point.ndvi))
    .sort((a, b) => a.date - b.date);
  if (!points.length) {
    holder.className = "timeseries-chart empty";
    holder.textContent = "当前时间范围内没有可用的 Sentinel-2 NDVI 观测。";
    return;
  }
  const width = 900;
  const height = 235;
  const margin = { top: 18, right: 28, bottom: 38, left: 42 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const values = points.map((point) => point.ndvi);
  const minValue = Math.max(0, Math.floor((Math.min(...values) - 0.05) * 10) / 10);
  const maxValue = Math.min(1, Math.ceil((Math.max(...values) + 0.05) * 10) / 10 || 1);
  const start = points[0].date.getTime();
  const end = points[points.length - 1].date.getTime() || start + 1;
  const x = (date) => margin.left + ((date.getTime() - start) / Math.max(1, end - start)) * plotW;
  const y = (value) => margin.top + (1 - (value - minValue) / Math.max(0.01, maxValue - minValue)) * plotH;
  const path = points.map((point, index) => `${index ? "L" : "M"}${x(point.date).toFixed(1)},${y(point.ndvi).toFixed(1)}`).join(" ");
  const grid = [];
  for (let i = 0; i <= 4; i += 1) {
    const value = minValue + ((maxValue - minValue) * i) / 4;
    const yy = y(value);
    grid.push(`<line class="chart-grid" x1="${margin.left}" x2="${width - margin.right}" y1="${yy}" y2="${yy}"/><text class="chart-label" x="8" y="${yy + 3}">${value.toFixed(2)}</text>`);
  }
  const ticks = [points[0], points[Math.floor(points.length / 2)], points[points.length - 1]];
  const tickLabels = ticks.map((point) => `<text class="chart-label" text-anchor="middle" x="${x(point.date)}" y="${height - 12}">${point.label.slice(0, 7)}</text>`).join("");
  const eventDate = new Date(payload.event_date);
  const eventX = eventDate >= points[0].date && eventDate <= points[points.length - 1].date ? x(eventDate) : null;
  const eventMarker = eventX === null ? "" : `<line class="chart-event-line" x1="${eventX}" x2="${eventX}" y1="${margin.top}" y2="${height - margin.bottom}"/><text class="chart-event-label" x="${eventX + 5}" y="${margin.top + 11}">作业日期</text>`;
  const circles = points.map((point) => `<circle class="chart-point" cx="${x(point.date)}" cy="${y(point.ndvi)}" r="3"/>`).join("");
  holder.className = "timeseries-chart";
  holder.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="NDVI 时序图">
    ${grid.join("")}
    <line class="chart-axis" x1="${margin.left}" x2="${width - margin.right}" y1="${height - margin.bottom}" y2="${height - margin.bottom}"/>
    <line class="chart-axis" x1="${margin.left}" x2="${margin.left}" y1="${margin.top}" y2="${height - margin.bottom}"/>
    ${eventMarker}<path class="chart-line" d="${path}"/>${circles}${tickLabels}
  </svg><div class="muted">${payload.period_label} · ${points.length} 景有效观测；缺测日期不连线，红色虚线为台账作业日期。</div>`;
}

function renderDetail(feature, layerData) {
  const p = feature.properties;
  const deltaClass = Number(p.delta_evi_pct) < -10 ? "metric-good" : "metric-neutral";
  $("#review-status").value = p.review_status || "pending";
  $("#reviewer-name").value = p.review_reviewer || "";
  $("#review-note").value = p.review_note || "";
  $("#review-feedback").textContent = p.review_updated_at ? `上次保存：${p.review_updated_at}` : "尚未保存";
  $("#detail-title").textContent = eventLabel(feature);
  $("#detail-content").innerHTML = `
    <div class="detail-table">
      <div class="detail-item"><span>时间</span><strong>${escapeHtml(p.date_start || p.event_date || "—")} 至 ${escapeHtml(p.date_end || "—")}</strong></div>
      <div class="detail-item"><span>地点</span><strong>${escapeHtml(p.location || "—")}</strong></div>
      <div class="detail-item"><span>小班号</span><strong>${escapeHtml(p.plot_tokens || p.plot_text || "—")}</strong></div>
      <div class="detail-item"><span>采伐强度</span><strong>${escapeHtml(p.intensity_level || "—")} / ${fmt(p.intensity_pct, 1)}%</strong></div>
      <div class="detail-item"><span>台账面积</span><strong>${fmt(p.area_record_ha, 3)} ha</strong></div>
      <div class="detail-item"><span>空间状态</span><strong>${p.has_geometry ? "已有边界" : "待补边界"}</strong></div>
      <div class="detail-item"><span>HLS EVI 相对变化</span><strong class="${deltaClass}">${fmt(p.delta_evi_pct, 2)}%</strong></div>
      <div class="detail-item"><span>HLS dNBR</span><strong>${fmt(p.dnbr, 4)}</strong></div>
    </div>`;
  if (!p.has_geometry) {
    $("#detection-content").innerHTML = '<div class="notice">该事件目前没有可靠空间边界，平台保留台账记录，但暂不生成影像对比和变化框。</div>';
    return;
  }
  $("#pre-period").textContent = `${layerData.pre_period.start} — ${layerData.pre_period.end}`;
  $("#post-period").textContent = `${layerData.post_period.start} — ${layerData.post_period.end}`;
  $("#viewer-meta").textContent = `${layerData.period_label} · ${layerData.sensor} · ${layerData.resolution_m} m · 前 ${layerData.pre_period.scene_count} 景 / 后 ${layerData.post_period.scene_count} 景`;
  const quality = layerData.quality || {};
  $("#detail-content").innerHTML += `
    <div class="quality-strip">
      <span>Sentinel-2 NDVI 前：<strong>${fmt(quality.pre_ndvi_median, 3)}</strong></span>
      <span>后：<strong>${fmt(quality.post_ndvi_median, 3)}</strong></span>
      <span>差值：<strong>${fmt(quality.ndvi_change, 3)}</strong></span>
      <span>有效像元：<strong>${quality.pre_valid_pixels ?? "—"} / ${quality.post_valid_pixels ?? "—"}</strong></span>
    </div>`;
}

async function selectEvent(eventId) {
  state.selectedId = eventId;
  renderEventList();
  const feature = state.allEvents.find((item) => item.properties.event_id === eventId);
  if (!feature) return;
  $("#viewer-title").textContent = eventLabel(feature);
  clearDynamicLayers();
  fitFeature(feature);
  renderBoundary(feature);
  if (!feature.geometry) {
    renderDetail(feature, {});
    renderTimeSeries({ points: [] });
    return;
  }
  $("#timeseries-chart").className = "timeseries-chart empty";
  $("#timeseries-chart").textContent = "正在加载逐景 NDVI 时序…";
  $("#loading").classList.remove("hidden");
  $("#layer-note").textContent = "正在生成 Sentinel-2 前后影像和变化斑块…";
  try {
    const [response, tsResponse] = await Promise.all([
      fetch(`/api/event/${encodeURIComponent(eventId)}/layers?mode=${state.mode}`),
      fetch(`/api/event/${encodeURIComponent(eventId)}/timeseries?mode=${state.mode}`),
    ]);
    const payload = await response.json();
    const tsPayload = await tsResponse.json();
    if (!response.ok) throw new Error(payload.error || "影像请求失败");
    if (!tsResponse.ok) throw new Error(tsPayload.error || "时序请求失败");
    setHealth(true, "Earth Engine 已连接 · mimetic-cooler-496216-t4");
    state.preImage = L.tileLayer(payload.tiles.pre_rgb, { opacity: 1, attribution: "Google Earth Engine / Sentinel-2" }).addTo(state.preMap);
    state.postImage = L.tileLayer(payload.tiles.post_rgb, { opacity: 1, attribution: "Google Earth Engine / Sentinel-2" }).addTo(state.postMap);
    state.changeMaskTile = L.tileLayer(payload.tiles.change_mask, { opacity: 0.82, attribution: "NDVI change" }).addTo(state.postMap);
    state.heatLayer = L.tileLayer(payload.tiles.change_heat, { opacity: 0.55, attribution: "NDVI difference" });
    renderDetail(feature, payload);
    renderChangePolygons(payload.detected_change);
    renderTimeSeries(tsPayload);
    $("#toggle-change").checked = true;
    $("#toggle-heat").checked = false;
    $("#layer-note").textContent = `${payload.detected_change.features.length} 个候选变化斑块已叠加到右侧影像。`;
  } catch (error) {
    $("#detection-content").innerHTML = `<div class="notice">${escapeHtml(error.message)}</div>`;
    $("#layer-note").textContent = "影像请求失败，请查看平台终端日志。";
  } finally {
    $("#loading").classList.add("hidden");
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
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "保存失败");
    const feature = state.allEvents.find((item) => item.properties.event_id === state.selectedId);
    if (feature) {
      feature.properties.review_status = payload.status;
      feature.properties.review_reviewer = payload.reviewer;
      feature.properties.review_note = payload.note;
      feature.properties.review_updated_at = payload.updated_at;
    }
    feedback.textContent = `已保存：${payload.updated_at}`;
    renderEventList();
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
  link.download = `${state.selectedId}_sentinel2_change_candidates.geojson`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function exportReviewCSV() {
  window.location.href = "/api/export/events.csv";
}

async function loadInitialData() {
  const health = await fetch("/api/health").then((response) => response.json());
  if (health.ok && health.earth_engine_ready) setHealth(true, `Earth Engine 已连接 · ${health.project}`);
  else if (health.ok) setHealth(true, `平台已启动 · 选择事件后连接 Earth Engine`);
  else setHealth(false, "Earth Engine 未连接");
  const response = await fetch("/api/events");
  const payload = await response.json();
  state.allEvents = payload.features || [];
  loadTypeOptions();
  applyFilters();
  const first = state.visibleEvents.find((feature) => feature.properties.has_geometry);
  if (first) selectEvent(first.properties.event_id);
}

$("#year-filter").addEventListener("change", applyFilters);
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
$("#export-review").addEventListener("click", exportReviewCSV);

initMaps();
loadInitialData().catch((error) => {
  setHealth(false, "平台初始化失败");
  $("#event-list").innerHTML = `<div class="notice" style="margin:15px">${escapeHtml(error.message)}</div>`;
});
