# 异质定端初态与零长度点实体：界面合同

本文记录下一阶段开发源代码的界面适配，界面版本仍沿用0.6开发基线，不代表已生成0.7发行包、PDF、跨平台安装验收或原厂精度等效。0.6冻结产物不作覆盖。实际物理推导与恢复验收以 [异质材料核心说明](HETEROGENEOUS_MATERIAL_CORE_NOTES.md) 为准；本文不扩大其物理适用范围。

## 1. 输入入口与旧合同

在“敷设仿真 → 动态敷设 → 动态初态来源”选择“明确定端自然长 · 实际平衡求解”。仍使用原始边界 schema `oceanroute.dynamic.initial-equilibrium.v1`，声明船端、固定锚端、总自然长或逐段自然长，以及可选数值初值与求解预算。材料区段和在线实体分别填写在“混合缆型与缆体动力学”的主配置中，不能塞进原始初态边界对象。

- `material_segments`：保持真实制造坐标、正缆湿重、正EA、质量与直径等材料声明。初始活动区间允许w/EA分区变化。界面不把材料变化强制对齐离散节点、不取任意单一缆型替换整条初态。
- `inline_bodies`：已投入实体仅支持 `length_m=0` 的点负载；其 `wet_weight_n` 可为负，代表向上的浮力。质量、湿重和实际节点分配由后端材料算子处理。未来未投入的有限长度实体保持原动态声明能力，不能据此声称已实现初始有限杆、刚体姿态或形状碰撞。
- 初始活动缆仍须 `EI=0`、恒定海流及每一条分层流样本均为0、不支持波流。边界、床格和求解额度仍执行实际后端验收。初始速度为命令执行前的零速度，不是移动施工稳态或历史状态重建。

旧同w/EA且无已投入实体的结果和恢复档案保持 provenance v1。新异质材料或已投入点实体使用 provenance v2。二者继续使用 `material-lumped-mass-xpbd-cable-lay-v4` 和 `oceanroute.dynamic.checkpoint` schema3；界面不通过改变版本号转换、重造或降级恢复证据。

原“坡床 / 变深静力”的“用此定端输入预备同质无流动态”按钮仍只创建明确同质草稿，原节点仍只是优化初值。这个按钮未被改写成异质静力求解入口。

## 2. 真实proof展示

`EquilibriumDiagnostics` 同时接收 prepare 的 `provenance` 和动态结果的 `initialization`，按实际schema区分v1/v2。持续任务展示真实保存的 `checkpoint.physical_checkpoint.state.initialization_provenance`；它是原始初态证据，不是当前输出帧的力。

v2的 `material_loading` 保持完整原始JSON，并拆出三个可审查表格：

| 表格 | 实际字段及单位 |
|---|---|
| 初态逐段材料 | `initial_snapshot.rest_lengths_m`、制造坐标起止、实际等效 `segment_ea_n`、初始段张力；`segment_cable_wet_weight_n` 单位N，`segment_compliance_m_n` 单位m/N |
| 零长度点实体 | ID、制造坐标、声明长度、干质量、**有符号**湿重、投入比例及完整 `node_fractions`；系数顺序为船→锚的材料节点 |
| 初态逐节点力 | 实际坐标、制造坐标、干质量和等效惯性质量；缆、点实体及总有符号湿重；法向力、固定端反力、净力残差 |

摘要显示真实 `signed_total_wet_weight_n` 与 `absolute_load_scale_n`，不取绝对值代替湿重、不把负点负载截成0；很小的非零残差和柔度使用科学计数显示，避免误看成严格0。法向接触力仍为单侧非负量，不能与有符号外载混淆。端反力包含端节点载荷，不能当作首末缆段张力。

未接触床面的固定锚端保持独立灰色支持点，`touchdown` 和 `bottom_tension_n` 的null不补成固定锚坐标或0。3D实体标记只表示求解返回的位置，图例明确其不是刚体形状；不会画出未经求解的有限杆长度。

## 3. 合成输入与地理计划

“加载明确合成异质缆与零长度点实体初态”按钮只载入公开声明的合成输入，不绘预计算形状或声称验收完成。该小例为8节点、非均匀自然段共27m、制造原点4m、船端制造坐标31m、两缆型及−50N零长度点负载；独立离床力平衡位置只作数值初值。点击预备或运行后，才展示真实后端结果。

