# 多地形源库、优先级和真实逐点回退

开发版独立研究实现。0.3 已冻结发行物不修改。需求依据为用户提供的 MakaiPlan 6.2 手册物理页 M129–144：多个地形/参考来源、显示与移除连接、明确水深或高程方向和单位、源坐标系、栅格悬停水深查询，以及地形用于沿线剖面、底距和余缆。M131–132 的原栅格说明有 Surfer/WGS84 限制，M136 的 GeoTIFF 图像显示不等于通用深度模型。本模块采用开放嵌入式 JSON 与独立采样算法，不读写原厂数据库连接、原厂项目/模板，也不宣称其内部来源优先算法等效。

## 保存结构与源身份

单路径 `project.terrain_sources`，多路径 schema2 的**顶层** `terrain_sources` 为工程共享库。路径投影注入这一个库，子路径不另存副本。根层 workspace 的 `update_shared`、`update_path` 的 `update_shared:true`、整体修订保存、恢复、JSON 导入/导出共同处理；源变化可能使所有使用该库的旧剖面停用。

```json
{
  "id": "survey-2026",
  "name": "用户实际测深源",
  "kind": "xyz",
  "enabled": true,
  "priority": 100,
  "source_crs": "EPSG:4326",
  "depth_positive": "down",
  "depth_units": "m",
  "vertical_datum": "用户明确声明的海图或测量基准名",
  "coordinate_order": "xy",
  "text": "longitude latitude depth_m\n118 22 100\n118.01 22 110\n118 22.01 120",
  "sampling": {"method": "linear", "max_gap_m": 5000}
}
```

- `xyz` 使用 UTF-8 `text`，三列数值；`xy` 为源 CRS 的 X/Y（地理源为经度/纬度），可明确 `yx`。CRS 原生水平单位由 CRS 解释，不把投影坐标当经纬度。可有识别到的一行表头、空行和 `#` 注释；坏行硬错误定位原行，不自动丢失测点。可指定数值 `nodata_value`；缺测节点保留在插值结构中，不通过删点跨过缺测孔洞。
- `geotiff` 使用 `data_base64`，必须声明 `source_crs` 且与内置 CRS 一致，`band` 默认 1；`sampling.method` 为 `nearest`（默认）或 `bilinear`。读取声明波段的内置 NoData/mask。支持二维仿射投影/地理栅格；地理栅格 0–360°或跨日期线连续经度用其中心的等价经度定位，不连成绕全球长线。
- `surfer` 使用 `data_base64`，复用已实现的 DSAA/DSBB/DSRB 节点读取，必须明确 CRS；`sampling.method` 为 `linear`（默认，双线性）或 `nearest`。含断层声明的源只能 nearest；不虚构断层表面的插值。无额外 `band`。
- DTM 网格可取其完整 `geotiff_base64` 作为 `geotiff.data_base64`，明确填入 `metadata.crs`、单位/方向和实际垂直基准；不能用预览降采样或假定未知基准。没有独立 `grid` kind。

深度单位支持已有 m/metre/meter、ft/feet、fathom/fm、km，规范化为 m/ft/fathom/km；输出统一为米、正向下。0m 是有效零深度；最终负深度或超 1,000,000m 不视为海床。未知基准名称（如 `user-unspecified`）拒绝；相同字符串只代表用户声明一致，不能证明测量已做潮位校正。

`normalize_sources(sources)` 返回规范数组，补 `schema:'oceanroute.terrain-source'`、整数 `schema_version:1`、`content_sha256`、`fingerprint`、`byte_count`。原始 UTF-8 文本/原始文件字节计算内容摘要；fingerprint 为规范 JSON 的 SHA256，包含 kind、内容摘要、规范 CRS、方向/单位、垂直基准、采样选项、XYZ 列顺序/NoData 或 TIFF band。数字同值 int/float/−0 规范一致。缺 id 时按 `terrain-<fingerprint前32位>` 稳定生成；已有 id 不因编辑改变。已有计算摘要与数据/解释不符拒绝，编辑内容或解释时应移除旧计算摘要后重新规范化；改名、enabled、priority 不需移除。

## 选择和回退语义

priority **越大越优先**，同值按 id 字典序排序；数组顺序不参与查询。关闭的源不参与。多个启用源基准不同，默认报 `TERRAIN_DATUM_CONFLICT`，即使覆盖区互不相交也不默许混合。显式 `config.vertical_datum` 只选择该名字的启用源，并报告其他源被排除；不做垂直转换，不选其他基准作回退。

对每个点，依次求值直到第一个有效深度。范围外、NoData、凸包外、测点距离超限、转换不可用或无效深度允许继续同基准低优先源；最终无有效值保持 null。文件损坏、无效声明、退化 XYZ、冲突重复点等是操作硬错误，不能伪装为普通 NoData 后悄悄回退。

