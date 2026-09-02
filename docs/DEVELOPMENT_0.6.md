# 冻结 0.5 之后的投影编辑与真实平衡初态开发

## 发行后的阅读定位

本文保留0.6发行前的开发输入合同和验收记录；下文“未发行”“0.5基线”及8767服务指当时开发对象。0.6正式行为、安装与当前门禁分别以 [用户手册](USER_MANUAL.md)、[设计文档](DESIGN.md) 和 [发行记录](RELEASE_NOTES.md) 为准。开发历史计数不与正式回归累计。

2026-10-04。本文件记录下一阶段**未发行源代码**，文件名中的 0.6 是开发阶段标记。当前 Python、前端 package 和界面版本仍以 0.5.0 / 0.5 为基线；本文不发布新版本，也不修改冻结的 0.5 ZIP、wheel、PDF、界面或发行摘要。完整复现目标继续保留，以下进展不表示全原厂功能、文件格式、数值精度或施工流程已经等效。

本阶段落实了投影画布的实际点位编辑、二维定端静力到真实动力初态的显式接线、地理计划的平衡初态映射，以及相应材料/预算/检查点保护。它们使用现有 WGS84 路线、完整工作区、真实坐标操作和独立研究模型；没有用显示曲线或假应答替代求解。

## 1. 独立运行与数据目录

在源码根目录、已有 Python 虚拟环境和前端依赖时，分别执行：

```sh
npm --prefix web run build -- --outDir dist-0.6-next
OCEANROUTE_DATA_DIR="$PWD/.oceanroute-dev-0.6" .venv/bin/python scripts/run_development_server.py --frontend web/dist-0.6-next --port 8767
```

打开 `http://127.0.0.1:8767`。该启动器将当前后端与 `web/dist-0.6-next` 同源提供，工程和作业记录使用独立数据目录。已有 8767 服务时无需再启动第二个实例；不要把本轮编译写入 `web/dist-next`、`oceanroute/static` 或冻结发行目录。环境准备仍按 [README.md](../README.md)；这里不是无需 Python 的原生桌面安装包，也不是一次新发行的首装验收。

需要 Vite 开发界面时，可在 `web/` 设置 `OCEANROUTE_API_PROXY=http://127.0.0.1:8767` 并使用独立端口。正式浏览器验证应明确指定同源编译界面和 API；`OCEANROUTE_E2E_EXTERNAL_SERVER=1` 只关闭 Playwright 自动启动器，不启动服务：

```sh
cd web
OCEANROUTE_E2E_EXTERNAL_SERVER=1 OCEANROUTE_E2E_BASE_URL=http://127.0.0.1:8767 OCEANROUTE_E2E_API_URL=http://127.0.0.1:8767/api OCEANROUTE_ARTIFACTS_DIR=artifacts/dev-0.6/all npx playwright test --reporter=line
```

以上是复核命令，不把尚未执行的命令视为通过证据。冻结 0.5 服务、编译资源和产物保留；它们的历史验收不作为本轮新源码验证。

## 2. 投影画布的实际点位编辑

入口为“路由工作空间 → 投影视图”。明确填写水平投影 CRS，点击“计算真实投影视图”。地图保留已分析路线的真实曲线和日期线分段；GIS 边按既有公开投影合同加密，Polygon 孔洞和第三坐标保留。图上网格与光标显示目标 CRS 的**原生 X/Y 单位**，英尺 CRS 不按米解释。显示投影不改变 WGS84 工程坐标，也不改测地长度模型。

操作流程：

