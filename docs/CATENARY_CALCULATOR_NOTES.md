# 四边界悬链线 Calculator 研究实现

开发模块为 `oceanroute/catenary_calculator.py`，公开函数 `calculate_catenary(config)`；API 为 `POST /api/simulation/catenary-calculator`，仅接受 `{ "config": {...} }`。它在冻结 0.4 发行包之后独立新增，未修改 `static_bathymetry.slope_catenary`、旧动态初始化或历史发行资产。全部结果仍标记 `validation_status="research"`。

提供的 MakaiPlan 6.2 手册物理第 250 页／M242 描述 Calculator 使用水深、海床坡度，加底部总张力、顶部总张力、顶角或入水长度之一。这里实现这四种**明确物理语义**的独立边界；手册没有明确有限弹性下入水长度是自然长还是伸长弧长，因此要求用户选择，不猜测原厂定义。公开需求也来自 [MakaiPlan 官方说明](https://www.makai.com/cable-software/makaiplan/)。本模块没有原厂算法、专有文件兼容、稳定性或海试精度证明。

## 1. 充分边界与坐标

只求均匀、正湿重、柔性缆在静水中的悬空段；触点切线沿床面，水中重量按每自然材料米给出。EA 为有限正值时采用线性轴向伸长，显式 `null` 表示不可伸长。没有海流、弯曲、扭转、附属体、海底尾缆、摩擦历史或施工运动。

必须输入完整 `oceanroute.bathymetry.v1` 网格。所有节点参加仿射平面检查；任何 NoData、非仿射场、缺来源或非米制坐标都拒绝，不能以四角拟合、平床、补零或投影代替。详见 [BATHYMETRY_NOTES.md](BATHYMETRY_NOTES.md)。局部 x 东、y 北，米；z 向上，以模型海面 0 为基准。来源的 `vertical_datum` 是声明，不自动转换潮位或海图基准；调用方必须已完成明确对齐。

船端实际位置给定且 z≤0，需至少高于其已知海床 .001 m。`heading_deg` 是触点向船的**水平**航向，0 北、90 东。顶角是同方向切线相对水平的有符号角；允许有效的负顶角，例如船端在海面下且床面更低的明确模型。底张力 B 是总幅值，不是水平 H。

## 2. 精确请求合同

共同字段如下；未知字段及同时混入旧 `bottom_tension_n/depth_m` 等第二边界会 HTTP 422。

| 字段 | 默认、范围与含义 |
|---|---|
| `seabed_grid` | 必填；完整已知的仿射床场 |
| `vessel_position_m` | `[0,0,0]`，有限局部米坐标，z≤0 |
| `heading_deg` | 90，−36000..36000 |
| `wet_weight_n_m` | 4，1e−6..20000；每自然材料米 |
| `ea_n` | 1e8，100..1e12，或显式 null |
| `nodes` | 64，3..1000；显示点按自然长取样，不能改变长度定义 |
| `plane_tolerance_m` | 1e−7，1e−10..1e−5；全格节点绝对仿射拟合容限 |
| `max_natural_length_m` | 1e6，.001..1e6；根的可选自然长度域 |
| `root_policy` | 默认 `require_unique`；另有下节三种策略 |
| `max_root_iterations` | 100，1..200；每个括号根的上限 |
| `max_function_evaluations` | 1200，1..20000；全部逆边界与极值/有限域求解共用额度 |
| `max_work_units` | 1000000，1..5000000；预检和实际计账的归一化额度，非 FLOPs |
| `max_output_bytes` | 8388608，1..33554432；全部候选和选中结果的 JSON 额度 |

唯一必填 `boundary` 只能是以下之一，字段必须恰好匹配：

```json
{"kind":"bottom_tension","value_n":34000}
{"kind":"top_tension","value_n":68000}
{"kind":"top_angle","value_deg":60,"reference":"horizontal","direction":"touchdown_to_vessel"}
{"kind":"cable_in_water","value_m":3464.1016151377544,"length_basis":"natural"}
{"kind":"cable_in_water","value_m":3464.1016151377544,"length_basis":"stretched_arc"}
```

底张力范围 `[1e−6,1e9] N`，0 极限不支持；顶部总张力 `[1e−6,1e12] N`，并不保证这一范围内每个值都可达。长度 `.001..1e6 m`。顶角严格位于 `(−90,90)`，并高于床面切角；没有额外人为角度 epsilon。极小但合法角先在有限 B 域验可达性，数值不可解析时拒绝，而不发生除零或宣称无限张力根可用。

## 3. 连续方程与完整根枚举

床面沿指定航向的坡度为 m，船端相对正下方床面的高度为 D。令自然材料坐标由触点向船增加：

```text
c0 = sqrt(1+m²), H = B/c0, V0 = mH
V(s)=V0+ws, T(s)=sqrt(H²+V(s)²)
dx/ds = H/T + H/EA
dz/ds = V/T + V/EA

r=Vtop/H, u=r-m>0, c=sqrt(1+r²)
F=c-c0-m[asinh(r)-asinh(m)]
I=integral_m^r sqrt(1+v²) dv
S=Hu/w
D=HF/w + H²u²/(2wEA)
L_stretched=S + H²I/(wEA)
```

不可伸长时所有 `1/EA` 项为零。伸长弧长使用连续弹性积分；既不是自然长度 S，也不是显示节点连接形成的弦长。小 u 使用 12 点 Gauss 积分，其他范围使用稳定原函数差；使用 u 而非直接求 `r−m` 的小差参数，避免抹掉近切向输入。

给 B 时正常上升量关于 S 严格增加。实际有限括号采用 `S_upper=D+sqrt(D²+2DB/w)`：由 `T(s)≤B+ws` 得上升量至少 `wS²/[2(B+wS)]`，故无需扩张区间。给顶角时 r 固定，D 关于 H 严格增加，只在 H 对应的有限 B 域内求根。

给自然长 S 时，B 对 u 严格下降；`D=S*F/u+wS²/(2EA)` 的导数符号为 `Q=u²/c−F`，且 `Q'=u(1+mr)/c³`。m≥0 时严格单调，m<0 时恰有一个最大值，故有 0、1 或 2 个数学根。

给顶部总张力 T 时，`H=T/c`，D 关于 r 在 m<0 时亦单峰。B 域可把 r 区间分成两部分，必须保留 **r<0** 部分，不能只取正平方根。给有限 EA 的伸长弧长 L 时，`H=2wL/[u+sqrt(u²+4IwL/EA)]`，H 对 u 严格下降，因此两端 B 可各自映成有限 u 括号。真实高度导数符号为

```text
J=u*c−2I
R=Q−(H/EA)*u*J/c.
```

m≥0 时单调；m<0 时恰有一峰。该结论不假定小应变，也不假定 Q、J 零点的先后顺序。完整独立证明见 [CATENARY_INDEPENDENT_REVIEW.md](CATENARY_INDEPENDENT_REVIEW.md)。按已证明的单调分支逐段括号求根，不扫 log(B) 猜根，也不把无穷远有限高度极限当成有限根。

### 数值临界情形

内峰处 `|height residual|≤64eps*max(1,D)` 时，输入的舍入和极值计算不足以证明这是 0 根、一个真重根还是两个非常接近的根。因此返回 `root_enumeration_complete=false`、`accepted=false`、`selected=null`，公开峰参数、残差、分辨容限和可能根数 `[0,1,2]`。此实现**不认证恰在临界峰值的真重根**，也不把邻近双根合并为一根。峰下 1e−9 m 的实际测试仍保留两个不同根。有限端点恰为浮点根时显式处理；非零残差在分辨容限内的端点亦报告数值未闭合，不凭容差宣称数学域内唯一根。

声明的是有限 B 域，不能用上述单峰理论推断数值求解未完成时的实际根数。迭代或全部函数额度耗尽、未解析有限端点等都不得选取已经找到的部分解作为成功结果。

## 4. 数学根、物理候选与选择

先枚举整个 `B∈[1e−6,1e9] N` 内的根；再对自然长超 `max_natural_length_m` 的根标记域外排除，保留其真实理论值。每个可选域内根调用**未改动**的 `slope_catenary`，用原始完整网格验船、触点、全部缆投影覆盖、床高、切向、穿透、整体力平衡，以及与请求边界的残差。没有床下投影或外推。

节点顺序仍为船→触点，材料坐标 S→0。缆上的端力为船端 `+T(S)`、触点 `−T(0)`；与湿重 `(0,0,−wS)` 相加应为零。连续 z(s) 是凸函数；两端不高于模型海面时整个悬空段也不高于海面。完整已知仿射场和单调水平坐标保证端点之间的床场覆盖。拟合与原网格的小差、数值容限、线性应变告警仍由完整 forward 结果报告。

选择策略严格如下：

- `require_unique`：可选 B/自然长数学域内恰一根，且该根通过原网格/力/边界验收，才接受。两个数学根中只有一个有实际覆盖，仍不默认选它。
- `enumerate`：仅枚举，不选择；顶层始终 `accepted=false`，即使仅有一个可用根。
- `lowest_bottom_tension` / `highest_bottom_tension`：明确选择可选数学域中 B 最低/最高根；若该根覆盖不足或验收失败，不回退到另一根。

完整响应的关键结构：

```text
accepted, validation_status="research", boundary, root_policy, assumptions, warnings
candidates[]:
  root_id, bottom_tension_n, natural_length_m, within_declared_domain, accepted
  theory:{B/H/T, natural_length_m, stretched_arc_length_m,
          top_angle_from_horizontal_deg, layback_m,
          verification_status:"mathematical_boundary_only"}
  boundary_residual:{actual,target,residual,units,tolerance} | null
  result:完整 slope_catenary 返回值 | null
  rejection_codes[], messages[]
selected:null | {root_id,candidate_index,bottom_tension_n,result:完整 forward}
summary:
  total_bottom_interval_root_count, mathematical_root_count,
  found_bottom_interval_root_count, excluded_natural_length_count,
  usable_root_count, selected_root_id
solver:
  root_enumeration_complete, stop_reason, failure, failure_diagnostic,
  同上根计数、有限分支参数/切点、逆解及forward次数、工作/输出预算
```

这里 theory 的实际键为 `bottom_tension_n/horizontal_tension_n/top_tension_n`，不是字面 B/H/T。覆盖失败的候选仍保留这些连续理论值；它们没有可用几何含义。`result` 即使存在也可能因逆边界验收失败而不可用，必须看候选 `accepted`。三维可用结果只取 `selected.result`，不能取第一个候选冒充已选解。

`total_bottom_interval_root_count` 是完整 B 域根数；`mathematical_root_count` 是再受自然长 cap 限制的可选数学域根数；超 cap 数量另列。枚举未闭合时前两计数为 **null／未知**，`found_bottom_interval_root_count` 仅是已公开候选数，不证明全域数量。可用根数表示完成物理验收的候选数，不能代替数学根数。

## 5. 预算、错误与实测

输入非法、未知或双边界、非有限值、NoData、非仿射场及声明工作/输出额度不足均 HTTP 422。真实根迭代失败、全域枚举未闭合、没有可达根、多根未选、选择了无覆盖根等返回 HTTP 200 的有限诊断，但 `accepted=false`、`selected=null`。不能把 HTTP 200 当成可用平衡解。

预检按全部声明函数次数、最多两候选 forward 的完整上界、全部网格拟合与节点输出计账。失败 forward 亦按其声明上界收费；成功 forward 的实际函数次数另报。输出预检包括两个完整候选及选中结果重复序列化，最终再查实际有限 JSON 字节数。工作额度是公开的归一化算法单位，不是 CPU 指令或 FLOPs 计量。

自有 `tests/test_catenary_calculator.py` 当前 71 项：公开平床四边界、正/负坡与有限 EA、独立连续 ODE/弧长积分、自然与伸长长区别、真实双根及选择、覆盖拒绝、cap 排除、近峰分辨拒绝、负顶角、90°旋转、无效输入、极值/预算和 14 项实际 HTTP 验收。独立代理另写研究证明与积分反例测试，不用本模块内部形状或根 helper 作为黄金输出。

公开图例的不可伸长平床实际得到 B=34000 N、顶部 T=68000 N、顶角 60°、自然及实际弧长 3464.101615 m、layback 2633.915794 m。有限 EA 坡床 `z=-30+.1x`、B=100 N、w=4、EA=1e5，得到 S=50.3803848342 m、L=50.4582281328 m；二者以各自长度基准反算均回到 B=100 N。

下坡可执行例：x=`[-29,0,29]`、y=`[-40,0,40]`，每行 z=`[-1,-30,-59]`，床面 `z=-30-x`，w=4、EA=null。自然长 28 m 对应两个覆盖有效根 B≈4.00397024440／48.57869286705 N，顶张力约 109.205472606／84.908285907 N。固定顶张力 90 N 则得到 B≈22.50902534224／77.48444300382 N。把 x 改为±10及对应床高±10，仅低 B 根有覆盖，默认和 explicit highest 都不选解。有限 EA=100、伸长弧长 28 m 的同床例得到 B≈3.32872918138／37.37336127924 N；实际自然长约20.233207713／20.451472940 m，绝不是把 28 m 当自然长。

## 6. 可执行请求与主来源

```json
{"config":{
  "seabed_grid":{"schema":"oceanroute.bathymetry.v1",
    "x_m":[-5000,0,5000],"y_m":[-5000,0,5000],
    "z_m":[[-2000,-2000,-2000],[-2000,-2000,-2000],[-2000,-2000,-2000]],
    "source":{"name":"explicit synthetic manual plane",
      "horizontal_crs":"LOCAL_CARTESIAN_METRES","origin_projected_m":[0,0],
      "vertical_datum":"already aligned model sea surface z=0"}},
  "wet_weight_n_m":17,"ea_n":null,"heading_deg":90,"nodes":41,
  "boundary":{"kind":"bottom_tension","value_n":34000},
  "root_policy":"require_unique"
}}
```

连续力与弹性几何由方程独立推导。[MoorPy 官方文档](https://moorpy.readthedocs.io/en/latest/)与[维护机构 Catenary 源码](https://github.com/NatLabRockies/MoorPy/blob/main/moorpy/Catenary.py)提供自然长、湿重、EA 及准静态缆建模的公开背景，未调用或复制其逆边界算法。单峰证明是本项目独立研究结果；括号求解接口采用 [SciPy 官方 brentq 文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.brentq.html)。

本实现不自动生成完整动态 checkpoint、不替换变深施工初始化、不移除现有动态/稳态 guard，不把独立静水悬空段当成完整施工或原厂等效软件。`accepted=true` 只证明声明模型与数值条件中的已选根通过验收。
