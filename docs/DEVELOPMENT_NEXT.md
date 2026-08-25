# 冻结0.4之后的开发记录与0.5发行验收

本文保留2026-10-04、Calculator加入之前的1009项后端/58项浏览器开发阶段证据，并记录随后已完成的0.5阶段发行验收。这些历史运行不算作0.5最终回归，也不与新结果相加。用户步骤、设计和状态已整合到0.5正式文档；发行范围和产物见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。冻结0.4包及PDF独立保留。

本阶段补充共享地形更新的完整工作区预览、原生投影地图显示、二维地形静力研究求解，并在0.5加入四边界悬链线Calculator。它们是基于公开需求的独立实现，不是原厂引擎、数据库或文件格式兼容性证明，也不表示全软件功能已经完成。

## 保留的独立开发预览方式

独立开发预览使用 `http://127.0.0.1:8766`，发行启动器的默认端口为8765。以下命令在源码根目录运行，要求已有Python环境及前端依赖；分别构建和启动，避免覆盖冻结发行界面：

```sh
npm --prefix web run build -- --outDir dist-next
OCEANROUTE_DATA_DIR="$PWD/.oceanroute-dev-next" .venv/bin/python scripts/run_development_server.py --port 8766
```

该启动器使用 `web/dist-next` 和当前后端，工程保存到独立开发数据目录。它不升级既有ZIP/wheel，未安装源码依赖时须先按README准备环境。生产浏览器回归显式指定相同的界面和API服务；`OCEANROUTE_E2E_EXTERNAL_SERVER=1` 只关闭测试自动启动器，不启动任何服务。

## Calculator加入之前的验证记录

| 范围 | 已取得的证据 | 证据的含义 |
|---|---|---|
| 当时完整后端 | 1009 项通过，pytest 报告 46.92 s；外部运行计时约 47.39 s | 当时源码的后端回归全部通过，含真实 HTTP、SQLite 并发/恢复及数值反例 |
| 工作区地形原子预览 | 32 项专项测试 | 缺测、预算、共享制造分歧、固定域不足及合法几何草稿都有实际验收 |
| 投影地图 | 37 项专项测试 | 含原生单位、日期线、真实地区 datum 操作及独立审查发现问题的修复回归 |
| 二维地形静力 | 76 项专项测试 | 两层求解、独立力重构、段内穿床、摩擦容量和真实 HTTP 验收 |
| 新增浏览器流程 | 10 项串行通过，22.063 s：原本轮 9 项加合法空工作区 1 项 | 真实 API、完整工作区候选/撤销、投影和静力结果；含最小工程与空工作区兼容回归，无模拟 API 替身 |
| 当时完整浏览器回归 | 空工作区修复后的完整58项通过，168.333 s；0 skipped、flaky、unexpected | 当时独立编译界面和API均使用8766同源服务及同一隔离数据库，单worker、零重试 |
| 当时生产构建 | 该阶段入口整合后的 TypeScript/Vite 构建通过 | 证明当时前端可构建，单独不能代替发行包安装验收 |

后端完整运行见 [development_next_backend.json](../resources/validation/development_next_backend.json)。真实曲床 HTTP 独立物理核对见 [development_next_independent_physics.json](../resources/validation/development_next_independent_physics.json)：实际 18 节点、80 m 自然材料、320 N 湿重、4 个接触节点、81 次迭代；独立重构最大节点残力约 0.002325866 N，小于 0.01 N 容限。重构段张力与公开结果误差为 0，法向最大误差约 1.39×10⁻¹⁷。该明确合成例中的逐段密集采样是独立佐证，求解器另作逐单元二次极值检查；不能据此声称任意地形或海试精度。

该阶段完整生产浏览器记录见 [development_next_browser.json](../resources/validation/development_next_browser.json)，完整原报告为 `web/artifacts/dev-next/all-regression.json`。修复前57项同源8766生产回归为164.746 s，单独保留在 `development_next_pre_empty_browser.json`，不与该阶段58项重复加总。当时8个编译文件均与实际HTTP返回逐字节一致，见 [development_next_served_assets.json](../resources/validation/development_next_served_assets.json)。当时尚未完成的正式PDF、wheel外目录运行和便携包全新安装，随后已由下文0.5发行验收完成。

当时58项执行命令（在`web/`目录，要求8766服务已启动；不是0.5最终运行记录）：

