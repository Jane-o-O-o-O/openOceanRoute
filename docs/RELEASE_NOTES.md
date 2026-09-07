# OceanRoute 验证与发行进度

## 0.7阶段发行

2026-10-04。0.7新增异质自然材料与有符号零长度点载荷的实际初始平衡、稳恒水平流/深度剪切流定端平衡、方向质量块v5动力与历史流场恢复，并扩展地理施工窗口和初态界面。本版后端、生产浏览器、wheel来源、全新安装及31页PDF验收已实际完成；最终归档的成员、源码、路径/CRC和wheel内容审计也已通过，首装验收与归档检查分别保存真实证据。完整原厂复现仍未完成；下方0.1至0.6章节均为相应冻结版本的历史记录。

### 新增行为与范围

- 原始初态v1现在支持活动自然区间内的分段湿重/EA、不同阻力物性及已部署零长度点实体。材料积分以制造原点O为相对基准，保留每段柔度，将每段总缆湿重的一半分配到两端节点，并按自然材料份额分摊点载荷；负湿重可提供浮力，干质量仍单独用于惯性。点载荷分摊不表示已求解连续集中力折角或完整刚体运动。
- 原始初态v2明确冻结完整`initial_fluid`，用实际节点切向、法向缆阻力及各向同性点阻力求解零速度定端平衡。深度表覆盖恒流并按端点值保持，不把平均拖曳、零流解或冷起沉降当作稳恒海流初态；完整双线性床、法向支持及整段不穿床均须验收。
- v5采用方向相关3×3质量/阻力块和实际旧受力的合并预测，再投影约束增量。原始v2对应proof v3、checkpoint schema4；旧v1/proof v1或v2/schema3/v4保持原方案，真实旧版检查点亦经续算复核，不因未来海流自动升级。
- 恢复保留初始历史流场、原材料/床格/边界和受力证明；合法未来恒流或深度表override只改变后续响应。制造站用绝对容差复核，重签摘要不能代替物理核验。初始速度为零，船速/放缆命令从实际积分步施加；固定离床锚与无接触null触底结果分开显示。
- schema2地理施工映射增加异质/海流初态支持。完整格轴、船锚及数值seed共同真实水平重基准，模型Z和垂直声明不变；O=船端制造顶站−初始自然库存≥0，首次及续算不重复放出既有库存。移动ShipPlan偏移仍是局部平床初估，不认证曲床目标跟随。
- 初态界面可明确载入合成分层流例、重新声明当前初始流场、查看材料/流速/阻力逐节点证据及恢复历史声明；输入变化使旧结果失效。新增`examples/current-initial-dynamic.json`和`examples/current-initial-plan-voyage.json`是完整合成API请求，不是实测、工程文件或checkpoint。

操作与合同见 [用户手册](USER_MANUAL.md)、[异质材料核心](HETEROGENEOUS_MATERIAL_CORE_NOTES.md)、[稳恒流核心](CURRENT_EQUILIBRIUM_CORE_NOTES.md)、[历史流恢复](CURRENT_INITIALIZATION_NOTES.md)、[海流界面](CURRENT_INITIAL_UI_NOTES.md) 及 [独立初态审查](CURRENT_INITIAL_INDEPENDENT_REVIEW.md)。

### 本版实际验证

| 验证 | 实际结果 |
|---|---|
| 完整后端 | 1438通过，65.33秒；命令wall time 65.895489秒；119份输入未变 |
| 最终同源生产浏览器 | 80项全部一次passed，233.031036秒；8770、单worker、零跳过/失败/flaky/重试；91份输入未变 |
| 实际HTTP编译资源 | 8份资源与8770/8771、当前/发行/默认/内置界面逐字节一致 |
| wheel外源码目录 | 旧27+新3共30模块真实wheel来源、真实API及持久关闭重开/子任务续算通过；旧流程3.131585秒、新流程1.786254秒，分列记录 |
| 便携包全新首装 | 新虚拟环境联网安装、HTTP界面/分析及6个合成JSON通过；其中4个实际执行prepare/完整计算/分段与JSON续算，六个关键状态数组一致 |
| 首装隔离后端 | 1438通过，80.79秒；整个安装与验收117.22秒 |
| 正式PDF | 手册16页、设计15页；全部31页实际逐页视觉检查通过，字形与页面结构检查另有记录 |
| 初次安装包运行输入 | 212份运行/测试/界面/示例等输入与首装对象逐字节一致；最终归档成员亦与当前冻结字节一致 |