两个完整API请求示例另见 [异质动态初态](../examples/heterogeneous-initial-dynamic.json) 和 [异质地理制造窗口](../examples/heterogeneous-initial-plan-voyage.json)。前者为13节点与不同材料原点的离床合成例；它与界面8节点小例不是同一输入。两者均非测深、施工现场记录或恢复文件，不能直接冒充checkpoint。

“持续计算 / 网格审查 → 从活跃工程预备制造里程与作业指令窗口”显式区分两条分支：原平床解析分支采用同质解析缆形，允许加载的初始点实体未纳入该解析平衡；取得异质 / 点负载的真实平衡须使用地理床格定端分支。旧分支的point加载合同保持，界面不加新的硬拒绝。地理床格定端分支从工程真实制造区段映射异质w/EA及零长点实体；其材料来自工程共享库、制造区段与物性声明，不能在局部参数文本里偷偷覆盖整个材料库。有限长制造实体仍由地理桥接明确拒绝。

地理预备页展示真实初态proof、制造原点/初始活动库存、原床原点及平移诊断；应用时完整保留 `plan_mapping`。持续任务和子任务沿实际checkpoint恢复，初态活动库存不重新放出，零长实体不重复加重。当前原Ship Plan偏移仍为局部平床first-cut，真实变深定端初态不构成船位计划精度认证。

## 4. 异步与失败边界

预备和运行继续绑定工程及完整输入签名。任意材料、实体、床格、边界、控制或预算改变后，旧proof和旧缆形隐藏，结果下载禁用；迟到响应不得恢复旧证据。非法输入、初始有限长实体、活动EI、非零初始流或预算不足由真实API拒绝，不展示上次结果冒充成功。

完整checkpoint继续按原JSON下载与上传，v2的材料算子摘要、节点分配与有符号载荷不被界面删减。恢复路径不把proof JSON当成可编辑静力结果；保存证据内部仍由后端重新核查。

## 5. 开发验收记录

新增 [heterogeneous-initial.spec.ts](../web/tests/heterogeneous-initial.spec.ts) 三个真实API浏览器场景：

1. 合成异质初态的串联柔度、负点湿重、真实节点分配、无TD语义与完整checkpoint浏览器往返恢复。
2. 真实延迟预备响应丢弃，以及已投入有限长度、活动EI、非零流和初始化预算失败后不保留旧proof或形状。
3. 从持久工作区加载工程，真实地理混材窗口、原点平移、自然库存与点负载映射，部分停止及后台子任务继续。

2026-10-04，新增三个专项已在真实编译界面和同源8769实际通过，耗时7.649244秒；1个worker、每个结果均passed且retry=0，expected=3、skipped=0、unexpected=0、flaky=0。原始 [浏览器JSON](../resources/validation/development_0.7_final_specialist_browser.json) 与 [控制台记录](../resources/validation/development_0.7_final_specialist_browser.log) 分别保留。专项不能替代完整浏览器门禁，也不与旧74项或单项重跑结果相加。

首轮发现负湿重点实体被工作区制造关系校验拒绝；后端修复后，完整地理工程实际迁移、保存并在浏览器打开，再完成真实窗口预备和子任务续算。首轮失败记录保留为 `development_0.7_heterogeneous_pre_save_guard_browser.*`。上传恢复场景也修正了测试同步：等待异步文件读取完成再设置续算时长；业务入口没有为测试改变恢复语义。

实际执行使用 `OCEANROUTE_E2E_EXTERNAL_SERVER=1`、`OCEANROUTE_E2E_BASE_URL=http://127.0.0.1:8769`、同源 `/api`。已逐张实际查看 [真实3D与有符号材料proof](../web/artifacts/dev-0.7/heterogeneous-signed-point-load-proof.png) 和 [地理窗口子任务实际缆形](../web/artifacts/dev-0.7/heterogeneous-geographic-inventory-job.png)：无TD图例、独立固定锚、点负载标记、负湿重和表格数值与真实响应一致，未见字体或图例截断。地理截图是滚动到实际缆形的视口，不表示上方保存的初态proof或任务状态缺失。

开发构建目录为 `web/dist-0.7-next/`，主asset为 `index-DsBxxask.js`、CSS为 `index-0ARYPn3s.css`。主用户手册、PDF、版本与旧发行文件不在本次改动范围；尚无本扩展发行安装或跨平台验收。
