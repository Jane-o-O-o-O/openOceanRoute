# 稳恒海流初态的原始声明、历史证据与恢复

本文描述未发行的下一阶段开发分支。旧0.6冻结包、PDF和版本记录不变；本分支不是原厂格式、实船标定、完整维修 / 船舶模型或现场精度等效证明。水动力与非保守平衡核心由独立模块实现，本文限于 `initial_equilibrium.py` 和 `checkpoints.py` 的准入、独立复核及恢复合同。

## 1. 明确选择新分支

现有 `/api/simulation/prepare-equilibrium-initial` 和 `/api/simulation/dynamic` 的请求外壳仍为 `{project,config}`。启用流平衡必须显式使用 `config.initial_equilibrium.schema = oceanroute.dynamic.initial-equilibrium.v2`。不能通过将当前海流设为非零，把原v1请求或旧checkpoint自动升级。

raw v2保留真实船端、固定最老端、总自然长或逐段自然长二选一、可选数值节点初值和有界solver参数；新增必填 `initial_fluid`。材料区段、点实体、密度和拖曳物性仍来自主config，不允许在原始边界对象注入验收结果、逐节点载荷或EA。

```json
{
  "schema": "oceanroute.initial-fluid.v1",
  "operator": "node-secant-normal-cable-and-isotropic-body-drag-v1",
  "water_density_kg_m3": 1025,
  "current_m_s": [0.1, 0.12, 0],
  "current_profile": null,
  "depth_reference": "max(-model_z_m,0)",
  "profile_extrapolation": "hold_endpoints"
}
```

这是规范化初始流声明，放入raw v2的 `initial_fluid`，不是完整动态请求或恢复文件。字段必须完整且无未识别字段。水平流分别沿模型X/Y，单位m/s；垂向分量须0。密度沿用真实环境的1—2000kg/m³范围；水平分量分别在−20—20m/s。可选深度表为2—500行 `{depth_m,x_m_s,y_m_s}`，深度0—12000m且严格递增，按 `max(-z,0)` 线性插值，表域外保持端值。

有深度表时，其流速覆盖恒流，不与恒流叠加。规范声明仍记录完整恒流值：fresh时整个规范流对象必须与主config实际流 / 密度一致，不能悄悄选择另一组数据。`canonical_initial_fluid(config)` 用于形成声明，`validate_initial_fluid(raw)` 仅校验完整声明，不补缺失历史字段。

旧raw v1保留零初始恒流、每一条深度流样本均为0的原义。既有v1/v2历史proof和schema1/2/3保存状态继续其原模型与求解分支；旧model-v4保存状态后续出现非零流，不代表其历史初态已在流下平衡。

## 2. 物理范围与实际加载

初始活动区间可有异质正缆湿重、正EA、不同惯性质量 / 直径和已投入零长度点实体。点湿重可负。活动缆仍须EI=0，任何已有投入份额的有限长实体仍拒绝；波浪流体运动学、流致刚体姿态、部分浸没、流场反演和加载摩擦历史不在本分支范围内。

完整已知二维双线性床格、两个实际固定端、逐段自然量、原制造原点和paid=0保持。初始所有节点速度为0；实际作业命令从随后真实内部步开始。候选需实际非保守节点力求解和独立验收，不能把旧无流形状、几何投影或settling后的缆形称作初态已接受。

缆拖曳使用节点索引的相邻割线切向，投影到缆法向；点实体的拖曳系数按真实材料份额分配到节点，再使用各节点的流速。不是先在显示的实体位置求一股完整实体力，也不是连续rod / 独立接头动力学。真实 `local.drag` 和 `local.body_drag` 已含ρ/2；不得再次乘密度。

独立初态复核实际计算：

```text
F_external = cable_drag + body_drag + [0,0,-node_wet_weight]
F_free = F_axial + F_external + N*bed_normal
R_fixed = -(F_axial + F_external) at each fixed endpoint
```

自由节点的力、实际法向接触、单侧非负量、互补条件、全局力和整条直弦逐单元床面极值均需通过。端反力含端半缆湿重、点载及实际端拖曳，与首末段张力不同。未实际接触床面的固定最老端不伪装成触点；无TD时TD和底张力仍为null，不补零。

## 3. provenance v3

新分支使用 `oceanroute.dynamic.initial-equilibrium.provenance.v3`，来源为 `oceanroute.current_equilibrium.solve_current_equilibrium`。原完整 `initial_snapshot` 字段集合不变：位置、零速度、自然段、制造站、干 / 等效质量、总有符号湿重、逐段EA / 张力、实际法向力、固定端反力及净力残差。

`material_loading` 保留上一轮v2实际材料积分和完整点份额证据，即使材料同质也保留。新增 `fluid_loading`：