wheel为805,041字节，SHA256 `366b25ac2817ed40c45f755f4f8e4b61c27e79edbb9ce7f7af2a735b2333e489`。wheel smoke在源码之外提取加载，但依赖来自当前解释器，**不是全新安装**；全新环境证据由便携包首装报告单独提供。实测macOS/Python3.13.9/Chrome。首装回归有一条测试客户端依赖弃用提醒，未影响运行；Windows/Linux、Python3.10实机和免Python安装器未验收。

实际报告为`resources/validation/release_0.7_backend.json`、`release_0.7_browser.json`、`release_0.7_browser_execution.json`、`release_0.7_wheel_smoke.json`、`release_0.7_portable_smoke.json`、`release_0.7_manual_visual_review.json`、`release_0.7_design_visual_review.json`及`release_0.7_pdf_character_bounds.json`。后台/浏览器输入分别冻结于`release_0.7_backend_inputs.json`和`release_0.7_browser_inputs.json`；完整浏览器记录确认80个actual result均仅一次passed。新增专项已经包含在上述总集中，不累计开发阶段、重复复核或旧版本数量。

最终8资源HTTP证据见`release_0.7_served_assets.json`，31页PDF汇总见`release_0.7_pdf_qa.json`。`release_0.7_verified_runtime.json`独立核对119/91/212份输入、wheel53个包文件/58个RECORD成员及24项历史基线均一致；该报告没有重跑测试/安装，也不包含最终ZIP成员审计。

独立空间细化实际运行四组非零流、混材及正/负点载荷的12/24/48段对照，使用分段连续自然坐标ODE与独立离散牵引根；四组端点位移差均随本次网格细化减小。各网格共享底牵引/自然库存，但船端位置不同，**不是共同固定端BVP或接触/剪切流的空间收敛证明**。另外两组独立连续时间力ODE对照记录0.008→0.004→0.002秒步长的实际误差减小。证据见`development_0.7_current_continuous.json`、`development_0.7_current_time_refinement.json`及 [细化范围](CURRENT_REFINEMENT_NOTES.md)，均非海试精度界。

### 发行对象与最终归档检查

产物为`outputs/releases/OceanRoute-0.7-portable.zip`、`outputs/releases/oceanroute-0.7.0-py3-none-any.whl`、`output/pdf/OceanRoute_用户手册_0.7.pdf`和`output/pdf/OceanRoute_设计文档_0.7.pdf`。最终预览为 <http://127.0.0.1:8771>，正式浏览器验收对象为8770；历史0.6预览8768保留。解压后执行`python3 launcher.py`（Windows用`py launcher.py`），默认8765，仍需Python3.10+及首次依赖联网。

真实首装ZIP及212份输入摘要见`release_0.7_initial_archive.json`。最终封装可刷新文档、PDF、验证记录、截图和发行工具，212份运行对象保持与首装输入相同；未收入原厂PDF、开发者数据库、缓存或虚拟环境。`scripts/audit_release.py`实际核对完整成员集合、规范路径/CRC、当前源码字节、wheel精确53份包文件/58项RECORD、ZIP与wheel同包内容及24项历史基线，结果通过；真实报告为`resources/validation/release_0.7_artifact_audit.json`。报告内嵌后采用不写文件的`--verify-only`复核完整成员和报告自身字节，外部清单记录最终对象。最终大小与总包SHA256由压缩包外`outputs/releases/manifest-0.7.json`记录，避免自引用。

局部非线性平衡不保证任意seed或复杂曲床可达；N18曲床直线冷起耗尽默认评估预算的真实失败仍拒绝，不暗增预算或回退零流。初始EI/力矩、波浪与移动准稳态、有限杆/6DOF、加载历史摩擦、尺寸接触、完整维修动态、复杂床长航程误差、原厂native/设备和现场对照仍未完成。accepted、守恒残差、空间/时间细化和内部work均不代表原厂等效、工程认证或Token费用。

## 冻结0.6阶段发行

2026-10-04。0.6新增投影画布实际点位编辑、完整二维定端平衡动态初态、静力输入显式转动态草稿、地理计划/制造库存桥接及任务切换保护。提供含编译界面的源码便携包、wheel和版本PDF；完整原厂功能、原生文件、设备和海试精度仍未完成。0.1至0.5冻结发行物及历史摘要保持不变。

