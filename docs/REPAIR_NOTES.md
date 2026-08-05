# 回收、拖索与浮标静态研究工具

实现：`oceanroute/repair.py`。公开函数为 `recovery_shape(config)`、`steady_tow(config)`、`estimate_grapnel_rope(config)` 和 `size_buoy(config)`。所有结果标记 `validation_status: research`。本模块提供实际解析/数值静态计算，不能代表原厂维修模块、实船监控、回收控制或完整抓钩/浮标动力学。

## 来源与实现范围

用户提供的 MakaiPlanPro 介绍 PDF 第 9–10 页，以及 [Makai 官方 Repair Module](https://www.makai.com/cable-software/repair-module/)，描述了回收形状计算、根据船速/水深/海流估算抓钩绳长、支撑悬链线的浮标选型，也描述了实时回收和抓钩下放/着底等更完整功能。这里独立实现前述三项研究计算及其实际稳态拖索基础；后述实时/全动态能力没有实现。

解析悬链线参照 [Orcina 解析悬链线说明](https://www.orcina.com/webhelp/OrcaFlex/Content/html/Linetheory%2CAnalyticcatenary.htm) 与 [公开悬链线理论验证资料](https://www.orcina.com/wp-content/uploads/resources/validation/99-105-Theoretical-results.pdf)。法向二次阻力采用 [Orcina Morison 元素说明](https://www.orcina.com/webhelp/OrcaFlex/Content/html/Morisonelements.htm) 中公开的阻力形式，仅取稳态速度阻力部分。浮力依据 [OpenStax 阿基米德原理](https://openstax.org/books/university-physics-volume-1/pages/14-4-archimedes-principle-and-buoyancy)。这些是公开物理依据，不是原厂内部算法或校准数据。

## 通用约定与接口

四个接口均为 `POST /api/repair/{recovery|tow|rope|buoy}`，body 为 `{"config": {...}}`，不依赖当前规划工程。非法值、边界条件冲突或未支持的物理请求抛出 `ValueError`，HTTP 层返回 422。

长度 m、时间 s、质量 kg、力 N、水密度 kg/m³。湿重是全浸水后的向下净重量，不是干质量乘重力。局部坐标 X 向东、Y 向北、Z 向上；顶端固定在 `[0,0,0]`，海床为 `z=-depth_m`。艏向/力方位为北 0°、顺时针。**节点数组由顶端 index 0 排到海床触点末项**；`arc_from_touchdown_m` 相应由悬垂弧长降到 0。这是静态几何，不含动态帧、时间或速度历史。

通用参数：`depth_m` 默认 1000（0.001–12000）、`wet_weight_n_m` 默认 4（正值，至多 20000）、`nodes` 默认 64（整数 3–500），方向默认 90°。拖索另用 `diameter_m` 默认 0.02、`drag_coefficient` 默认 1.2、`water_density_kg_m3` 默认 1025、`bottom_tension_n` 默认 1000、`ship_speed_m_s` 默认 1.5、`current_x_m_s`/`current_y_m_s` 默认 0。物性和流速采用现有稳态模型的输入限值。

模型统一要求均匀柔性、不可伸长、全浸水、正湿重、平海床。非空的混合材料、附属体、分层海流、变化地形、波浪/运动表、动态断点或分段船令，以及正弯曲刚度/非零升沉会拒绝。EA 不进入此不可伸长静态模型。不能把这些输出用于表示混合缆、地形接触、回收惯性、沉积物吸力或缆体弯曲承载。

每项返回 `model`、`validation_status`、`assumptions` 字符串数组及结构化 `warnings`。几何工具返回 `nodes[N][3]`、`node_tension_n[N]` 和 `arc_from_touchdown_m[N]`。`end_forces` 的力向量含：

| 字段 | 含义 |
|---|---|
| `required_top_support_n` | 顶端支撑应施加给缆的力，向上的分量为正 |
| `cable_on_top_n` | 缆施加给顶端的力，等于上项的负值 |
| `rope_on_bottom_n` | 缆施加给底端对象的力；底端对象对缆的外力为其负值 |
| `integrated_gravity_n` | 悬垂缆段的全浸水重量向量 |
| `integrated_drag_n` | 悬垂缆段实际积分的阻力向量 |
| `balance_residual_n`、`balance_residual_norm_n` | 顶端支撑−底端力+重量+阻力的闭合误差 |

## 回收形状：剩余悬垂长度控制

输入必须给 `suspended_length_m`，或者给 `initial_suspended_length_m` 与 `retrieved_length_m`（后者默认 0），二者不可混用。剩余悬垂弧长 `L=initial−retrieved` 必须不小于水深 d，且至多 10⁶ m。这里的 initial 是**初始悬垂弧长**，不是已安装路由总长；不能用整个海底路由长度相减来模拟回收。

回收要求船速、海流、回收速度均为 0。不允许同时输入底张力、缆长或 layback 等竞争边界。触地点可自由移动，每个剩余长度是新的静态快照，不保留海床锚点、路由材料历史或回收时间。

正湿重 w 下，水平底张力 `H=w(L²−d²)/(2d)`，顶端垂向支撑 `V=wL`，顶张力 `sqrt(H²+V²)=H+wd`；悬链参数 `a=H/w`，水平距离 `a asinh(L/a)`。L=d 时是零水平张力的垂直极限，返回专门告警，不声称有可靠的拖拽/松弛控制。可选 `max_tension_n` 仅比较静态顶张力并告警。

```json
{"depth_m":100,"wet_weight_n_m":4,"suspended_length_m":150,"heading_deg":90,"nodes":96}
```

此例底张力 250 N、垂向提升力 600 N、总提升力 650 N、layback 约 100.590 m。结果 `summary` 包含标准悬链线字段及 `initial_suspended_length_m`、`retrieved_length_m`、`remaining_suspended_length_m`、`required_lift_tension_n`、`required_vertical_lift_n`、`required_horizontal_lift_n`；直接给剩余长度时前两项为 null。

## 稳态拖索：实际重量与阻力积分

`steady_tow` 输入水平 `bottom_tension_n`，可独立给 `bottom_heading_deg`（默认船艏向）。求解得到弧长和形状；不能同时指定悬垂/回收长度。相对水速 `u=current−ship_velocity`，缆切向 `t=T/|T|`，法向水速 `u_n=u−(u·t)t`，单位长阻力 `f_D=0.5 rho Cd D |u_n|u_n`。

从海床向上按弧长积分 `dr/ds=t`、`dT/ds=−f_D+w e_z`，同时积分实际阻力。从底端水平力开始，达到 z=d 的表面事件终止。无流/零阻力时直接复用现有解析悬链线；流载且底张力为 0 时明确拒绝。底端无垂向牵引是本模型声明的几何边界，不是着底碰撞求解。

积分相对/绝对容差均 10⁻⁸，悬垂长度至多 10⁶ m、力至多 10¹² N、函数评价至多 20000 次。未达到表面、越过平海床/表面或非有限结果会拒绝。纯法向阻力不作切向功，因此 `d|T|/ds=w dz/ds`；本模型中顶张力幅值仍为 H+wd，**海流会改变真实形状、弧长、顶端力方向及其垂向分量**。不能据幅值不变误认为没有阻力影响。

```json
{"depth_m":100,"wet_weight_n_m":4,"diameter_m":0.02,"bottom_tension_n":250,"bottom_heading_deg":90,"ship_speed_m_s":1,"heading_deg":90,"current_x_m_s":0,"current_y_m_s":0.3,"drag_coefficient":1.2,"water_density_kg_m3":1025,"nodes":96}
```

除通用几何/力平衡字段，输出 `node_force_vectors_n`（底向顶的内部牵引向量，数组仍顶到末端）；`summary` 含深度、悬垂长度、layback、touchdown、顶/最大/底张力、底端力方位、相对海流、顶端与水平面的夹角、触点切向及 `geometry_above_seabed`。`solver` 返回收敛状态、函数评价数和垂向边界误差。轴向阻力、流致振动、弹性和波浪惯性没有建模。

## 抓钩绳长：既定滑动接触条件下的估计

必须输入正值 `grapnel_wet_weight_n`。可选 `grapnel_mass_kg` 用于检查湿重不大于干重；`grapnel_friction_coefficient` 默认 0.5（0–2），`grapnel_drag_area_m2` 默认 0，`grapnel_drag_coefficient` 默认 1.2。有动摩擦时必须有正船速。输入底张力/底力方位会拒绝，因为它们必须由实际抓钩载荷计算。

抓钩被视为已在平床滑动的理想滑橇，绳端位于海床高度且力为水平。海床法向力 N 等于抓钩湿重；摩擦为 `−mu N ship_direction`；体阻力为 `0.5 rho Cd area |u|u`。绳力取二者合力的负值，实际幅值和方位输入 `steady_tow`，绳本身的重量与法向阻力继续真实积分。

```json
{"depth_m":100,"wet_weight_n_m":4,"diameter_m":0.02,"ship_speed_m_s":1,"heading_deg":90,"grapnel_wet_weight_n":1000,"grapnel_friction_coefficient":0.5,"grapnel_drag_area_m2":0.2,"grapnel_drag_coefficient":1.2,"available_rope_length_m":300,"extra_rope_m":10}
```

此例抓钩体阻力 123 N、摩擦 500 N、底绳力 623 N；实际悬垂绳长约 252.615 m，加声明余量后需放出约 262.615 m，顶张力约 1023 N。

返回完整 `rope_shape`（稳态拖索对象）、顶层节点/张力和 `grapnel_forces` 的绳力/阻力/摩擦/法向力/水平闭合误差。`summary` 区分 `minimum_suspended_rope_m`、`extra_rope_m`、`required_paid_rope_m`、`available_rope_length_m`、`rope_shortfall_m`、`available_length_reaches_seabed` 和 `declared_allowance_met`；未提供可用长度时后三项相关判断为 null。

可用长度不足时保留**条件计算形状**并明确告警、将够长判断置为 false，不把假定几何画成实际着底证明。`bottom_contact_assumed:true` 仅声明计算前提。余量是用户值；海床尾绳的阻力/摩擦、下放、着底过程、钩挂、缆线接触、旋转和土体贯入没有求解。

## 浮标：缆端真实垂向载荷与阿基米德选型

必须给 `buoy_mass_kg`（浮标干质量）和 `height_m`；浮体假定为直立等截面圆柱。`freeboard_m` 默认 0 且必须小于高度，`reserve_pct` 默认 20（0–500）；密度默认 1025。可选 `above_water_payload_mass_kg`、`rigging_wet_weight_n` 分别表示水上干质量及浸水附件净重量。

载荷必须二选一：嵌套 `cable` 配合 `cable_model:"recovery"`（默认）或 `"tow"`，复用实际静态求解；或者明确给 `support_force_n:[Fx,Fy,Fz]`。嵌套缆与浮标密度必须一致，Fz 必须非负。传入的向量是浮标需施加给缆的支撑力，缆施加给浮标的是其负值。

垂向总载荷 `W=Fz+g(buoy_mass+payload_mass)+rigging_wet_weight`，不错误使用绳张力幅值代替 Fz。设计载荷为 `W(1+reserve_pct/100)`；满足指定干舷的最小总浮体排水体积为 `Vmin=design_load/[rho g(1−freeboard/height)]`。可选 `displacement_volume_m3` 表示候选**完整浮体体积**，未给则采用 Vmin；水线面积 V/height，圆柱直径 sqrt(4A/pi)。

实际漂浮吃水为 `W/(rho g A)`，只在全体积浮力能支撑 W 时报告。不能浮起的候选返回吃水/实际干舷/实际排水量 null 和负垂向残差。能够浮起但未满足余量/干舷的候选独立标记失败。

```json
{"cable_model":"recovery","cable":{"depth_m":100,"wet_weight_n_m":4,"suspended_length_m":150,"heading_deg":90},"buoy_mass_kg":50,"height_m":2,"freeboard_m":0.5,"reserve_pct":20,"horizontal_restraint_n":[250,0]}
```

例中总垂载约 1090.333 N，最小浮体体积约 0.173554 m³，圆柱直径约 0.332397 m；实际吃水 1.25 m、干舷 0.75 m。给出的 `[250,0]` 水平外部约束平衡缆的水平牵引，仅用于力平衡示例，实际约束需用户另外说明。没有约束且缆端有水平力时返回 `HORIZONTAL_BUOY_EQUILIBRIUM_NOT_MET`，即使垂向浮力够用也不声明完整平衡。

输出 `cable_shape`（明确力向量输入时为 null）、`support_force_n` 及 `summary`：实际/设计垂载、水平缆力、最小/候选浮体体积、圆柱尺寸/水线面积、指定/实际干舷、吃水、实际排水量、最大浮力、指定干舷可用承载量、剩余承载量，以及 `vertical_equilibrium_possible`、`design_margin_and_freeboard_met`、`horizontal_equilibrium_met`、`complete_static_force_balance` 和力残差。

完整标记仅指静态**合力**闭合。浮标拖曳、漂移、系泊几何、倾斜、力矩/稳性、波浪、刚体六自由度与浮标附缆动力学均未求解；不能根据圆柱排水体积或用户余量声明实船可用/安全。

## 验证

`tests/test_repair.py` 校验解析悬链线提升力/重量/几何及垂直极限；回收剩余弧长的一致性；已有稳态求解器的无流和有流交叉比较；实际阻力积分与整体力闭合；相对速度的伽利略不变性及横流镜像；抓钩阻力/摩擦/法向力平衡、海流对方位和长度的影响及不足绳长；浮标真实垂载而非张力幅值、密度/附件载荷缩放、不足排水量和独立余量失败；非法/冲突/未支持输入拒绝及四接口有限 JSON 编码。验证证明这些研究公式和实现约定的一致性，未完成原厂结果、海试、设备数据库或工程规范的独立对标。