1. “选取 / 拖动”中拖动活跃路径点，释放后真实反算 WGS84。结果栏分别显示目标 X/Y、实际约束后的 X/Y 和最终经纬度；地理 CRS、适用区、转换缺网格或数值错误不会被忽略。
2. “追加点”中声明名称、当前共享库缆型和新段模式，然后点击画布。固定制造量模式必须明确填写缆长；空值不补零。新点水深保持未测量，不从旧点或底图编造深度。此入口追加至路径末端，不是任意段插点工具。
3. 选中点后“删除点”。路线至少保留两点；内部点删除会合并相邻段。不同缆型、冲突的余缆/埋设/未知分段字段会明确拒绝。固定缆量逐段保留并求和，缺少声明量时拒绝；混合固定与柔性段须使用当前有效分析中的实际柔性缆量。明确的 allowance、停留时长和额外费用相加。
4. 每次成功提交只建立一个完整工作区撤销记录。保存前仍由工作区控制器进行实际制造关联核验；成功分析后地图自动重投影，保留原生坐标视窗，不自动跳回全图。

已经配置制造域的点通过真实 `/api/constraints/edit` 求解。Rigid 支持用户明确移动；Clamped 采用实际锚线位置。Fixed Sliding 不允许任意 X/Y 拖动，只能通过原实物 KP 入口编辑。制造域启用时结构增删禁止；要继续结构操作须在约束页明确解除旧域，不能在后台静默丢弃链接。

投影转换与提交分别检查**完整工作区及草稿、活跃路径、修订、CRS 与投影输入**快照。输入改变、切换路径、保存修订或取消操作后，迟到结果不得回写。失败显示真实错误并清除候选；不能使用旧成功坐标冒充本次完成。改变底余缆路径可能令旧来源剖面失效，此时草稿保留、旧投影隐藏、保存拒绝；可使用“多源测深库 → 工作区多路线原子重采样”联合校验。共享备选制造量分歧仍整笔失败，不静默 fork、不改变既有库存。

同一派生项目上下文使用后端的五个明确字段：`workspace_id/path_id/assembly_id/role/workspace_revision`，未关联项以 null 声明。共享资源、装配、其他路径和未知扩展字段均保留在工作区唯一 source 中。真实保存修订冲突保留本窗口草稿，不以旧值覆盖另一窗口。实现见 [ProjectedMapView.tsx](../web/src/ProjectedMapView.tsx)、[projectedPointEdits.ts](../web/src/projectedPointEdits.ts) 和 [useWorkspaceDocument.ts](../web/src/useWorkspaceDocument.ts)；坐标与原子重采样基础合同见 [COORDINATE_NOTES.md](COORDINATE_NOTES.md)、[MAP_PROJECTION_NOTES.md](MAP_PROJECTION_NOTES.md)、[WORKSPACE_TERRAIN_NOTES.md](WORKSPACE_TERRAIN_NOTES.md)。

## 3. 明确二维定端的真实动态初态

入口为“敷设仿真 → 缆形与悬空段求解 → 动态敷设 → 动态初态来源”。原“水平悬链线 / 床面投影近似”保留；新方法为“明确定端自然长 · 实际平衡求解”。用户提供完整 `oceanroute.bathymetry.v1` 床格、船端和固定最老端位置，以及初始活动自然材料长或逐段自然长。

床格和原始边界使用同一局部 X 东 / Y 北 / Z 上米坐标，z=0 已对齐模型海面。来源 CRS、原点和垂直基准标签不自动实施变换。床面保持完整双线性场，不能用拟合平面代替曲床，不能由一维剖面挤出默认二维场。固定锚可以离床、船端可以在水下；端点须不穿床、查询单元须完整已知。

点击“预备并独立验证动态初态”会实际调用静力求解，并从材料加载、实际节点和完整直段重构力与几何证据。预备不是可恢复状态。点击“运行计算”仍按相同 raw request 重新求解验收后进入真实积分，不信任外部 `accepted:true`、显示帧或完整静力结果。

当前初始活动区间要求相同湿重 w、相同 EA、EI=0、无已部署实体、无初始海流或波浪载荷。不同干质量、直径和拖曳参数可保留其真实惯性差别；尚未放出的不同材料和实体可留作未来部署。初始接触为无切向静力支持，不推断静摩擦历史。主动态可声明 0..2 的摩擦系数，但该值不能替代缺失的初始受力历史。