### 新增行为与范围

- 投影画布在CRS原生单位下实际拖点、末端追加和删除，真实反算WGS84，再经制造域和完整工作区事务核验；一次提交对应一次完整撤销。Fixed Sliding保留实物KP入口，制造域启用时结构增删拒绝，共享量变不静默fork。输入、路径、完整草稿或修订变化时丢弃迟到响应。
- 定端初态使用完整床格、真实船锚端、自然库存或逐段自然长，独立求解并校核材料、受力及完整直段。零初速度进入实际预应力v4积分，schema3恢复复核原proof而不重跑静力；无接触TD和底张力为null，离床锚另列。初始活动区间仍限同湿重/EA、EI=0、无已部署实体/流/波，未来混材/实体部署不等于已实现异质初始平衡。
- 静力转动态只生成显式草稿，节点仅作数值seed，动态按更严格容限重新求解，不把accepted或显示帧当checkpoint。
- 地理床格、船锚和数值seed实际共同水平重基准，Z和声明垂直基准不变。schema2映射绑定初始自然库存、完整来源/物料/proof、计划控制和时间窗口；O=初始制造顶站−自然库存≥0，首块不重复投库存。原移动ShipPlan偏移仍是局部平床初估，不认证曲床目标跟随。schema2摘要明确标为“初始锚端与计划目标距离”，不把固定锚距离当实际触点误差。
- cancel/resume/checkpoint迟到响应绑定实际任务ID，切换任务后不替换新任务证据、不下载旧断点；真实子任务保留在后台。

步骤见 [用户手册](USER_MANUAL.md)，架构、力学和保存合同见 [设计文档](DESIGN.md)、[初始化说明](INITIAL_EQUILIBRIUM_NOTES.md) 和 [地理/制造独立审查](PLAN_EQUILIBRIUM_REVIEW.md)。两个随包JSON为明确合成API请求，不是现场测量、工作区文件或恢复状态。

### 本版实际验证

| 验证 | 实际结果 |
|---|---|
| 全部开发后端 | 1200通过，56.28秒；命令wall time 57.024秒 |
| 最终同源生产浏览器 | 74通过，219.128260秒；8768、单worker、0失败/跳过/flaky/重试；已含锚端标签语义断言 |
| 实际HTTP编译文件 | 8份文件与当前构建、内置静态资源、默认源码界面逐字节一致 |
| wheel外源码目录 | 27模块真实wheel来源，旧/新API、初态、地理库存、关闭重开及子任务续算通过；2.254535秒，使用现有解释器依赖 |
| 便携包全新首装 | 启动器创建全新环境、联网安装、HTTP界面/真实分析及两个新物理算例通过；隔离1200项后端66.54秒，整体101.53秒 |
| 正式PDF | 用户手册14页、设计13页，全部27页实际渲染并交叉逐页视觉检查通过 |

浏览器最终资源为 `index-0s_eXLiD.js` 与 `index-j8pAS7aI.css`。标签修正前74项217.425038秒保留为 `release_0.6_pre_anchor_label_browser.json`；发行前8767开发74项216.018431秒亦为独立历史记录，均不与最终74累计。独立初态19项、地理/制造26项已含于1200，投影6项、初态UI4项及任务响应3项已含于74，不再加总。

实测macOS/Python3.13.9/Chrome。包仍要求Python3.10+和首次依赖联网，不是免Python的exe/app；Windows/Linux与Python3.10实机未验收。全新环境有一条测试客户端依赖弃用提醒，未影响运行。平台、真实依赖版本与完整输出保存在首装报告。

报告为 `resources/validation/release_0.6_backend.json`、`release_0.6_browser.json`、`release_0.6_served_assets.json`、`release_0.6_wheel_smoke.json`、`release_0.6_portable_smoke.json` 和 `release_0.6_pdf_qa.json`。完整浏览器JSON逐项确认所有74个actual result一次passed，没有仅凭总摘要计数。

### 发行对象与封装证据

产物为 `outputs/releases/OceanRoute-0.6-portable.zip`、`oceanroute-0.6.0-py3-none-any.whl`、`output/pdf/OceanRoute_用户手册_0.6.pdf` 和 `OceanRoute_设计文档_0.6.pdf`。预览为 <http://127.0.0.1:8768>；解压后运行 `python3 launcher.py`（Windows用 `py launcher.py`），默认8765。第一份真实首装ZIP的大小/SHA和185份实际运行输入保存在 `release_0.6_initial_archive.json`。

