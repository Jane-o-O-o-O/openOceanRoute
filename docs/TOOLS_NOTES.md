# OceanRoute 工程工具说明与设计约定

本文件对应 `oceanroute/tools.py` 的实际实现。工具沿用项目内部单位：长度与水深米，角度度，水深正向下，余缆百分比。项目修改工具返回 `{project, report, warnings}`；调用方应读取返回项目后替换当前工程，并展示工具警告。输入工程对象不会被原地修改。

## 测地计算器

`geodetic(config)` 支持 WGS84 椭球恒向线和 WGS84 测地线。`curve` 使用 `rhumb` 或 `geodesic`。恒向线由椭球等距纬度与子午弧计算；测地线调用 PROJ／pyproj。球面大圆线与椭球测地线的名称不能混用。

反算示例：

```json
{
  "longitude1": 179,
  "latitude1": 45,
  "longitude2": -178,
  "latitude2": 48,
  "curve": "rhumb",
  "segments": 16
}
```

也可用 `from`／`to` 对象，每个对象含 `longitude`、`latitude`。正算使用 `from`、`bearing_deg`、`distance_m`、`curve`。输出包含正向初始方位、反向初始方位、终点、总长及等物理距离采样点。测地线的反向初始方位一般不是正向初始方位简单加 180°。

重复坐标的反算方位为空。恒向线非子午方向连接极点、恒向正算越过极点、超过最短经度弧的绕地航段会报错，须分段计算。`segments` 是 1–10,000 的整数；正算距离上限为 4,000 万米。

## 路线细分及测地线的恒向线近似

`subdivide_project(project, config)` 使用 `spacing_m` 指定最大目标间距。

- `mode: "same_curve"`：在每条原区间的同一条地理曲线上插点，保留原曲线、平面总长、海底剖面、缆量和附属体位置。每条原区间等距分割，原有转折点保留。
- `mode: "geodesic_as_rhumb"`：将原端点之间的 WGS84 测地线采样为顶点，再用短恒向线连接。空间路径已经改变；原地形剖面保持失效，须重新采样。既有每段实物缆量转为固定约束，避免空间路径改变时无提示地改变制造缆量。

细分后总路线点数最多 10,000。固定段按原区间的平面距离比例分配缆长，最后一个子段承接数值残差，使原固定缆长守恒。停时、额外费用和段尾附加缆长只留在原段的最后一个子段，避免重复计费。

只有已绑定当前路线签名、且此次操作证明物理曲线未变化的剖面，才能更新到新签名。失效剖面不会因细分而恢复有效。新增点的水深来自当前剖面的分段线性插值；缺测仍为空。

## 按深度自动定义缆型

`define_cables_by_depth(project, rules)` 必须使用当前签名有效的剖面。路线点线性近似不能单独满足自动分缆的数据条件。合成演示剖面可操作，但继续保留合成来源标记。

`rules` 可以直接是分段数组，也可以是包含 `bands` 的对象：

```json
{
  "start_kp_m": 0,
  "end_kp_m": 100000,
  "convert_fixed": false,
  "bands": [
    {"min_depth_m": 0, "max_depth_m": 100, "cable_type_id": "DA", "slack_pct": 2},
    {"min_depth_m": 100, "max_depth_m": 1000, "cable_type_id": "LWP", "slack_pct": 1.5},
    {"min_depth_m": 1000, "max_depth_m": null, "cable_type_id": "LW", "slack_pct": 1.5}
  ]
}
```

深度区间采用 `[min_depth_m, max_depth_m)`；最后一段用 `max_depth_m:null` 表示无上界。规则必须从 0 m 连续覆盖所有深度，不能留空或重叠。缆型必须已经存在于型号库。

转换点取有效分段线性 KP–Depth 剖面与每个深度阈值的精确交点。对于海岭和海谷造成的反复穿越，每次穿越都插入转换点；恰好位于地形采样点的转换也会处理。每个新区间以中点水深选择缆型。指定 KP 范围外的缆型保留。范围内任何缺测都会拒绝自动分配，避免跨缺测推断。

默认保留固定段缆长；规则余缆仅记录为目标值，工具提供警告。`convert_fixed:true` 才把这些段显式转换为柔性模式并依目标余缆重算缆量。分缆会改变缆型单价和推荐船速，因此成本与初步工期可以变化；报告提供变更前后的数值。

