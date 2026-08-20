# 投影地图显示合同与验收

本模块在冻结0.4之后新增，已纳入完成发行验收的0.5阶段版及正式文档。冻结0.4压缩包、wheel、PDF和摘要保持原状，不包含本模块；旧开发阶段结果与0.5最终回归分别记录。

## 与参考软件要求的对应

公开手册物理页122–126（印刷页114–118）说明地图可采用Mercator、UTM、极地及用户定义投影，UTM地图使用Easting/Northing网格；更改地图投影改变平面显示。现实现接受EPSG、WKT或PROJ二维水平投影，并把实际路线、GIS和其他工作区路径投影到同一平面。GeoMedia二进制CSF文件尚未解析，不能把支持WKT称为原厂CSF兼容。

原地理路线、底距、制造量和费用仍来自原工程的WGS84几何。投影显示不回写工程点位，也不把投影折线长度替换为路线KP。已有显式投影坐标编辑工具独立负责从原生XY转换经纬度。

## API

`POST /api/maps/project` 输入：

```json
{
  "target_crs": "EPSG:32650",
  "routes": [{"id":"path-1","name":"路线","role":"deployment","geometry":{"type":"MultiLineString","coordinates":[[[118,22],[118.1,22.1]]]}}],
  "points": [{"id":"p1","label":"P01","longitude":118,"latitude":22}],
  "layers": [],
  "config": {"max_vertices":100000,"densify_max_distance_m":5000,"max_operation_selections":2048}
}
```

routes最多100条，使用分析结果`analysis.route_geometry_segments`组装为LineString/MultiLineString；该字段含已加密且已在日期线分段的几何，而`route_geometry`字段本身尚未分段。此接口不从两端点猜测恒向线或测地线，不重连MultiLineString分段。points最多10,000，layers最多1000；各组ID唯一、长度最多128字符。图层缺省visible按true规范化；隐藏图层不投影，返回空FeatureCollection及明确skip原因；非有限JSON即使在隐藏资料中也被拒绝。

所有标准GeoJSON几何及Feature/FeatureCollection都受支持，包括多边形孔洞、MultiPolygon和GeometryCollection。多边形拓扑须有效，不自动修补自交或孔洞。原生图层边采用GeoJSON经纬度参数线性插值；每条边按WGS84纬度/经度的111700m/度上界加密。跨越179到-179但没有分段的图层边保留用户提供的长边语义，并发出警告，不猜成最短跨日期线路径。图层稀疏边不是海缆恒向线，不能从投影显示倒推海缆曲线。

输出使用原生投影单位、always XY顺序；英尺不假装成米。两个水平轴必须有相同换算因子的单位：实际PROJ二维单位步骤不能保证X英尺/Y米混合轴声明的逐轴结果，现明确拒绝此类CRS，而不输出误标坐标。可选第三坐标保持或线性插值，不做垂直基准变换。Feature ID、properties和孔洞保留；原GeoJSON bbox/crs被移除，避免把旧角度范围当新投影范围。唯一显示bounds来自实际全部投影顶点。

复用坐标模块的真实非ballpark操作、地区选择和最佳操作网格检查。无法转换任何点或超过顶点预算，整图`can_display:false`，routes/points/layers为空且bounds为null；不拼接部分成功图。适用区外但仍能计算的点有按实体合并的警告，不能据此声称当地精度。

每个可用操作的描述、声明精度、适用区及网络状态返回operations。未知操作精度为null，不虚构零误差。

## 预算与局部比例

请求最大16MiB，输出最大64MiB。默认100,000、最大250,000个生成顶点，既含原点又含加密点；加密前先检查所需顶点，不在超限后做大批转换。每批不超过10,000点复用已有转换合同。budget明确区分input_vertices、generated_vertices及实际transformed_vertices；这些是几何点数，不能称CPU FLOPs上限。

区域datum还需要独立的操作选择预算，`max_operation_selections`默认2048、最大10000；变换前计入全部顶点的地区选择及最多1000个控制点局部诊断。同datum转换每批选择一次，局部诊断共用同一个固定操作；不同datum不能只根据一个操作的矩形bbox推断全图都可安全复用。超过预算整图在转换前拒绝。operation_selections对每个已开始批次/诊断预先收取保守额度，早期失败时可能多于实际PROJ尝试，不能称CPU硬时间限制。真实ED50地区操作1000点实耗约5.84秒，说明同datum大图性能不能套用于区域datum；大规模安全地区复用仍需后续优化和验证。

