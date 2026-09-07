# 真实二维静力初态与动态材料状态映射

本文说明 0.6 引入的真实定端初态及后续开发扩展的共同合同。0.6 与更早发行文件保持冻结；分段材料和已部署零长度点载荷的新增证据见 [0.7 开发记录](DEVELOPMENT_0.7.md)，尚未生成新发行包。不声称 Makai 原厂等效、实船认证或完整施工自动求解。显式二维床面可以是完整双线性曲床，不能按平面拟合后冒充其平衡。

## 入口、坐标和原始边界

`simulate_lay(project, config)` 在原动态配置中新增 `initial_equilibrium`。必须同时给真实 `seabed_grid`，不能同时给 `seabed_profile`。所有位置均在**同一局部东/北米坐标**，z 向上并已经对齐模型海面 0；datum 标签不实施潮位或垂直基准转换。船端可以在模型海面或水下；固定最老端可以在床上或悬空，但不得穿床。固定外部支持点不是自动推断的触地点。

```json
{
  "nodes": 18,
  "wet_weight_n_m": 4,
  "ea_n": 10000,
  "ship_speed_m_s": 0,
  "payout_m_s": 0,
  "seabed_friction": 0.5,
  "initial_suspended_material_m": 125,
  "duration_s": 0.2,
  "dt_s": 0.1,
  "internal_dt_s": 0.01,
  "solver_iterations": 24,
  "seabed_grid": {
    "schema": "oceanroute.bathymetry.v1",
    "x_m": [-100,0,100],
    "y_m": [-100,0,100],
    "z_m": [[-52,-45,-38],[-50,-40,-30],[-48,-35,-22]],
    "source": {
      "name": "explicit synthetic z=-40+0.1x+0.05y+0.0003xy",
      "horizontal_crs": "LOCAL_CARTESIAN_METRES",
      "origin_projected_m": [0,0],
      "vertical_datum": "already aligned to model sea surface zero"
    }
  },
  "initial_equilibrium": {
    "schema": "oceanroute.dynamic.initial-equilibrium.v1",
    "vessel_position_m": [0,0,0],
    "anchor_position_m": [-60,-10,-46.32],
    "natural_length_m": 80
  }
}
```

`initial_equilibrium` **只**接受 `schema/vessel_position_m/anchor_position_m/natural_length_m/rest_lengths_m/initial_positions_m/solver`。自然长度与段长二选一；总自然长不超过 1e6 m，每段不少于 1e-4 m；节点由主配置 `nodes` 决定，6..80。显式段长数组顺序是船→固定端，必须正好 N-1 项。`initial_positions_m` 若提供，仅是 N×3 的可行优化初值，须与两个端点相符；不代表已通过平衡。不接受带 `accepted`、显示帧或完整静力结果的替代对象。

`solver` 的键和范围：

| 字段 | 默认 | 范围/用途 |
|---|---:|---|
| force_tolerance_n | 0.01 | 1e-6..0.01 N |
| relative_force_tolerance | 1e-7 | 1e-9..1e-7；旧同质路径按 max(1,wL)，扩展路径按缆总湿重加点载荷湿重绝对值之和缩放，与绝对值取较大者 |
| contact_tolerance_m | 1e-8 | 1e-9..1e-8 m，不采用旧静力默认 1e-6 |
| max_solver_iterations | 300 | 1..600 |
| max_function_evaluations | 2000 | 1..10000 |
| max_segment_samples | 20000 | 10..200000 |
| max_work_units | 200000000 | 1..2000000000；包含静力和独立映射预估 |

## 真实力平衡、接触和材料

先以真实动态 resolved 配置构造材料积分。**初始活动自然材料区间**允许不同湿重 w 与 EA，但必须逐区间零 EI；已部署实体只允许零长度点载荷，其湿重可以有符号。各段 EA 使用串联柔度，缆总段量分到两端节点，点实体按制造站真实插值分配；质量与湿重不混用。所有初始海流及 `current_profile` 样本必须为零，初始波运动仍不支持。不同干质量、直径或拖曳系数保留其真实动态差别。尚未放出的 EI、有限长度实体可保留供现有研究动态模型，不能据此声称已解有限杆的初态或刚体姿态。

旧公开 `static_equilibrium` 保持标量物性合同；扩展初态调用同一内部定端能量核心，直接传入已解析的真实逐段 EA、节点有符号湿重与载荷尺度。它做固定端、自然段长约束的 SLSQP 求解，接着从**实际节点和动态材料加载**独立重构 Hooke 张力、端点载荷、节点内力、床面法向、单侧支持、全局/节点残力与互补条件。沿每条完整直段逐双线性单元验精确二次 gap 极值；未知单元、床内直段、穿透、坍塌、未收敛和超预算均明确拒绝，不投影后继续使用旧张力。优化器的 `success` 或输入的 `accepted` 都不能替代这些验收。此版本只寻找局部静止平衡，不证明唯一性、全局能量最低或历史加载可达性。