可直接使用已通过真实 API 执行的 [initial-equilibrium-dynamic.json](../examples/initial-equilibrium-dynamic.json)。文件是完整 `{config}` 动态请求：明确合成的 20 m 平床、船端 `[0,0,0]`、离床固定端 `[-15,0,-10]`、20 m 自然库存和 0.2 s 零命令计算。它不包含现场测量，也不是工程文件、静力结果或恢复 checkpoint。省略的 `project` 在此 API 中表示空工程，物性使用请求及公开默认，不继承当前界面工程库。

在源码根目录、8767 服务运行时，API 用法为：

```sh
curl --fail-with-body -H 'Content-Type: application/json' --data-binary @examples/initial-equilibrium-dynamic.json http://127.0.0.1:8767/api/simulation/dynamic
```

在界面可点击“加载明确合成定端平衡动态算例”进入相同类别的显式合成草稿；若要逐字段复核文件，物性与数值字段填入动力设置，将 `config.seabed_grid` 放入“分层海流与变化海床 → 二维床面网格 JSON”，将 `config.initial_equilibrium` 放入“明确动态定端初态 JSON”。**不能把整个请求文件粘贴到初态字段或恢复状态字段。** 界面请求使用当前工程库，复核时还需核对所有已解析物性；它不会把 API 请求包装对象自动导入成新工程。

原始初始化对象只允许边界、自然长/段长、可行数值初值和求解器设置，主配置拥有物性与节点数。自然长和段长二选一，段数=nodes−1；自然材料不等于几何弦长或伸长弧长。时刻 0 是施加运动之前的零速度快照，初始库存不计作本次新放缆。实际没有床面接触时，`touchdown` 和 `bottom_tension_n` 为 null；固定支持点与 `anchor_segment_tension_n` 独立显示，不填零、不假标触地。

## 4. 从静力界面显式转为动态草稿

在“坡床 / 变深静力 → 变深定端”运行无摩擦固定端自然长求解。当前输入匹配且候选独立验收通过后，点击“用此定端输入预备同质无流动态”。这一操作切换到动态页面，保留声明端点、自然材料和完整床格；原静力节点仅作为优化初值。

界面明确载入同质、EI=0、零初始流、零节点速度、零船速与零放缆的草稿，并清除旧混材、实体、船舶计划、剖面和恢复草稿。它不修改规划工作区，不自动生成 checkpoint。动态入口采用更严格的初始化力/几何容限重新求解；先前静力优化 `success` 或显示形状均不能免除该检查。需要后续运动时，用户再显式设置船速、放缆与允许的控制。

参数或床格更改后，定端平衡模式隐藏旧形状和证据、禁用旧结果下载；真实迟到预备响应被丢弃。缺测、越界、无可用平衡或预算错误不会保留旧通过状态。

## 5. 地理计划中的平衡初态与制造库存

入口为“持续计算 / 网格审查 → 从活跃工程预备制造里程与作业指令窗口”，选择“地理床格 · 实际定端平衡”。同时明确提供地理绑定二维床格和地理定端初态，调用既有 `POST /api/shipplan/prepare-voyage`：

```text
config.seabed_grid = 完整 bathymetry.v1 对象
config.equilibrium_start = {
  anchor: {longitude, latitude, z_model_m},
  vessel_z_m,
  natural_length_m 或 rest_lengths_m,
  initial_positions_m?，solver?
}
```

床格必须拥有实际水平投影 CRS、east/north 轴和米单位。LOCAL 任意床格、英尺格或地理度数格不能补标签直接使用。船端 XY 来自原计划真实窗口开始船位；锚经纬度经真实最佳水平操作变换；床格全部 x/y 轴、初值和端点共同平移到该船位原点，z 值和垂直声明保持不变。数值初值是**原床格的局部米坐标**，不是绝对投影 E/N。

