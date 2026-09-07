# 稳恒海流下真实定端初态：下一阶段范围与验收提案

> 历史提案：下文保留实施前的状态。0.7 的实际实现与限制以 [海流核心说明](CURRENT_EQUILIBRIUM_CORE_NOTES.md)、[初始化合同](CURRENT_INITIALIZATION_NOTES.md) 和本版发行记录为准；提案本身不是通过证据。

2026-10-04。本文只读当前源码后提出下一阶段计划，**没有实施此功能，没有新增通过证据，也不是原厂精度说明**。本轮只新增本文，不修改运行源、既有 38 项独立用例、其他测试、主手册、PDF、版本或冻结发行物。当前异质材料/零长度点实体初态已经使用真实材料加载和完整双线性床面；当前 fresh 初态仍要求初始恒流及深度流表全零。v4 的后续海流指令允许变化，不等于已有非零流平衡初态。

目标是固定两个真实端点、每段自然量固定、初始节点速度为零，在声明的稳恒海流下求解当前离散缆/点实体受力平衡，并让真实动力首步保持该平衡。不能以平均阻力、平床替换、无流形状加冷起 settling 或冻结自由节点代替它。

## 1. 真实动态加载算子

节点顺序仍为船端到固定最老端。自然段长 ℓ、制造原点 O 和材料站 `q_j=O+sum(rest[j:])` 不改变。沿用当前共享 O 相对区间积分、串联柔度 `C_i=integral(1/EA) ds`、逐段 `EA_i=ℓ_i/C_i`、cable 半总量节点分配及 point 的自然材料份额 α。初始库存仍是 sum(rest)，新放缆为零；不能先重划网格以消除材料/点位置问题。

`simulation.py` 当前在每个内部步使用如下算子。这里只写其真实离散意义，不赋予额外连续实体意义。

```text
Kc_j = (rho/2) * H_j(integral Cd(s)*diameter(s) ds)  [kg/m]
Kb_j = (rho/2) * sum_b(alpha_b,j*Cd_b*area_b)         [kg/m]
a_j  = np.gradient(positions, axis=0)_j
tau_j = a_j / max(norm(a_j), 1e-12)
P_j = I - tau_j*tau_j^T
u_j = U(p_j) - v_j
Dc_j = Kc_j * norm(P_j*u_j) * (P_j*u_j)              [N]
Db_j = Kb_j * norm(u_j) * u_j                        [N]
```

其中 H 是现有每自然元素总量各一半给两端的算子。`np.gradient` 按节点索引计算：自由节点的未归一切向量是 `(p_(j+1)-p_(j-1))/2`，端点用单侧差分。它不是按自然段长加权的连续切线；非均匀 rest 的静力和动力必须采用同一选择，不能在新初态中悄悄替换。

主流速为局部模型 x/y 方向的 `current_x_m_s/current_y_m_s`，垂向流速为零。有 `current_profile` 时，实际流速改用该表在 `max(-z,0)` 上的逐分量线性插值，端深之外按现有端值保持；主恒流值不再叠加。表是稳恒深度剖面，不是二维/三维观测流场，datum 标签不转换潮位或水深。主配置每个水平分量沿用当前 ±20 m/s、深度表 2—500 行的明确输入边界。均匀水平流也可能产生竖直 cable 拖曳分量，因为 P 随缆向变化；不能把 `Dc_z` 强制归零。

点 body 的**当前拖曳**是按 α 将阻力系数放入节点，再使用各节点的流速/速度进行上述 isotropic Db 计算。它不先在 `p_body=sum(alpha*p)` 处求流速或完整 body 速度，再分配一股实体阻力。显示点位置、节点质量 lumping、body 虚功重力及此节点拖曳近似应分别解释；不能把它当作已有独立点接点/转动/接触模型。

初态 v=0 时取 `D_j=Dc_j(p,U,0)+Db_j(p,U,0)`。惯性质量与附加质量不构成额外静载；湿重仍使用有符号实际节点值，负 body 湿重不取绝对值加入受力。

## 2. 非保守静力与完整床面