最终封装仅刷新文档、PDF、报告、截图和发行工具：169份运行/测试/界面/配置/新示例源码、8份编译资源、内置手册、启动器与旧示例共185份文件保持与真实首装包完全相同，见 `release_0.6_verified_runtime.json`。新ZIP收入本版63份截图/浏览器证据，不反复打入所有历史PNG；历史图片仍在源码仓库和相应冻结旧包，旧产物不被修改。原厂PDF、开发者数据库、缓存或环境不进入发行包。

`scripts/audit_release.py`独立核对所需全部成员集合、规范路径/CRC、全部源码字节、wheel package精确集合/RECORD、ZIP与wheel同包内容、审计报告自身字节及19份历史产物。完整审计结果见 `release_0.6_artifact_audit.json`；外部 `outputs/releases/manifest-0.6.json`记录最终大小/SHA256，避免总包摘要自引用。`--verify-only`可在报告嵌入后只读核查，不改写证据。

[异质初态方案](HETEROGENEOUS_INITIAL_SCOPE.md)仅为下一阶段准备，不是已实现能力。初始异质w/EA/点实体、EI/力矩、流波准稳态、摩擦加载历史、尺寸/刚体接触、复杂床长航程误差、原厂native/黄金基准与设备/现场比较仍待推进；accepted、守恒残差和内部work不能当作工程认证或Token费用。

## 0.5阶段发行

2026-10-04。0.5新增真实投影地图、整工程地形更新、坡床/变深定端静力及四边界悬链线Calculator。提供源码便携包、内置界面的wheel和两份版本PDF；完整原厂功能、设备接入、原生文件及海试精度仍未完成。0.1至0.4包与0.4 PDF保持冻结。

| 验证 | 实际结果 |
|---|---|
| 开发环境全部后端 | 1113通过，48.82秒；源码冻结后运行 |
| 生产构建与实际浏览器 | 61通过，174.77秒；界面/API同源8766，1 worker、0跳过/失败/重试/不稳定；8份编译文件与HTTP字节相同 |
| Calculator新增证据 | 主集71、独立数学/HTTP集33，均已包含于1113；3项新增真实浏览器流程已包含于61，不另加总 |
| wheel外源码目录运行 | 25个实际模块来源、旧/新真实API流程通过，1.618秒；投影、原子地形、多边界/两长度、坡床与变化床接触均实际运行 |
| 便携包全新环境首装 | 启动器创建干净环境、联网安装、HTTP界面/真实分析通过；1113项全部通过，65.69秒，整体94.58秒 |
| 新PDF逐页视觉检查 | 用户手册11页、设计10页，全部21页实际渲染复核通过 |

实测macOS/Python3.13.9/Chrome。便携包仍要求Python3.10+，首次依赖安装需要联网；未验收Windows/Linux原生安装或Python3.10实机。干净安装的测试客户端有一项依赖弃用提醒，不影响运行。Calculator测试通过、静力accepted或守恒残差通过不代表原厂等效、稳定性、加载可达性或海试精度。

实际报告在 `resources/validation/release_0.5_backend.json`、`release_0.5_browser.json`、`release_0.5_wheel_smoke.json`、`release_0.5_portable_smoke.json` 和 `release_0.5_pdf_qa.json`；生产浏览器完整原始结果为 `web/artifacts/release-0.5/all-regression.json`。此前1009/58开发结果保留为历史记录，不充当本版最终回归。

首装使用的初始ZIP摘要保存在 `release_0.5_initial_archive.json`。最终封装仅刷新已核对的文档、新设计PDF中的模块名、验证记录和发行/smoke脚本；154份运行/测试/界面源码、8份编译资源、内置手册、启动器与示例均与实际验收初始包逐字节核对，见 `release_0.5_verified_runtime.json`。原始源码快照递归纳入了一份旧0.4生成CSS，保留该快照并明确排除这一生成文件，用真实HTTP已验收的8份0.5资源另作核验；没有把旧CSS冒充通过测试的新资源。

发行脚本从0.5起自动选本版后缀PDF，拒绝错版后缀，避免默认打入冻结0.4文档。wheel首次smoke中的模块名错误修正为 `static_bathymetry.py` 内的两个函数后，在同一未变wheel上重新完成上述真实流程。最终ZIP/wheel成员、CRC、路径安全、RECORD、PDF摘要与历史十份包/PDF不变的独立审计见 `release_0.5_artifact_audit.json`；最终文件大小/SHA256保存在压缩包外的 `outputs/releases/manifest-0.5.json`，避免自身摘要循环。