可直接使用已通过真实 API 执行的 [geographic-equilibrium-voyage.json](../examples/geographic-equilibrium-voyage.json)。它是完整 `{project,config}` **预备请求**，含两点合成工程、显式材料、EPSG:3857 变深床格、地理固定端和 0.1 s 窗口，不是任务结果或恢复文件。床格声明的高程已经对齐模型海面；例中坐标与起伏均为合成数据，不能当作实测海区。

```sh
curl --fail-with-body -H 'Content-Type: application/json' --data-binary @examples/geographic-equilibrium-voyage.json http://127.0.0.1:8767/api/shipplan/prepare-voyage
```

在界面复核这个例子时，先将其中 `project` 对象单独作为工程 JSON 导入，再在上述预备入口填入 `config.seabed_grid`、`config.equilibrium_start`，并逐项对应窗口时长、计划张力、采样站距、动力及任务参数。完整请求的外层 `{project,config}` 不能作为工作区文件直接导入。预备响应中的 `config` 才是下一步实际任务配置；创建任务时保留同一 `project` 与完整 `plan_mapping`。预备响应也不能粘贴到“恢复 checkpoint”入口。

设窗口起点制造顶站 K₀、明确自然库存 L，则最老材料原点 O=K₀−L≥0。初始船端材料为 K₀、固定端材料为 O、初始新放缆为 0；后续只累计实际指令放缆积分。L 可以包含床接触段，不能统称为“水中悬垂长”，也不能在任务开始时再投入一次。原始制造区段、附属体与当前共享材料库仍需一致；缺失物性、库存不足或有限长度实体明确拒绝。

返回完整 `config.plan_mapping`，新地理平衡分支为 `oceanroute.plan-voyage-mapping` **schema_version=2**。其中保存原/局部时间、自然库存、材料原点、真实控制行、原床快照、投影重基准、初态 proof 和锚—计划目标实际偏差。必须原样保留该映射并“应用预备窗口到任务配置”，再提交后台任务；改变工程或物理输入后须重新预备。

这个新分支保持真实变深床，不再要求平床；未提供新两字段的旧解析分支仍要求完整已知定深床。原 Ship Plan 船位偏移仍是局部平床初估，返回 `PLAN_OFFSETS_REMAIN_FLAT_LOCAL_FIRST_CUT`；不能把一次曲床初态验收说成整个移动船位/触地点计划已达到准稳态。投影米单位也不保证地面比例为 1，应核实区域坐标系、比例和实际测深基准。

准备层成功后，首次实际动力仍从 raw request 独立验收，随后连续块恢复实际状态。`completed` 只表示请求时段完成；`stopped/failed/cancelled/interrupted` 保留真实终止时刻与原因，不显示为完成。达到分块数量限制后的真实 checkpoint 可以创建子任务续算，初始库存不再次计入 payout；超出已预备指令窗口拒绝。当前工程不匹配保存摘要时不能据旧状态冒称采用新工程。

详细地理合同与独立解析坐标/制造对照见 [PLAN_EQUILIBRIUM_REVIEW.md](PLAN_EQUILIBRIUM_REVIEW.md) 和 [plan_equilibrium_frame.py](../oceanroute/plan_equilibrium_frame.py)。旧解析窗口合同见 [PLAN_VOYAGE_NOTES.md](PLAN_VOYAGE_NOTES.md)。

## 6. 预应力 v4 与 schema3 完整恢复

旧冷起 XPBD 的微小 scalar compliance 残差不能证明离散平衡保持。独立闭式材料例曾在零命令下出现实际漂移；增加迭代数不能消除这个反例。新显式平衡初态路径使用 `material-lumped-mass-xpbd-cable-lay-v4` 和 `implicit-compliant-material-nodes-equilibrium-prestress-v4`，每个内部步从实际材料/几何重算张力与非负法向支持，设置对应预应力乘子，并**实际施加**其质量加权位置修正后继续非线性约束、接触、速度和摩擦求解。没有冻结自由节点，也不跳过静止阶段积分。

