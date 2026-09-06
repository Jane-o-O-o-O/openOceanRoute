# 异质材料与已部署点载荷的实际平衡初态

2026-10-04，0.7 开发扩展。本文描述当前新增实现；版本号及已冻结 0.1—0.6 发行物没有因本扩展改写。`HETEROGENEOUS_INITIAL_SCOPE.md` 是此前的建议文件，不能作为功能已实现或验收通过的证据。本模型为独立离散研究模型，没有 Makai 原厂算法、实船标定或现场工程等效证明。

## 1. 输入与物理边界

动态和预备入口仍使用原始 `initial_equilibrium.schema = oceanroute.dynamic.initial-equilibrium.v1`。船端、固定最老端、总自然长或各段自然长、可选初值及有界 solver 参数均未改变。物性仍来自主配置中的 `material_segments` 和 `inline_bodies`，不得在原始初态对象中注入逐段 EA、节点力或 `accepted`。预备与真正启动均实际求解，预备成功不代替后续启动验收。

初始自然材料区间可有不同的正湿重 w、有限正 EA、干质量、直径及阻力参数；**活动材料 EI 必须为零**。允许初始已部署 `length_m=0` 点载荷，湿重可为负，仍遵守原来的干质量/排水检查。已有任何部署份额的有限长度实体仍明确拒绝；尚未部署的不同材料、EI 或实体可留作以后真实动力加载。恒定海流和每条 `current_profile` 样本必须全零；初始波浪运动学仍不支持。初态节点速度为零，后续船动、升沉和放缆从真实首个内部步开始。

本轮没有解决有限尺寸实体接触、刚体转动、有限杆静力、EI/端力矩、初始流阻、移动铺设准稳态、海浪传播、土体变形或历史粘着。动态摩擦系数可非零，因本初态所需静态切向支持为零；不能据此推断加载历史。

## 2. 共享稳定材料积分

元素按船端→最老端排列，自然段长 ℓ 保持原样；制造原点 O 不移动，也不再次放出初始库存。材料坐标为 `q_j = O + sum(rest[j:])`，初始新增放缆为零，顶部细分目标仍取 `max(rest)`。

每段使用真实覆盖区间的局部积分：

```text
W_i = integral w(s) ds                  [N]
C_i = integral 1/EA(s) ds               [m/N]
EA_i = rest_i / C_i                     [N]
dry_i = integral mass_kg_m(s) ds        [kg]
added_i = integral rho*pi*d(s)^2/4 ds   [kg]
```

湿重、干质量及附加质量的 cable 节点分配仍为每个自然段总量的一半给两端。跨物性边界的元素并不改成连续重力一阶矩积分；保持这一离散算子，静力才与实际动力一致。逐段 EA 为串联柔度形成的调和值，绝不采用算术平均。

`_MaterialModel.loads` 的多行材料积分现在按局部区间重叠执行，不再由巨大前缀总量相减。内部区间和点载荷份额使用 **O 相对坐标**，显示制造 KP 才加 O，避免在短自然段积分前先舍入绝对 KP。保留输入边界浮点数本身的二进制值，没有补造测量精度。逐段覆盖、有限正柔度、正质量及实际局部 EA 范围均独立检查；数值范围无法可靠覆盖时拒绝。

这修复了真实冻结 0.6 wheel 的合法反例：O=2**26、五段各 1/1024 m，长前驱的 EA=100/湿重=20000，活动段 EA=1e12/湿重=1e-6。旧算子给出五段无穷 EA、湿重零和错误干质量；新算子恢复有限真实柔度、湿重 4.8828125e-9 N、干质量 4.8828125e-5 kg。另独立 Decimal 积分检验了 O=1e8、非二进制短段及跨边界/点站，不能只用坐标恰好精确的单例证明稳定性。

## 3. 点载荷与有符号势能

点站在元素 i 内时，按自然材料坐标生成 `alpha_i = (station-q_(i+1))/rest_i`、`alpha_(i+1)=1-alpha_i`。点位置为 `sum(alpha*p)`，节点湿重增加 `alpha*body.wet_weight_n`；干质量和实际等效惯性质量同样按既有材料份额分配。负湿重表示向上净浮力，不将等效质量乘重力冒充湿重。

静力势能与真实节点内力使用同一系数：

```text
T_i = EA_i * max(chord_i/rest_i - 1, 0)
E = sum[EA_i/(2*rest_i) * max(chord_i-rest_i,0)^2]
    + sum[node_wet_weight_j * z_j]
```

点 body 的 `B*z_body` 对节点位置的梯度恰为 `alpha*B`。这是当前直弦离散点载荷，不在段内新增独立折角、质量矩阵耦合项、接触或转动自由度。要研究连续集中力接点，应显式让材料站与节点对齐，再作细化对照。有限实体的未来动态分摊仍是其既有质量/湿重/拖曳模型，不意味着初态有限杆静力已实现。

缩放使用 `absolute_load_scale_n = sum(cable_element_weight) + sum(deployed_fraction*abs(body_weight))`。它不会把同节点的大正负载荷相互抵消后伪装成小加载。实际总湿重仍有符号；节点 cable/body 分项分别保存。

## 4. 静力核心与动态固定点