```sh
OCEANROUTE_E2E_EXTERNAL_SERVER=1 OCEANROUTE_E2E_BASE_URL=http://127.0.0.1:8766 OCEANROUTE_E2E_API_URL=http://127.0.0.1:8766/api OCEANROUTE_ARTIFACTS_DIR=artifacts/dev-next/all PLAYWRIGHT_JSON_OUTPUT_NAME=artifacts/dev-next/all-regression.json npx playwright test --reporter=line,json
```

普通`npm run test:e2e`仍使用配置启动器及默认8765 API；所有新旧spec共享相同显式URL覆盖规则。零重试通过不表示原厂或实测精度。冻结0.1至0.4共8个ZIP/wheel和0.4两份PDF的原始摘要均匹配，见 [development_next_frozen_artifacts.json](../resources/validation/development_next_frozen_artifacts.json)。

## 合法空工作区与最小工程

独立审核确认后端允许 `paths=[]`、`active_path_id=null` 的schema2工作区，但旧界面直接读取不存在的活跃路径导致白屏。当前界面保留可空路径状态，显示“工作区尚无规划路线”，共享缆库、GIS、地形库、独立库存、未知扩展字段和修订都留在原工作区。

可保存、导出、恢复或明确导入完整工作区。在“明确新增第一条路线”中选择已有共享缆型、填写真实起终点坐标及关系策略；也可从单路径JSON入口加入匹配共享库的路线。没有自动添加演示路径、缆材物性或水深。移除最后路线须满足原有装配与安装接替校验；不删除无关联库存时，库存仍独立存在并按实物核算。

最小合法非空工程缺少可选的legs、bodies、rules或标签时，界面用与内核一致的结构显示缺省，不填湿重、EA、EI、质量或直径。原始工作区在未编辑保存时仍保留原数据及未知字段。保存期间继续编辑的后续草稿，经隔离数据库和延迟响应实测仍保留；先后修订分别保存先后内容。相关独立审核见 [development_next_ui_review.json](../resources/validation/development_next_ui_review.json)，空工作区完整真实浏览器用例见 `web/tests/empty-workspace.spec.ts`。

## 1. 共享地形与多路径制造量的原子预览

入口为“工程工具 → 多源测深库 → 工作区多路线原子重采样”。保存的完整 schema2 工作区保持有效，新来源留在独立草稿。可一起提交尚未验收的活跃路径几何草稿；其共享缆材、GIS 和来源字段必须仍与旧工作区相符。修改其他共享数据时须先处理相应工程编辑，不能在地形请求中悄悄丢弃。

使用步骤：

1. 打开完整工作区，在来源草稿明确 XYZ、GeoTIFF 或 Surfer 内容、CRS、单位、垂向符号、垂直基准、开关和优先级。
2. 选择重采样路径、最大站距、垂直基准筛选和制造策略。默认选入全部路径；来源绑定剖面及柔性底余缆依赖的受影响路径必须参与。
3. 点击“预览工作区原子重采样”，检查逐路径真实深度、来源尝试/回退/缺测、底距及制造量前后差额，再检查每套装配的关联路线是否一致。
4. 仅完整候选通过时可点击“原子应用完整工作区”。该操作替换整个本地编辑文档，可整体撤销；还需用工程“保存”一次写入完整修订。

真实接口是 `POST /api/workspace/terrain/preview`，输入 `{workspace,sources,config?,draft?}`；`draft` 为 `{path_id,project}`。结果包含 `can_apply`、完整输入摘要、逐路径 `samples/quality/error` 和逐装配制造差额。成功才返回完整候选 `workspace/project/analysis`；失败时这三个字段全部为 null，不提供部分可应用工程。

默认 `manufacturing_policy="update_consistent"`：独占柔性装配可根据实际新剖面更新；共享的替代路径必须得出相同制造顺序、缆型、长度、组件和参考位置，才能整套更新一次。分歧明确失败，不自动 fork 或重复采购。`preserve` 要求现有制造量不变；需要另一套制造方案时，应先明确建立独立装配关系。

固定缆长、Rigid/Clamped/Sliding 状态和 Path Link 域仍参加真实校验。新地形造成库存不足时整笔拒绝；不得通过删除约束、重新捕获状态、改变津贴/组件库存或扩大固定量来通过。柔性链接可更新为当前实际缆 KP，这与扩大固定库存不同。完整工作区还会进行最终制造关联和约束校验。

任一路径缺测、越界、必选路径遗漏、约束不满足、制造分歧或预算耗尽，都使整笔不可应用。缺测保留 null 和真实来源诊断，不补零、不外推，也不从路线点水深救回。一般运行失败返回 HTTP 200 的失败报告；无效旧工作区、非法 schema/config/来源或容不下完整诊断的输出以 HTTP 422 拒绝。

