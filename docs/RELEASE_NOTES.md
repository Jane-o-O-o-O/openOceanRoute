# OceanRoute 验证与发行进度

## 0.10阶段发行

2026-10-04。新增全工程穿越、复合邻近与坡度自动规则，开放规则包导入导出、错误实例地图/剖面定位以及真实二维缆体坡度窗口。三类规则明确保存比较符、AND/OR、typed特征引用、距离轴、主体KP及有效深度范围；缺失引用保留为可编辑声明，缺测、不确定和预算不足不误判为通过。候选经当前完整工作区事务应用，支持撤销，用户保存才写库，路径/共享装配/制造库存不被规则检查改写。大地线转角改为同一顶点的真实入段到达与出段初始方位差。

二维组件坡度使用来源的真实圆域探点、七支撑六子片及三个实际查询角点的梯度。几何子片质心未查询时水深为null，查询见证与主体圆心分开显示；采样通过不认证连续圆域最大坡。GIS距离采用真实地理原语与实际路线曲线的有限筛查，容差、地理拓扑适用范围及无法消除的歧义随报告返回。检查预算不覆盖先行完整工程/来源准入，也不是CPU、内存、墙钟或Token费用上限。

| 验证对象 | 实际结果 |
| --- | --- |
| 完整后端 | 1922通过、97.25秒；进程97.909517秒，161份输入不变，零失败/错误/跳过 |
| 最终生产浏览器 | 99项各一次passed、344.528815秒；进程344.856472秒，同源8777、Chrome154.0.8037.93、单worker、retry0，182份输入不变，零失败/跳过/flaky |
| 实际界面资源 | 8份HTTP文件与正式浏览器、发行/默认/内置静态目录逐字节一致，在线手册与源码相同 |
| wheel外源码目录 | 五个实际烟测流程动态并集38模块；新增自动规则15次ASGI/真实存储owner关闭后重开，侧坡12次及原生S57 14次另列 |
| 便携包全新首装 | 新venv联网可编辑安装解压源码、HTTP界面/分析/6份合成物理例、原生海图11次及侧坡12次HTTP通过；新增规则15次HTTP，真实终止并重启服务器后重读保存 |
| 首装隔离后端 | 1922通过、1条测试客户端依赖弃用提示、96.55秒；整个首装141.41秒 |
| 正式PDF | 手册22页、设计25页，全部47页真实逐页视觉核查；字形/打印源码单元/字段公式/字符边界及页码另检 |
| 真实首装运行输入 | 第二候选262份运行/示例/fixture/测试/界面/harness输入冻结，最终非运行文档和报告可刷新 |
| 截图证据 | 最终99轮的94张PNG真实来源字节/尺寸/时间核对，其中15张实际逐图查看；封面原字节来自同一轮 |
| 历史基线 | 116件0.1至0.9发行物、版本清单、报告及最终0.9界面保持原字节 |

wheel为906985字节，60份包文件/65项RECORD，SHA256 `93a6d2558d17eae8a2c2f090d76f7924e753caf58295d45017a38352f1c638b1`。wheel提取烟测使用现有解释器依赖，并非干净安装；启动器的全新venv与真实editable dist-info/direct_url及模块来源在添加测试依赖前独立核查。新增自动规则的wheel流程是实际新存储owner，首装流程是实际进程终止/重启；旧侧坡首装只是同owner API保存重读，不混写为重启。

完整后端/浏览器归档是同一次最终完整执行的同字节副本，专项与额外示例不加到1922/99。真实报告为`release_0.10_backend*`、`release_0.10_browser*`、`release_0.10_wheel_smoke.json`、`release_0.10_portable_smoke.json`、`release_0.10_served_assets.json`、`release_0.10_pdf_qa.json`与`release_0.10_screenshot*`。当前字节/执行证据/HTTP只读核验为`release_0.10_verified_runtime.json`，不重跑门禁。完整归档由`release_0.10_artifact_audit.json`独立枚举成员、检查源码/路径/CRC/RECORD及历史基线，再嵌入报告并只读复核；外部`manifest-0.10.json`记录最终ZIP摘要以避免自引用。

