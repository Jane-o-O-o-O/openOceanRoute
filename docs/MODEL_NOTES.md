# OceanRoute 海缆力学模型说明

版本：独立研究模型，动态模型 v2，2026-10-04。实现文件：`oceanroute/simulation.py`。

本模块包含解析悬链线、三维稳态形状近似、带放缆边界的材料节点动态求解和海床静态跨距分析。全部结果返回 `validation_status: "research"`。通过解析解和数值守恒测试只能证明部分数学与程序性质；目前没有海试、实验室标定，也没有证明与 MakaiPlan Pro 的结果精度相同。

## 1. 单位、坐标和结果解释

内部采用 SI 单位：m、s、N、kg。坐标为局部东向 x、北向 y、向上 z；海面 z=0，水深正数 d 对应海床 z=-d。`heading_deg` 使用航海方位：0° 北、90° 东。节点按船端到海床端排列。动态结果的节点数会随放缆增加，调用者不可假定所有帧数组长度相等。

各模型返回 `model`、`validation_status`、`coordinate_convention`、`assumptions`、`warnings`、`summary` 和 `solver`。动态帧返回 `time_s`、`ship`、`nodes`、`top_tension_n`、`bottom_tension_n`、`touchdown`；还返回材料长度、放出长度、几何长度、各节点张力、接触节点数和最大轴向应变。

动态 `top_tension_n` 是第一材料单元的轴向力，`bottom_tension_n` 是首次接触海床前的单元轴向力，不包括固定边界节点半单元的全部惯性/水动力反力。它们属于单元中心估计，网格加密后趋向端部值。触地点是当前材料网格中第一个接触海床的节点，空间分辨率由单元尺寸决定；它不是连续曲线的高精度触地点。`max_top_tension_n` 只统计输出帧，`max_tension_n` 统计所有内部时间步。

`solver.converged` 只表示该求解器定义的离散残差满足阈值，不能证明网格独立性、物理正确性或工程可用性。

## 2. 解析悬链线 `catenary(config)`

输入均匀水中重力 w>0、水深 d 和水平底部张力 H≥0，令 a=H/w。水平触底的不可伸长悬链线满足：

```
suspended length S = sqrt(d * (d + 2a))
layback X = 2a * asinh(sqrt(d / (2a)))
top tension Ttop = H + w*d
top vertical reaction Vtop = w*S
minimum curvature radius = a
```

X 的实现采用等价的双曲正弦形式，避免极小 d/a 时 `acosh(1+d/a)` 丢失精度。H=0 返回垂直极限并提示触底曲率奇异。

可选边界条件 `cable_length_m` 或 `layback_m` 分别反求 H；它们与 `bottom_tension_n` 互斥。长度是悬空弧长，不能直接当作整条路由的制造长度。该模型无海流、弹性、抗弯、浮力变化或中继器，海床水平。

| 参数 | 默认值 | 约束 |
|---|---:|---|
| `depth_m` | 1000 | 0.001–12000 |
| `wet_weight_n_m` | 4 | 0.000001–20000，正水中重力 |
| `bottom_tension_n` | 1000 | 0–1000000000 |
| `cable_length_m` | 不设置 | 不小于水深，至多 1000000 |
| `layback_m` | 不设置 | 0–1000000 |
| `nodes` | 64 | 3–1000，整数 |
| `heading_deg` | 90 | -36000–36000，计算时归一化 |

## 3. 三维稳态近似 `steady_state(config)`

该模型假设海缆形状随船匀速平移。从海床水平张力初始条件向海面沿弧长积分：

```
dr/ds = T / |T|
dT/ds = -f
f = [0,0,-w] + 0.5*rho*Cd*D*|u_normal|*u_normal
u = current - vessel_velocity
u_normal = u - (u dot tangent)*tangent
```

当前仅使用法向阻力。材料放缆速度改变切向流动，但对法向分量没有影响，因此 `payout_m_s` 不改变此稳态受力形状；它用于输出名义底部余缆率。它不能求解积累余缆、瞬态启停或突然转向。零船速但正放缆、或放缆/船速差超过 5% 时返回告警。

以达到指定水深为终止事件。无阻力时数值解应回到解析悬链线，本项目对此有直接比较测试。若不能达到海面，或函数求值超过 20000 次，则抛出 `ValueError`，不输出貌似成功的结果。