公开 `static_equilibrium(config)` 继续原标量 w/EA 合同，仍拒绝外部注入材料数组。新内部 `_static_equilibrium_core(config, *, segment_ea_n, node_wet_weight_n, load_scale_n)` 从共享材料算子接收真实逐段 EA 和有符号节点湿重，执行同一受约束势能优化、单边床面法向接触及独立力平衡验收。

实际二维双线性床面、全部直弦的逐单元 gap 极值、NoData/出界/穿床和塌缩检查仍保留；不把投影后几何当作力平衡。两固定端的反力包含端节点半段湿重及端点 body 载荷，与首末段张力不同。离床固定最老端不吸附到海底；无实际接触时 TD 和底张力仍为 null。

候选需优化收敛并通过真实节点残力、全局平衡、互补量、床面及材料复核，才可映射至动力。找到了局部候选不证明唯一、全局能量最低或加载可达性；更严格容限可能被实际残力拒绝。

映射继续 model-v4 / checkpoint schema3 / `implicit-compliant-material-nodes-equilibrium-prestress-v4`。每步对真实局部 EA、湿重和质量重构预应力，并同时施加 lambda 与质量加权位置修正；自由节点没有冻结。本轮没有引入新的可演化状态，也没有用重新分网格改变自然库存。

## 5. 证明 v2 与恢复

同 w/EA、无已部署 body 的新初态仍生成 provenance.v1。异质 w/EA 或已有点载荷生成 `oceanroute.dynamic.initial-equilibrium.provenance.v2`，来源明确为 `oceanroute.static_bathymetry._static_equilibrium_core`；原 v1 来源及物理限制保留，不能将扩展证明降级成 v1。

v2 在 `initialization.material_loading` 保存：

| 字段 | 含义 |
| --- | --- |
| `operator` | `natural-half-segment-cable-and-material-linear-point-load-v1` |
| `declarations_sha256` | 解析材料行、实体、O、密度及附加质量系数的完整声明摘要 |
| `segment_cable_wet_weight_n` | 每段 cable 湿重 N |
| `segment_compliance_m_n` | 每段串联柔度 m/N |
| `node_cable_wet_weight_n` / `node_body_wet_weight_n` | 每节点分项载荷 N |
| `signed_total_wet_weight_n` / `absolute_load_scale_n` | 有符号总湿重与抗抵消加载尺度 |
| `point_bodies` | 原解析点声明、`deployed_fraction` 与完整 `node_fractions` |

原 `initial_snapshot` 中位置、零速度、自然段、制造站、干/等效质量、总湿重、逐段 EA/张力、实际法向支持、端反力和残力仍全部保存。未来未部署有限 body 由完整主配置及声明摘要保留；它不列为已解点载荷。

恢复不再优化；独立从完整冻结配置重算原始材料、点份额、初态力/床面证据，以及**每个时刻的当前**自然段与全部 10 项材料载荷数组。公开 `read_checkpoint` 本身就拒绝重签摘要后的假当前湿重/质量/EA/拖曳或制造站。制造 q 使用绝对 1e-8 m 容差，不能因 O=1e8 而相对放宽到厘米。

真实冻结 0.6 wheel 生成的正常 v1 断点仍读入和续算。稳定积分造成的正常浮点差异在 v1 实数证明中按 rel=1e-9 / abs=1e-8 有界复核；结构、标签和计数保持严格，实际力/床面验收仍重新执行。错误残力报告及物理失配不会因兼容而获得接受。公开摘要/checksum用于一致性检查，不是可信签名或工程认证。

## 6. 工作额与字节容量

初始静力、独立映射和恢复验证仍分别计账；主动态 12M 不暗中放大，初始化默认 200M/最高 2B 的声明额度保留。初始化新分项含有界 N×material 与 N×body 运算；有放缆时动态局部重叠积分也按多行材料数计入。每次公共 reader 的当前材料核验追加明确 `current_material_verification_work_units`。这些为归一化计费单位，不是 FLOPs、CPU 时间、token 价格或固定性能保证。

独立断点上限 2 MB，保存断点批次 16 MB。本轮预检按实际 UTF-8/JSON 转义的点元数据计入 v2 重复证明，不将长实体标识视为固定成本。完整动态响应另有 64 MB 保守预检与返回前有限 JSON 实际字节检查，计入逐节点、帧、body 标识、重复 proof 与 checkpoints。相关上界由 `solver.json_volume_bounds` 返回；超限在昂贵初始化前拒绝。没有靠人为缩短允许 ID 来掩盖重复字节。

## 7. 验证范围

自有测试 `test_heterogeneous_material_core.py` 覆盖共享积分、独立异质自由链平衡、半段端载、signed 势能梯度、实际多步固定点/运动/JSON续算、重签证明/当前载荷、v1 真旧包兼容与预算/字节预检。独立审查 `test_heterogeneous_initial_review.py` 包括 Decimal 大 O 与短段积分、分项/点份额、真正坏初值优化、真实平床和 xy 双线性床接触、旋转、公共 HTTP/耐久恢复及错误状态。规划桥接与连续模型细化另由独立任务报告。

这些验证证明所声明离散方程的一致性及受限数值行为，不能从同网格固定点推导连续点接头、海底材料、全航程或现场张力精度。当前发行安装、跨平台和生产浏览器证据应分别查实际发行记录，不沿用 0.6 门禁为本扩展背书。