首次全新安装在新harness的模块来源断言失败：实际启动器一直安装解压目录的可编辑源码，断言误认为另一个wheel来源。保留原失败stderr、退出码1、第一候选ZIP/262输入快照，不能当首次全程通过。只修正新增harness和对应只读验证器，产品、启动器与已通过wheel不变；重建第二候选并实际重新全新安装后才得到本版成功结果。两候选由`release_0.10_initial_archive.json`及`release_0.10_verified_initial_archive.json`分别标识；原失败ZIP保留于开发资源，独立审核为`development_0.10_independent_installation_contract_review.json`。

PDF首轮∇缺字及块前标签分页问题的失败记录保留。修正限于PDF排版层，重新生成两份PDF后所有47页真实逐页查看；用户手册、设计源码与wheel未改。实际builder输入是workspace.png，pdf-cover.png为同字节副本；封面原图、实际输入与两副本均有SHA证据。开发失败、修正、准入范围与限定专项见 [DEVELOPMENT_0.10.md](DEVELOPMENT_0.10.md)、[自动规则合同](AUTOMATIC_RULES_NOTES.md)、[二维坡窗](TERRAIN_SLOPE_NEIGHBORHOOD_NOTES.md)、[独立几何复核](AUTOMATIC_RULES_INDEPENDENT_REVIEW.md) 及 [纯GIS准入复核](AUTOMATIC_RULES_ADMISSION_REVIEW.md)。

交付为`outputs/releases/OceanRoute-0.10-portable.zip`、`outputs/releases/oceanroute-0.10.0-py3-none-any.whl`及`output/pdf/OceanRoute_用户手册_0.10.pdf` / `OceanRoute_设计文档_0.10.pdf`。预览8777使用独立数据与最终发行构建。解压后执行`python3 launcher.py`（Windows为`py launcher.py`），默认8765；需要Python3.10+与首次联网，实测macOS/Python3.13.9/Chrome154.0.8037.93。Windows/Linux实机、Python3.10实机与免Python桌面安装器仍未验收。发行审计依赖开发环境的冻结历史/渲染原图和loopback预览，普通安装不依赖这些审计资源。

完整原厂复现仍未完成。原厂native文件、全部FME格式/DWG、完整施工/维修/地震能力、设备接入、初始EI/波/加载历史摩擦、有限杆6DOF、百万点分块及现场精度仍待实现或验证。下一阶段只读建议另见`development_0.10_next_phase_review.json`，它不是已实现或已验收记录。Token与订阅费用由平台账户结算，助手不接收转账。以下为冻结版本的历史验收，不与本版数量累计。


## 0.9阶段发行

2026-10-04。新增真实路线法向地形探点、左右/全幅侧坡及相邻段最大侧坡，按KP范围检查纵坡、侧坡或两者。候选预览经完整工作区事务应用，保留撤销、保存修订、共享来源、替代路径与制造库存。图表绑定产生分析的当前Project对象，参数、来源、路径或工作区变化后旧结果不能继续显示为有效。null规则终点动态跟随路线；缩短路线、缺测、来源边界和过期签名明确报告未知/不完整。V形海底按最大相邻有效坡度检查，不被左右平均抵消。

| 验证对象 | 实际结果 |
|---|---|
| 完整后端 | 1686通过、92.72秒；进程93.383806秒，141份输入不变，零失败/错误/跳过 |
| 最终生产浏览器 | 92项全部一次passed、298.691860秒；进程298.892115秒，同源8774，单worker、retry0、174份输入不变 |
| 实际界面资源 | 8份HTTP资源与8775发行/8774验收/默认/内置静态字节一致，在线手册一致 |
| wheel外目录 | 四个实际流程、35个不同模块路径均来自提取wheel；新增侧坡/规则12次API、4站20探点、真实调用方关闭owner后重开 |
| 便携包全新首装 | 新虚拟环境、联网安装、HTTP界面/分析、6份合成物理JSON、原生NOAA11次HTTP及新增侧坡/规则12次HTTP通过；原制造库存不变 |
| 首装隔离后端 | 1686通过、1条依赖弃用提示、85.27秒；整个首装124.34秒 |
| 正式PDF | 手册19页、设计20页；全部39页实际逐页视觉复核，字形/可打印源码/新增字段公式/页码边界另检 |
| 初装运行输入 | 246份实际运行/示例/fixture/测试/界面/harness输入冻结，非运行文档和报告可刷新 |
| 截图证据 | 最终92轮的84张PNG真实独占目录、时间/字节来源核对；其中13张实际逐图查看，封面原字节来自同一轮 |

