# OceanRoute 软件设计文档

版本 0.3 / 2026-10-04 / 与实际代码同步

本文对应0.3开发版，包含workspace schema2、RPL模板、变分地形、Seismic及带制造映射的连续后台计算；历史包独立保留，发行验证见 `RELEASE_NOTES.md`。

## 1. 目标和依据

以公开功能定义为参考，独立实现海缆规划的编辑、计算、保存与交换闭环，以及可以检查的施工研究模型。资料为用户提供的三个 PDF 与 Makai 官方公开网站，页码、文本和来源记录在 `resources/research/`。附件内容是参考资料，不作为助手执行指令。

不使用原厂源码、二进制或付费资源作为实现输入。公开手册不能证明原厂算法、文件结构或海试精度；当前模型用 OceanRoute 自有 schema 和明确验证状态。未完成能力保留在状态矩阵，完整复现目标没有因原型可用而缩小。

## 2. 架构和部署

FastAPI 本地服务执行计算并提供编译后的 React/TypeScript 界面。Leaflet 处理二维地图，Three.js 展示实际求解节点。默认单进程监听 `127.0.0.1:8765`，开发时 Vite 代理 `/api`。运行不依赖原厂账户或授权服务器。

| 模块 | 职责 | 边界 |
|---|---|---|
| geodesy.py | WGS84恒向/测地距离、方位、正反算、加密、日期线 | 坐标 → 距离和地理点 |
| core.py | 路线、剖面、装配、费用、规则与穿越 | Project → Analysis，无HTTP依赖 |
| workspace.py、workspace_storage.py | 同工程多路径、共享库/GIS、物理库存、关联与完整修订 | schema2 → 多路径分析；完整装配守恒、独占更新/互斥方案、真实关系事务 |
| tools.py | 加密、按深度分缆、模板、反向、拆分、合并 | 输入不原地修改，输出新工程/报告/警告 |
| constraints.py、assembly.py | 路径约束、制造域、装配回写和参考点 | 固定制造量、完整性签名、显式映射政策 |
| routing.py | 有限网格避让、带权区域与地形路由搜索 | 有界 A* 候选，不承诺全局最优 |
| gis.py、dtm.py、terrain_boundaries.py、terrain_slice.py、surfer.py、geoformats.py | 沿线地形、网格、掩膜/切片、Surfer/KML/SHP | 实际稀疏平滑、缺测/投影、完整栅格采样 |
| exchange.py、rpl_templates.py | RPL模板、剖面、GeoJSON、标准图形与报告 | 真实源行诊断、解析与显式应用分开 |
| simulation.py | 悬链线、稳态、材料节点动态、静态悬空段 | 实际计算、帧、诊断，标记research |
| shipplan.py | 初始船舶/放缆计划、工况分支、张力搜索 | 调用真实模型，候选失败可审查 |
| plan_voyage.py | 规划与制造库存到局部动力窗口 | 自然初始材料、地理指令、制造预算与保存一致性 |
| checkpoints.py、sea.py | 完整动态状态、海况/RAO/Monte Carlo | 状态完整性、实际分支求解、研究假设 |
| survey.py、repair.py | 实敷对账、回收/拖索/浮标研究 | 原设计不被观测覆盖、端力与适用条件明确 |
| seismic.py | 真实稳态应答器算子、均匀水平流加权反演 | 明确ENU/材料映射/测量噪声；接受状态不同于优化收敛 |
| voyage.py、voyage_jobs.py | 真实状态连续求解、海床网格粗化、后台任务 | 内部步稳定证据、材料守恒/位置误差、双解检验；原锚与全材料保持活动 |
| storage.py | 旧schema1单路径工程与修订SQLite事务 | 保留旧数据及迁移入口，与workspace表同库共存 |
| api.py | 输入验证、HTTP错误、文件输出、静态服务 | 错误422，未找到404，禁止非有限JSON |
| web/src | 编辑联动、撤销、保存、三维、导入下载 | 只展示当前返回数据，不补假模型结果 |

