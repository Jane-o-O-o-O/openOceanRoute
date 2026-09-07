# 稳恒水平海流中的真实定端受力平衡

2026-10-04。本模块实现独立、有限工作量的非保守离散静力研究，不声称采用 Makai 的私有算法或具有现场施工精度。本轮新增 `hydrodynamics.py`、`current_equilibrium.py` 和其专项用例；旧公开标量 `static_equilibrium` 的输入与势能求解合同没有改变。原发行包及其验收证据不构成本轮新算子的验证。

## 1. 共享流场与加载接口

`canonical_initial_fluid(config)` 从真实主配置解析流场；`validate_initial_fluid(raw)` 只接受完整、已支持的历史声明。两者返回下列规范结构：

```json
{
  "schema": "oceanroute.initial-fluid.v1",
  "operator": "node-secant-normal-cable-and-isotropic-body-drag-v1",
  "water_density_kg_m3": 1025.0,
  "current_m_s": [0.1, 0.12, 0.0],
  "current_profile": null,
  "depth_reference": "max(-model_z_m,0)",
  "profile_extrapolation": "hold_endpoints"
}
```

`current_profile` 也可为 2—500 行的完整 `{depth_m,x_m_s,y_m_s}` 列表，深度 0—12000 m 且严格递增。每一水平流速分量允许 ±20 m/s，垂向流速必须为零；密度沿用现材料合同的 1—2000 kg/m³，而非推定海水密度。主配置流表缺省的 x/y 分量规范化为 0；历史规范表必须完整。未知表字段、规范结构缺字段/未知字段/版本、非有限数和 bool 数值均拒绝。

有深度表时，实际 U 在 `max(-z,0)` 处逐分量线性插值，表外使用端值；恒流值保留为原始声明，但不叠加或取平均。z 是已对齐模型海面 0 的高度，标签不自动换算潮位或海图基准。所有向量使用同一局部 x 东/y 北/z 向上坐标；地理框架转换由上层完整转换几何和流向，核心不猜测。

`hydrodynamic_loads(p,v,local,fluid)` 返回五个 N×3 ndarray：

| 字段 | 单位和实际含义 |
|---|---|
| `node_current_m_s` | 每节点实际 U，m/s |
| `node_tangent` | 按节点索引差分归一的切向量 |
| `node_cable_drag_force_n` | 缆正常阻力，N |
| `node_body_drag_force_n` | 点实体节点各向同性阻力，N |
| `node_total_drag_force_n` | 上述阻力向量相加，N |

`local['drag']` 和 `local['body_drag']` 是真实材料/点份额加载的非负 N 数组，已含 rho/2，核心不再次乘密度。对已有点实体，系数先按自然材料份额 α 分入节点，再按各节点 U/v 算阻力；不是在显示实体平均位置算一股阻力再分配。有限 rod、姿态和独立实体接触未由本接口解决。

`HydrodynamicField(initial_fluid)` 严格验证并一次解析流表，`.loads(p,v,local)` 调用相同算子；`profile_rows` 给调用方记录一次解析工作。静力和新动力分支重复评估时复用实例，避免每次有限差分/内部步解析 500 行。规范输入在构造时独立复制。

```text
a_j = np.gradient(p, axis=0)_j
tau_j = a_j / |a_j|                 按索引，不按 rest 加权
u_j = U(p_j) - v_j
u_normal = u_j - (u_j·tau_j) tau_j
Dc_j = Kc_j |u_normal| u_normal
Db_j = Kb_j |u_j| u_j
```

切向长度 ≤1e−10 m 或不可有限归一的几何明确拒绝，即使该候选流速或系数为零。折返的中心切向不能被悄悄当成零向量、从而把缆拖曳改成各向同性。共流相对速度为零时两类阻力严格为零。水平流可产生竖直缆阻力，核心保留该分量。

## 2. 实际非保守静力核心

接口：

```python
solve_current_equilibrium(
    static,
    segment_ea_n=actual_harmonic_ea,
    node_wet_weight_n=actual_signed_nodal_wet_load,
    cable_drag_factor=actual_kc,
    body_drag_factor=actual_kb,
    initial_fluid=canonical_fluid,
    load_scale_n=absolute_material_and_point_load_scale,
)
```

`static` 使用既有定端原始配置字段：真实 `seabed_grid`、`vessel_position_m`、`anchor_position_m`、自然总长或逐段 `rest_lengths_m` 二选一、可选 `initial_positions_m` 及 bounded solver 字段。节点为 6—80，段自然长 ≥0.0001 m，总自然量 ≤1e6 m；EA 100—1e12 N。未知字段拒绝。只有 frictionless contact，无 prescribed_stick 历史；本静力配置 μ=0，不限制上层后续动态 μ≥0。

材料系数必须已通过共享自然材料积分和部署实体约束。本核心不自行平均材料、不改制造坐标/库存、不把已 accepted JSON 当作材料。静止惯性/附加质量不作为新静载；节点湿重可有符号。标量湿重/EA 字段仍作兼容语法检查，但实际数组决定力。