产物为 `outputs/releases/OceanRoute-0.5-portable.zip`、`outputs/releases/oceanroute-0.5.0-py3-none-any.whl`、`output/pdf/OceanRoute_用户手册_0.5.pdf` 和 `output/pdf/OceanRoute_设计文档_0.5.pdf`。当前开发预览为 <http://127.0.0.1:8766>；解压安装后的默认端口为8765。解压后运行 `python3 launcher.py`（Windows用 `py launcher.py`），启动器创建环境并打开浏览器。

投影画布目前支持平移/缩放/选点，点位修改经WGS84地图或独立XY工具；地形事务对所有必需目标整批校核并原子保存。Calculator只支持完整仿射床、正湿重、有限EA或显式不可伸长的声明有限张力域；下坡多根须逐根验收且显式选择，近峰根数未分辨时不可选择，不从失败选根回退。变化二维床静力固定锚不是触地点，不自动建立动态初态。完整摩擦历史、变深规划到动力初态、原厂native、船载协议、完整刚体/疲劳/维修动力与复杂床长航程精度仍待实现或验证，详见状态矩阵及各模块说明。

## 冻结0.4记录

2026-10-04。0.4新增显式投影坐标编辑、共享多源地形、真实二维接触及来源到动力网格转换。完整复现目标仍未完成，0.1/0.2/0.3冻结包保留。完整后端861项46.43秒通过；实际编译静态界面48项149.31秒通过，0跳过/失败/重试；全新临时环境启动器首装、HTTP及真实分析通过，再运行隔离环境861项65.98秒全部通过（整个首装验收110.25秒）。

正式wheel在源目录之外用现有依赖执行真实21模块/API流程通过，1.24秒；用户手册9页、设计9页共18页全部实际逐页渲染复核，修复一处协方差公式特殊字符缺失。发行形式仍要求Python3.10+和首次联网安装，实测macOS/Python3.13.9/Chrome；Windows/Linux与原厂/海试精度未验收。

实际报告分别为 resources/validation/release_0.4_backend.json、release_0.4_browser.json、release_0.4_portable_smoke.json、release_0.4_wheel_smoke.json、release_0.4_pdf_qa.json。159个运行/测试/界面/配置/验收脚本文件与全新首装的初始压缩包逐字节相同，冻结摘要见release_0.4_verified_runtime.json。最终封装只收入已核对文档、截图和报告；ZIP/wheel全部成员、RECORD、路径安全及历史六包未变的独立审计在release_0.4_artifact_audit.json。最终文件大小/SHA由外部manifest-0.4.json记录，避免审计报告自身引用ZIP摘要。

当前发行包 outputs/releases/OceanRoute-0.4-portable.zip、oceanroute-0.4.0-py3-none-any.whl；两PDF在output/pdf/，历史包独立保留。新安装仅一条测试客户端依赖弃用提醒，不影响运行。高EA瞬态/冲击峰值仍须另验步长收敛，残差通过和测试通过不等于工程载荷认证。柔性底余缆工作区的共享来源单独变更会因缺有效剖面安全拒绝，原库不变；全路径重采样的原子GUI事务尚未提供，详见TERRAIN_SOURCES_NOTES.md。

## 0.4新增合同与限制

坐标只转二维水平与原生单位，非ballpark最佳操作缺网格拒绝，任意坏点禁止整批应用。共享库按(-priority,id)逐点真实来源回退，同名垂直基准或明确筛选，改变解释/优先级停用旧剖面；反向/拆分/合并的派生来源仍受库摘要约束。来源到动力网格需明确海面高h并真实查询z=-depth-h，NoData不填零。二维动态是高度场点接触与有界速度冲量摩擦，新检查点完整冻结源/网格/接触；原静态/波浪组合与变深ShipPlan自动初态仍拒绝，不进入平床粗化。

合同分别见 `COORDINATE_NOTES.md`、`TERRAIN_SOURCES_NOTES.md`、`TERRAIN_BATHYMETRY_NOTES.md`、`BATHYMETRY_NOTES.md`。

## 冻结0.3记录

2026-10-04。0.3为独立实现的可运行阶段版，下列记录只对应冻结0.3包，不代替0.4验证。

## 0.3新增能力