计算内核可独立从 Python 调用。未知值为 null，绝不输出 NaN/Infinity；程序对文件大小、点数和求解工作量设置明确上限。

## 3. 实体与里程

当前开发版以 `Workspace(schema_version=2)` 作为一个工程的唯一来源，含名称、统一币种、共享 cable_types/layers、多条 paths、多套 assemblies、显式 associations、active_path_id 和完整 saved_revision。每个 Cable Path 内嵌一个 schema1 Project，保留 route、profile、bodies、assembly_references、costs、rules、events 等原核字段。共享缆材/GIS和独立修订不保存在子实体中。选中路径经 materialize_path 注入共享资源和 workspace_context，复用原 core；路径ID即投影ID，路径名称与工程名称分开。子实体字段见 `CONTRACT.md`，完整开放容器合同见 [WORKSPACE_NOTES.md](WORKSPACE_NOTES.md)。

制造装配是以实物里程排序的 cable/body/reference 库存，不是另一个 Project。association 将一个路径与一套完整实物关联，role=deployment 或 alternative。每条路径最多一套装配，同一装配最多一个 deployment；alternative 必须有明确主投放路径。模型不自动分割库存或同时敷设同一实物两次。As-Laid 调查成果保持独立GIS/对账数据，当前路径kind只支持cable。

```mermaid
erDiagram
    WORKSPACE ||--o{ CABLE_PATH : contains
    WORKSPACE ||--o{ ASSEMBLY : owns_inventory
    WORKSPACE ||--|| SHARED_RESOURCES : owns
    CABLE_PATH ||--|| PROJECT_SCHEMA1 : embeds
    CABLE_PATH ||--o| ASSOCIATION : selects_inventory
    ASSEMBLY ||--o{ ASSOCIATION : referenced_by
    ASSEMBLY ||--|{ MANUFACTURING_ITEM : contains
```

RoutePoint 是地理点和注释；RouteLeg 对应相邻点对，定义缆型、固定量/柔性目标、余缆基准、埋设和停时。ProfileSample 沿路线表面 KP 定位。CableType 是物性/价格/速度库；Body 保存实物起始端、有限长度、占用规则及单件价格。Layer 保存经过检查的 WGS84 GeoJSON 和用途。Event 表达同一 KP 可重复的独立作业。

路线 KP、底距、实物 cable KP 是三套坐标，不能互代。SLD 按实物装配推导，材料量、附件量和总长分开。附加缆长可绑定缆型覆盖；有限体用起点与末端处理，在变向和拆分中维护完整跨度。工作区schema_version为2，core仍消费schema1子路径；完整JSON导入支持显式schema1迁移，不支持的版本拒绝。

## 4. 地理、地形和余缆

测地线调用 PROJ/pyproj WGS84 Geod；恒向线使用椭球等距纬度和子午弧。haversine 不是恒向算法。曲线加密显示，跨日期线 GeoJSON 分段，屏幕经度展开避免绕地球连线。

有效剖面在区间边界插值，再对所有小段累计 `sqrt(ΔKP²+ΔDepth²)`，包含中间海岭/海谷；缺测不会静默变0或跨空区补算。点水深可形成有标签的线性近似，不证明中间海底已测。

柔性表面量 `L=D(1+s/100)`，柔性底量 `L=B(1+s/100)`；固定量保持L并反算余缆。同 KP 附加量不计算百分比。材料费按有效缆材量价，附件计件；船舶初步工期按平面距离/推荐速度加停时，单独标明不含实际放缆动力响应。币种不默换算。

## 5. 有效性和转换不变量

profile 保存路线签名、来源和采样质量。签名含坐标、曲线和坐标系；位置变化导致旧剖面失效。能证明原空间曲线未变的同曲线加密可同步签名；几何改变的转换不能恢复失效水深。签名证明关联一致，不证明测深质量。

