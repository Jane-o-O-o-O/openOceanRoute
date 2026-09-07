# 稳恒流初态界面与实际验收

2026-10-04。本文记录当前 0.7 候选源码与同源专项，不代表完整原厂复现、现场精度或发行安装验收。

## 1. 实际操作入口

在“敷设仿真 → 缆形与悬空段求解 → 动态敷设 → 动态初态来源”，新计算初态方法有三项：原水平悬链线 / 床面投影近似、明确定端自然长 · 实际平衡求解、明确定端稳恒流 · 实际平衡求解。

“加载明确合成分层流与异质初态”直接载入 [current-initial-dynamic.json](../examples/current-initial-dynamic.json) 的配置：三种实际材料、27 m 自然库存、制造原点 37 m、四个零长度点实体，其中一个 −25 N 浮力点；床格、种子与流场均为明确合成输入，不是测量或已验收形状。载入后没有计算图形或 proof，须点击“预备并独立验证动态初态”，再运行实际动态积分。

水密度、X / Y 海流在数值字段中，分层流仍在“分层海流与变化海床”的 JSON 表中。选择稳恒流模式并点击“重新声明当前初始流场”，会明确生成新的 raw v2 `initial_fluid` 输入，包含当前密度、恒流与深度表。它不执行求解、不自动改写历史、不声称规范校验已通过；实际 prepare 核查完整声明与 fresh 主配置一致。不点击此按钮而修改主环境，原声明会不匹配并被真实接口拒绝。

raw v1 的零恒流及每条零分层样本要求保持；不是依据下拉框或非零 current 自动升级版本。原 JSON 入口仍接收显式 raw v1 / v2，版本由输入声明及后台分派决定。raw v2 即使声明零流仍使用 model-v5 / checkpoint schema4。旧静力转动态按钮仍提供原零流 raw v1 种子，没有被改造成流平衡入口。

## 2. 实际证据展示

初态预备、实际动态结果与地理计划 / Voyage 共用 `EquilibriumDiagnostics`。它保留旧 proof v1 / v2 展示，新增 proof v3 的“稳恒流 / 材料证据 v3”标识与“冻结的历史初始流场”：

- 水密度、完整声明恒流及实际恒流 / 分层来源；存在分层表时覆盖恒流，并按 max(−模型 Z,0) 深度插值、端点外保持端值。
- 每个真实初态节点的 U、割线单位切向、缆 / 点阻力因子，以及两类实际三维阻力和完整外载荷。因子已含 ρ/2，单位 kg/m；外载荷是两类阻力加 [0,0,−实际有符号湿重]，不含内力和床法向支持。缆法向阻力的真实垂向分量保留。
- 原材料自然长、EA / 串联柔度、缆湿重、点负载完整份额，以及逐节点有符号湿重、非负床法向、固定端反力和净力残差。端反力不当作首末缆段张力。
- 完整原始 proof JSON；表格可横向滚动，不丢列或简化载荷。

无实际床接触时 TD 与底张力为 null；图例保留“无实际触地点”与灰色固定支持点，点实体显示位置但不宣称有限杆长或刚体形状。画布图例置于真实 canvas 之上，避免后附加的 canvas 盖住文字。二维床与瞬态节点均来自实际求解结果。

## 3. 保存恢复与后续控制

保存末状态或所选真实状态，下载完整 checkpoint，而非显示帧。上传后保持原数值、材料、床格、边界与历史初态，设置新增时长后继续；新 schema4 / model-v5 与旧状态均交后台按自己的版本验证，不迁移标签。

“用当前船速、放缆和环境参数覆盖后续作业”仅控制未来。分层流 JSON 留空保留已存表；填入新表才显式替换后续分层流，有表时恒定流字段不参与实际流速计算。真实 proof v3 历史 U / 密度 / 载荷保持原值，不用当前未来流重写或重新优化历史。初始化自然库存不会再次投入；改变未来流会产生真实瞬态，不钉住原自由节点。