两个端点固定，反力包括端节点半段湿重与实际端节点拖曳。固定最老端可以离床，既不是自动投影触点，也不承受虚构床摩擦。主自然长包括全部活动库存，不能冒称仅悬垂长度。

每段无压缩 Hooke 张力及自由节点力为：

```text
T_i = EA_i max(d_i/rest_i - 1, 0)
F_j = F_axial,j - W_j e_z + Dc_j(p,0) + Db_j(p,0)
g_j = z_j - bed(x_j,y_j)
n_j = [-bed_x,-bed_y,1] / |[-bed_x,-bed_y,1]|
F_j + N_j n_j = 0
g_j >= 0, N_j >= 0, N_j*g_j = 0
R_endpoint = -F_endpoint
```

自由位置 3NF 与法向反力 NF 一起求解，共 4NF 个未知量。位置使用自然长度尺度 L，反力/受力使用 S。附加 Fischer–Burmeister 残差 `hypot(g/L,N/S)-g/L-N/S`，其零点等价于非负互补。SciPy 有界 trust-region least-squares 每次重新查询真实 p 的流速、索引切向、双线性床法向及全部力；不存在缆拖曳势能或冻结外载替代。

几何迭代限制在床格 x/y 和 −12000≤z≤0 域内。**中间反力迭代允许有符号**，由互补方程约束零根的非负性。若另把 N 强行放在 SciPy 的非负边界，零反力会被严格内部起点推离零、导致离床候选停滞。最终去掉数值级负反力，重新计算实际力/互补；显著原始负反力、离床非零反力或修改后的力失衡都拒绝。反力没有被作为真实负床支持接受。

位置列数值 Jacobian 的每次评估计费，反力列使用实际法向和 FB 的解析导数；有限差分会使用实际所在单元/流表一侧，没有假称床格分界处全局光滑。当前固定相对步长并不提供误差区间或任意高 EA 的收敛保证。切向退化、NoData 或域外试探会结束该局部求解并返回有限候选的明确拒绝；初始请求本身的非法床/种子/预算以 ValueError 拒绝。

默认使用原始种子，或从两端直线沿床高获得仅供迭代的可行数值猜测。**本实现没有调用无流子解**，返回 `initialization.auxiliary_no_current_solves=0`。精确种子仍经过真实受力评估和真实 Jacobian/优化，再独立验收；不是仅看 accepted 字段。复杂 slack 曲床从直线冷猜测可能耗尽默认 2000 次载荷评估，结果保持未 accepted，不暗增预算、不输出无流候选。更好的可行种子或用户显式更大限额可用于后续研究，但仍不保证任意可行输入的局部收敛。

## 3. 独立验收和返回证据

最终验收重新计算 Hooke 张力、实际缆/点阻力、完整床高/法向、法向支撑和端反力，不直接信任优化器缓存或 success。检查自由节点最大残力、全局外力和、实际非穿透、非负反力、`|N*g*n_z|` 和离床反力。每条直弦按经过的全部双线性单元检查精确二次 clearance 极值；节点全在床上方而弦穿床仍拒绝。NoData 单元不会填零或外推。

求解缩放的保守阻力上界为 `Umax² * (sum Kc + sum Kb)`。它必须有限且 ≤1e18 N，否则明确超出本数值力域；不把旧 2.2e10 N 的湿重尺度上限偷偷扩展为任意拖曳尺度。最终**物理力容限**使用实际 `absolute_wet_load + sum|Dc| + sum|Db|`，不是用保守上界放松实际验收；绝对/相对容限与既有静力范围相同。

返回与原内部静力核心兼容：`accepted`、完整节点/自然段/逐段 EA 和张力、`node_external_force_n`（重力加两类拖曳）、内部力/床支持/端支持/残力、床高/法向/clearance/mask、`segment_clearance`、`end_forces_on_cable_n`、`summary`、`solver`。另外公开完整 `initial_fluid`、五个水动力节点数组、实际 Kc/Kb。`summary.drag_potential_exists=false`，轴向弹性能和重力势能仅是诊断，**没有**把二者和称为完整流下势能目标。

`frames` 是兼容静力显示的单个 time=0 形状；首末张力仍是首末段张力，不能由此把最老固定端解释为触点。上层动态/初态负责真实 nullable touchdown 与 proof。该核心的 `accepted` 表示规定容限内的离散静止力平衡，不能解释为唯一解、稳定解、现场校准或原厂等效。

失败 `solver.rejection_codes` 分开标明：优化未收敛、实际计算额度耗尽、候选覆盖/切向非法、节点/全局力未收敛、接触互补未收敛、节点穿床、负法向、离床反力、完整弦交床/未知和塌缩。可显示有限失败形状，不能将其作为已平衡动态初态。

## 4. 工作量预检与实际计数