默认总查询点 50,000、硬上限 200,000，每路径最多 50,000；总逻辑查询工作默认 60M、上限 200M；完整结果默认 32 MiB、上限 64 MiB。它们是显式逻辑预算，不是硬执行时间或内存保证。输入改变，包括未保存草稿和修订改变，旧预览不可再应用。预览本身不写数据库；候选保留旧 `saved_revision`，保存仍走 `POST /api/workspaces` 的乐观修订保护。恢复旧完整修订会一起恢复来源、剖面、制造实体及关联。

详见 [WORKSPACE_TERRAIN_NOTES.md](WORKSPACE_TERRAIN_NOTES.md)、[TERRAIN_SOURCES_NOTES.md](TERRAIN_SOURCES_NOTES.md)、[WORKSPACE_NOTES.md](WORKSPACE_NOTES.md) 和 [CONSTRAINT_NOTES.md](CONSTRAINT_NOTES.md)。输入摘要用于防止旧候选误应用，修订用于并发保护；二者不构成安全认证或新增账户权限系统。

## 2. 原生投影地图显示

入口为路线地图中的“投影视图”。填写明确的 EPSG、WKT 或 PROJ 二维水平投影，点击“计算真实投影视图”。结果可平移、缩放、定位、选择点位并下载；坐标读数和网格按目标 CRS 原生单位、always XY 顺序显示。修改 CRS、路径、GIS 或预算后，旧结果隐藏，须重新计算。

真实接口是 `POST /api/maps/project`，输入 `{target_crs,routes,points,layers,config?}`。路线必须使用分析返回的 `route_geometry_segments` 组装 LineString/MultiLineString；这些是已加密、已切日期线的真实 WGS84 恒向/测地曲线。`route_geometry` 本身仍未切日期线。接口保持分段连接关系，不把两个控制点的直连线称为海缆曲线。

GIS 边按其 GeoJSON 经度/纬度线性参数加密，保留孔洞、属性、Feature ID 和可选第三坐标。第三坐标仅保持或线性插值，不做垂直基准转换。未切开的 179°→−179°图层边保留其长边语义并警告，不能自动猜成最短跨日期线路径。目标 CRS 的轴单位须有相同换算因子；X 英尺/Y 米这类混合轴单位明确拒绝，避免 PROJ 输出与元数据误标。

坐标采用实际非 ballpark 水平操作；最佳地区操作所需网格缺失时拒绝，不静默降级。任一点无法投影或顶点/地区操作选择超预算，整图 `can_display=false`，路线、点和图层为空，bounds 为 null。适用区外但仍可计算的点有明确警告，其坐标不能证明当地精度。

可用控制点返回局部东/北比例、方向及夹角，由实际水平操作的 WGS84 ±1 m 邻域计算。投影接缝导致不连续时，坐标可保留，但 `local_axes=null` 并警告；不会把日期线跨世界跳跃误报为约两千万倍比例。Mercator 精确极点无有限像，极点也没有唯一真北方向。该比例不是高程修正的地面综合比例或测量精度。

输入最多 16 MiB、输出最多 64 MiB；默认 100,000、最多 250,000 个生成顶点。`max_operation_selections` 默认 2048、最多 10000，地区 datum 顶点选择和局部诊断在变换前计入。独立审查中真实 ED50 地区转换 1000 点约 5.84 s，同 datum 100,000 点约 0.70 s 的个例不能外推；选择预算也不是 CPU 硬时间限制。安全的大规模地区操作复用仍待优化验证。

投影结果只影响显示，不回写经纬度、剖面、制造量、费用或工程 KP。需要编辑投影 XY 坐标时使用已有独立坐标转换工具。当前投影视图没有重投影在线瓦片或栅格底图；GeoMedia CSF 二进制、原厂标注/绘图全套和原生数据库往返仍未支持。详见 [MAP_PROJECTION_NOTES.md](MAP_PROJECTION_NOTES.md) 与 [COORDINATE_NOTES.md](COORDINATE_NOTES.md)。

## 3. 明确边界的二维地形静力研究界面

入口为“敷设仿真 → 坡床 / 变深静力”。默认床格为空，必须明确载入完整 `oceanroute.bathymetry.v1` JSON，或从实际地形桥接响应读取 `seabed_grid`。合成例由用户点击按钮明确载入；没有把一维剖面挤出为默认二维网格。

