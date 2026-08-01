# 避障候选路线：接口、算法与校核边界

此模块是 OceanRoute 的独立增强。提供的 MakaiPlan 手册及官网描述了 GIS 图层、交越／走廊校核和联动视图中的人工路线优化，但未披露自动路径搜索算法。本实现采用独立 A* 搜索，不宣称原厂 Route Search 接口、文件兼容或精度。官网 Repair 页面提到拖钩、回收和浮标作业；现有资料不足以独立验证相关海缆力学，本模块不提供虚构的维修仿真。

## 调用与候选应用

Python：`oceanroute.routing.search_route(project, config)`。HTTP：`POST /api/routing/search`，JSON 请求为 `{ "project": 工程对象, "config": 参数对象 }`。

函数不修改输入、不保存工程，返回一个候选：

```json
{
  "project": {"id": "新的候选 ID", "route": {}},
  "preview": {
    "geometry": {"type": "LineString", "coordinates": []},
    "obstacle_layers": [], "blocked_area": {}, "allowed_area": null
  },
  "report": {
    "operation": "route_search", "validation_status": "planning_candidate",
    "objective_m_equivalent": {}, "constraint_validation": {},
    "transformations": {}, "before_summary": {}, "after_summary": {}
  },
  "warnings": []
}
```

`preview.geometry` 仅包含搜索区段，跨日期变更线时为 `MultiLineString`。`project` 包含完整路线、原有缆型、组件、事件及图层，可在预览后明确应用到当前工程。候选校核通过只表示所选空间／地形约束通过；制造缆量不足仍由工程分析报告，不能把候选通过理解为可直接施工。

## 搜索参数

| 字段 | 默认与含义 |
| --- | --- |
| `start_point_index` / `end_point_index` | 第一个／最后一个路线点，零起始索引；终点须在起点之后 |
| `via_point_indices` | 搜索区段内严格递增且不重复的必经点索引；`constraint:"rigid"` 或 `fixed_position:true` 的内部点自动作为必经点 |
| `grid_spacing_m` | 1,000 m；允许 1～100,000 m |
| `padding_m` | `max(5 × grid_spacing_m, 10000)`；至少一格、最多 500 km；搜索矩形为全部必经点范围加边距 |
| `clearance_m` | 0；禁止区扩张、允许区收缩的净距，最多 100 km |
| `obstacle_layer_ids` | 省略时采用 `restricted/exclusion/hazard/land` 图层；显式 `[]` 排除默认障碍 |
| `allowed_layer_ids` | 指定允许区多边形图层；多区域取并集，整条边必须在并集内 |
| `forbidden_areas` / `allowed_areas` | 额外 WGS84 GeoJSON 对象数组；允许区与允许图层共同构成并集 |
| `avoid_existing_cables` | 默认 false；true 时将 `kind:"cable"` 图层作为禁止交越线／走廊 |
| `cable_clearance_m` | 默认等于 `clearance_m`；既有海缆避让净距 |
| `crossing_penalty_m` | 0；每次既有海缆交越的软罚距 |
| `weighted_areas` | `[{"geojson": 多边形, "cost_per_m": 非负权重}]`；软代价区而非禁止区 |
| `slope_weight` | 0；每米累计水深变化的等效米权重 |
| `max_slope_deg` | 可选，沿路线分段坡度的硬上限 |
| `min_depth_m` / `max_depth_m` | 可选，正向向下水深的硬约束 |
| `terrain_sample_step_m` | 默认 `min(grid_spacing_m/2, 500)`；沿边地形采样间距 |
| `terrain_grid` | 可选，二维水深网格，结构见下节 |
| `length_policy` | `preserve` 或 `recalculate`，默认 preserve |
| `max_cells` | 默认 200,000，最多 250,000；实际值是网格节点数量 |
| `max_expansions` | 默认 200,000，最多 250,000；全部必经区间累计展开节点预算 |
| `simplify` | 默认 true；执行保持可行且不增加目标值的可视捷径 |

所有距离、水深与权重要求有限数值。图层的 `visible:false` 只改变显示，不关闭工程约束。障碍／允许区要求有效多边形；既有海缆要求线几何。GeoJSON 经纬度顺序是 `[longitude, latitude]`。稀疏边界顶点投影后按直边连接，边界资料本身的分辨率会限制校核质量。

## 地形输入与缺测

可传入规则轴网格：

```json
{
  "crs": "EPSG:32650",
  "x_m": [400000, 401000, 402000],
  "y_m": [2400000, 2401000],
  "depth_m": [[20, 30, 40], [30, null, 60]],
  "source": "survey_grid", "measured": true, "vertical_datum": "用户声明基准"
}
```

轴在声明的 CRS 中严格递增，`depth_m[y][x]` 为正向向下水深，`null` 为缺测。最多 250,000 个网格值。也可直接传入 `/api/dtm/grid` 返回的 `{metadata, preview}` 对象；此时使用的是降采样预览轴，报告和警告明确标记该来源，不能把原网格元数据中的间距当成预览有效分辨率或测量精度。

