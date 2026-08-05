# 实敷观测与规划路线对账

`oceanroute/survey.py` 提供独立调查对账模型。用户手册 PDF 物理页 79 区分计划 Cable Path 与实际 As-Laid Path，术语表把 Off Path 定义为沿敷设方向右侧为正的垂直偏差。手册还区分船位 As-Sailed Plan。这里使用这些公开概念，实际最近点与观测积分算法由本项目独立实现，不能宣称原厂算法、原生格式或制造商精度兼容。

## 调用及可复算小例

Python 为 `reconcile_survey(project, config)`；HTTP 为 `POST /api/survey/reconcile`，请求 `{project,config}`。不修改输入工程、路线、制造装配或水深剖面。工程遵循 `docs/CONTRACT.md`，例如赤道上向东 300 m 的计划路线，固定缆长 505 m，两端规划水深 0/400 m：

```json
{
  "crs": "EPSG:4326",
  "route": {
    "curve": "rhumb", "mode": "fixed", "slack_basis": "surface", "slack_pct": 0,
    "points": [
      {"id":"start","longitude":0,"latitude":0,"depth_m":0},
      {"id":"end","longitude":0.002694945852358564,"latitude":0,"depth_m":400}
    ],
    "legs": [{"cable_type_id":"A","fixed_cable_length_m":505}]
  },
  "cable_types": [{"id":"A","cost_per_m":1,"lay_speed_m_s":1}],
  "bodies": [], "costs": {}
}
```

对应 config：

```json
{
  "source": "人工核验的调查小例",
  "observation_kind": "cable_position_survey",
  "observations": [
    {"id":"survey-start","longitude":0,"latitude":0,"depth_m":0,"cable_kp_m":1000},
    {"id":"survey-end","longitude":0.002694945852358564,"latitude":0,"depth_m":400,"cable_kp_m":1505}
  ],
  "depth_datums_aligned": true,
  "vertical_datum": "本小例共同基准",
  "cable_kp_offset_m": -1000,
  "max_gap_m": 1000,
  "max_deviation_m": 50,
  "max_match_distance_m": 10000
}
```

实际结果：规划/观测水平长 300 m，观测底距 500 m，观测实物长 505 m，底余缆 1%，平面余缆 68.33333333%。点水深残差和对齐后的实物缆 KP 残差均为 0；此小例用于核验账目，没有代表真实调查精度。

## 输入和阈值

| 字段 | 规则 |
| --- | --- |
| `observations` | 有序数组，1–5,000 点，按正敷设方向提供；longitude/latitude 为 WGS84 十进制度 |
| 观测 `id` | 可省略，自动 obs-N；指定时必须非空唯一 |
| `depth_m` | 可省略/null；正向下，范围 0–20,000 m，不推定埋深或海床接触 |
| `cable_kp_m` | 可省略/null；用户测得的实物缆里程，范围 0–1e9 m；非缺测值严格递增，重复/逆序拒绝 |
| `time_s` | 可省略/null；非负秒，按输入顺序非递减，仅保留来源，不用于隐含速度计算 |
| `break_before` | 布尔值，明确在本点前断开调查链 |
| `text` | observations 未提供时可使用 CSV/TSV/分号文本；最多 2 MB，表头 longitude,latitude；可选 depth_m,cable_kp_m,time_s,id,break_before |
| `delimiter` | 可显式指定逗号、Tab 或分号；否则自动识别，不接受未经声明的坐标/深度单位转换 |
| `observation_kind` | cable_position_survey（默认）/reported_touchdown/vessel_track；船位只做几何对照，不生成实测底距、实物长、余缆 |
| `depth_datums_aligned` | 默认 false；只有明确 true 且两种水深已知时计算点水深差 |
| `vertical_datum`、`source` | 来源说明，默认 unspecified/user_observations；不替用户证明共同基准 |
| `cable_kp_offset_m` | 可省略，定义对齐缆KP=观测缆KP+offset；缺省时不计算绝对缆KP残差，相邻实物长度仍可计算 |
| `max_gap_m` | 默认 10,000 m，范围 .01–1e6 m；更大相邻间距不跨空档积分 |
| `max_deviation_m` | 默认 50 m，范围 0–1e6 m；匹配点法向横偏绝对值超过它发出警告 |
| `max_match_distance_m` | 默认 10,000 m，范围 .01–1e6 m；超过最短残差距离的观测不配对 |
| `route_sample_step_m` | 默认 1,000 m，范围 25–20,000 m；搜索区间长度上限，最多 20,000 区间 |
| `station_tolerance_m` | 默认 .01 m，范围 .001–10 m；一维最近点数值细化站位容差，不能当作测量精度 |
| `ambiguity_distance_m` | 默认 .1 m，范围 .001–100 m；多个候选残差相差不超过此值时检查KP歧义 |
| `ambiguity_kp_separation_m` | 默认 100 m，范围 1–1e7 m；候选路线KP至少相隔此值才视为不同路线匹配 |
| `max_work_evaluations` | 默认 2,000,000，1–10,000,000 整数；向量化采样和每次细化距离计算均计预算 |
| `geometry_step_m` | 默认 1,000 m，10–20,000 m；观测/残差 GeoJSON 测地加密间隔 |
| `max_output_vertices` | 默认 100,000，10–250,000 整数；包括观测点、测量线、残差线及日期线切点 |

输入没有强制指定规划路线 KP 的模式。规划 KP 总是来自实际最近曲线查询，实测物理缆 KP 来自用户调查。回环歧义不会用实物 KP 猜测消除。