新的 `oceanroute.dynamic.checkpoint` 为 **schema_version=3**，保留当前完整动力位置、速度、自然段、材料、控制和床格，并保存原 raw request 与完整初态 provenance。恢复校核原始材料、受力、完整段床面和公开诊断，使用保存的真实当前状态，不重新运行静力优化器，不从显示帧反猜速度。旧 schema1/v2 和 schema2/v3 按原方案继续，不能删除新 proof 后降级。

voyage 外层检查点仍为 `oceanroute.voyage.checkpoint` schema_version=1；其物理子检查点可采用上述新 schema3。工作区 schema2、规划映射 schema2 和物理恢复 schema3 是不同文档，不能仅按数字互换。公开 checksum 用于失配/完整性检查，不是身份认证或受信签名；重算 checksum 也不能使错误物料或受力 proof 通过实际复核。

主动态原工作额度保持，初始化另有明确静力/材料/完整段检查预算；昂贵静力求解前先做容量和工作预检。`solver.initialization_work` 分列本次新初始化、历史 work 和恢复 proof 校核，`charged_normalized_work_units` 是分层归一化额度，不是 FLOPs、CPU 时间或 token 费用。voyage 首块只计一次初始化，后续恢复块不重复求静力；二维床面仍禁止套用原平床粗化策略。额度、覆盖、节点或容量限制不会被隐式放宽。

精确输入范围和数值方案见 [INITIAL_EQUILIBRIUM_NOTES.md](INITIAL_EQUILIBRIUM_NOTES.md)，独立反例、闭式力递推和原样恢复见 [EQUILIBRIUM_INITIAL_REVIEW.md](EQUILIBRIUM_INITIAL_REVIEW.md)。

## 7. 本轮验证记录

截至当前记录，本轮完整后端 `.venv/bin/python -m pytest -q` 实际 exit 0，1200 项通过、0 失败、0 跳过；随后 collect-only 再确认 1200 项。环境为 Python 3.13.9，报告未记录本次全量 wall time，不能引用其他运行时长。证据见 [development_0.6_backend_validation.json](../resources/validation/development_0.6_backend_validation.json)。

最终合并生产编译为 1880 模块，主资源为 `index-DtaO1b7e.js` 与 `index-j8pAS7aI.css`。实际同源生产服务为 `http://127.0.0.1:8767`，静态目录为 `web/dist-0.6-next`。持续计算场景容器高度与图例裁切问题修复后，重新执行了完整门禁；以下记录对应最终构建。

| 验证对象 | 实际结果与证据 |
|---|---|
| 最终同源编译界面完整浏览器门禁 | 74 passed，216.018431 s；expected=74、skipped=0、unexpected=0、flaky=0，单 worker，逐结果 retry=0。原始记录：[development_0.6_browser.json](../resources/validation/development_0.6_browser.json) |
| 最终实际服务编译文件 | 8 个文件逐项与同源 HTTP 返回内容一致，包含主 JS/CSS、各分包、HTML 与 favicon；[development_0.6_served_assets.json](../resources/validation/development_0.6_served_assets.json) 保存大小和 SHA256 |
| 独立任务响应竞态专项 | 3 项局部生产浏览器用例通过，5.5 s；cancel、resume、checkpoint 响应均先取真实 API 再延迟，检查切任务后证据关联。详见 [PHYSICS_UI_INDEPENDENT_REVIEW.md](PHYSICS_UI_INDEPENDENT_REVIEW.md) |

独立竞态 3 项、投影编辑 6 项和新初态界面 4 项均已包含在最终 74 项中，不能再相加。首轮 74 项 / 216.087229 s 的结果另存于 [development_0.6_pre_scene_layout_browser.json](../resources/validation/development_0.6_pre_scene_layout_browser.json)，对应资源记录为 [development_0.6_pre_scene_layout_served_assets.json](../resources/validation/development_0.6_pre_scene_layout_served_assets.json)，汇总为 [development_0.6_pre_scene_layout_verified_runtime.json](../resources/validation/development_0.6_pre_scene_layout_verified_runtime.json)，不能与最终轮累计。后端测试、浏览器测试、局部专项、截图和编译核对分别表达不同证据。冻结 0.5 的回归、首装、PDF 与产物摘要属于历史发行证据，不代替本轮新源码验证。