tools 统一处理反向、拆分与合并中的实物、剖面、事件和附加量。固定量比例分配并承接浮点残差；有限刚体不允许被切开。异地合并增加真实连接区间，不能靠移动端点隐藏距离。工具报告变换前后材料量、费用和地形来源，相关不变量有测试。

路径约束捕获制造域、Path Link 物理位置与已物化签名。刚性点不会被求解器自动移动，夹持点保留域内表面比例，滑动点随固定制造位置求解。直接改变已经捕获的几何/材料会被拒绝；未支持的结构变换需显式清除约束。制造清单替换要求明确的表面比例映射，参考点为零长度，有限体占用既有材料时总量守恒。

工作区关联再次核验 core 所得制造总长、缆型顺序/区间、体的身份/跨度/物性/费用、制造参考身份与实物站位；总长相同不足以构成合法关联。比较容差为1e-5，长度量对应米，不作制造公差证明。同曲线细分允许合并同型区间作比较，同时保留库存实际条目ID。不同装配的制造条目ID不可复用；显式alternative通过同一assembly实体共享，默认independent复制则生成新体/参考/缆条目身份和装配。数值签名规范化同值int/float及正负零，关联判定仍用数值比较。

update_path默认auto_exclusive：独占deployment的柔性制造量随core自然更新，固定段/Path Link域不放松。共享库存量变拒绝，显式fork才创建新物理实体并保留旧库存；主投放路径fork/移除后仍有备选时必须指定successor_path_id。共享资源修改需显式update_shared并核验全工程。set_active只选择编辑投影，set_deployment才切换安装归属。

总账按唯一assembly计采购、按deployment计船费/埋设/事件/预备费，alternative不重复采购或安装；未分配库存仍计采购，但没有安装/预备费；未关联方案只供分析。所有库、路径、装配必须同币种，不自动换汇。路径费用摘要仍可单独展示，不能直接相加当作全工程费。完整分配与费用政策见 [WORKSPACE_NOTES.md](WORKSPACE_NOTES.md)。

## 6. GIS与DTM

XYZ 转局部 AEQD 米制坐标后使用 Delaunay 线性插值或 IDW；默认凸包外不外推，最近测点过远时缺测。GeoTIFF 按 CRS 转换后采样第一波段，保留NoData、水深正方向和垂直基准。EPSG 不替代潮位基准。

KML 保留属性与高度，Shapefile ZIP 按声明 CRS 和字符编码转换。Surfer 独立读取 DSAA/DSBB/DSRB，按显式坐标系、垂直符号及单位采样，断层网格限制为最近节点，不将缺测补为零。路由搜索在局部 AEQD 网格上检查边与障碍，二维地形才可支撑新曲线的地形约束；旧沿线剖面不能代替二维海底图。

DTM 生成规则米制网格，梯度给出坡度/坡向，照明得阴影，ContourPy 得等深线并回转WGS84。四波段 GeoTIFF 和下采样预览分别提供，不混淆预览与计算分辨率。当前200k散点/250k单元上限，不声称原手册百万点分块性能。限制区、穿越、缓冲采用局部投影与Shapely，结果不等同完整工程认证。

minimum_curvature以去平面趋势残差构建掩膜内稀疏薄板二阶差分、膜张力一阶差分和测点双线性软约束，用LSMR实际求解。每次矩阵乘法按非零访问量计预算；不把IDW结果重命名为最小曲率。纯薄板要求完整2×2单元的边连通及每域实际观测算子仿射零空间锚定，孤立细边节点保留NoData；正张力按四邻膜图检验常量锚。无锚分量保留NoData；全部无有效锚、薄板细窄域/仅角连接或无约束矩阵列等拒绝。输出真实收敛、条件估计、残差与能量，容差是LSMR相对停止条件，不等同原厂SOR或边界张力。