| 字段 | 含义与单位 |
|---|---|
| `operator` | 真实节点割线 / 缆法向 / isotropic body加载算子标识 |
| `initial_fluid` / `initial_fluid_sha256` | 完整冻结历史流与规范摘要 |
| `node_fluid_velocity_m_s` | 每个初态节点实际U，m/s |
| `node_tangent` | 船→最老端顺序的单位割线切向 |
| `node_cable_drag_factor` / `node_body_drag_factor` | 实际自然积分 / 点份额系数，含ρ/2，kg/m |
| `node_cable_drag_n` / `node_body_drag_n` | 初始零节点速度下的两类实际三维拖曳，N |
| `node_external_force_n` | 两类拖曳加真实有符号湿重的完整外力，N |

缆法向拖曳可有垂向分量，即使输入流仅水平；证据不删除该分量。`initial_conditions` 单独绑定历史流摘要、零初始节点速度、零静态切向支持和命令启动政策，不把非零初始流错误描述为零。

物理验收与求解器停止分别检查。proof中的力 / 床 / 弦 / 互补量由独立映射复算，不能只信任optimizer的accepted。实际局部静止候选不证明唯一、全局稳定、连续实体精度或施工可达性。

## 4. checkpoint schema4和历史恢复

新组合严格绑定：

```text
raw initial-equilibrium.v2
  -> provenance.v3
  -> model material-lumped-mass-xpbd-cable-lay-v5
  -> checkpoint schema_version 4
  -> scheme implicit-compliant-material-nodes-current-equilibrium-prestress-v5
```

任一混用或降级拒绝。没有增加新的可演化状态；完整材料数组、节点位置 / 速度、自然段、实际法向 / 摩擦脉冲和数值时钟仍保存。完整JSON及checksum用于一致性，不是可信签名或工程认证。

读取历史初态只用raw v2冻结的 `initial_fluid` 和原snapshot，不以当前mainconfig的后续流重算过去，也不重新优化。实际材料、系数、节点U / 切向 / 拖曳 / 外力、床法向、支持及残力全部重新核查；重签摘要的假向量、假份额或假物性不能仅凭checksum通过。

合法后续current或current_profile override只改变未来控制，产生实际瞬态；不能修改raw历史流、密度、床面、自然库存或初态边界。最初活动库存不会再次投入。rho / 材料 / 拖曳声明及raw初态整体仍冻结。每个当前时刻的十项材料载荷继续独立复算；制造坐标q使用绝对容差，不能因大制造原点放宽误差。

旧schema1/2/3与真实旧wheel产生的v1/v2proof保持原分派，不重新优化、不换scheme或标签。未来非零流不触发迁移。

## 5. 工作量与容量

新非保守核心的预检直接消费 `estimate_current_equilibrium_work(static)` 的实际4×自由节点变量模型、密集求解、残差 / 有限差分调用及最终弦验收上界。初始化还单列真实材料解析和独立流 / 力 / 床映射预算。旧零流能量优化器的预估保持原义；不拿旧3NF估计替代新4NF计算。

初始化默认200M、硬上限2B的声明额度保持；预算不足明确拒绝，不自动提高额度、减少材料或退回无流候选。独立证明读取按有界材料 / 深度表 / 节点水动力 / 弦检查收费，不重算静力。主动态、Voyage分块及总任务预算分别由对应模块控制，不能将归一化work当FLOPs、token价格、CPU时间或固定性能承诺。

保守proof容量计入实际UTF-8 / JSON转义的点元数据、完整流表与逐节点水动力证据，以及在checkpoint / 状态批次 / 响应中的重复。独立checkpoint2MB、保存批次16MB、完整动态响应64MB的求解前限制及最终有限JSON检查保持；超限不能靠丢历史流或负点字段回避。直接本地prepare另在优化前预留原config、完整proof和实际状态的2MB容量，不绕过dynamic入口的容量保护。

## 6. 验收记录

新增 `tests/test_current_initialization.py` 覆盖完整canonical流准入、fresh配置一致性、独立自然材料 / 拖曳 / 外力复算、历史流与未来override、完整JSON恢复、重签后的虚假proof和当前材料、预算 / 真元数据容量、以及真实冻结0.6wheel兼容。旧测试文件没有替换。

2026-10-04，新增31项初始化用例已实际全部通过。恒流与深度剪切例保留1e−6N绝对力容限，没有以放宽容限替代求解收敛。随后将新31项与既有checkpoint、定端初始化、异质材料、独立旧初态及真实旧wheel兼容用例串行合跑，176项全部exit0；collect-only再核176。这个联合数已包含新31项，不相加，也不等于完整后端门禁。

实际联合命令为：

```sh
.venv/bin/python -m pytest tests/test_current_initialization.py tests/test_checkpoints.py tests/test_equilibrium_initialization.py tests/test_equilibrium_initial_review.py tests/test_heterogeneous_material_core.py tests/test_heterogeneous_initial_review.py tests/test_legacy_initial_proof_compatibility.py -q
```

后续完整后端、同源浏览器与发布验收须各自记录；不能沿用上一轮1284 / 77门禁为新流初态背书。尚无本扩展发行安装、PDF、跨平台或现场标定验收。