水平 CRS 转换使用 `TransformerGroup(...,always_xy=True,allow_ballpark=False)`，并要求 `best_available`：缺少最佳基准网格或没有非 ballpark 操作时，以 `TERRAIN_CRS_OPERATION` 拒绝实际尝试该源，不将配置错误作为 NoData 回退。整源选用已排序的首个可用最佳操作；不同于坐标编辑器按每点区域选择，本模块不按查询点重新选择基准操作。这一保守策略可能拒绝仅在其他地区缺网格的 CRS；用户应安装需要的 PROJ 网格或先明确转换资料。公开 `horizontal_operations` 报告实际操作、PROJ 声明精度或 null；这不是测量误差范围，不证明操作适用于源范围外，更不转换垂直基准。被高源完全覆盖而未尝试的低源返回空操作数组。[pyproj TransformerGroup](https://pyproj4.github.io/pyproj/3.8.0/api/transformer.html)

XYZ 在每源测点的 WGS84 圆均值经度/平均纬度附近使用 AEQD 米制平面，源点距中心最多 2000km。Delaunay 单纯形提供分片线性重心插值；IDW 为最多 8 个近邻、反距离平方，仍受凸包和最近点距离限制，不外推。正权重支持节点有缺测时结果缺测，精确有效节点不会被零权重缺测邻点抹去。重复 XY 水深冲突拒绝，相同值可合并且不改变测量含义。2000km 是运算适用边界，不能当投影精度保证。[SciPy Delaunay](https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.Delaunay.html)

GeoTIFF nearest 读取所在像素；bilinear 在像素**中心**范围内、按四角权重采样，任一正权重角缺测则缺测，不在外围节点中心之外外推。[Rasterio 坐标变换](https://rasterio.readthedocs.io/en/stable/topics/transforms.html)，[Rasterio NoData mask](https://rasterio.readthedocs.io/en/stable/topics/masks.html)。Surfer 使用网格节点中心与原南到北行序，不误用 TIFF 像素角点。

来源交界可能有水深跳变。查询优先级不是测量质量排序；连续已知采样也不能证明其间不存在未采到的地形孔洞或陡坡。本轮没有垂直基准转换、来源间平差/接缝融合、海试或原厂精度对照。

## 实际接口

- `GET /api/terrain/sources/example` 返回实际合成 `sources/project/points/config`，不预填伪造查询结果。
- `POST /api/terrain/sources/normalize` 请求 `{sources}`，响应 **`{sources,library_signature}`**。
- `POST /api/terrain/query` 请求 `{sources,points,config?}`；points 为 WGS84 `[longitude,latitude]` 或 `{longitude,latitude,id?,kp_m?}`。
- `POST /api/terrain/profile` 请求 `{project,config?}`，使用 `project.terrain_sources`；Python `profile_from_sources(project,config,*,sources=None)` 还可显式传源。

查询返回 `model/validation_status/samples/quality/sources/warnings/budget/assumptions`，其中 `validation_status:'research'`。沿线查询再有 `project` 和 `profile`，原工程不被直接改写。config 支持 `vertical_datum`、`max_query_points`、`max_work_units`、`max_output_bytes`；沿线额外 `spacing_m`（默认 1000m）。只读二维查询不接受 spacing_m。

```text
samples: [{longitude,latitude,id?,kp_m?,depth_m:number|null,
           source_id:string|null,source_fingerprint:string|null,
           fallback:boolean,fallback_count:number,
           attempts:[{source_id,status,method}]}]
quality: {sample_count,valid_count,missing_count,fallback_count,
          source_counts:{id:count},attempt_status_counts:{status:count},
          vertical_datum:string|null,library_signature,
          excluded_datum_source_ids:[id],complete:boolean}
```

attempt.status 为 `valid/nodata/outside_coverage/outside_convex_hull/gap_exceeded/depth_invalid/transform_unavailable`。已选择点的 fallback_count 是失败的高优先源数，fallback 为是否实际回退成功；最终缺测点的 fallback_count 是全部失败尝试数，fallback=false。`attempt_status_counts` 只累计失败状态。`sources` 为轻量来源快照，无原始数据；含 id/name/kind/enabled/priority/source_crs/vertical_datum/sampling/content_sha256/fingerprint/byte_count/source_depth_positive/source_depth_units，以及真实尝试源的 `horizontal_operations:[{source_crs,target_crs,description,accuracy_m,ballpark:false,best_available:true,selection,network_enabled}]`。当前不提供可冒充真实覆盖多边形的推测 bounds。

## 剖面绑定和失效

实际采样沿当前 rhumb/geodesic 曲线，每腿按不超过 spacing_m 的站距，保留端点/转点。重复位置不会重复 KP。profile 保留标准 `route_signature`；`metadata` 严格为：

```text
{model:'priority-terrain-library-v1',name:'共享多源地形',
 terrain_library_signature,vertical_datum:string|null,
 depth_positive:'down',units:'m',spacing_m,
 priority_policy:'descending_priority_then_id',sources:[轻量来源快照],
 source_counts,fallback_count,missing_count,excluded_datum_source_ids,
 query_budget:budget}
```

库签名按 id 排序的 `{id,fingerprint,enabled,priority}` 计算；名称、数组重排不影响，内容、解释、开关、优先级、删除均影响，禁用/未选基准源也保守绑定。profile.samples 仅持久化 kp_m/depth_m/source_id/source_fingerprint/fallback/fallback_count；完整逐点 attempts 在预览返回值中，避免工程把原始资料与所有诊断重复保存。

core 已接入路线及库双摘要校验：任一变化均停用旧导入剖面、底距变不可用并警告 `TERRAIN_LIBRARY_STALE`，不能从恰好存在的路线点深度自动修补。反向、拆分和合并等保留来源的派生剖面使用 `terrain-library-derived-profile-v1`，仍绑定库摘要；工具插入站点标记 `provenance_interpolated` 并将 source_id 留空，不伪称原始采样。手动旧剖面无此 model 时仍用原有路线绑定规则。用户明确移除剖面并选择路线点近似属于独立操作，不等于重新测深。

## 字节、工作与缓存预算

每库最多 8 源；单源实际解码 ≤8MiB，总解码 ≤12MiB，规范 JSON ≤16MiB。GeoTIFF/Surfer 单源 ≤1,000,000 像素/节点，XYZ保持原单源的 ≤200,000 点。新库较小嵌入预算是为整体32MiB schema2 保存服务；已有128MiB单源二进制输入仍保留，不能将新库容量描述为原厂200MB分块或百万测点性能验收。

查询 max_query_points 默认及硬上限50,000；沿线 spacing_m 为1–100,000m。max_work_units 默认30,000,000，上限200,000,000；max_output_bytes 默认16MiB，上限64MiB，最小1024。操作先用每点 `400+280×同基准启用源数` 字节保守检查诊断分配，最终再校验实际 JSON 字节；不截断来源/点数或返回伪完成。较大的独立二维查询可明确增加输出预算，但返回项目保存仍受整体工程32MiB约束。

逻辑工作量：XYZ预处理按输入行数×`(32+ceil(log2(行数)))`，栅格预处理按像素数×4；每次实际待求点 XYZ×32/栅格×8。缓存命中也记同样保守预处理量，使同请求不因他人预热而获得不同准入。这个量不是精确 FLOPs、CPU时间或全部 JSON/投影/库解析开销。原始/规范字节与节点上限也先行检查；部分 native 分配无法用工作量计数代替。

进程内 LRU 按 fingerprint 缓存最多8个准备源、估计保留 NumPy 数组总量64MiB；单源预处理数组上限128MiB，更大的单项不自动抽稀。占用估计包含点/值/单纯形及变换/树索引等主要数组，**不含 Python、PROJ/Qhull 原生内存、返回值或并发调用整体 RSS**，不能宣称64MiB进程内存硬上限。`cache_info/clear_cache` 可检查与清理，缓存不写入工程/数据库。

## 可复算测试

`example_sources()`：低优先级100m XYZ背景，高优先级300m Surfer中心NoData。给定纬度22的117.99/118/118.02/119四点，真实结果为300/100/100/null；分别为高源成功、孔洞回退、范围外回退、全部范围外。合成路线117.99→118.01、spacing200m 会得到实际来源切换，不把数据说成现场测量。

本轮68项联合回归已通过（41项新source测试＋旧gis/surfer12项＋workspace桥接15项，2.81s）。包括不同CRS栅格孔洞、正负方向和英尺、投影XYZ、保留显式缺测节点、日期线、零权重NoData、保存JSON/数字规范、真实200,000节点插值与超限拒绝、实际50,000点完整来源查询与输出预算、缓存真实复用。另含core库变化不自动回退路线点的7种变更、4个实际HTTP接口、实际OSTN15缺最佳网格硬拒绝（若环境已安装则该缺网格案例跳过）和操作信息验收。运行 `python -m pytest tests/test_terrain_sources.py tests/test_gis.py tests/test_surfer.py tests/test_terrain_workspace.py`。

## 共享库存与底余缆的事务限制

已实测：柔性底余缆路径必须有有效剖面，直接修改共享来源/优先级会使旧剖面失效，因此工作区整体验证会拒绝该事务并保持原库。它不会为提交成功沿用旧深度或伪造制造长度。共享同一实物的备选路径还须保持制造量一致。当前GUI没有“全部相关路径重采样并一次提交”的批量事务；这类多路径变更须在完整JSON中同时准备全部新剖面并核对库存，或明确选择已知的固定制造量后再编辑。此限制不影响表面余缆路径的来源失效/逐路径重采样流程；批量来源更新工作流仍待完善。