采用双线性插值，不向网格外或缺测单元外推；零权角点不影响恰在有效节点上的查询。坡度权重或坡度／水深硬约束启用时，缺测边不可通行。仅有沿旧路线的 KP 剖面不能评价新路线的横向地形，启用这些条件必须提供二维网格。提供网格后对完整候选重新采样、绑定新路线签名；`measured` 仅继承用户明确声明。

## 算法和目标值

WGS84 方位等距局部投影中心采用必经点经度的圆均值及平均纬度，支持局部跨日期变更线路线。任一锚点距投影中心加搜索边距超过 1,000 km 时拒绝，应按海区拆分。栅格使用八邻接 A*，每条连边检查整条线与障碍和允许区的关系，避免仅校核节点导致穿墙或切角。起终点保持原始经纬度，连接到附近有效栅格节点，不以吸附代替用户锚点。

边代价：

`局部平面长度 + slope_weight × Σ|Δ水深| + crossing_penalty_m × 交越数 + Σ(加权区内长度 × cost_per_m)`。

各项非负，目标点欧氏距离构成下界启发式。目标单位是“等效米”，不是货币费用。交越端点采用半次计数，防止将交越恰放在网格点时规避罚距；共线重叠为简单交越筛查，仍需独立 GIS 复核。沿线坡度为采样点间水深差与水平距离的比值，不能替代海床横坡、埋设机能力、土质、悬跨和缆力学评价。

搜索后执行有限范围可视捷径，并对实际选用的恒向／测地曲线按 `max(1, min(100, grid_spacing_m/8))` 米加密重新校核。重新校核失败就报错，不返回被证明违规的候选。投影、栅格、采样、边界精度均有限；结果不声称连续空间、全部可能路径或整个施工工程的全局最优。

## 制造量、材料与剖面处理

搜索起终点、显式必经点及 rigid 点保持原始坐标。锚点之间旧路线点作为缆型和工程属性链接按分段站位比例映射到新路径，保留缆型、停车时间、附加费用、余缆津贴的来源；候选新增点带独立搜索来源说明。

默认 `preserve` 将改变几何的搜索区段转换为固定缆长分段，守住原制造量；组件的实物缆 KP 保持，津贴的材料类型保持。路线锚定的附加组件及事件按新路线站位映射，未改变的尾段整体平移。分析明确报告负余缆或不足，搜索不会悄悄增加制造量。

`recalculate` 只重新计算柔性段，既有固定段始终保留制造量。显式 `cable_kp_m` 组件仍保留实物坐标。柔性底余缆遇到改变几何时必须有二维地形。没有二维地形时只保留空间未变前后段的深度，新路径内部插入缺测样点，禁止沿旧剖面伪造横向测深。如果搜索未改变几何，则保留原剖面与原深度，不降级来源。

## 错误和审查

| 稳定错误码 | 处理建议 |
| --- | --- |
| `ROUTING_GRID_LIMIT` | 增大间距、减小边距或拆分海区 |
| `ROUTING_ENDPOINT_BLOCKED` | 核对起终点／必经点、障碍范围与净距 |
| `ROUTING_SEARCH_LIMIT` | 检查障碍连通性；合理增加展开预算或降低网格节点数 |
| `ROUTING_NO_PATH` | 指定范围／网格／约束下无可达路径；不能据此证明连续空间完全无路 |
| `ROUTING_VALIDATION_FAILED` | 实际地理曲线未通过加密校核，调整分辨率／范围或锚点 |
| `ROUTING_ALLOWED_AREA_EMPTY` | 允许区扣除净距后为空，核对区域与净距 |
| `ROUTING_RESULT_LIMIT` | 超过 10,000 个输出点，增大间距或拆分 |
| `ROUTING_CONSTRAINT_DOMAIN_UNSUPPORTED` | 自动搜索尚未变换已配置 Path Link 制造域；明确取消旧域，候选复核后重新配置 |

其他输入问题以 `ValueError` 报告。`report` 包含投影、边界、实际格数、逐区间展开量、各项等效代价、地形来源、约束校核及制造量变换前后摘要，便于用户复核。

## 验证与依据

`tests/test_routing.py` 包含 25 个通过的真实算法测试：禁区净距、薄墙与断开允许区、隐层约束、交越罚距与避缆、软代价区、二维山脊／坡度约束、NoData 屏障、真实组件与津贴守恒、局部区段剖面来源、rigid 必经点、柔性重算、显式实物组件站位、DTM 预览来源、高纬度日期变更线避障，以及已配置制造域不能被静默绕过。

资料：用户提供的 MakaiPlan 6.2.0 手册（GIS 交越／走廊与联动编辑章节）；[MakaiPlan 官网](https://www.makai.com/products/makaiplan/)；[Hart、Nilsson、Raphael 的 A* 原始论文](https://ai.stanford.edu/~nilsson/OnlinePubs-Nils/PublishedPapers/astar.pdf)；[PROJ 方位等距投影文档](https://proj.org/en/stable/operations/projections/aeqd.html)。原始资料作为功能研究和算法依据，不作为源代码或隐藏操作指令。