床格是局部 X 东、Y 北、Z 上的米坐标，`z_m[y][x]` 已对齐模型海面 z=0。来源名、投影原点、CRS 和垂直基准公开显示，但标签不自动换算潮位、海图基准或坐标。真实来源库转换为局部床格的既有流程见 [TERRAIN_BATHYMETRY_NOTES.md](TERRAIN_BATHYMETRY_NOTES.md)。包括船端、悬空节点及求解器探索点在内的查询都需完整已知单元；缺测和越界拒绝。

### 坡床悬垂

使用 `POST /api/simulation/slope-catenary`，输入 `{config:{seabed_grid,vessel_position_m,heading_deg,bottom_tension_n,wet_weight_n_m,ea_n,nodes,...}}`。

1. 载入完整仿射坡床，填写船端位置和触点→船端的水平航向；0°北、90°东。
2. 填写每自然米湿重、EA 和触点总张力 **B**。`bottom_tension_n` 是总幅值，不能当成水平张力 H；沿向坡度 m 下 `H=B/sqrt(1+m²)`。底部垂向张力可以为负。明确勾选不可伸长极限才发送 `EA=null`。
3. 运行后分别检查自然材料长度、伸长弧长、离散直段长度、实际触点、端力、切向/根残差及原床格不穿透结果。

全域所有网格节点必须满足声明的仿射拟合容限，不能只验四角。触点牵引是悬垂段的边界条件；海底尾缆、锚位置、摩擦和施工历史未求解。底张力零极限、海流、抗弯、混合材料和实体等输入不支持。

明确合成例 `z=-30+.1x+.05y`、船 `[0,0,0]`、B=100 N、w=4 N/自然米、EA=100000 N 得自然长约 50.3803848342 m、伸长弧长约 50.4582281328 m、水平滞后约 34.8380900285 m。这个数值个例不能外推为任意工程验收。

另作了提供手册物理250页（印刷242页）Calculator截图的单一数值对照：平床2000 m、w=17 N/m、B=34000 N、显式不可伸长，真实API给顶部68000 N、顶角60°、自然长3464.101615 m、layback2633.915794 m，与截图的舍入显示一致。独立平床闭式值也一致，记录见 [development_next_manual_catenary_figure.json](../resources/validation/development_next_manual_catenary_figure.json)。这是公开图例校核；没有取得原厂可执行程序的黄金结果，也不能据此声称原厂引擎或工程精度等效。

### 变深定端

使用 `POST /api/simulation/static-bathymetry`，输入 `{config:{seabed_grid,vessel_position_m,anchor_position_m,wet_weight_n_m,ea_n,nodes,natural_length_m|rest_lengths_m,contact_policy,...}}`。

1. 明确船端和锚端两个固定位置、已知自然材料长，或每段正自然长数组；段数必须等于 nodes−1。EA 为正有限值，此模式不接受 null。
2. 选择自由节点无摩擦，或声明床上内节点的粘着位置、正摩擦系数 μ。可选初始节点形状只是迭代初值，必须与边界/粘着点一致且不穿床。
3. 检查实际节点、逐段张力、自然材料/湿重守恒、法向力、实际三维摩擦力、所需粘着力、力残差和完整直段最小 clearance。

输出顺序为船→锚，材料坐标从总自然长递减到 0。**固定锚端不是 touchdown**：它可以是离床的明确支持点，界面不会以触地点符号标记。端反力包含端节点半段湿重，与首末段缆张力不同；静态接触力单位为 N，不是动态冲量 N·s。

明确粘着模式只验证用户声明位置的所需力是否满足 `N>=0` 和 `|Ft|<=μN`，不会推断摩擦加载历史。容量不足时保留所需量，实际可用摩擦按容量截断，公开残力并 `accepted=false`。直段跨双线性床的完整检查包含单元边界及二次极值；即使节点都平衡，段内穿床仍拒绝，不把缆段投影回床后称为平衡。

### 结果和作用范围

`solver.converged` 与 `accepted` 必须分别阅读。实际优化未收敛、预算耗尽、摩擦不足、力残差失败、段内穿床或塌缩，可返回有限的最佳形状和拒绝代码，但 **accepted=false 是不可用的平衡候选**。界面明确显示“未通过独立验收”，允许下载未验收诊断，不显示通过标签。无效边界、缺测、越界、互斥或未知字段、非有限值及提前预算超额返回 HTTP 422，界面不会保留可用旧形状。

accepted=true 仅说明该模型的声明数值、边界和力检查通过，不证明全局最低能量、稳定性、加载可达性或海试精度。定端模型不含海流、弯曲/扭转、混合材料/实体、埋设、土体或运动边界，也不自动细化材料网格。