- 定宽/多行RPL模板：开放模板JSON、完整或分列DMS、单位/缆型/分段归属、真实物理源行与逐记录诊断；先预览后明确应用，陈旧预览、坏CSV语法及非法分段不能应用。默认collect，显式skip仍提示跨坏记录桥接风险。见 [RPL_TEMPLATE_NOTES.md](RPL_TEMPLATE_NOTES.md)。
- 地形扩展：真实稀疏变分薄板/膜张力求解、实际算子零空间锚定和工作量预算、BLN/GeoJSON裁剪、完整GeoTIFF自由折线切片及缺测分段CSV/BLN。非原厂SOR；亚格孔洞未必由离散算子分辨，切片距离是局部投影量。见 [DTM_NOTES.md](DTM_NOTES.md)。
- 规划制造到施工窗口：真实ShipPlan/SLD生成材料、船位和放缆；初始自然悬垂库存不重复放出，逐块核对船端制造位置，映射随真实结果/检查点保存。改工程后旧恢复拒绝，空工程表示使用保存快照；只支持有效定深平床和同物性初态，有限长体明确拒绝，初始锚偏差真实显示。见 [PLAN_VOYAGE_NOTES.md](PLAN_VOYAGE_NOTES.md)、[PLAN_VOYAGE_REVIEW.md](PLAN_VOYAGE_REVIEW.md)。

## 0.3验证

| 验证 | 实际结果 |
|---|---|
| 开发环境全部后端测试 | 708通过，39.21秒 |
| 新增后端证据 | RPL31、DTM57、制造施工映射主集20/独立8；共116新增，已包含于708，不再叠加；DTM旧2项亦保持通过 |
| React/TypeScript生产构建 | 通过；编译界面显示0.3 |
| 完整浏览器回归 | 39通过，约2.2分钟；31旧用例+6项RPL/DTM+2项制造施工映射 |
| wheel外目录运行 | 0.3、17关键模块来源、6静态资源和旧/新增真实API流程通过；最终包再次核对 |
| 压缩包全新环境首装及回归 | 启动器创建隔离环境、联网安装、HTTP界面/真实分析通过；708项后端通过，54.39秒，首装完整流程80.65秒 |
| PDF逐页视觉检查 | 手册9页、设计8页，全部17页实际渲染复核通过；去除强制分页与嵌套整章导致的大留白 |

开发后端、浏览器、wheel、全新安装及PDF记录依次为 `resources/validation/release_0.3_backend.json`、`release_0.3_browser.json`、`release_0.3_wheel_smoke.json`、`release_0.3_portable_smoke.json` 和 `release_0.3_pdf_qa.json`。初始完整回归发现独立日期线测试把投影舍入限定在1e-8米，实测差5.4e-8米；采用有依据的1e-7米容差后完整708项通过，未更改制造守恒或业务校核阈值。恢复时工程失配反例经真实求解复现并修复，不以放宽断点核验通过。

DTM万节点例使用显式500M工作预算，143.399M实际非零访问工作量，默认50M不足；不是百万点性能证据。10秒规划施工例新增放缆5.1m、初始约12.23844m库存分别核对，不能证明触地点贴合计划或海试精度。

## 0.3发行范围

当前发行路径为 `outputs/releases/OceanRoute-0.3-portable.zip` 与 `oceanroute-0.3.0-py3-none-any.whl`；最终大小、SHA256、PDF页数和检查报告记录在 `manifest-0.3.json`。最终构建会清除本包的setuptools缓存，逐文件核对wheel与当前源码/界面/手册；历史包摘要保持不变。需要Python3.10+，首次安装联网，尚非免Python桌面安装器。全新环境有一项测试客户端依赖弃用提醒，不影响服务运行。Windows/Linux原生安装、Python3.10环境、原厂黄金结果、现场设备及工程精度尚未验收。完整状态矩阵保留未完成项，不把研究阶段版称为完整原厂功能/精度等效。

## 冻结0.2记录

2026-10-04。0.2为独立实现的可运行阶段版，完整复现目标仍未完成。下列开发环境、wheel外目录运行和压缩包全新安装均已实际验证，0.1历史包独立保留。

## 0.2新增能力