最终持续计算截图 [equilibrium-geographic-durable-job.png](../web/artifacts/dev-0.6/final/equilibrium-geographic-durable-job.png) 已实际查看，完整图例位于视窗内；`equilibrium-initial.spec.ts` 同时以真实浏览器 bounding box 检查场景和图例边界。截图不替代真实计算、材料或恢复断言。

已有可追溯专项包括：

- [projected-map-editing.spec.ts](../web/tests/projected-map-editing.spec.ts)：UTM 实际拖点/一次撤销/保存重开、英尺原生单位和平移缩放、固定量增删、Rigid/Clamped/Fixed Sliding、共享底余缆草稿与原子制造分歧、真实修订冲突、迟到响应和非法域拒绝。
- [equilibrium-initial.spec.ts](../web/tests/equilibrium-initial.spec.ts)：实际初态预备/动力 proof、无接触 null TD、静力输入转动态重新验收、真实迟到/坏床拒绝，以及地理平衡窗口的持久任务、partial stop 和实际子任务恢复。
- [EQUILIBRIUM_INITIAL_REVIEW.md](EQUILIBRIUM_INITIAL_REVIEW.md)：独立离散力、非均匀自然段/制造坐标与惯性物性、端半湿重、零流预应力固定点、真实运动与错误 proof 的数值对照；不表示初始不同 w/EA 已实现。
- [PLAN_EQUILIBRIUM_REVIEW.md](PLAN_EQUILIBRIUM_REVIEW.md)：独立解析投影/共同原点、制造库存、非均匀自然段、重算 checksum 后的失配反例，以及真实 HTTP/恢复。

冻结产物的基线清单单独保留在 [development_0.6_frozen_artifacts.json](../resources/validation/development_0.6_frozen_artifacts.json)，上述后端记录重新核对 19 份旧发行 ZIP、wheel、PDF 和版本 manifest，大小及 SHA 均保持一致。本轮没有生成 0.6 发行包或新 PDF，也没有新发行的首装/跨平台验收；主用户手册、设计文档与正式发布更新属于后续发行工作。开发门禁通过不能当作完整原厂复现或一次新发行验收。

最终运行范围、上述报告、两个实际 API 合成例、实际视觉 QA 和源文件快照汇总于 [development_0.6_verified_runtime.json](../resources/validation/development_0.6_verified_runtime.json)。

## 8. 保留的适用限制与后续差距

本轮解决了明确支持边界下的实际静力—动力初态与地理/库存一致性，完整目标仍有以下未完工作：初始不同 w/EA 区间、已部署实体与 EI/端力矩的真正非均匀静力；有流静止或移动铺设准稳态；可追踪加载历史的静摩擦；缆径/段间/实体连续接触；变化深度下波流与完整船舶边界；复杂床面的长航程误差控制、现场状态重建和校准。

新预应力方案仍为顺序线性化研究积分器，初态固定点对照不提供大运动、冲击、高 EA、粗网格或所有步长的普遍载荷精度保证。accepted 只表示声明模型的独立数值检查通过，不证明唯一性、全局最低能量、稳定性、加载可达性或海试精度。

投影画布尚未重投影在线瓦片/栅格底图，追加入口不替代任意段材料重划工具；GeoMedia CSF、原厂 native 文件/数据库、完整标注绘图、实船硬件协议与原厂黄金结果仍未取得兼容或等效证据。工程资源与制造域保护不会因继续复现而绕过。下一发行版本、正式手册/PDF、打包首装和跨平台验收须另行完成，不沿用冻结 0.5 的通过数字。