静力界面只提供计算预览和结果/诊断下载，不修改规划工程或制造装配，**不能自动生成动态初态或可恢复 checkpoint**。现有二维动态新鲜初态仍带水平悬链线投影近似和启动沉降说明；规划航程平床/均匀初始区间限制也保持。参数或床格改变后旧静力形状隐藏、下载禁用，须重新求解。

详见 [STATIC_BATHYMETRY_NOTES.md](STATIC_BATHYMETRY_NOTES.md) 和 [BATHYMETRY_NOTES.md](BATHYMETRY_NOTES.md)。这些功能落实了公开手册的部分地形、地图和悬链线需求。

### 0.5已加入的四边界Calculator

`POST /api/simulation/catenary-calculator` 接受完整已知仿射床及底部总张力、顶部总张力、相对水平的顶角或入水缆长之一。入水缆长必须明确选择自然材料长或连续伸长弧长；不能把两者混用，也不声称原厂未说明的弹性长度定义已经确定。

逆解在声明有限张力域内按已证明的单调分支枚举，保留下坡双根；各根还须接受原床格覆盖和实际forward验收。默认多根不选择，显式最低/最高底张力策略的指定根失败时不回退。近峰根数未分辨或预算耗尽时不可选择已找到的部分解。只选中并通过验收的 `selected.result` 可作为静力预览，仍不自动生成动态初态。合同和独立数学推导见 [CATENARY_CALCULATOR_NOTES.md](CATENARY_CALCULATOR_NOTES.md) 与 [CATENARY_INDEPENDENT_REVIEW.md](CATENARY_INDEPENDENT_REVIEW.md)。

## 已完成的0.5正式文档与发行验收

此前“正式文档与发行待办”中的用户步骤/设计整合、版本PDF全页检查、wheel外目录运行和便携包全新安装，已在0.5完成。下表只记录0.5的实际验收；上文1009/58及各专项数量不另行加总。

| 范围 | 0.5实际结果与记录 |
|---|---|
| 开发环境全部后端 | 1113项通过，pytest报告48.82 s；见 [release_0.5_backend.json](../resources/validation/release_0.5_backend.json) |
| 生产构建与完整浏览器 | 61项通过，174.768815 s，零跳过、失败、不稳定或重试；界面/API同源8766，单worker。见 [release_0.5_browser.json](../resources/validation/release_0.5_browser.json) |
| 实际服务的编译资源 | 8份编译文件与HTTP响应逐字节相同；见 [release_0.5_served_assets.json](../resources/validation/release_0.5_served_assets.json) |
| wheel外源码目录运行 | 25个实际模块来源及真实API流程通过，1.618259 s；使用已有依赖环境，不能单独代替全新首装。见 [release_0.5_wheel_smoke.json](../resources/validation/release_0.5_wheel_smoke.json) |
| 便携包全新环境首装 | 干净环境安装、HTTP界面及真实分析通过；隔离环境1113项回归通过，65.69 s，首装整体94.58 s。见 [release_0.5_portable_smoke.json](../resources/validation/release_0.5_portable_smoke.json) |
| 正式版本PDF | 用户手册11页、设计10页，共21页全部实际渲染及逐页视觉QA；见 [release_0.5_pdf_qa.json](../resources/validation/release_0.5_pdf_qa.json) |

实测环境为macOS/Python3.13.9及Chrome。发行包仍要求Python3.10+和首次联网安装，未验收Windows/Linux原生安装或Python3.10实机。Calculator主集71项、独立集33项已包含于1113；3项新增浏览器流程已包含于61，不另加总。具体产物、包内容核验和外置SHA256清单以 [RELEASE_NOTES.md](RELEASE_NOTES.md) 及其中的0.5报告为准；冻结0.1至0.4产物不覆盖。

## 仍需实现或验证的范围

0.5发行验收不等于全原厂功能或工程精度等效。当前仍缺原厂native文件/数据库与GeoMedia CSF兼容、完整地图标注和栅格底图重投影、船载设备/实时协议、完整摩擦及施工历史、运动铺设准稳态、完整刚体/疲劳/维修动力学，以及变深规划航程到平衡动态初态的可靠映射。二维定端静力和四边界Calculator都不自动建立动态初态；固定域和库存校核不能因地形更新而绕过。原厂黄金结果、复杂床长航程误差及海试精度尚未取得验收证据，详见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) 及各模型说明。后续修改须按实际影响重新验证，不能沿用本次发行结果。