静力取 `Ft=0` 的正常支持；它对任意非负动态摩擦系数都满足库仑容量。主动态 `seabed_friction` 可为 0..2，没有强制改成零，亦没有推断 `prescribed_stick` 历史。时间 0 的动态接触冲量真实为零；初始静力 N 是独立 provenance 中的**力**，不会乘虚构步长伪造已积分冲量。

初始自然段长逐值原样保留，不能再用旧 `chords/(1+continuous_average_tension/EA)` 反算。材料 origin 仍为 `initial_suspended_material_m`，节点制造坐标从 `origin+L` 递减至 origin。80 m 案例 origin125 对应船端材料205；初始 `paid_out_m=0`，因为该 L 是初始活动库存，后续放出只另加一次。质量、湿重和 EA 用相同自然材料积分重新核对。

`segment_target_m=max(initial_rest)` 避免非均匀顶部自然段在 feed=0 时被旧细分规则立即拆分。船/固定端位置不改变；固定端不被吸附到床上，船的原 z 用作升沉基准，避免首步跳到 z=0。记录的 `depth_m/reference_depth_m` 是固定端水平位置下的实际床深，而非把悬空固定端称为触地点。

## 预应力与真实动力固定点

独立反例发现：旧冷起 XPBD 即使轴向 scalar compliance 残差约 1e-17 m，也会让精确离散平衡漂移；增加迭代数没有消除该误差。新路径采用独立 `model=material-lumped-mass-xpbd-cable-lay-v4`，数值方案 `implicit-compliant-material-nodes-equilibrium-prestress-v4`，旧 v2/v3 完全保留。

每个真实内部步在放缆/材料更新后，从当前旧位置重算正 Hooke 张力 T 和内力，令 `lambda_axial=-h²T`，**同时实际施加**相应 `h² M^-1 F_axial` 位置修正。在旧位置真实接触的自由节点，从当前内力和湿重算非负正常支持 N，令 `lambda_normal=h²N` 并实际施加 `M^-1 lambda_normal n`。随后继续原有非线性轴向、弯曲和单侧接触迭代、速度更新与摩擦冲量。仅给 lambda 非零而不施加修正是不正确的。

当初始内力/湿重/正常支持已严格平衡，重力预测与这两个实际力修正相消；有限静力残力则仍产生真实加速度。没有锁定自由节点，没有跳过静态时段的动力求解。船速、放缆、规定升沉在首个真实内部步施加；时刻 0 是 **actuation 前零速度 snapshot**，不能称为有初始航速、海流或升沉速度的移动准稳态。后续运动、材料切换和实体部署均会产生动态响应。

这仍是顺序线性化研究积分器，不是完整非线性隐式 Newton 求解。初态固定点验收不能证明大运动、接触冲击、粗网格、高 EA 或所有时间步的载荷精度。原 `HARD_CONTACT_LOAD_RESOLUTION` 与载荷/位置收敛区别继续适用。

## 公开 helper、响应与断点

准备层可调用：

```python
from oceanroute.initial_equilibrium import resolve_initial_equilibrium
state = resolve_initial_equilibrium(project, dynamic_config)
```

返回有限 JSON：`positions/rest_lengths_m/segment_tension_n/initial_material_length_m/segment_target_m/reference_depth_m/provenance/solver/estimated_work_units`。它真实重求解但不运行动态，不构造假 checkpoint。准备阶段成功后，实际启动仍从相同 raw request 再求解验收；不能把 helper 响应当免检动态状态。`estimate_initial_equilibrium_work(project, config)` 只返回合法物料/边界蓝图和声明 work 上界，**不**声称已得到平衡。

动态顶层 `initialization` 和 `checkpoint.state.initialization_provenance` 保存同一完整 proof：原始 request/grid 摘要、两个真正边界、初始位置/零速度/自然长/制造坐标/质量/湿重/EA/张力/正常支持/端反力/残力、实际优化计数与独立几何验收。`slope_equilibrium=true` 仅指这份原始初态；`initial_time_s=0` 明确其时基，不把后续动态帧称为静力。

新路径帧有 `segment_tension_n/anchor_position_m/anchor_segment_tension_n/touchdown_detected`。无实际海床接触时 `touchdown/touchdown_node_index/bottom_tension_n` 为 null，不能用悬空固定端或零张力冒充 TD。若有接触，底张力仍指首接触前的段；固定端末段张力另列。静力端反力含端节点半段湿重，不能当作首/末段张力。旧近似路径的兼容响应未改。