GeoJSON/明确CRS的简单BLN给出有效掩膜，源点与网格节点受同一边界筛选；亚格孔洞未命中节点时不被曲率差分完整分辨，等深线与切片另核对原多边形。切片读取完整GeoTIFF，在栅格投影中累计折线水平距离并回转WGS84，不能当作航路椭球KP；边界/栅格相交与间隔检查识别缺测，不仅看离散站点。未知水深为null，空档底距不积分，整个底距无完整覆盖时为null。最小曲率单次上限20k源点/40k活动未知节点、最多256域，切片上限10k站点；精确参数见 `DTM_NOTES.md`。

RPL模板schema1明确固定宽度或分隔多行、索引起点、物理头部行、注释、字段单位、DMS/深度方向和分段归属。固定位置按Unicode代码点，CSV逻辑行保留物理源行范围；不使用eval。输出点、分段、源KP与计算KP、错误/警告及can_apply，默认collect有错禁用，显式skip仍保留桥接风险；非法分段或损坏语法不能应用。源KP不覆写WGS84几何，实物累计KP差形成固定制造段。模板和预览有独立字节/记录预算，界面用输入/工程签名禁用陈旧应用；详见 `RPL_TEMPLATE_NOTES.md`。

## 7. 物理求解和施工计划

解析悬链线有解析几何/张力基准；稳态积分重力和法向流阻平衡；动态采用材料节点、惯性、轴向约束、阻力、附加质量及海床接触。EA/EI处理有具体数值意义，材料量、接触、张力及收敛随结果输出。内部积分步长和输出帧间隔分开，计算工作量显式限制。

混合材料/有限体按材料坐标配置，分布质量模型不声称完整刚体六自由度。模拟窗口的固定边界、海床、初始状态和未建模项列入假设。悬空段为静态小斜率张力梁/障碍接触，不是全波浪频域疲劳模型。公式、参数、来源、限制与验证见 `MODEL_NOTES.md`。

ShipPlan 根据规划段和稳态偏移生成指令，偏移变化产生有位置和时长的过渡指令，仍需动态核验。Look Ahead 对共同初始条件或完整 checkpoint 实际动态求解；张力搜索记录已评估候选、误差和失败，只在真实目标满足时报告成功。完整状态含位置、速度、材料、载荷、边界与命令时基，SHA256 验证序列化完整性。续算保留绝对时间及累计量，只允许声明的未来控制变化；断点引入新的内部步边界时仍需考虑离散误差。三维动画来自求解器。详细合同见 `MODEL_NOTES.md` 和 `SHIPPLAN_NOTES.md`。

海况模块使用有限离散波谱及随机相位，用户 RAO 复数频响插值生成垂向运动。有限水深 Airy 运动学可进入动态法向流阻，尚无完整流体惯性或船舶六自由度响应。Monte Carlo 逐个实际求解，以独立种子和受限参数扰动计算样本统计。配置、公开方程来源和限制见 `SEA_NOTES.md`。

调查对账在实际 WGS84 曲线上查询最近规划 KP，分别输出横偏、端点沿向残差及回环歧义。完整观测链才给出总量，缺测/空档不跨越积分；水深基准和实物 KP 偏移显式对齐。维修研究复用解析缆形并积分稳态拖曳载荷，抓钩绳长基于已着底受力条件；浮标按垂向端力和阿基米德关系选型，水平平衡另行核对。两者均不代表海床接触识别或完整维修动态控制。见 `SURVEY_NOTES.md` 与 `REPAIR_NOTES.md`。

Seismic研究复用repair.steady_tow的实际三维均匀缆稳态平衡，按传感器从船端的悬垂弧长在节点间插值位置，再加共同ENU船位。观测可用arc_from_vessel_m，或material_m与top_material_m映射；超出任一候选悬垂段拒绝。参考系、全浸水正湿重、平海床、底端已知力和船速/艏向均显式输入；混合材料、动态、分层流、波浪等不支持请求拒绝，不静默降阶。