- 同工程多Cable Path/Assembly关系库：workspace schema2共享缆材/GIS、多路径、独立制造库存及显式投放/互斥备选关联；独占柔性制造量随编辑更新，共享量变需要明确fork，制造实体只采购一次。完整工程修订、并发冲突保护、schema1迁移及开放JSON往返见 [WORKSPACE_NOTES.md](WORKSPACE_NOTES.md)。
- Seismic研究功能：使用实际稳态缆形预测应答器位置，按用户声明的ENU/材料映射和绝对测量协方差反演均匀东/北流；接受状态同时检查收敛、可辨识性、数值稳定、边界及误差一致性，不代表现场声学定位精度。见 [SEISMIC_NOTES.md](SEISMIC_NOTES.md)。
- 连续航程研究计算：分块恢复完整材料状态，按内部稳定接触证据作平床材料守恒粗化，并真实运行原/候选网格短时双解；后台任务支持取消、断点续算、故障恢复、目录单写者、额度和删除。见 [VOYAGE_NOTES.md](VOYAGE_NOTES.md)、[VOYAGE_PHYSICS_REVIEW.md](VOYAGE_PHYSICS_REVIEW.md) 和 [VOYAGE_JOB_REVIEW.md](VOYAGE_JOB_REVIEW.md)。

## 0.2已完成的开发环境验证

| 验证 | 实际结果 |
|---|---|
| 全部后端测试 | 592通过，36.02秒 |
| 完整浏览器回归 | 31通过，约1.8分钟 |
| 3D视图fog修复后的相关复核 | 10通过，35.6秒；属于已有浏览器用例的重复复核，不另加总 |
| React/TypeScript生产构建 | 通过 |
| 新增模块证据 | 工作空间31、Seismic57、连续航程主集10、独立物理26、持久化完整性25，均已纳入592项后端总数 |
| 0.2 zip/wheel构建 | 通过；包含编译界面、实际手册、模型说明和两份8页PDF |
| 0.2 wheel脱离源码目录运行 | 通过；核对0.2版本及模块来源、6静态资源、完整手册、旧分析/修订保护、真实工作区迁移/共享库存/完整修订恢复、Seismic反算、后台连续任务/检查点/子任务续算及关闭重开 |
| 0.2压缩包全新环境首次安装启动及回归 | 启动器创建隔离环境、联网安装依赖、HTTP界面和实际路线分析通过；隔离环境592项后端通过，48.06秒 |
| PDF视觉检查 | 用户手册8页、设计文档8页逐页渲染检查，修复孤尾和孤标题 |

这组记录不包含Windows/Linux原生安装、Python 3.10环境或现场工程认证。新增UI用例已包含于31项完整浏览器回归，不能与重复复核或后端模块数量相加。

## 1800秒连续计算的实际证据

配置与真实摘要为 [resources/validation/voyage_1800s.json](../resources/validation/voyage_1800s.json)。该例是水深10m、平床、无流的明确合成工况，船速0.5m/s、放缆0.6m/s、内部步长0.05s；没有把它推广为复杂海底或实船航程验证。

| 指标 | 实际结果 |
|---|---|
| 请求/实际计算时长 | 1800秒，completed；60块均数值收敛 |
| 接受粗化批次/内节点合并 | 56批/1184次 |
| 最终活动节点 | 117 |
| 累计放缆量 | 约1080m |
| 材料长度平衡残差 | 7.285×10⁻¹⁰m |
| 程序工作量估计 | 109,146,264工作单元；不是CPU指令或token |
| 本机实际墙钟时间 | 42.12秒 |
| 局部完整材料probe最大位置差 | 3.775×10⁻⁵m |

局部probe和材料平衡检查不能证明1800秒累计误差；尚无相同1800秒全细网格参考、独立求解器全程对照或海试。安全粗化不成立时保留原状态，预算/节点/历史达到限制时如实停止，不能显示为完整请求已完成。

## 0.2发行与适用范围

产物为 `outputs/releases/OceanRoute-0.2-portable.zip` 与 `outputs/releases/oceanroute-0.2.0-py3-none-any.whl`；校验值和大小见同目录 `manifest-0.2.json`。完整wheel和隔离环境报告随源码放在 `resources/validation/release_0.2_wheel_smoke.json`、`release_0.2_portable_smoke.json`，核验过的运行文件摘要见 `release_0.2_verified_runtime.json`。最终封装只更新文档、验收脚本和记录，运行代码/测试/示例与已验收压缩包逐文件SHA256核对。

