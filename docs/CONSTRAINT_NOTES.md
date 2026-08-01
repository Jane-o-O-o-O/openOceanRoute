# Path Link 域与真实约束编辑

`oceanroute/constraints.py` 将用户提供手册 M186–211（PDF 物理页）中的点类型、链接和固定缆长域落实为独立可测试的规划语义。它不读取原厂数据库，也不声称原厂内部求解器或格式兼容。

## 三类点与制造域

| 点类型 | 实际行为 |
| --- | --- |
| Rigid | 地理转折锚点，自动联动不会改变其坐标。手册允许用户明确拖动／输入经纬度，本实现的手动 `moves` 也允许；`automatic:true` 请求不能搬动 Rigid |
| Clamped | 位于相邻两个 Rigid 锚点之间的同一恒向／测地曲线上，不允许转折。锚点移动时保持沿曲线的距离分数 `fraction` |
| Sliding | 固定模式由 Path Link 的实物 `cable_kp_m` 反求路线位置，可跨越未链接 Rigid 转折。它不构成余缆域边界。柔性模式按 Clamped 行为联动 |

点保存在 `route.points`，额外字段为 `constraint`、`anchor_start_id`、`anchor_end_id`、`fraction`。未指定类型的既有点在配置时按 Rigid 处理。两端必须为 Rigid，内部 Clamped／Sliding 初次配置时必须实际位于其相邻 Rigid 的直线曲线上；不能只给一个转折点换图标。

`route.path_links` 独立保存路径与制造站位的关系：

```json
{
  "id": "link-transition",
  "point_id": "transition",
  "cable_kp_m": 2400,
  "slack_change": false,
  "assembly_item_id": "可选的组件或制造参考 ID"
}
```

Rigid／Clamped Link 可以是 `slack_change:true` 域边界；Sliding 必须有链接且 `slack_change:false`。路线两端必须有边界链接，制造站位为 0 和实物总长。相邻边界之间是制造域，可以跨多个未链接的 Rigid 转折。域的制造缆量不随地理编辑增长；改变域按常数平面余缆重分配，不改变域外缆量和余缆。Clamped 边界移动会改变左右相邻域。

基础缆长、海底长度与实物总长分别记账。津贴／additional 组件在某路线 KP 增加实物长度、没有平面距离，因此固定域的均匀平面余缆基于基础制造缆量；这些插入独立保留其制造站位，不被误当成可铺平到更长海床的免费缆量。域边界不得位于插入的实物区间内部。`replace` 组件占用有限实物长度，保留其起始制造站位；其覆盖的缆材从材料汇总中扣除。

## 配置、编辑、求解

三个函数均不修改输入、不自动保存，返回 `{project,report,warnings}`：

| Python 函数 | HTTP 入口 |
| --- | --- |
| `configure_constraints(project, config)` | `POST /api/constraints/configure` |
| `edit_constrained_project(project, config)` | `POST /api/constraints/edit` |
| `solve_constraints(project, config)` | `POST /api/constraints/solve` |

HTTP 请求为 `{ "project": 工程, "config": 配置 }`。配置示例：

明确解除旧域时调用 configure，`config:{"clear":true}`；它移除域状态、链接与点类型，保留当前固定分段长度、几何、组件和保存版本，返回可审查的解除警告。此操作是解除旧联动规则，不是增加缆量。

```json
{
  "mode": "fixed",
  "points": [
    {"point_id": "transition", "constraint": "sliding"},
    {"point_id": "marker", "constraint": "clamped"}
  ],
  "path_links": [
    {"point_id": "start", "slack_change": true},
    {"point_id": "transition", "slack_change": false},
    {"point_id": "end", "slack_change": true}
  ]
}
```

配置捕获当前制造区段、有限组件和插入，没有立即重新分配已有缆量；切换固定／柔性模式本身不改变路线、制造量、材料成本或已有效剖面。切到柔性时捕获当前实际平面余缆作为新目标。省略 `path_links` 时，自动捕获两端、既有非 Sliding 缆型／目标余缆／余缆基准变化边界，以及 Sliding Link；显式提供链接数组可以控制域范围。配置中提供 `cable_kp_m` 必须匹配当前点的制造站位，不能借配置悄悄改制造数据。

编辑输入：

```json
{
  "moves": [
    {"point_id": "altercourse", "longitude": 118.02, "latitude": 23.01},
    {"point_id": "marker", "fraction": 0.35},
    {"point_id": "transition", "cable_kp_m": 2600}
  ]
}
```