断点 `schema_version=3/model-v4` 保存当前完整真实动力状态，加这份原始 proof。旧同质且无已部署实体的 proof v1 保持原义，异质或点载荷使用 proof v2，另存共享加载算子、完整声明摘要、逐段柔度与缆/点湿重分项、逐实体节点份额。恢复冻结 raw `initial_equilibrium`、床网格、物料与数值设置；单份 2 MB、批量 16 MB 容量预估包含完整 proof，不借旧节点大小遗漏 N×body 份额。完整初始 snapshot 重新构造材料/力/整段覆盖证明，公开残力和几何诊断也必须对应复算值。所有时刻的公共 reader 还会从当前自然段与冻结声明复核实际材料加载。时间 0 状态必须与真实初态一致，初始库存/target/固定端不能变。恢复**不重新运行静力优化器**，不根据最后显示帧猜速度。未来运动或海流控制的合法改变不会改写历史初态。旧 schema1/v2 和 schema2/v3 继续原方案，不能删 proof 后降级。checksum 仍仅检测意外修改，不是抵御恶意伪造的签名；有意重算 checksum 后的错误物料/受力 proof 仍会被物理复核拒绝。

## 分层预算和长时连续求解

主动态原 12M 额度保持。初态另有显式静力上限，预估包括声明的 dense iteration×free variables³、评估×变量²，以及材料和独立完整直段验证上界；先完成动态容量/输出 preflight，再运行昂贵静力优化。单份断点 2 MB、保存批次 16 MB、完整动态响应 64 MB 均计入实际 UTF-8/JSON 转义的重复 proof、逐节点和实体元数据，返回前再检查实际有限 JSON 字节；不靠缩短允许 ID 来掩盖重复量。初始化失败不会返回伪造动力 checkpoint。

`solver.estimated_work_units` 为本次动态/恢复证明的预估；`solver.initialization_work` 分列本次初始化收费、原始 work、静力/映射 components、原始实际优化 iteration/evaluation，以及恢复 proof 校核额度。`solver.charged_normalized_work_units` 是动态与本次新初始化之和，不能称为 FLOPs、CPU 时间或实测性能。

voyage 首块预留并加上初始化收费，后续块仅恢复 proof/当前状态，不每块重求静力。总额度不足明确拒绝或停止，不暗增预算；累计 charged work 保留于真实 voyage checkpoint。调用层的 JSON 编解码、框架自身重复校核、CRS 校验和渲染不在 solver normalized cost 中，输入/输出仍有各自容量守卫。二维床面仍禁止原平床粗化；达到节点/覆盖/时间/工作额度就停止，不能据连续多个短块宣称已解决完整变深海区施工。

## 实际验收及剩余范围

0.6 原阶段的自有 24 项和独立 19 项验收覆盖曲床节点/段力平衡、库存/制造坐标/质量、不均匀自然段、悬空固定端、水下船基准、无接触 null TD、预应力固定点、真正运动/后续材料与实体放出、当时的初态适用限制、NoData、不收敛、预算先行、JSON中途恢复不重解、错误 proof 与一次性长时初始化收费。它们不能代替新增初始异质材料与点实体的验收；新增实际证据另见 0.7 开发记录。

曲床例 `z=-40+.1x+.05y+.0003xy`, L80/N18/EA1e4/w4 真实残力约 .002325866 N，最小完整段 gap 为 0。另独立非均匀闭式 N8/L27/H150/Vbottom15/EA1e4/w4 精确离散平衡，在 0.1 s、h=.008/.004/.002 上新 v4 最大位移约 4.80e-14/1.21e-13/1.78e-15 m；这是明确合成固定点例，不是一般动态误差上界。

完整目标还需实现：已部署有限长度实体的静力与姿态；EI 弯曲能及其边界力矩/接触；海流拖曳下的静止或移动准稳态；可追踪加载历史的静摩擦；缆径、段间及实体连续接触；变深波流/完整实船边界；复杂床面长航程与独立误差控制。当前点载荷仍在单条直弦内插值，未在段内增加独立折角；载荷 lumping 也不是完整实体惯性。离散固定点或连续端位移收敛的合成验证不能覆盖这些缺口。

## 主来源与本轮独立推导

[XPBD 原论文（作者维护副本）](https://matthias-research.github.io/pages/publications/XPBD.pdf)给出质量加权位置修正与累计乘子的动力学来源。本轮预应力 predictor 与正常支持固定点处理是针对本缆模型的独立实现和验收，不声称该论文的冷起算法自动提供坡床平衡保持。[SciPy SLSQP 官方文档](https://docs.scipy.org/doc/scipy/reference/optimize.minimize-slsqp.html)解释优化终止信息；本模块额外重构真实力与床内完整段几何，优化器 success 不是物理证明。静力能量、双线性接触和制造材料合同详见 [STATIC_BATHYMETRY_NOTES.md](STATIC_BATHYMETRY_NOTES.md)、[BATHYMETRY_NOTES.md](BATHYMETRY_NOTES.md)、[MODEL_NOTES.md](MODEL_NOTES.md)。