源码压缩包采用Python启动器，要求Python 3.10+；它不是免Python的Windows exe或macOS app。主服务默认 `127.0.0.1:8765`，首次依赖安装和在线底图需要网络；用户数据与发行程序分开保存。实测为macOS、Python 3.13.9、Node.js 24和Google Chrome；隔离环境依赖版本记录在首装报告，592项回归有一项测试客户端依赖弃用提醒，不影响服务运行。

当前没有原厂黄金输出、海试或独立工程验收，不宣称完整MakaiPlan/MakaiPlan Pro功能与精度等效。尚未完成原厂原生文件、实船设备/实时控制协议、完整6DOF/波浪疲劳/钩挂维修动力学、复杂海底长航程精度及百万点地形验收。Seismic不是实时Kalman同化或硬性穿点控制；连续航程未自动把规划KP/制造里程映射为动力材料坐标。Windows锁、真实断电和网络文件系统仍待实机验证。完整状态及各模型限制见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。

## 冻结0.1历史记录

以下保留2026-10-04的0.1交付事实、当时未完成功能及当时测试环境。它们只说明冻结0.1产物，不替代后续发行验收，也不将已在0.2新增的能力继续列为当前未实现。

### 0.1交付内容和启动

- `outputs/releases/OceanRoute-0.1-portable.zip`：完整程序源码、编译界面、启动器、示例、测试、模型说明和两份 PDF。解压后 macOS/Linux 用 `python3 launcher.py`，Windows 用 `py launcher.py`；需要 Python 3.10+，首次安装依赖需要联网。
- `outputs/releases/oceanroute-0.1.0-py3-none-any.whl`：含内置界面和手册的 Python 安装包；可用 `python -m pip install 'oceanroute-0.1.0-py3-none-any.whl[terrain]'`，之后运行 `oceanroute --open`。
- 主手册 PDF 6 页、设计 PDF 5 页，逐页渲染检查。详细参数和研究模型限制在 `docs/*_NOTES.md`。

压缩包不是免 Python 的 Windows exe 或 macOS app。主服务默认 `127.0.0.1:8765`；离线经纬网不需要网络，在线底图需联网。用户工程存在 `.oceanroute`，发行包不含开发者工程数据库或 `.venv`。

### 0.1本轮验证

| 验证 | 结果 |
|---|---|
| 开发环境全部后端测试 | 443通过，8.32秒 |
| 完整浏览器回归 | 23通过，约1分钟；生产页面实际数值/剖面/状态已核对 |
| React/TypeScript生产构建 | 通过；主包约472KB，其他功能按需加载 |
| wheel脱离源码目录运行 | 内置界面和6个静态资源、内置手册、实际分析及版本保护通过 |
| 压缩包全新环境首次安装启动 | 启动器实际创建隔离环境、联网安装依赖、HTTP服务/内置界面/实际分析通过 |
| 全新环境后端回归 | 443通过，36.22秒；测试客户端有1项依赖弃用提醒，不影响运行 |
| 合成示例 | 制造回写、海况实际求解、调查匹配和四类维修工具逐个实际执行通过 |

本轮实测环境为 macOS、Python 3.13、Node.js 24 和 Google Chrome。全新安装实测使用 FastAPI 0.142.2、NumPy 2.5.3、SciPy 1.18.1、pyproj 3.8.0、Shapely 2.1.2、Rasterio 1.5.2、ContourPy 1.4.0。Windows/Linux的原生安装和Python 3.10环境尚未实际验收，跨平台启动路径已处理。

修复包括：JavaScript JSON整数/浮点等价的约束签名、缺少/过期保存版本拒绝覆盖、合法津贴省略字段、制造附件绝对结束里程、参考点零长度声明，以及工具/约束/装配预览与当前输入不一致时禁止应用。

### 0.1当时仍需推进

当前覆盖规划、制造/路径约束、开放交换、地形/避让、施工计划、局部三维动力、完整仿真断点、海况/垂向RAO、调查对账和部分维修研究工具。未完成地震缆应答器/海流同化、完整全航程移动窗口、完整钩挂/回收/浮标动力学、波浪疲劳、原厂原生文件和船载协议；百万点地形性能也未验收。

没有原厂黄金输出、实测海试或独立工程验收，不能宣称与完整 MakaiPlan Pro 功能/精度等效。状态矩阵和每个模型的假设随源代码交付；不通过原厂授权机制获取源码、付费组件或许可。

Token 由使用平台按账户结算，助手不接受转账，也不能把内部工作量统计冒充最终账单。