地理窗口仍在“持续计算 / 网格审查 → 从活跃工程预备制造里程与作业指令窗口”。显式 `equilibrium_start.schema` 选择 raw v2；主环境由后台形成匹配的 `initial_fluid`，不在地理边界里另注入流。完整例子为 [current-initial-plan-voyage.json](../examples/current-initial-plan-voyage.json)。这是完整 `{project,config}` 预备请求，不是 checkpoint。预备 / 应用 / 后台提交仍保留制造来源、原点、指令窗与签名；子任务恢复保持真实父状态及历史证据。

## 4. 失效与范围

完整工程、参数、初态 JSON、材料、实体、床面、流表及保存选项都进入输入快照。输入改变隐藏旧证据 / 图形、禁旧结果下载；真实请求迟到成功或失败不写回新输入。初始声明不匹配、旧 raw v1 非零流、初态 EI / 有限实体 / 波流不支持、预算 / 容量或数值验收失败，均呈现真实错误，不能降级成无流形状。

这里只是固定端、零初始节点速度、稳恒水平或模型深度剪切流下的局部初态与后续真实动态。活动 EI 须为0，初始已部署实体须为零长度点；不求有限 rod 独立姿态、初始波浪、粘着加载历史或运动初始边界。材料物理、算子、力 / 整弦验收及容量限制见 [CURRENT_INITIALIZATION_NOTES.md](CURRENT_INITIALIZATION_NOTES.md)、[CURRENT_EQUILIBRIUM_CORE_NOTES.md](CURRENT_EQUILIBRIUM_CORE_NOTES.md)。

## 5. 本轮实测

`web/tests/current-initialization.spec.ts` 新增三项，真实同源编译服务及 API 均为 8770，external server、单 worker、retry0。最终三项全通过，原始执行耗时 **7.928053 s**，expected3 / skipped0 / unexpected0 / flaky0，逐结果 retry0：

1. raw v2 深度剪切流真实 prepare / dynamic，逐节点外力恒等式、缆垂向阻力、signed point、schema4 / v5，真实状态下载→浏览器 JSON 上传→未来分层流覆盖；历史 proof、库存及节点续接保持，真实自由节点发生瞬态。
2. 实际 `route.fetch` 已算响应受控延迟，期间改水密度，旧证据丢弃；密度 / current 不匹配失败，显式重新声明后恒流求解通过；工作量1与 raw v1 非零流真实422且无旧图或下载。
3. 实际工作区迁移 / 保存 / 打开，地理 raw v2 预备 / 应用 / 持久任务部分停止→子任务完成，proof3 / CP4、原点4 m / 初始top31 m / 库存27 m、−50 N点与流历史保持，实际 TD null。无TD图例文字与画布边界均检查。

原始报告和日志为 `resources/validation/development_0.7_current_specialist_browser.json` / `.log`。首轮 −0 / 0 JSON 等值断言和 implicit label locator 问题保留为 `pre_json_roundtrip_browser.*` 历史；一次已通过但图例遮挡的构建保留为 `pre_legend_specialist_browser.*`，不与最终数字相加。最终比较仅规范化 JSON 等值零，非零向量、物料和时钟仍按原实际值核对。

实际构建命令 `npm run build -- --outDir dist-0.7-current`，1882 modules，Vite 1.52 s；完整 stdout 为 `resources/validation/development_0.7_current_ui_build.log`。最终主资源 `index-BeQNh0-t.js` / `index-C_2x6pQW.css`。尚不能将该三项代替完整浏览器或全后端、PDF、wheel、首装与跨平台门禁。

已逐张实际看过专项目录 `web/artifacts/dev-0.7-current/special` 的流载荷、失败无旧证据、地理历史流 / −50 N点、地理任务 / 3D图例四张截图。图例修复后文字与床格不被盖住；宽表保留可滚动列，完整窗和点力证据可阅读。0.6及更早发行产物与旧截图未覆盖。