在悬链线参数基础上增加：`ship_speed_m_s` 默认 1.5（0–20）、`payout_m_s` 默认等于船速（0–25）、`current_x_m_s/current_y_m_s` 默认 0（±20）、`diameter_m` 默认 0.02（0.0001–2）、`drag_coefficient` 默认 1.2（0–10）、`water_density_kg_m3` 默认 1025（1–2000）。底部张力必须大于零；`nodes` 最大 500。海流为均匀定常值。`recommended_ship_offset_m` 是船到预期触地点的局部平面偏移，并不构成完整的可执行船舶计划。

## 4. 动态敷设 `simulate_lay(project, config)`

材料节点受到相邻单元张力、水中重力、法向水阻力、海床接触和可选抗弯作用。节点质量按相邻材料长度的一半分配。默认干质量由水中重力与圆截面排水质量估算：

```
dry_mass_per_m = wet_weight_n_m / 9.80665 + rho*pi*D^2/4
effective_mass_per_m = dry_mass_per_m + Ca*rho*pi*D^2/4
```

本项目将附加质量作各向同性近似，忽略流体加速度项及自由面部分浸没。法向二次阻力使用半隐式速度更新。动力学建模的常见集中质量、节点受力和海缆水动力表述可见 [MoorDyn 官方模型结构文档](https://moordyn.readthedocs.io/en/latest/structure.html)。本项目没有调用 MoorDyn，也没有采用其验证结论。

轴向势能只在拉伸时存在：

```
E_axial = EA/(2*rest_length) * max(length-rest_length, 0)^2
alpha = rest_length / EA
alpha_tilde = alpha / h^2
```

通过带顺应性的隐式位置约束求解单元力，以免高 EA 导致显式积分的极小稳定时间步。每次迭代解相邻单元耦合的三对角系统，拉力乘子限制在非正值；力估计为 `-lambda/h^2`。这一数值策略参考 [Macklin、Müller、Chentanez 的 XPBD 原始论文](https://matthias-research.github.io/pages/publications/XPBD.pdf)，但应用到单侧张力、放缆边界和海床的实现为本项目独立近似。

`ei_n_m2`>0 时使用离散切线差抗弯能量和割线导数，属于近似各向同性梁；不包含有限转角杆的完整转动自由度、扭转或制造残余曲率。弯曲半径由离散方向变化计算，容易受网格影响。

船端为给定位置边界。放缆增长顶端单元的自然长度；超过目标单元长度两倍时，在顶端单元插入新材料节点。自然长度总量始终等于初始材料长度加累计放缆量。最老海床端固定，因此这是局部有限敷设窗口，不能据此声称完成了任意长、自由滑动的跨洋全航次仿真。

海床不穿透采用位置投影，滑动速度按重力支持下的规则化 Coulomb 摩擦减小。其摩擦法向力采用水中重量近似，未求出真实接触法向反力；当前不输出“拖底力”的工程估计。可选 `seabed_profile` 沿 x 变化，在 y 方向外推相同地形，区间外保持端点深度；不是三维测深网格。

初态取无流悬链线。打开海流会引起真实求解的启动过渡，因此短时结果也包含初始化效应。动态 v2 可通过显式 `material_segments` 和 `inline_bodies` 输入实际混合缆段及附属体载荷；工程 route KP 与动态材料坐标不同，程序不会自动把项目的缆型/附属体位置当作材料位置。未提供明确映射时，保留基础均匀缆并给出说明。这里的 heave 只规定船端正弦升沉，未包括波浪水粒子速度、波浪载荷、船舶响应或 GPS/ADCP/放缆机误差模型。

### 动态新增参数

| 参数 | 默认值 | 含义/约束 |
|---|---:|---|
| `duration_s` | 120 | (0,7200] |
| `dt_s` | 1 | 输出间隔，0.02–60 |
| `internal_dt_s` | 0.1 | 内部步长上限，0.002–0.25；输出时刻/控制事件会进一步细分 |
| `nodes` | 24 | 初始材料节点数，6–100 |
| `solver_iterations` | 16 | 每步约束迭代，2–40 |
| `ea_n` | 100000000 | 100–1000000000000 |
| `ei_n_m2` | 0 | 0–10000000000 |
| `mass_kg_m` | 上式估算 | 正干质量，且必须大于水中重力/g |
| `added_mass_coefficient` | 1 | 0–10 |
| `damping_ratio` | 0.03 | 0–1；只耗散相邻节点轴向相对速度 |
| `seabed_friction` | 0.5 | 0–2 |
| `heave_amplitude_m` | 0 | 0–20 |
| `heave_period_s` | 10 | 0.1–1000；启用升沉时内部步长≤周期/40 |
| `max_tension_n` | 1000000000000 | 超限告警，可由工程选中缆型继承 |
| `min_bend_radius_m` | 0 | 弯曲半径告警阈值 |
| `cable_type_id` | 首路段缆型/首缆型 | 仅用于默认力学属性选择 |
| `ship_plan` | 空 | 按时间递增的 `{time_s,speed_m_s,heading_deg,payout_m_s}`，分段常值 |
| `ship_plan_horizon_s` | `duration_s` | 预先存储后续船命令的时基上限，允许短窗口保留之后的控制事件 |
| `current_profile` | 不设置 | 按水深递增的 `{depth_m,x_m_s,y_m_s}`，线性插值 |
| `seabed_profile` | 不设置 | x 递增的 `{x_m,depth_m}` |
| `initial_suspended_material_m` | 0 | 固定最老海床端的材料起始坐标，0–100000000 m |
| `material_segments` | 不设置 | 连续覆盖初始缆与全部放缆的材料性质区间，最多 256 个 |
| `inline_bodies` | 空 | 明确材料位置的附属体平移载荷，最多 128 个 |
| `save_checkpoints` | False | 每个输出时刻保存真实积分状态，用于任选帧续算 |
| `checkpoint_times_s` | 空 | 最多 256 个唯一局部时刻，可在常规输出网格之外保存 |
| `resume_state` | 不设置 | 原样传入先前 `checkpoint` 或 `checkpoints[]` 项；`duration_s` 为继续运行时长 |
| `vessel_motion_series` | 不设置 | `{time_s,heave_m}` 的规定垂向位移；首点 0 秒/0 位移，覆盖时长，不与正弦升沉混用 |
| `wave_kinematics` | 不设置 | 恒定水深线性 Airy 波分量，仅通过水粒子速度改变相对阻力 |
| `max_work_units` | 12000000 | 可收紧本次计算预算，海况/前瞻/搜索按批次进一步约束 |

计算上限：最多 256 材料节点、2001 输出帧、预估 30000 内部步。预估工作量为 `steps*nodes*(iterations+body_count_if_payout)+frames*nodes*body_count`，不超过 12000000；结果返回 estimated_work_units。超过则拒绝计算并说明如何缩短时段或降低网格密度。每个数值字段必须有限，禁止布尔数值、NaN、Infinity、负自然长度、不递增剖面和重复控制时刻。

2026-10-04 本机动态 v2 普通算例（200 s、输出 201 帧、24 初始节点、1000 m 水深、1.5 m/s 放缆和船速、16 次迭代）耗时约 0.9 s；含 EI=10 的同算例约 3.1 s。此为具体机器的测量，不是所有输入的性能保证。

### 动态 v2：材料坐标、混合缆段与实体载荷

`initial_suspended_material_m` 是固定最老海床端的自然长度材料坐标，默认 0。材料坐标从海床端向船端增加。船端初值为 origin+initial_material_length_m，随后增加累计放缆量；已有材料的位置不能因为新增缆段而重新编号。帧中 `node_material_m` 与节点数组同向排列，从船端高值下降到海床 origin。

这个局部坐标不是海面 KP，也不是沿海床的距离。只有用户掌握装配材料位置或明确映射后，才能把工程缆型转换与附属体位置转换到当前窗口。过往已经在窗口后方的实体应另行记录，不能把其坐标设成 origin 之前仍要求当前窗口模拟。

混合输入示例：

```json
{
  "initial_suspended_material_m": 0,
  "material_segments": [
    {"id":"LW", "start_m":0, "end_m":250,
     "wet_weight_n_m":4, "mass_kg_m":1, "diameter_m":0.02,
     "ea_n":100000000, "ei_n_m2":10, "drag_coefficient":1.2},
    {"id":"HW", "start_m":250, "end_m":1000,
     "wet_weight_n_m":8, "mass_kg_m":2, "diameter_m":0.035,
     "ea_n":150000000, "ei_n_m2":20, "drag_coefficient":1.2}
  ],
  "inline_bodies": [
    {"id":"repeater-1", "material_m":130, "length_m":2,
     "wet_weight_n":200, "mass_kg":50,
     "drag_area_m2":0.05, "drag_coefficient":1.2}
  ]
}
```

缆段 start/end 为自然长度区间，两端必须正向、按材料位置递增、连续且无重叠（浮点坐标检查容差 1e-8 m）。所有初始材料及本次完整放缆都必须被覆盖。制造缆段可超出此次窗口，不会因未投入的未来材料而增加当前载荷。各局部湿重、干质量、直径、EA、EI、Cd 使用相同物理约束；局部正干质量必须大于局部湿重/g。未指定的湿重、直径、EA、EI、Cd 继承基础参数；缺失 mass 则根据该缆段的湿重与直径重新推导，而不是套用另一个型号的干质量。

单元跨越缆型边界时，程序按自然长度准确积分湿重、干质量、排水附加质量和 Cd*D，分别分配到节点；轴向刚度使用串联顺应性：`EA_eff = element_length / integral(ds/EA_local)`。局部抗弯刚度先按单元平均，再按相邻两段长度/刚度的串联形式求节点弯曲刚度；一侧 EI=0 时该弯曲约束不生效。这仍是割线近似梁，不是完整多材料杆。

初始几何仍是基础湿重的解析悬链线。自然长度会迭代调整，使局部 EA 与预设轴向张力一致；混合重量、实体和抗弯并未在启动前联合求得静态平衡。结果明确给出 HETEROGENEOUS_INITIAL_TRANSIENT 告警，需要留出沉降时间并核查动态收敛。

附属体字段：`material_m` 为实体较低材料端的起点；`length_m` 默认 0，为点荷载。正长度实体在 `[material_m,material_m+length_m]` 上分布，并随材料进入逐步施加载荷。`mass_kg` 和 `wet_weight_n` 必填，mass≥0，wet_weight 可为负值（浮体），且必须≤mass*g，避免负排水体积。`drag_area_m2` 默认 0、范围 0–10000；Cd 默认 1.2、范围 0–10。实体材料区间不得重叠，id 必须唯一。

实体总质量、水中重力和独立各向同性二次阻力，通过线性材料形函数分配到接收节点；有限实体的形函数在相交材料区间积分，守恒已投入质量、重力和材料一阶矩。实体有效附加质量采用 `Ca*max(0,mass_kg-wet_weight_n/g)`。实体载荷增加到已有缆段上；如果真实制造实体替代了一段缆，应先在材料区间中给出实际剩余缆/连接结构性质，不能默认重复加载就是替代建模。

帧新增 node_material_m、node_mass_kg、node_wet_weight_n、segment_ea_n、segment_wet_weight_n_m、segment_diameter_m。inline_bodies 帧记录 id、投入比例、分配节点得到的中心位置、已投入干质量/水中重力与接触比例；完全未投入的实体位置为 null。总结返回材料坐标起止、缆段与实体数量、总干质量、有效质量、水中重力。接触在承载节点上发生；实体中心位置和接触比例不是刚体形状碰撞结果。

还未实现实体转动自由度、长度刚性、偏心质量、详细水动力形状、端部夹角约束、实体与海床几何碰撞，以及入水/分网时严格动量能量传递。不能把这份平移载荷模型描述成完整中继器或接头刚体模拟。

实测示例：水深 100 m、底张力 1000 N、16 初始节点、20 s、1.1 m/s 放缆，上例初始船端材料坐标约 244.918 m，最终约 266.918 m，跨过 250 m 边界；首单元最终湿重约 7.077 N/m，50 kg/200 N 实体载荷实际加入节点。此例约 0.6 s、离散残差收敛；这只是数值程序验证，不构成工程现场精度证明。

## 5. 静态海床跨距 `span_analysis(config)`

给定海床剖面、正水中重力 w、水平残余张力 H 和抗弯刚度 EI，求解小斜率张力梁在刚性海床上方的势能极小值：

```
E = 1/2 * integral(H*z'^2 + EI*z''^2) dx + integral(w*z) dx
subject to z(x) >= seabed_z(x)
EI*z'''' - H*z'' + w = R,  R>=0 at unilateral interior contact
```

二阶差分离散后，采用稀疏活跃集接触求解。端点固定在剖面两端深度并允许自由转动。无接触且 EI=0 的解为抛物线；H=0 的自由跨距对应简支均布载荷梁。本项目分别用这些解析解检验形状和收敛。

`profile` 必须至少两个、至多 2000 个 `{x_m,depth_m}` 样本，x 严格增加，水深 0–12000。计算网格 `nodes` 默认 101（5–401）。H 默认 1000（0–1000000000）；EI 默认 0（0–1000000000000）；w 默认 4（0.000001–20000）。`span_clearance_m` 默认 0.01（大于 0、至多 10），用于识别悬空区间；`min_bend_radius_m` 用于曲率限值告警。也接受 `seabed_profile` 作为 `profile` 的别名。水平范围限制为 0.1–1000000 m。

返回各网格点海床/海缆水深、净空、节点垂直反力、近似张力、半径、弯矩和剪力，以及悬空区间、最长跨距和力平衡残差。反力单位 N，是节点集中力，不是单位长度压力。离散边界反力如为负值，说明固定端需要向下约束，真实仅接触端可能抬起，必须重新定义求解边界。

小斜率近似在 |dz/dx|>0.3 时告警。模型没有给定海缆制造长度、非线性轴向应变、海床摩擦/土体、疲劳和涡激振动，不能自动判断工程安全或代替完整的 power-cable 接触梁分析。

## 6. 已完成验证与未完成验证

运行：`.venv/bin/python -m pytest tests/test_simulation.py tests/test_heterogeneous.py -q`。

- 解析悬链线重量/顶端反力平衡、几何弧长、逆边界条件、垂直极限和航向。
- 无阻力三维稳态与解析悬链线一致；横向海流正负方向镜像。
- 动态自然长度/累计放缆守恒、海床不穿透、非负拉力、转向启停控制积分、深度海流加载；静止悬链线的端力网格收敛和运动形状的时间步收敛。
- 平床零悬空、全部重量由海床反力承担；张力自由跨距匹配抛物线；弯曲自由跨距按网格加密趋向简支梁解析解。
- 输入拒绝、计算上限与全部结果严格 JSON 有限数检验。
- 混合模型与明确均匀材料输入等价；相邻相同缆型分割不改变物理解；放缆跨越材料边界改变实际载荷/形状；材料原点平移不改变物理结果。
- 实体干质量、水中重力和材料一阶矩守恒；质量与阻力分别改变运动；有限实体投入比例与材料长度相符；底部接触与浮体载荷有限；带实体工况时间步加密使形状差异减小。
- 缆型覆盖缺口/重叠、超出完整放缆覆盖、实体重叠、不一致质量/重力及非法输入拒绝。
- 普通输出断点与任意 2.375/4.25 s 断点：混合材料、附属体、转向启停、升沉和新增材料节点的连续计算/分段续算，位置、速度、自然长度、材料坐标、质量、刚度和张力在 1e−9 绝对容差/1e−11 相对容差内一致。
- 真实 Node.js `JSON.parse`→`JSON.stringify`→Python 断点往返可续算；缺字段、错误版本、完整性校验失败、虚假局部物性/接触、改边界或数值参数及保存量超限均拒绝。

独立海况模块支持有限谱、用户垂向 RAO 表与 Airy 流速的实际动态接入，以及声明独立参数分布的少量 Monte Carlo 真实求解，详见 [SEA_NOTES.md](SEA_NOTES.md)。

未完成：抗弯动态的解析基准、完整能量收支、接触摩擦实验验证、非线性/含波浪加速度惯性载荷、多材料杆与实体完整刚体动力学、实体通过放缆机/入水的过程、独立外部数值模型交叉验证、船舶/执行器/传感器控制误差与硬件选择预算、土体与埋设、多维地形接触、原厂黑箱对比、实验室和海试标定。所有这些限制必须保留在能力说明中。

## 7. 公开参考来源与独立实现边界

- [MakaiPlan Pro 官网](https://www.makai.com/cable-software/makaiplan-pro/) 和用户提供的 `MakaiPlanPro.pdf` 用于确定产品功能方向，包括稳态初步船舶计划、3D 动态、海流、启停/转向和跨距分析；它们没有提供可重现原厂模型的算法或海试标定参数。
- [MoorDyn 官方 Model Structure](https://moordyn.readthedocs.io/en/latest/structure.html) 说明集中质量线单元、节点水动力和材料参数。仅作为公开模型架构参考。
- [XPBD 原始论文，Macklin et al., 2016](https://matthias-research.github.io/pages/publications/XPBD.pdf) 说明带顺应性的隐式位置约束与力乘子估计。该论文不是海缆模型的工程标定资料。
- [Orcina 官方 Line data: Statics](https://www.orcina.com/webhelp/OrcaFlex/Content/html/Linedata%2CStatics.htm) 对悬链线静态与完整静态的适用因素作区分。OceanRoute 的解析模型也必须保留其未含弯曲和水动力的边界。

没有原厂私有源代码、未授权付费资源或绕过许可证的内容进入本模块。所有方程和程序均来自公开的常规力学方法与本项目独立实现。

## 8. 真实状态断点与续算

每次动态计算都返回最终 `checkpoint`。它与可视化帧分开：帧不是求解器状态，不能从最后两帧反算速度来续算。`save_checkpoints: true` 额外返回每输出时刻的 `checkpoints`；`checkpoint_times_s` 选择任意局部秒数。保存点也加入帧序列，便于选择与核对。

断点是纯 JSON，schema=`oceanroute.dynamic.checkpoint`、schema_version=1、model=`material-lumped-mass-xpbd-cable-lay-v2`、validation_status=`research`。来源注明 `oceanroute.simulation.simulate_lay`，另保存绝对 `time_s`、完整解析后的配置、数值积分方案/时间网格和 SHA256 完整性摘要。

`state` 包含实际节点位置和速度、各单元自然长度、节点材料坐标/干与等效质量/水中重量、局部 EA/EI/湿重/直径、分配的缆与实体阻力系数、当前拉力、船与锚边界、海床接触掩码、累计已放材料、初始材料长度、网格分割目标、绝对船命令及当前位置、升沉相位/高度偏移和累计数值统计。未来放缆量按保存命令和继续时长重新积分，材料覆盖仍须足够。材料/实体分布由自然长度和原配置重算并与保存值核对。XPBD 乘子每内部步本来就重新初始化，保存末步拉力用于初帧和统计，不伪称跨步保留不存在的历史乘子。

```python
run = simulate_lay(project, {
    "depth_m": 30, "bottom_tension_n": 100,
    "nodes": 8, "duration_s": 4, "dt_s": 0.5,
    "internal_dt_s": 0.05, "ship_speed_m_s": 0.5, "payout_m_s": 0.6,
    "checkpoint_times_s": [1.375, 2.375]
})
saved = run["checkpoints"][0]
continued = simulate_lay(project, {
    "resume_state": saved, "duration_s": 2,
    "ship_plan": [
        {"time_s": 0, "speed_m_s": 0.7, "heading_deg": 70, "payout_m_s": 0.6},
        {"time_s": 1, "speed_m_s": 0, "payout_m_s": 0}
    ]
})
assert continued["frames"][0]["time_s"] == 1.375
assert continued["frames"][-1]["time_s"] == 3.375
```

只提交 `resume_state` 与 `duration_s` 时，保留绝对船命令、当前激活命令、后续事件、海流和升沉相位。显式 `ship_speed_m_s`、`payout_m_s`、`heading_deg` 在断点立即覆盖当前命令，已有后续事件仍保留；显式 `ship_plan` 完整替换未来命令，行内时间从断点起算，0 表示立即生效。新海流/深度海流及升沉参数也可输入。改变升沉参数时，新正弦位移从保存船高开始，位置连续但新的规定垂向速度可产生过渡，并明确告警。

材料、附属体、原材料起点、底部锚条件、海床、基础水密度/缆属性、摩擦/阻尼、原网格和 dt/internal_dt/迭代次数不可在续算时改变。可原样带入相同值；其他值明确拒绝。要改变模型应另建初始工况，不能把旧状态当作新状态已满足平衡。

输出时间始终为累计绝对时间。正常输出网格保留最初网格原点，即从 1.375 s 继续且原 dt=0.5 时，下一正常输出是 1.5 s。`summary.paid_out_m`、材料长度和最大力/最大应变为整个保存历史的累计指标；`interval_paid_out_m`、`interval_max_internal_top_tension_n`、`interval_max_tension_n`、`interval_minimum_bend_radius_m` 只评价本次继续时段。顶部内部步峰值另独立于 `max_top_tension_n` 的输出采样峰值。

断点摘要是完整性检测，不是身份认证或工程有效性签名。相同数值的 1/1.0、0/−0 在哈希前正规化，浏览器 JSON 保存不会仅因格式改变而失败；内部状态仍不得手工修改。单断点≤2 MB；每次保存至多 256 点，保存节点总量预估≤32000，保存 JSON 总体积保守预估≤16 MB，船命令历史≤1000。总体积同时计入每个独立断点重复保存的运动表/海流/地形等配置。旧模型/缺字段/越界或数值设置不一致均拒绝，纯 JSON 不执行代码。

任意保存时刻会使积分在该时刻截断一个内部步。与采用相同保存边界的连续运行比较，续算仅有浮点舍入差；如果拿未设置该保存边界的运行作比较，新增的短内部步可能产生正常数值离散差异，应做时间步加密分析。断点是独立研究仿真的状态，不能当作真实施工传感器同步或实船实时监控。