保留无压缩 Hooke 张力：

```text
d_i = norm(p_(i+1)-p_i)
T_i = max(d_i-ℓ_i,0)/C_i
F_j = F_axial,j - W_j*e_z + Dc_j(p,U,0) + Db_j(p,U,0)
g_j = z_j - bed(x_j,y_j)
n_j = [-bed_x,-bed_y,1] / norm([-bed_x,-bed_y,1])
F_j + N_j*n_j = 0             (自由节点)
g_j >= 0, N_j >= 0, N_j*g_j = 0
R_endpoint = -F_endpoint      (固定端支持，不另算床支持)
```

两端反力包含端半 cable 湿重、端点 point 份额以及端点实际水动力，不等于首末段张力。固定最老端可离床，不吸附为触点；没有真实接触则 TD 仍为 null。初始只支持法向接触，切向支持为零，因此允许后续动态 μ>=0，但不能推断已有粘着历史。

不能向旧势能简单加一个所谓“拖曳势能”。当前 cable 拖曳随邻节点定义的方向变化，深度流又随位置变化，力 Jacobian 一般不对称。例如竖直直链受到水平恒流时，自由节点 j 的竖向 cable 拖曳对 `p_(j+1),x` 有非零一阶导数，而邻节点的横向拖曳对 `p_j,z` 对应导数为零；不存在通常的势能 Hessian 对称性。深度变化的 body 横向阻力同样可有 `d(Db_x)/dz != d(Db_z)/dx`。把旧能量最小化器中的一个冻结外载解当最终结果不成立。

建议独立非线性力/接触核心，使用有界 least-squares/半光滑互补或明确活动集求解。每次候选评估都从实际 p 更新流速、节点切向、完整床法向及受力。可用无流解和流速逐步延续作 numerical seed，但每个子问题收费、每个最终结果必须复核全部非线性载荷；延续失败不能返回无流候选。流速延续不是 loading-history 摩擦，也不证明解唯一、稳定或全局最优。

一种可执行的最小残差选择是：自由位置 3NF 维、未知法向力 NF 维，总 4NF 个未知量。令 `a=g/L_scale`、`b=N/S_scale`，加入 `phi(a,b)=sqrt(a*a+b*b)-a-b`，同时求 `F+N*n=0`。力与互补量必须按各自单位缩放；不要把 SLSQP gap 的乘子直接称 N。最终仍独立检查力残差、非负法向力、gap、`N*g*n_z`、端反力与全局受力平衡，不能仅以 least-squares success 或小目标值标 accepted。

**完整弦检查保留。** 节点在床上不代表弦不穿床：最终每条直弦仍须按其经过的全部双线性单元检查二次 gap 极值。NoData、超出床格、塌缩、不可解析切向或不满足预算均明确失败，不填零、不外推、不 flatten。普通床格分界处的法向以及深度表 knots 只有分段光滑性；数值 Jacobian、活动集更新和验收必须使用实际同一侧算子，不能假定全域光滑。

建议明确拒绝近折返导致 `norm(np.gradient(p))` 无法可靠归一的候选，而不是以零切向暗中改成 isotropic cable drag。该阈值和失败原因须公开；一般折返、段间碰撞和完整 rod 仍是后续目标。海面 z=0 只是本模型输入域，不是可凭空提供向下反力的障碍；若候选需要该反力，按真实受力失败处理。

## 3. 受力 Jacobian 的可审核路径

材料、自然段长、α 和 Kc/Kb 在本次定端求解中固定，不能每次函数评估重新平均材料。对于非退化切向和非零正常相对流，令 `u=P*U`：

```text
delta_tau = P * delta_a / norm(a)
delta_u = P*delta_U - (delta_tau*tau^T + tau*delta_tau^T)*U
delta_Dc = Kc * [norm(u)*I + u*u^T/norm(u)] * delta_u
```

当 u=0 时，`norm(u)*u` 的一阶导数为零，不使用含 0/0 的表达式。Db 对 U 的导数为对应 isotropic 形式。流表在节点深度处的实际斜率参与 delta_U；床法向随真实双线性梯度变化。张力开启/关闭、流表 knots、床单元分界和接触切换分别处理，不把非对称水动力 Jacobian 替换成某个能量 Hessian。