estimate_current 只拟合共同的东/北向均匀海流。给定绝对标准差或 SPD 测量协方差，经 Cholesky 白化残差，用有界非线性最小二乘实际重复求解；缺测轴删除，不补零。有限差分 Jacobian 及步长减半检查用于局部秩/稳定性判断；只有优化收敛、rank=2、敏感度稳定、无活动边界、误差一致时 estimate_accepted 为 true。协方差 (JᵀJ)⁻¹ 不按残差强制缩放；未接受时仍保留 best-fit 几何，但协方差/标准差为 null。时间仅为独立记录标签，不是 Kalman 传播；形式协方差不传播物性、船位、材料映射或模型误差。输出实际节点、应答器预测/观测/残差、可辨识性和真实计算预算。没有强制穿点、实时同化、OBC 全程回收或原厂精度证明；`POST /api/seismic/predict` 与 `POST /api/seismic/estimate` 请求 `{config}`，详见 [SEISMIC_NOTES.md](SEISMIC_NOTES.md)。

连续计算逐chunk恢复完整动态状态，检查整个内部步序列的海床接触/低速证据；只对远离边界/触地点/实体、同材料、零EI、近直线的平床元素合并。自然长度及材料积分不变，删除节点的动量按两邻实际质量增量转移；带符号应变及材料插值位置同时受限。原网格/候选网格短时双解比较所有原材料点的重构、速度、接触、张力/内部峰值与数值收敛，未通过则保留原状态。没有删除尾缆或引入新固定触地点。逐转移/probe阈值不构成累计航程误差界，完整公式、合同和实际独立审核见 `VOYAGE_NOTES.md`、`VOYAGE_PHYSICS_REVIEW.md`。

prepare_plan_voyage先运行真实ShipPlan与SLD，把固定窗口投影到AEQD并保留原放缆率；投影端点决定局部船速/航向，采样弦差受明确容差约束。制造材料、初始自然长度和动态初态使用相同EA/预应力语义。默认从源计划库存足以容纳初始悬垂段的时刻开始，初始前缀不重复放出。每段制造坐标、路线KP和源/局部时间分别保存；不把初始锚强制贴到计划触地点。

映射SHA256含原工程、完整源动态配置和区间预算，启动、检查点读取与每个保存分块校核实际材料船端位置/参数。恢复保存映射后，非空工程必须匹配原摘要，空工程显式使用保存快照；超过预备时间窗拒绝。现阶段只接受有效定深平床、同物性初始库存，未来可混合材料/转向，零长实体以形函数加载，有限长体拒绝；它不是实敷历史重建或复杂地形施工认证。HTTP为 `/api/shipplan/prepare-voyage`，详情见 `PLAN_VOYAGE_NOTES.md`。

## 8. 保存、文件和界面一致性

SQLite WAL 与BEGIN IMMEDIATE保存工程和修订。开发版workspace使用独立normalized表存当前工程元数据、path、assembly和association；关系有复合外键、每路径单关联和每装配唯一deployment的部分唯一索引，完整历史存JSON快照。旧ProjectStore表保留用于schema1数据，不把多个旧工程列表当作同工程关系库。事务读取避免跨修订混装父/子记录；制造校核通过后，版本比较、父/关系重写与历史追加在一个事务中完成，失败全部回滚。

saved_revision保护整个工程的多窗口冲突，已有ID缺少修订或使用旧号均拒绝；恢复历史必须带expected_revision，并追加新修订。投影没有独立saved_revision。schema1迁移建立新工作区/路径/装配，保留origin_project_id；schema2导入建立新工程ID、保留内部关系并记录origin_workspace_id，均不继承旧修订。底层拆分/合并仍产生独立schema1结果；加入当前工作区须经路径操作及关联守恒核验。默认本机服务，不含云协作或账号系统。