## 按缆型分配余缆模板

`apply_slack_template(project, template)` 接受 `mapping`、`by_cable_type` 或 `entries`。

```json
{
  "mapping": {
    "DA": 2,
    "LW": {"slack_pct": 1.5, "slack_basis": "bottom"}
  },
  "fixed_policy": "keep"
}
```

可设置 `start_kp_m`／`end_kp_m`；工具先在范围边界插入连接点，避免把模板误用到范围外。`fixed_policy` 的三种行为：`keep` 保留固定缆长并提示目标余缆不改制造量；`convert` 显式转换柔性模式；`reject` 遇到固定段报错。未列出的缆型不修改。

## 反向、拆分和合并

`reverse_project(project)` 同时反向路线、剖面、约束区间、附加缆长、事件与实物装配。附属体 `cable_kp_m` 是实物的**起始端**，所以新位置使用 `总实物长 − 原附属体末端`，不是简单地用总长减原起始端。

路线 KP 定位的 replacement 附属体在反向后改用显式 cable KP，以保持有限刚性体的完整物理跨度。段尾 `allowance_m` 转成绑定原位置的路线 allowance。附加缆长保存有效的 `cable_type_id`，避免恰好在缆型转换点反向时改变缆材数量和费用。

当同一个 KP 同时有 additional 附属体和普通附加缆长，原模型的插入顺序不能直接表示其反序。工具将额外附属体长度表达为带 `reserved_for_body_id` 的占位 allowance，并将附属体改为 replace，保留准确的实物跨度、缆量及费用；报告说明此次表示转换。占位长度不算作普通附加缆材的统计量。数据表示可能变化，但实物结果须保持不变量。

`split_project(project, config)` 支持 `point_index` 或内部 `kp_m`。指定任意 KP 时先沿当前曲线插点。拆分穿过附属体有限物理跨度会报错，不能把刚性体裁成两块。

拆分边界的路线 allowances、additional 附属体和事件归 A；从边界开始的 replacement 附属体归 B。每项只保留一次。B 的路线 KP 与显式 cable KP 分别减去各自的分割里程。有效剖面在边界插值、裁剪、重新绑定；失效剖面保持失效。

`merge_projects(projects, config)` 按输入数组顺序连接 2–100 个工程。曲线模型须相同。相同 ID 但参数不同的缆型自动加来源前缀，并更新区间引用。点、附属体、事件、图层 ID 保持唯一；拆分工程共享的完全相同图层只保留一次。

```json
{
  "connect_gaps": true,
  "bridge_cable_type_id": "LW",
  "bridge_slack_pct": 1.5,
  "cost_policy": "require_equal",
  "name": "合并后的路线"
}
```

端点完全重合时共用一个点。端点不同会新增真实连接区间，绝不通过移动原点隐藏距离；超过 `join_tolerance_m`（默认 0.01 m）时需要显式 `connect_gaps:true`。新连接区间采用指定柔性平面余缆并标记无测深。原有固定缆长保持，新增连接缆量单独增加。相接端点的水深冲突标为缺测；有效来源、路线点近似和合成来源都进入合并剖面来源记录。

不同币种不能直接合并。船费、埋设费、预备费默认要求相同。`cost_policy:"first"` 显式使用首个工程费率重算，并发出警告。

## 验证证据与精度边界

`tests/test_tools.py` 覆盖：测地反算／正算往返；真实等距点；同曲线细分的平面长、剖面底长、固定缆量、附属体位置、停时和费用守恒；反复阈值穿越和范围边界；固定余缆模板三种策略；拆分再合并后的材料、装配、地形、成本守恒；有限附属体起始端反向；缆型转换点附加缆长的材料分类；合成来源标记；失效剖面不会被转换工具恢复。

这些是独立规划算法的回归验证，没有证明 Makai 原生文件兼容、实海精度或动态仿真等效。有效签名只证明剖面绑定的几何正确，不证明测深本身经过独立质量认证。

公开算法和格式参考：[pyproj Geod 文档](https://pyproj4.github.io/pyproj/stable/api/geod.html)、[GeographicLib RhumbSolve](https://geographiclib.sourceforge.io/C++/doc/RhumbSolve.1.html)、[GeoJSON RFC 7946](https://www.rfc-editor.org/rfc/rfc7946)。
