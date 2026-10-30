# 淳安 HLS 遥感提取输入包

> 口径说明：本说明中的 HLS 事件级汇总脚本是前期辅助分析，使用当时匹配到的小班几何统计事件级指标。当前森林变化平台的 Sentinel‑2 识别采用独立流程：营造林项目矢量只在缺少实测坐标时提供整体中心近似锚点，不用矢量边界裁剪像元；营造林项目矢量包含采伐、抚育、补植、改培等项目类型，小班号命中不等于采伐证对应。平台 Excel 中的 EVI、NBR、NDMI 则按识别单元计算。请勿把本输入包的事件级 HLS 指标或几何当作平台的像元识别范围。

## 已生成文件

- priority30_event_catalog.csv：实施方案“优先核查30条”整理出的事件主表，保留日期、作业类型、采伐强度、记录面积和原始来源。
- priority30_event_polygons.geojson：按乡镇+小班号命中的营造林项目矢量事件面；这些面只提供项目资料中的近似定位候选，不证明对应采伐证。无法找到项目面但能由严格配准实测坐标定位的事件，以点作为辅助定位。
- priority30_geometry_match_summary.csv：每个事件的空间匹配状态和来源。
- hls_resilience_timeseries.js：GEE Code Editor 脚本，读取 HLS L30/S30 v002，计算 EVI、NBR、NDMI 和事件级前后统计，并导出 CSV。
- priority30_hls_event_metrics.csv：通过 Earth Engine Python API 计算出的 20 个已定位事件结果表。
- priority30_hls_event_metrics.geojson：包含事件几何和同一组指标的 GeoJSON 结果。
- priority30_hls_event_metrics_summary.md：本次提取的口径、结果范围和质量限制摘要。
- run_gee_extract.py：不依赖上传 Asset，直接读取本地 GeoJSON 构造 Earth Engine FeatureCollection 并计算指标。

## 空间匹配说明

事件清单中部分 2024—2025 采伐记录的小班号能与营造林项目矢量按乡镇和小班号命中；由于项目矢量包含采伐、抚育、补植、改培等类型，这种命中只能作为近似定位候选，不能称为采伐证空间精确匹配。其他事件可能只有严格配准实测点，或当前资料中没有可确认的空间边界。point_fallback_measured 不能用于遥感面积统计，需补充与采伐证对应的小班面后再作为 A 类事件。

## GEE 操作顺序

1. 登录 Earth Engine Code Editor。
2. 上传 priority30_event_polygons.geojson（或压缩包）到 Assets，资产名建议为 `priority30_event_polygons`，项目使用 `mimetic-cooler-496216-t4`。
3. 打开 hls_resilience_timeseries.js；脚本已经预填推荐资产路径 `projects/mimetic-cooler-496216-t4/assets/priority30_event_polygons`，如实际资产名不同再修改。
4. 先运行地图和 print，检查事件位置、影像数量、EVI 前后窗口及有效像元数。
5. 确认无误后运行 Export.table.toDrive 任务，并在 Tasks 面板启动导出。
6. 对 A 类事件抽查至少 10 个原始影像；按实施方案记录云、云影、物候、地形阴影和边界不吻合等问题。
7. 台风、风倒、病虫害等补充事件可复用同一脚本，但应在事件主表中把 event_type、日期精度和原因单独编码，不能与计划性采伐直接混合计算同一抵抗力均值。

## 本地 Python API 运行方式

本机默认 Python 3.14 与 Earth Engine 依赖目录中的 cffi 二进制版本不一致。请使用工作区自带 Python 3.12 运行：

```powershell
& 'C:\Users\Jvaschainsaw\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' '.\gee_inputs\run_gee_extract.py'
```

脚本使用已有 Earth Engine 凭据初始化项目 `mimetic-cooler-496216-t4`，不需要把 GeoJSON 上传为 Asset。当前运行结果为 20 个事件，均有前后有效像元；其中 18 个是小班面匹配结果，2 个是实测点辅助定位结果。

HLS v002 在 Earth Engine 中已经以反射率单位提供波段值。脚本不会再次乘以 `0.0001`；重复缩放会把 EVI、NBR 和 NDMI 压低约四个数量级。

## 当前口径限制

本文件上文的 `run_gee_extract.py` 和 Code Editor 脚本是早期事件级研究流程，其营造林项目矢量面匹配口径不用于新版森林变化平台的空间识别。新版平台优先使用实测点；缺少实测坐标时，营造林项目矢量仅取整体中心点作定位锚点。识别范围、像元面积和单元中心均来自 Sentinel-2 变化像元。新版平台另用 HLS 30 m 生成前 3 年同季节基线、许可期逐月核查及后 1—3 年观察值，详见 `forest_change_platform/README.md`。

- 实施方案要求核心分析 2022—2025；工作区的采伐清单日期实际为 2024—2026，2026 条目需单列。
- 脚本默认不把 EVI 下降直接解释为生态损失，也不自动计算恢复程度或恢复时间；恢复目标需要先由验收标准、同龄更新林参照或其他明确规则提供。
- 30 m HLS 适合事件级趋势和变化核查；小于一个像元或边界零碎的地块应使用 Sentinel-2 原生 10 m 影像辅助核对，并在结果中保留尺度说明。
- 后窗口采用事件日期后 30—120 天，不再按事件月份截断，以避免冬季或雨季因清晰影像不足而全部缺测；这会增加物候差异，EVI 变化必须结合原始影像和同季节基线解释。