Rigid 使用明确的经纬度；Clamped 使用 fraction，或经纬度拖动后由后端在实际锚线寻找最近位置并投影为 fraction。Fixed Sliding 只接受明确实物 KP；Flexible Sliding 与 Clamped 一样可输入 fraction／经纬度。多点编辑同一 ID 不得重复。`solve` 不接受 moves，用于复核当前已配置工程。

固定模式保留制造缆型分段。地理转折和制造转换原本恰好共点但没有链接时，转折保持地理意义，转换会生成独立 `generated_constraint_transition:true` 的 Sliding 点，以免移动转折时改变 A/B 缆型制造量。Sliding 可以越过未链接转折，重新归属相邻 Rigid 锚线；排序和 RPL 也相应更新。有限组件的制造位置变更属于装配编辑，不能借 Sliding 移点改变其覆盖的材料；零长度制造参考可链接并明确移动实物站位，不改变材料量。

## 状态、校核和来源

`route.constraint_state.version=1` 保存固定制造背景与 `materialized_signature`。核心分析校验该签名，拒绝对已配置路线直接 patch 坐标、缆段、津贴、有限组件位置或链接而绕过求解。普通标注、单价、停车时间、附加费用修改不触发制造域失效。显式重新配置会按当前工程重新捕获状态。

签名递归规范化同值整数／浮点及正负零，兼容浏览器 JSON 的单一数值类型；真实 Node.js `JSON.parse/stringify` 往返后仍可分析和编辑。它用于检测未求解的材料修改，不是安全认证签名。

固定求解返回前实际核对总实物缆量、逐缆型材料长度和有限组件起始制造站位；失败不返回候选。`report.domains` 包含域边界、基础可用制造缆量、实物长度、改变标记、平面余缆、已知底距／底余缆、缺缆量及 `uniform_surface_slack_applied`。首次配置和未改变域保持已有分配，不把无编辑重算伪称为已均匀化。`report.link_placements` 同时给路线 KP 与实物缆 KP。

不足为 `CONSTRAINT_DOMAIN_SHORTAGE`，工程分析还提供逐段 `CABLE_SHORTAGE`；负余缆是需要修改路线或制造装配的结果，不会被截为零。固定平面缺缆报告不替代已知更长海床的逐段底距校核。

改变的 Rigid 锚线插入缺测剖面节点，原剖面仅在完全未改变的地理曲线区间保留，尾段 KP 按新累计距离平移。新剖面绑定新路线签名，部分保留来源明确，不能把原测深迁移到新地理线路。柔性底余缆遇到改变几何时拒绝，需要独立重新采样地形。

## 当前明确支持与拒绝范围

- 支持统一固定或统一柔性模式、相邻 Rigid 锚线、多个 Slack-Change 域、制造缆型区段、`replace`／`additional` 有限组件、津贴、零长度制造参考和地理事件。
- 拒绝固定／柔性混合域、重复／零长基础路线段、非直线 Clamped 初始位置、跨 Rigid 转折的 Clamped 锚域、Sliding 域边界、反序域链接、重合点和不匹配制造链接。
- Sliding 恰与一个 Rigid 转折或其他标记重合时要求显式合并后重新配置，避免两个不同约束实体被静默吞掉。
- 固定制造装配内容变更、反向／拆分／合并／重新分缆型的约束状态变换尚未实现；这些结构工具对已配置状态明确拒绝。可明确取消旧约束或进行实制回写，再以新工程重新配置，不会静默丢掉域。
- 工程短到未链接组件／参考超出总实物范围时核心拒绝；不删除实体或伪造延伸路线。

预期错误以 `ConstraintError(ValueError)` 返回稳定 `CONSTRAINT_*` 码。UI 在检测到 `route.constraint_state` 后，主地图拖动和坐标输入须调用约束编辑入口，不能先本地写点再做普通分析。

## 可测不变量

`tests/test_constraints.py` 的 19 项测试涵盖：切换模式零缆量变化、固定域跨转折的统一余缆、域外长度不变、Clamped 精确曲线分数、Sliding 越过未链接转折、制造缆型转换分离、有限组件与津贴守恒、局部剖面来源、柔性 Sliding 和未链接组件、沿线拖动投影、无编辑测深保留、直接 patch 拒绝、冲突配置、高纬日期变更线、制造参考链接、价格／作业元数据编辑、明确解除旧域不改制造量／保存版本，以及真实 JavaScript 数值往返。

核心／结构工具另外校验制造参考的实物范围、零占用、反向站位、拆分边界归 A、合并连接段偏移与跨实体 ID 冲突。参考不增加制造量和费用，SLD 以零长度 `kind:"reference"` 展示。