## 实际算法及账目

按工程选定 WGS84 恒向线或测地线分解路线，每个小区间取端点和中点。以“观测到中点的 WGS84 测地距离减半区间弧长”作为三角不等式下界筛选；有可能改善结果或形成歧义的区间用黄金分割细化。这是有预算的分段数值查询，不是全局最优或无误差保证。距离和测地插值使用 [PROJ/pyproj Geod](https://pyproj4.github.io/pyproj/stable/api/geod.html)，恒向线复用本项目已验证的椭球模型。

最近路线 KP 是平面沿线累计里程。取该点局部路线切向方位 β，取最近点至观测的测地方位 α 和距离 d，输出 `cross_track_m=d*sin(α−β)`、`along_track_residual_m=d*cos(α−β)`。右侧为正。内部曲线投影的沿向分量接近零；端点可能含很大的沿向分量，最短距离不能全部误称横偏。转折点局部法向不唯一，明确标注并排除横偏汇总。多个相隔较远 KP 的候选在歧义阈值内时，确定性展示最早 KP，排除区间对账和横偏统计，不悄悄选定回环分支。

相邻已连接调查点之间用 WGS84 最短测地距离 H 表示观测线，只有两点水深已知时计算 `B=hypot(H,Δdepth)`。这是所测样点之间的线性三维近似；不恢复隐藏海底粗糙度，也不代表埋设量或真实接触证明。只有两点实物缆 KP 已知时计算 `L=Δcable_kp`；平面余缆为 `100*(L/H−1)`，底余缆为 `100*(L/B−1)`。零分母、缺测、空档都返回 null，不返回 Infinity，不补 0。

规划底距积分包含配对 KP 之间所有有效规划剖面采样，插值段界而不是仅比较端点。过期剖面保持未知。规划实物缆KP按照现有工程各段固定/柔性制造长加津贴与 additional 组件跳跃；replace 组件占用已存在制造长度，不额外增加总长。同一 KP 的插入按核心右连续 convention 计入该点，下游区间中的制造插入不能解释为测得的额外海床距离。

断开后 `observed_surface_kp_m`、`observed_bottom_kp_m` 的完整累计保持未知，`chain_index`、`chain_surface_kp_m` 继续记录本地连续链。所有完整总长仅在全链可用时提供；`known_measured_*` 是已知片段之和，不冒充完整总长。缺少一个物理站位不会越过它用远端站位分配中间各段的缆量。

## 输出合同

返回 `{observations,segments,summary,comparison,geojson,geojson_layers,warnings,source,model,assumptions}`。

| 对象 | 实际字段 |
| --- | --- |
| 观测 row | id/index/longitude/latitude/observed_depth_m、route_kp_m、cross_track_m、nearest_distance_m、matched、ambiguity、nearest_longitude/latitude、planned_depth_m、depth_difference_m、planned_cable_kp_m、aligned_observed_cable_kp_m、cable_kp_difference_m、endpoint_projection、tangent_ambiguous、chain_index/chain_surface_kp_m 等 |
| 别名 | planned_kp_m=route_kp_m，signed_cross_track_m=cross_track_m，depth_m=observed_depth_m，ambiguous_match=ambiguity |
| segment | connected/comparable、观测水平/底/缆长度、平面/底余缆、对应规划起止KP/长度/余缆和差值；未匹配/歧义/反序不自动配对 |
| summary | matched/unmatched/ambiguous/deviation_exceeded_count、mean_signed/rms/max_abs/q95_abs_cross_track_m、统计有效点数、完整链/水深/物理站位状态、完整与部分长度、预算实际消耗 |
| comparison | paired_segment_count、配对规划/观测水平长、planned_route_signature、planned_profile_metadata；规划剖面失效/缺测/近似警告带 scope=planned_route |
| geojson | 单个 FeatureCollection，properties.feature_role=observed_point/measured_line/residual_vector |
| geojson_layers | observed_points/measured_line/residual_vectors 三个独立 FeatureCollection；只生成连续已测线和已匹配残差向量 |
| model | identity=independent-wgs84-sampled-as-laid-reconciliation-v1，validation_status=research，实际阈值与方法 |

几何在日期线切分为 LineString/MultiLineString，按 [RFC 7946 §3.1.9](https://datatracker.ietf.org/doc/html/rfc7946#section-3.1.9) 表示；不会将 ±180° 两侧的调查线画成绕地球一周。点和线不改变原设计；UI“加入工程图层”仅复制这份调查 GeoJSON。

`SURVEY_*` 错误具有 `.code`，例如 SAMPLE_LIMIT、CABLE_STATION_ORDER、WORK_LIMIT、ROUTE_GRID_LIMIT、OUTPUT_LIMIT、EMPTY_ROUTE。警告包含 code/message/severity，关键包括 OUTSIDE_MATCH_RADIUS、AMBIGUOUS_KP、GAP、KP_NONMONOTONIC、TURN_TANGENT、DEVIATION_LIMIT、DEPTH_DATUM_UNALIGNED、VESSEL_TRACK。工程自身的结构和数值错误由核心正常拒绝。

验证命令 `.venv/bin/python -m pytest tests/test_survey.py -q`：24 项覆盖两类弯曲 WGS84 路线、横偏符号随敷设方向反向、实测 3-4-5 底距和余缆、高纬日期线、回环歧义、缺深/缺缆KP/空档、制造津贴/组件及核心默认值、过期剖面、船位区别、输入不变性、严格有限 JSON 和各类预算上限。