前1000个控制点可返回真实局部east/north显示比例、北方向及两轴夹角：从WGS84地表沿东/北方向各取±1m，经过选定实际操作后作中心差分，并按原生XY轴的单位分别换算。这含水平datum操作。它不是高程/海拔修正的combined ground scale，也不是源数据精度证明；无法求局部导数时保持有效投影坐标并明确警告。投影视图不能直接使用未重投影的Web Mercator OSM瓦片。

邻域跨投影接缝或不够平滑时不报告被wrap污染的局部尺度，保持坐标但local_axes为null并警告。独立日期线反例的两千万倍假比例已复算并拒绝；连续UTM分支仍正常返回比例。精确地理极点不声称有唯一真北方向；Mercator极点无有限像，连PROJ的有限浮点极限近似也不作为可显示坐标。

相关原生接口说明：[pyproj Transformer](https://pyproj4.github.io/pyproj/stable/api/transformer.html)、[PROJ地图投影](https://proj.org/en/stable/usage/projections.html)、[pyproj局部投影因子](https://pyproj4.github.io/pyproj/stable/api/proj.html#pyproj.Proj.get_factors)。局部差分与独立PROJ因子作实际数值对照；实现不是复制原厂引擎。

## 当前证据及差距

`tests/test_map_projection.py`覆盖已知UTM坐标、英尺单位、极地实际坐标与北方向、独立局部比例、原图不变、日期线分段、孔洞/高度/属性保留、全部几何类型、拓扑/域外/网格缺失失败、超限预检、跨10,000点批次以及真实HTTP合同。100,000个实际路线顶点在本机生成有限JSON及真实投影约0.70秒，约4.04MB输出；此单一同datum算例不是一般地区网格转换性能保证。

Calculator加入之前，投影视图的两项真实浏览器流程已包含于该开发阶段完整58项生产回归：实际UTM路线、孔洞与安全文本标签、选点/局部北东方向，以及非法CRS/真实顶点预算拒绝无部分图。历史记录为 [development_next_browser.json](../resources/validation/development_next_browser.json) 和 [development_next_served_assets.json](../resources/validation/development_next_served_assets.json)；当时1009项后端/58项浏览器不能充当0.5最终回归，也不与新运行相加。

0.5实际完整后端1113项通过，48.82 s；生产构建的61项浏览器通过，174.768815 s，包含上述两项投影流程，零跳过、失败、不稳定或重试。界面与API同源8766，单worker；8份编译文件与实际HTTP返回逐字节核对。记录见 [release_0.5_backend.json](../resources/validation/release_0.5_backend.json)、[release_0.5_browser.json](../resources/validation/release_0.5_browser.json) 和 [release_0.5_served_assets.json](../resources/validation/release_0.5_served_assets.json)。专项投影测试和重复执行均不另加总。

0.5 wheel外源码目录的25模块/真实API流程通过，1.618259 s，其中实际运行UTM投影；便携包全新环境1113项回归通过，65.69 s，首装整体94.58 s。正式用户手册11页、设计10页全部逐页渲染复核通过。对应 [release_0.5_wheel_smoke.json](../resources/validation/release_0.5_wheel_smoke.json)、[release_0.5_portable_smoke.json](../resources/validation/release_0.5_portable_smoke.json) 和 [release_0.5_pdf_qa.json](../resources/validation/release_0.5_pdf_qa.json)。实测macOS/Python3.13.9及Chrome；未验收Windows/Linux原生安装或Python3.10实机。正式文档与发行安装已完成，不再作为本模块待办；完整产物范围见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

仍缺GeoMedia CSF、原厂地图标注/绘图全套、在线瓦片/栅格底图重投影与原生数据库格式往返，地区datum大规模安全操作复用也仍需优化验证。现有投影与局部比例不构成原厂或海试精度证明。船载设备及变深规划到动态初态等系统差距见 [DEVELOPMENT_NEXT.md](DEVELOPMENT_NEXT.md) 和 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)，不因地图显示通过验收而视为完成。