可先使用有界数值 Jacobian实现，但必须计入每列的实际函数评估、局部床/流查询及 N×M/N×B 预处理。独立有限差分校核应包含端/自由节点切向、异质 Kc、深度流、非节点 body α，不能只验均匀直缆。

## 4. v4 首步不能直接复用

当前 v4 内部步先做 cable 半隐式拖曳速度 kick，再以已更新速度做 body kick，然后加湿重；随后给位置添加轴力与只含轴力/湿重的法向 prestress kick。它适合当前明确的无流初态 fixed point，不能通过仅取消 zero-current guard 自动适配流下平衡。

单独看静止节点的 cable 步：`a=Kc*norm(P*U)` 时，

```text
delta_v_c = (h*a/(M+h*a)) * (P*U)
M*delta_v_c/h != Kc*norm(P*U)*(P*U)   (有限 h、非零 a)
```

body kick 还使用 cable kick 后的速度。因此物理等式 `F_axial-W+D+N*n=0` 并不抵消现有 predictor；旧法向 warmstart 本身又遗漏 D。由此出现的冷起漂移不是“初态已平衡”的证据，不能靠加大阻尼或锁点掩盖。

建议为新分支实现能保持真实平衡的 combined-force predictor，再保留正确累计轴向/接触乘子及其位置意义。一个可推导的局部半隐式方案，在旧 p/v 上取

```text
A_c = Kc*norm(P*(U-v_old));  A_b = Kb*norm(U-v_old)
B = A_c*P + A_b*I
(M*I+h*B)*v_star = M*v_old + h*(F_axial_old-W*e_z+N_old*n+B*U)
p_predict = p_old+h*v_star
```

v_old=0、完整旧受力平衡时右边为零，故 v_star=0。`M*I+h*B` 对正质量为正定。零阻力时退化为原明确的质量/力预测，且 v_old=U、其余力为零时保持共流速度，不引入虚构拖曳。它是待实现/细化验收的分裂离散方案，**不是已经完成的 fully implicit 流固求解**。

若 predictor 已纳入旧轴力/法向支持，后续 warm multipliers `lambda_axial=-h²*T_old` 和接触 `h²*N_old` 只承担力增量/几何约束，不能再把相同旧支持 kick 一次。使用质量逆还是阻尼矩阵逆参与投影必须明确定义；后者涉及逐节点 3×3 方向质量块，不能仍假称标量 XPBD。无论选择哪一种，都应推导实际步的受力/乘子关系，并在独立时间细化与共流测试中验收。

后续真实船动、升沉、放缆、材料切换和 current override 要重算当步载荷/支持，不从初始 proof 复制永久反力，不冻结自由节点。单次变化可以产生真实瞬态；初态静止不是全时刻准稳态。

## 5. 建议输入、版本与恢复合同

以下是**尚未注册的建议合同**。保持原有 `/api/simulation/prepare-equilibrium-initial`、`/api/simulation/dynamic` 的 payload `{project,config}` 外壳；不在本文虚构现有可运行的流初态 endpoint。建议用 raw `oceanroute.dynamic.initial-equilibrium.v2` 明确启用新分支，继承旧真实端点、natural/rest 二选一、可选 seed 和 bounded solver；主配置继续拥有全部 material_segments、inline_bodies、rho 与拖曳参数。

新增声明 `initial_fluid` 保存规范化完整初始流配置：水平恒流或明确深度表、对应模型坐标/深度约定、端值保持政策及算子版本。fresh 必须与主配置实际使用的流完全一致；不得静默选一套数据。它可以由预备接口从主配置解析后形成可审查 raw 候选，但启动仍重新解析/求解，不信任 accepted 结果。

建议新 `provenance.v3` 在 v2 材料证据之外保存：