`estimate_current_equilibrium_work(static)` 返回严格相同的预检，不执行优化，字段为 `estimated_work_units,max_work_units,components,variables,max_solver_iterations,max_function_evaluations,max_segment_samples,work_basis`。默认 dense-Jacobian cap300、**包含所有有限差分的**载荷评估 cap2000、弦查询 cap20000、work200M，硬 cap 分别600/10000/200000/2B，不自行提高。

```text
variables = 4*(nodes-2)
estimate = grid_nodes
           + max_jacobians * variables^3
           + max_all_evaluations * (variables^2 + 32*nodes + 500)
           + 5*max_chord_samples + 40*nodes + 500
```

500 项额度覆盖一次最大规范流表解析，重复评估使用缓存表和实际逐节点插值。该预检在24节点/300Jac cap时会超过默认200M，需要用户显式降低上限或提高声明工作量，不能沿用旧3NF估计。

`solver.function_evaluations` 包含初始真实载荷及全部实际 FD 查询；`finite_difference_evaluations` 和 `jacobian_evaluations` 分开公开，并对实际调用计数门禁。SciPy 自身 `nfev` 不含数值 Jacobian 评估，故没有拿它当实际全部评估。精确缓存命中不重复收费。另有一次独立最终受力核验和有界完整弦核验；`actual_work_units/charged_work_units` 按同一归一化基准计数，失败弦检查保守收费其上限。

这些是声明的**归一化收费单位**，不是字面 FLOPs、token 或 CPU 硬时限；Python/JSON/CRS/上层重复解析不被此数字假称全面覆盖。共享材料解析、proof 独立复核、动态内部步和 voyage 总预算由调用方分别收费。本 core 自身最多80节点/10000格点/500表行、没有重复长实体ID元数据；上层仍须实施真实 standalone/batch/complete-response 字节边界。

## 5. 可执行实际主流程

下例从已有明确的合成异质初态输入启动，保留真实零长负湿重点实体，输入文件不被改写：

```python
import json
from oceanroute.hydrodynamics import canonical_initial_fluid
from oceanroute.initial_equilibrium import resolve_initial_equilibrium

config = json.load(open("examples/heterogeneous-initial-dynamic.json"))["config"]
config.update(water_density_kg_m3=1025.0, current_x_m_s=0.1, current_y_m_s=0.12)
config["inline_bodies"][0].update(drag_area_m2=0.02, drag_coefficient=1.4)
raw = config["initial_equilibrium"]
raw["schema"] = "oceanroute.dynamic.initial-equilibrium.v2"
raw["initial_fluid"] = canonical_initial_fluid(config)
prepared = resolve_initial_equilibrium({}, config)
assert prepared["provenance"]["verification"]["accepted"]
```

原始初态、provenance-v3、model-v5/schema4 和历史流恢复的上层字段/控制语义由相应新初态/动力说明给出。历史流证明不因未来合法流命令被重写；这里只定义共享历史流算子和静力求解，不能据本说明声称旧 v4 分裂已支持非零流固定点。

## 6. 实际专项证据与剩余边界

本文件完成时 `tests/test_current_equilibrium_core.py` **24项实际通过**。参考值由直接段牵引/Hooke/独立 secant 投影构造，不以生产静力输出当 expected。包括非零流精确形状、超过100N坏种子的真实优化、genuine xy 双线性床法向与侧流点实体的解析接触、增加正常缆拖曳后真实形状/法向支撑变化、水平90°旋转、非保守 Jacobian、深度剪切/端值保持/共流零阻力、缓存表独立复制、全弦ridge穿透拒绝、非法输入及实际 countergate 的有限失败输出。

核心24项与新初态/动力专项联合 **61项实际通过**。13节点原合成负点实体例在 force_tol=1e−6 N 时，恒流/剪切流分别用7/8个 Jacobian、240/275个全部载荷评估完成，最大自由节点残力均 <4e−11 N；没有放宽原容限。这些是具体研究模型数值证据，不是新发行首装/跨平台/实船验收，也不替代独立空间网格或连续缆参考。

完整目标仍有未解项：运动准稳态、有限 rod/旋转/力矩、EI/torsion、加载史静摩擦、段间及实体连续接触、三维空间/时间流场、流体加速度/波流、海面部分浸没、船舶六自由度、稳定性和现场校准。节点离散点载荷 fixed point 不证明连续集中力折角或床面工程误差已收敛。复杂曲床局部冷猜测失败是明确限制，不以“支持海流”标签掩盖。

算法公开依据：SciPy [least_squares官方文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html)说明有界最小二乘以及 nfev 与数值 Jacobian 调用分开；PETSc [TAO官方互补文档](https://web.cels.anl.gov/projects/petsc/vault/petsc-3.12.5/docs/tao_manual.pdf)给出 Fischer–Burmeister 方程。物理加载严格复用当前实际离散算子；这些通用算法来源不证明与原厂实现一致。