HTTP 的 `POST /api/workspace/migrate`、`POST /api/workspace/action` 和 `POST /api/workspace/import` 返回 workspace/project/analysis/report/warnings envelope；`POST /api/workspace/analyze` 直接收工作区并返回分析；`POST /api/workspace/export` 输出完整 JSON。`GET/POST /api/workspaces` 列出/保存工程，`GET /api/workspaces/{id}/revisions` 与 `POST /api/workspaces/{id}/restore/{revision}` 读取/恢复整工程历史。接口使用未知值 null 和有限 JSON，非法结构/混币/数量不一致/版本冲突为 422，不存在的存储实体为 404。精确请求及 Python 签名见 [WORKSPACE_NOTES.md](WORKSPACE_NOTES.md)。

导入保留行错误，CSV输出防电子表格公式解释，XML/HTML转义用户文本。JSON是完整工程载体；KML/GeoJSON/DXF/SVG为开放交换，不能保证原厂属性往返。缺少原生schema和样例时不声称兼容。

界面以完整workspace和活跃draft作为一个document状态；草稿经update_path校核后再物化，切换/保存/完整导出先提交草稿。共享关联拒绝时保留草稿，明确fork或撤销，不覆写合法库存。响应对应输入快照，防慢响应覆盖新状态；制造关系未通过时不允许保存为已接受工程。撤销/重做覆盖整个document，与数据库修订分开。各路径地图用同次分析的真实route_geometry与日期线segments；Seismic三维显示实际稳态节点和完整观测点，未测轴不伪造。

Voyage后台单工作线程、最多四个运行/排队任务，以规范UUID定位有限JSON文件。文件fsync、随机独占临时文件、原子replace及POSIX目录fsync保护已完成chunk；重启从实际checkpoint/result校验并重建状态，坏status隔离。跨进程目录锁直到所有写者退出才释放，未启动的API对象惰性不抢锁。取消停于完整chunk，恢复建立新任务并保留parent_job_id；预算/容量停止与失败不伪装完成。默认保存任务250个、数据1GB，降低配额后仍可读/删旧任务；元数据64KB独立限制。主算/两个probe都在剩余工作预算预检，帧只保留真实采样。模型checkpoint带方法标识与SHA256，旧开发方法不兼容时拒恢复。HTTP合同与耐久测试见 `VOYAGE_NOTES.md`、`VOYAGE_JOB_REVIEW.md`；Windows/断电/网络盘尚无实机验证。

## 9. 验证与后续工程证据

验证层次为解析/测地基准、材料/费用不变量、缺测/失效、文件生命周期、API集成及浏览器操作。覆盖日期线、剖面山谷、缆型转换点附加量、有限体跨度、平衡/接触和真实下载；通过测试不证明实海误差或未覆盖规模。

0.3全部后端708项通过，39.21秒，包含新增RPL31、DTM57及制造施工映射28项；模块数量不能再与总数叠加。验证真实解析与物理源行、薄板/膜数值基准及不适定/缺测拒绝、制造初始库存不重复放出、日期线船位、混合材料/实体与真实持久恢复。0.2原有工作区/Seismic/后台耐久回归仍包含在内。浏览器、安装、PDF及历史版本证据分别见 `RELEASE_NOTES.md`。

1800秒合成浅水连续案例实际完成60块计算、1184次网格合并，最终117个活动节点，累计放缆1080米，材料平衡残差7.285e-10米；局部双解检查最大材料位置误差3.775e-5米。该案例没有1800秒全细网格参考，局部误差不能作为全航程精度界。配置与结果保存于 `resources/validation/voyage_1800s.json`，另外120秒案例有全材料参考比较。

后续需有权使用的RPL/SLD/地形和张力/触地点数据、原厂开放交换黄金样例及工程人员独立验收，建立误差预算。当前能力、未完成项和验证状态见 `IMPLEMENTATION_STATUS.md`。