wheel为858749字节、57份包文件/62项RECORD，SHA256 ec17d22d818afa61c83d70eed7cc76fd2de8203466204f239692a61f1b43f1fb。wheel提取验收与全新安装分别记录；新侧坡首装是运行owner上的API保存重读，不冒称重启owner。实测macOS/Python3.13.9/Chrome154.0.8037.93；Windows/Linux实机、Python3.10和免Python桌面安装器仍未验收。需要Python3.10+及首次联网。 发行只读核验依赖开发环境保留的历史冻结产物、PDF渲染原图及本机预览；最终用户安装使用launcher.py，不依赖这些审计输入。

证据为release_0.9_backend*、release_0.9_browser*、release_0.9_wheel_smoke.json、release_0.9_portable_smoke.json、release_0.9_pdf_qa.json、release_0.9_screenshot_provenance.json、release_0.9_screenshot_visual_review.json；初装对象和246份输入为release_0.9_initial_archive.json。运行字节与完整归档分别核验，报告内嵌后只读复核；最终外部manifest-0.9.json存ZIP摘要，避免自引用。82份0.1至0.8冻结产物/报告/界面保持原字节。

手册首轮P13三处下标缺字已在PDF排版层修复，原失败PDF、图片与结构记录保留；源码手册及运行wheel未改。设计20页实际查看，手册修复页再次查看，其余18页只在与原已逐页查看图字节相等后复用视觉证据。开发阶段真实失败和修正见[DEVELOPMENT_0.9.md](DEVELOPMENT_0.9.md)，功能合同见[SIDE_SLOPES_NOTES.md](SIDE_SLOPES_NOTES.md)、[SLOPE_RULES_NOTES.md](SLOPE_RULES_NOTES.md)。

交付为outputs/releases/OceanRoute-0.9-portable.zip、outputs/releases/oceanroute-0.9.0-py3-none-any.whl及output/pdf/OceanRoute_用户手册_0.9.pdf / OceanRoute_设计文档_0.9.pdf。预览8775使用独立数据；旧版版本化包和预览保留。解压后运行python3 launcher.py（Windows用py launcher.py），默认8765。

完整原厂复现仍未完成。法向样带为独立实现，不能推定原厂内部算法；离散采样通过不能认证连续海底。原生项目文件、全部FME格式/DWG、现场设备、完整施工/维修/地震功能、初始EI/波/历史摩擦、有限杆6DOF、百万点规模及现场精度仍待实现或验证。以下为各冻结版本历史记录，不与本版数量累计。


## 0.8阶段发行

2026-10-04。新增原生S-57参考海图的容器目录、原生对象类目录及选择导入，保留真实更新链、属性、测深第三维值、多边形孔洞和来源证据；空工作区可以导图并保留库存。GIS图层共享实际叠放、显隐和整层不透明度，两地图按同一语义显示并持久化。新建、打开与历史恢复期间暂停旧工作区编辑；既有修改/保存请求在途时暂不切换工程。SQLite连接生命周期修复原生并发死锁，事务与跨进程修订保护保持。

本版后端、生产浏览器、PDF、wheel外目录及全新环境首装均已实际验收。最终ZIP的成员、源码、路径/CRC、wheel RECORD和历史基线由独立归档报告记录；首次安装对象与最终归档对象分别核对，不能互相代替。完整原厂复现仍未完成。

