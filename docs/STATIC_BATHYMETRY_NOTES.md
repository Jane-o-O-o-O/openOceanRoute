# 二维坡床静力研究模型

此模块是冻结 0.4 发行包之后的开发工作，新增 `oceanroute/static_bathymetry.py`，不覆盖历史 ZIP、wheel、PDF 或验证报告，不修改旧动态初始化。两种新函数均返回 `validation_status="research"`。`accepted=true` 只说明已满足本模型声明的数值、边界和力平衡验收；没有海试标定、稳定性或原厂精度证明。

## 1. 公开要求与充分边界

提供的 MakaiPlan 6.2 手册 PDF 250 页（印刷 M242）说明悬链线计算器使用水深、海床坡度和顶部张力、底部张力、顶角或入水长之一；PDF 22 页（M14）指出有底张力时触底切向沿海床。[MakaiPlan Pro 官方说明](https://www.makai.com/cable-software/makaiplan-pro/)另有变深地形下的动态模拟和通过动态结果修订稳态船计划的要求。

这些说明没有提供原厂方程、数值算法或校准参数。本模块独立从连续缆的力平衡和弹性伸长关系推导平面坡床公式，再用独立的有限段静力模型验证。四种逆边界另由0.5的 [CATENARY_CALCULATOR_NOTES.md](CATENARY_CALCULATOR_NOTES.md) 包装实现，不改变本文forward合同；持续施工的稳态解和任意历史状态自动恢复仍未实现。

对该手册Calculator截图还作了实际HTTP和独立平床闭式对照：深2000 m、湿重17 N/m、总底张力34000 N、显式不可伸长，得到顶部张力68000 N、相对水平顶角60°、自然长3464.101615 m和layback2633.915794 m，与图中舍入值一致。证据为 [development_next_manual_catenary_figure.json](../resources/validation/development_next_manual_catenary_figure.json)。这只是一个公开手册图例，不是原厂程序黄金输出或海试精度。不可伸长例中自然长与伸长弧长相等，不能推断手册有限弹性“Cable in Water”的长度定义。

二维曲床上，仅给水深、航向和一个底张力不能确定整条缆的铺设状态。任意床场静力模型需要两个端点的实际位置、自然长度及摩擦历史选择。新模块不能从路由 KP 或规划制造长度猜出这些条件。

## 2. 坐标、海面和床场

两函数共用 [BATHYMETRY_NOTES.md](BATHYMETRY_NOTES.md) 中的完整 `seabed_grid`：局部 x 向东、y 向北，米；`z_m[y][x]` 正向上，相对模型海面 `z=0`。`source.vertical_datum` 仅记录来源，不转换潮位、海图基准或地理投影；输入必须已显式对齐。`source.sha256` 是外部来源声明，不是自动证明网格内容的哈希。

覆盖政策严格：包括悬空节点和船端在内的所有查询点都需完整已知单元；不外推、不补零、不以平床代替缺测。静力求解器探索到 NoData 单元也会抛出 `ValueError`，即使另一条可能存在的路径没有缺测。这是当前支持范围的限制。

## 3. `slope_catenary(config)`：仿射坡床切向悬链线

开发 API 为 `POST /api/simulation/slope-catenary`，请求 `{ "config": {...} }`。它只求触点到船端的悬垂段。触点的总张力和水平航向是边界条件；海底尾缆、锚点位置、摩擦及先前铺设历史未求解。

| 输入 | 默认与范围/语义 |
|---|---|
| `seabed_grid` | 必须提供；全部网格节点已知，且全域可拟合一个仿射平面 |
| `vessel_position_m` | `[0,0,0]`；有限三维局部米坐标，z≤0，至少高于已知床面 .001 m |
| `heading_deg` | 90，−36000..36000；触点到船的**水平**方向，0 北、90 东 |
| `bottom_tension_n` | 1000，1e−6..1e9；触点**总张力幅值**，不是水平 H；0 极限明确不支持 |
| `wet_weight_n_m` | 4，1e−6..20000；每自然米水中重量 |
| `ea_n` | 1e8，100..1e12；显式 `null` 才表示不可伸长极限 |
| `nodes` | 64，3..1000；按自然长度等间距取显示节点 |
| `plane_tolerance_m` | 1e−7，1e−10..1e−5；所有网格节点的绝对仿射拟合容限 |
| `max_natural_length_m` | 1e6，.001..1e6；根的真实最大自然长度边界 |
| `max_root_iterations` | 100，1..200 |
| `max_work_units` | 250000，1..1000000；归一化计算额度，非 FLOPs |

其他键，包括海流、抗弯、混合材料、实体、`depth_m` 和运动条件，明确拒绝。水深由实际船位和完整床场计算。每个网格节点都参加缩放坐标的仿射拟合检查，绝不只检查四角；拟合后仍用原始双线性床场检查节点的床高、法向、触点位置和不穿透。

令水平单位向量为 d，床面 `z=b+gx*x+gy*y`，沿向坡度 `m=(gx,gy)·d`。给触点总张力 B>0，定义：

```text
H = B / sqrt(1+m²)
V0 = m H
V(s) = V0 + w s
T(s) = sqrt(H² + V(s)²)
dx/ds = (1+T(s)/EA) H/T(s)
dz/ds = (1+T(s)/EA) V(s)/T(s)

x(s) = H/w [asinh(V(s)/H)-asinh(V0/H)] + H s/EA
z(s) = [T(s)-B]/w + [V0 s + w s²/2]/EA
```

`s` 从触点向船增加，是**未伸长的材料弧长**；EA 为 null 时所有 1/EA 项为零。触点垂直张力可为负，取决于沿向坡度，不能把 `bottom_tension_n` 当成 H。

对船位相对床面高度 D，求单调根：

```text
z(S)-m x(S)=D
d[z(s)-m x(s)]/ds = w s [1/T(s)+1/EA] > 0, s>0
```

因此给定本边界有唯一正自然长度，但若该长度超限或实际触点/缆投影出了已知网格，则失败，不创造额外床面。正负坡都使用同一方程。平面内悬垂曲线相对坡床高度非负，可核验无穿透；没有求解后的投影步骤。

高张力时使用有理化 `T−B` 和稳定 asinh 差；很小 `wS/H` 时用 12 点 Gauss 积分求正常上升量，避免 `z−m*x` 的相消。恰在最大长度的可达端点，只允许 32eps×实际高度尺度的浮点余量，并明确输出迭代数 0，避免依赖 SciPy 端点根的未定义计数。确实不足的长度不会因此通过。

`nodes` 从船到触点排序；`node_material_m` 从 S 递减到 0。`node_tension_vectors_n` 统一表示由触点沿材料方向向船的张力向量。缆上的船端力为 `+T(S)`，触点力为 `−T(0)`；与总湿重 `(0,0,−wS)` 直接相加检验整体平衡。

输出包含真实 `natural_length_m`、`stretched_arc_length_m` 和离散 `geometric_chord_length_m`，三者不能混同。`summary` 另含 H、底/顶垂向分量、触点相对模型海面深度、上下缆角和总力残差；`solver` 报根、切向、穿透及预算验收。`end_forces_on_cable_n` 明确是作用在缆上的端反力。轴向应变超过 5% 给出线性本构有效性告警。

可执行合成例：grid x/y 都为 `[-100,0,100]`，每点 z=`−30+.1x+.05y`；来源为 `LOCAL_CARTESIAN_METRES`、原点 `[0,0]`、明确已经对齐模型海面。使用 w=4、B=100、EA=1e5、heading=90、nodes=41，得到 S=50.3803848342 m、实际弧长=50.4582281328 m、layback=34.8380900285 m、触点深=33.4838090028 m、顶张力=233.7121292147 N。H=99.503719021 N、底垂向=9.9503719021 N；根残差约 3.55e−15 m，整体残力为 0，无床下投影。这仅是显式合成平面个例。

## 4. `static_equilibrium(config)`：明确边界的二维接触静力

开发 API 为 `POST /api/simulation/static-bathymetry`，请求 `{ "config": {...} }`。它不是平移施工中的 `steady_state`，也不接受以底张力代替端点/自然长度。

| 输入 | 默认与范围/语义 |
|---|---|
| `seabed_grid` | 必须提供；可非仿射，但每条真实查询轨迹需要完整覆盖 |
| `vessel_position_m`, `anchor_position_m` | 必须提供；固定端点，相距≥.001 m，不低于床面且 z≤0；锚可以是床外的明确支持点 |
| `natural_length_m` | 与下面 rest 二选一；.001..1e6；均匀自然段长 |
| `rest_lengths_m` | 5..79 个正段长，每段≥1e−4 m，总和≤1e6 m；显式不均匀材料网格 |
| `nodes` | 24，6..80；若给 rest，必须等于段数+1 |
| `wet_weight_n_m`, `ea_n` | 4、1e6；分别 1e−6..20000、100..1e12；EA 不接受 null |
| `initial_positions_m` | 可选 N×3；必须与端点和声明的粘着点相符且不穿床；只是迭代初值 |
| `contact_policy` | `frictionless` 或 `prescribed_stick` |
| `seabed_friction` | 0，0..2；frictionless 必须为 0 |
| `sticking_nodes` | `[{node_index,position_m}]`；prescribed_stick 必须有正 μ 和明确床上内节点位置；非推断历史 |
| `force_tolerance_n` | .01，1e−6..1 |
| `relative_force_tolerance` | 1e−5，1e−9..1e−3；有效节点容限为两种力容限的较大者，相对基准 max(1,wL) |
| `contact_tolerance_m` | 1e−6，1e−9..1e−4 |
| `max_solver_iterations` | 300，1..600 |
| `max_function_evaluations` | 2000，1..10000；真实不同迭代位置评估额度 |
| `max_segment_samples` | 20000，10..200000；最后完整直段检查额度 |
| `max_work_units` | 200000000，1..2000000000；包括声明迭代×自由变量³、函数次数×变量²及段内验证的额度，非 FLOPs |

无海流、弯曲/扭转、混合材料、实体或运动边界；这些字段拒绝而不静默忽略。高节点数和高迭代上限可能提前超过默认工作额度，需降低明确上限。尺度化优化变量及能量避免把米坐标和高 EA 直接作为未缩放的最小化量。

### 能量、接触和独立力验收

对自然段长 l0、实际直段长 l、EA，使用张力-only 能量：

```text
T_i = EA * max(l_i/l0_i - 1, 0)
E = sum[EA/(2 l0_i) * max(l_i-l0_i,0)²] + sum[w_node_i z_i]
g_i = z_i - bed(x_i,y_i) >= 0
```

湿重按每段一半分配至两端，材料长度与重量不因形状优化而变化。SLSQP 使用能量和床高梯度的解析 Jacobian。无显式初值时，直连形状仅在自由内节点竖直抬到床外，作为可行初值；这一步没有静力成功的含义。

最后从每段真实长度和 Hooke 张力重新重构等大反向的内部力，加实际湿重；不直接信任优化器输出的“success”。自由接触节点只允许 `R=N*n, N>=0`，节点净力向量残差必须满足声明容限。全局残力容限是内节点数×节点力容限（独立节点残差的三角不等式上界）；互补容限是接触米容限×max(1,最大正常力)，实际值和容限均公开。床上净力不能由矩形网格边界或隐形支持来平衡。端点反力单独计入外部边界，不伪装成床摩擦。

`prescribed_stick` 固定用户声明的位置，再算该点**所需**的支持力，分解为正常/切向量，验 `N>=0` 及 `|Ft|<=μN`。正常力不允许黏着负值；实际可用摩擦按容量截断。若容量不足，所需量保留在 `node_required_stick_*` 和 `stick_feasibility`，实际可用接触力与残力另列，`accepted=false`。其他自由床接触仍取正常反力；不声称已重建所有材料点的粘滑历史。

`node_boundary_force_n` 和 `end_forces_on_cable_n` 包括离散端节点的半段湿重；与首段内部 `top_tension_n` 不是同一个量。向连续悬链线比较端反力时必须包含这一差别。

### 段内接触不是只检查节点

最后按所有 x/y 网格交叉点，把每条直缆段分为真实单元区间。双线性床沿直段的限制是二次函数；检查区间两端和二次极值即可取得段内真实最小 clearance。每个区间中点强制四角已知支持，跨 NoData 不通过。

此模型只在节点施加接触力，**未**给段内新增接触自由度。若节点虽平衡而直段穿床，返回 `STRAIGHT_SEGMENT_BED_INTERSECTION`、`accepted=false`，要求细化/改变明确材料网格；不会把全段投影回床面后冒充平衡。直段自碰撞、实体几何、埋设和土体接触没有实现。

### 输出和失败语义

两端固定顺序为船→锚；材料坐标从总自然长递减到 0。每节点公开湿重、内部/外部/边界/接触力、床高/法向、clearance、接触mask和三维净力残差。接触正常力是 N 标量，摩擦力是三维 N 向量，**不是**动态冲量。`segment_tension_n` 是由各段实际应变直接算的正张力。

无效边界、缺测、越界、互斥/未知字段、非有限值或提前计算超额抛 `ValueError`。实际优化不收敛、函数或段检查额度耗尽、摩擦容量不足、力残差失败、隐藏段内穿床或段塌缩，返回有限可序列化最佳结果及 `solver.rejection_codes`，但 `accepted=false`。`solver.converged` 表示优化器终止判据；没有自由节点时表示明确条件位置已给定，仍需独立力可行性验收，均不等于 `accepted`。

SLSQP 只寻找一个局部静止平衡候选；曲床可能有多个解，不证明全局最低能量、加载可达性或稳定性。未进行自动网格细化。默认 EA=1e6 不保证所有几何都能收敛；长尾缆/高 EA/大余长研究例可能明确拒绝，必须查看 actual residuals。

## 5. 可复现实测证据与限制

`tests/test_static_bathymetry.py` 覆盖平床闭式、正负/横坡×不可伸长或弹性、独立连续力 ODE 积分、90°旋转和实际移船基准、每格仿射/缺测/预算，以及第二层独立节点力重构、闭式离散坡床接触、摩擦可行/不可行和连续弧形网格收敛。

完整曲床合成例：x/y=`[-100,0,100]`，z=`−40+.1x+.05y+.0003xy`，船 `[0,0,0]`、锚 `[-60,-10,-46.32]`、自然长 80 m、18 节点、EA=1e4、w=4。实际 81 次优化迭代，4 接触节点，最大节点残力约 .002325866 N，小于 .01 N；直段最小 gap 为 0。几何、法向、接触/边界力旋转 90°误差在该测试中≤5.3e−11。只证明该明确合成例。

平面悬空例与第一层独立连续解比较，8/16/32 节点最大位置误差约 .082996/.017909/.004176 m，船端反力误差约 1.82216/.389655/.092980 N，随细化下降。不能把该均匀、无流、柔性缆案例外推到默认高 EA、动态冲击或任意海床。

特意构造的双线性 ridge 例中，全部节点满足静力平衡并在床上，但中间直段穿床 1.2 m；实际 guard 拒绝。这项否定验收说明投影、节点接触和优化器 success 均不足以证明真实缆段已合理贴床。

现 `simulate_lay` 的新鲜 2D 初态仍是水平触底悬链线再投影，保留 `slope_equilibrium=false` 与启动沉降告警。本模块尚未接入动态初态/checkpoint，不能把新的静力结果或可视帧当成完整可恢复动力状态。旧 `catenary/steady_state/span_analysis` 的二维 guard 与 `prepare_plan_voyage` 的平床/均匀初始区间限制保持。自动把变深规划航程映射为平衡初态、接续物料/边界、具有海流的真正准稳态和完整静摩擦历史仍需后续工作。

## 6. 主来源与独立推导

- [MakaiPlan Pro 官方功能](https://www.makai.com/cable-software/makaiplan-pro/)用于公开需求定位；不据其商业精度陈述宣称 OceanRoute 等效。
- [MoorPy 官方文档](https://moorpy.readthedocs.io/en/latest/index.html)说明准静态缆建模的参数和研究范围；[维护机构的 Catenary 源码](https://github.com/NatLabRockies/MoorPy/blob/main/moorpy/Catenary.py)使用自然长度、EA、湿重和床坡边界。本模块公式从 `dT/ds=-f` 与线性伸长独立推导，未调用或复制其求解器。
- [Orcina 官方 friction theory](https://www.orcina.com/webhelp/OrcaFlex/Content/html/Frictiontheory.htm)区分静态接触目标/历史与 Coulomb 容量。这里使用用户明确位置下的严格力可行性检查，没有实现其 shear stiffness、target return mapping 或原厂静态策略。
- [SciPy 官方 SLSQP 文档](https://docs.scipy.org/doc/scipy/reference/optimize.minimize-slsqp.html)用于数值实现接口；独立节点力和完整段几何检查是本模块额外验收，并非仅把其 success 当物理证明。