- 冻结初始流声明与摘要；初始逐节点 U、切向、Kc/Kb、cable/body 拖曳向量。
- 原完整零速快照；实际节点外力、法向力、固定端反力、力/互补/弦验收及 solver 收费。
- 真实载荷算子和新 predictor 标识；仍 paid=0、target=max(rest)、相同 O/自然库存，时间零 contact impulse=0。

**建议 model-v5/checkpoint schema4。** 当前 proposed predictor 改变有限 h 的积分/分裂规则，不能只增加 proof 标签仍称 v4。schema4 明确新 scheme 与初始流历史；schema1/2/3 和 v1/v2 proof 继续原解析/步进。旧 v4 checkpoint 后续可能已有非零 current，不能仅据 current 非零自动改成 v5。新显式分支在零流时也用自身 scheme 作回归；用户未启用新分支的零流路径保持 v4 及原兼容合同。版本最终命名由后续实现确定，本文没有升版本。

Resume 从冻结 raw 初始流和原 snapshot 复核**历史初态**，不以当前主配置的未来 current 值重新计算旧 proof。当前主流/流表可以通过既有合法 control override 改变，必须保存新控制有效时刻并照常产生真实瞬态；不会改写最初流场、重新求静力或再次投初始库存。该 control 包括在 t=0 选择新未来指令的分支，历史快照与即将施加的指令须分开标识。

原初态声明随 `initial_equilibrium` 冻结；读取须绑定原几何/材料/床面/初始流与 accepted 证据。所有时刻的 current material evidence 继续按实际 rest 和完整声明重算。制造 q 用绝对容差，不能由大 O 放宽。故意重签 checksum 后的流、阻力系数、份额或外力篡改仍须拒绝；完整自洽新构造不是安全签名认证，不能声称防恶意认证。原 schema3 的真实 0.6 wheel JSON 应继续由原分支无 optimizer 续算。

地理计划桥接须保留同一度量床面/船锚/seed frame、制造 `top-L` 来源和真实水动力坐标；初始固定锚不是规划触点。未来支持流初态时需更新 mapping validator 对新 scheme/proof 的分派与 raw 初始流绑定，不能只取消当前均匀/零流守卫。

## 6. 明确的工作量与容量预算

保留当前主动态 12M/次、静力 default200M/hard2B、voyage 单独总预算，以及节点/材料/点数和 JSON 限额。新非线性核心的变量最多 `4*(N-2)`，不能沿用旧 `3*(N-2)` 能量优化器工作估计。有限差分的每个 Jacobian 列、line-search、continuation/active-set 子解以及独立 final 重验都计入声明和实际计数；额度不足明确失败，不用无流候选或减少材料行作 fallback。

物料系数/α 解析一次，核计 N×M 与 N×B；深度表解析一次，但每次 p 评估重算实际 U。按实际调用计流查询、节点切向、水动力/法向/残力/Jacobian、dense 解和完整弦预算。Fresh、resume proof、current-state 复核与 voyage 的首块 reservation/后块 countergate 要同步；初始化只收费一次且每块不重新优化。

力缩放不能仅用净湿重。建议 `S_F = S_weight + sum(norm(Dc_j)) + sum(norm(Db_j))`，并可用声明 `U_max²*(sum(Kc)+sum(Kb))` 给独立保守上界。不得平均流、丢竖分量或让相抵载荷隐藏尺度。已有 weight-only 内核的 `2.2e10 N` 载荷尺度上界不自动覆盖所有当前允许的高 rho/area/current 拖曳组合：后续必须明确有限新上界/数值域，或公开拒绝无法可靠处理的组合；不能静默 clamp。

新 snapshot 的逐节点 U/拖曳/接触信息和冻结流表须进入实际 UTF-8/转义 metadata 预估，求解前检查 standalone2MB、batch16MB、complete-response64MB，再保留最终有限 JSON 保险。不能只按节点数或固定长度 body ID 估计；新节点元数据还会在帧、proof、checkpoint 和计划映射中重复。归一化 work 不是 FLOPs、token、CPU 硬时限；JSON/CRS/框架重复校核也不能被 solver 计数冒称全面覆盖。

## 7. 下一阶段独立验收计划