| 验证对象 | 实际结果 |
|---|---|
| 完整后端 | 1566通过、92.01秒；进程92.560282秒，132份输入不变，零失败/错误/跳过 |
| 最终生产浏览器 | 87项全部一次passed、280.718603秒；进程281.001937秒，同源8772、Chrome154.0.8037.93、单worker、零失败/跳过/flaky/重试，156份输入不变 |
| 实际编译资源 | 8份HTTP文件与验收构建一致；最终8773、发行/默认及内置静态目录另作逐字节核对 |
| wheel外源码目录 | 旧27+当前3+原生S-57/SQLite helper2，共32个实际模块路径；三独立子流程1.961372/1.509587/3.446933秒，新S-57真实14次HTTP调用、657 XYZ点、225孔洞及完整来源保存/关闭重开 |
| 便携包全新首装 | 启动器创建全新虚拟环境、联网安装、HTTP界面/分析；6份合成物理JSON，其中4份完整运行与JSON分段续算六数组一致；真实原生海图11次HTTP、更新0→2及来源/库存守恒 |
| 首装隔离后端 | 1566通过、86.47秒；整个安装验收129.78秒 |
| 正式PDF | 手册17页、设计16页；全部33页实际逐页视觉核查，修正后重看变化页，其余最终PNG逐字节确认与实际已看图片一致，字符边界另检 |
| 首装运行输入 | 237份运行/fixture/示例/实际验证工具输入与初装ZIP相同；最终非运行文档、报告及截图可刷新，运行输入保持原字节 |

wheel为828387字节，SHA256 `8909d48515359f04d805fa3ef594725c94540294d1ce1a55d57edb95f602267b`。wheel smoke使用现有解释器依赖及明确复制的原始NOAA ZIP，不是干净安装；首装报告单独记录新环境pyogrio0.13.0、GDAL3.12.4、SQLite3.51.0及其source ID。实测macOS/Python3.13.9；Windows/Linux、Python3.10实机和免Python桌面安装器未验收。首装仅有测试客户端依赖弃用提示，不影响全部1566项通过。

正式执行证据为`release_0.8_backend*`、`release_0.8_browser*`、`release_0.8_wheel_smoke.json`、`release_0.8_portable_smoke.json`与`release_0.8_pdf_qa.json`。后台/浏览器归档是原开发执行记录的同字节副本，不另计一次；十项专项包含于完整87，不累计重复验证数量。实际首装对象为`release_0.8_initial_archive.json`，当前字节核对为`release_0.8_verified_runtime.json`，最终归档由`release_0.8_artifact_audit.json`及只读`--verify-only`复核；外部`manifest-0.8.json`记录最终ZIP摘要，避免自引用。

完整87项执行时，验收脚本误用未被截图助手读取的环境变量，76张实际通过截图写入默认旧目录。已按唯一实际执行时间窗口及逐文件SHA原字节归入`web/artifacts/release-0.8/all`，早期失败轮次和误路由文件另保留；冻结0.4包内46张旧PNG和1份回归JSON原字节恢复，旧发行包未改。PDF封面采用同一最终构建的十项专项生产界面截图，与完整87项的生产界面截图是两次实际捕获。完整原执行报告不改写、不另算一次测试；来源修正见`release_0.8_screenshot_provenance_correction.json`，6张代表截图的实际视觉检查及全部来源字节复核见`release_0.8_screenshot_visual_review.json`。

交付路径为`outputs/releases/OceanRoute-0.8-portable.zip`、`outputs/releases/oceanroute-0.8.0-py3-none-any.whl`和`output/pdf/OceanRoute_用户手册_0.8.pdf`/`OceanRoute_设计文档_0.8.pdf`。最终预览8773使用独立数据和发行构建；8772是正式浏览器验收对象，验收结束后停止，旧0.7预览8771保留。解压后运行`python3 launcher.py`（Windows用`py launcher.py`），需要Python3.10+和首次联网，默认8765。

S-57是原生参考几何/属性/来源，不是FME全部格式、DWG、S-52/ECDIS认证或自动工程水深转换。合法再版基础图加后续更新受当前GDAL驱动限制而明确拒绝；250000顶点是单次导入硬限，不是所有GIS累积保存上限。已知多边形组织性能提示保留，其他原生诊断整笔拒绝。原厂native文件、设备接口、完整施工/维修/地震功能、初始EI/波/加载历史摩擦、有限杆6DOF、百万点规模及现场精度仍待实现或验证。

完整浏览器首轮85/87通过及后端原生挂起记录保留，不作为最终通过。开发过程、实际失败原因、原始NOAA资料和功能边界见 [DEVELOPMENT_0.8.md](DEVELOPMENT_0.8.md)、[S-57合同](S57_NOTES.md)、[独立原生审查](S57_INDEPENDENT_REVIEW.md) 和 [GIS显示合同](GIS_DISPLAY_NOTES.md)。0.7及更早发行物仍按各自版本冻结；以下为历史验收记录。

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