这些是建议用例，**尚未执行，不能计入现 38 项或本轮全门禁**。

1. **算子/Jacobian。** 用独立 Decimal 区间与自然站构造异质 Kc/Kb/α，核对真实 normal cable/isotropic point 拖曳；做恒流与深度剪切流、非均匀 rest、水平旋转及方向有限差分。检查 cable 竖拖曳非零、切向流 cable drag 为零、共流相对速度为零，不把 body 显示位置当唯一水动力点。
2. **独立离床平衡。** 给定自然量和端点用另一路积分/节点力残差构造参考；可以用竖直链和沿独立法向的恒流作解析分量例，但不能使用被测加载/helper产生 expected。真实非零流、异质材料、signed point 需在每自由节点闭合轴力/湿重/两类拖曳，端支持包括端拖曳。无接触 TD=null。
3. **完整曲床接触。** 用 genuine xy-bilinear 床与侧向流得到实际接触，独立逐节点法向及逐单元弦二次极值，核对 N>=0、Ft=0、互补/全局力与端支持。NoData、穿弦、上浮需要未声明海面反力、切向退化及不收敛分别拒绝。
4. **真正首步与细化。** 在 h 多级和迭代多级实际运行固定命令的非零流零速初态；检查位置、速度、Hooke 张力、法向支持、材料/库存。验收应足以抓到旧 sequential kick 的有限 h 漂移，不能允许以 settling 后结果过关。真实船动/放缆/流速改变必须使自由节点响应；不能锁点。共流、zero-flow 与零阻力分支另作基本一致性检查。
5. **独立连续细化。** 恒流场的无接触连续缆，沿自然坐标可用 `p'=F/T*(1+T/EA)`、`F'=w*e_z-drag_per_natural_m`（坐标自最老端向船）积分并在点站作真实载荷跳变；具体符号由该方向独立核对。比较共同牵引/自然库存的网格序列与真正共同定端 BVP 两类证据，不能混称。同一网格 fixed point 不证明连续 point kink/海床工程精度。
6. **恢复与权威性。** 真实 JSON/HTTP/后台 owner close-reopen-resume 对照 uninterrupted；确认优化只首块执行、没有重复 payout。用真 schema3/v4 旧 JSON 验旧分派；新 proof 原流不被 future-current override 改写。重签后篡改原流表、当前控制时刻、拖曳参数/向量、α、q、法向支持、scheme/downgrade 及原预算，按实际一致性拒绝。
7. **资源边界。** 对真实 N×M/N×B、数值 Jacobian 多列、多次流延续和重复 metadata 输入，证明工作量/字节检查在昂贵解前，实际 countergate 生效。失败整笔不产生可应用的 balanced candidate，不自动抬库存、清约束或 fallback 成无流。

## 8. 仍保留完整目标

本提案限于稳恒水平恒流/明确深度剖面、固定端零速、真实异质自然材料和零长度 point、完整已知双线性床。有限 rod/姿态/转动、EI/端力矩、段间和实体连续接触、摩擦加载历史、运动准稳态、波浪流体加速度/空间波流、海面部分浸没、船舶六自由度与现场标定仍未解决；不借原厂手册标题宣称算法等效。

受力收敛不证明平衡稳定或唯一。非保守 follower 荷载的局部静止候选还需要扰动、时间/网格细化和独立现场比较，才可能评价施工适用性；本提案不会把 accepted 直接解释成工程认证。

本地依据：[simulation.py](../oceanroute/simulation.py)、[initial_equilibrium.py](../oceanroute/initial_equilibrium.py)、[checkpoints.py](../oceanroute/checkpoints.py)、[static_bathymetry.py](../oceanroute/static_bathymetry.py)、[voyage.py](../oceanroute/voyage.py)、[当前异质初态说明](HETEROGENEOUS_MATERIAL_CORE_NOTES.md)、[此前物料扩展推导](HETEROGENEOUS_INITIAL_SCOPE.md)、[真实初态映射说明](INITIAL_EQUILIBRIUM_NOTES.md)。本次没有联网推断原厂私有算法，没有新代码执行验收或发布承诺。
