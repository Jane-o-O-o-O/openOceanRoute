# 真圆弧人工端点编辑（0.12 开发）

本模块是公开规划需求的独立实现。原手册物理第197–198页的 Radius 菜单含 Edit、Remove 和 Fix points / convert to rhumb lines；本入口覆盖保留半径与有向分支的编辑。Remove 与转恒向的范围必须由各自实际入口说明，不能由本入口推定完成。旧0.11及此前发行产物不因本开发阶段而重写；本说明不是整版发行、海试或原厂等效验收。

## 接口与声明

- `arc_edit.edit_arc_project(project, config=None)` 返回 `{project, report, warnings}`，输入不变、不保存。
- `arc_edit_workspace.preview_arc_edit_workspace(workspace, path_id, config)` 返回完整旧工作区动作式候选 `{workspace, project, analysis, warnings, tool_report, operation, result_selection_point_id, scope}`。
- `POST /api/tools/arc-edit` 严格接收 `{project,config}`。
- `POST /api/workspace/arc-edit-preview` 严格接收 `{workspace,path_id,config}`。
- 应用复用 `/api/workspace/action` 的 `update_path` / `auto_exclusive`；持久保存仍是独立的 `/api/workspaces` 操作。预览不增修订，应用不自动写库；整工作区的 revision 守卫保持。

```json
{
  "moves": [{"point_id":"p1","longitude":118.1,"latitude":22.0}],
  "arc_options": [{"start_point_id":"p0","end_point_id":"p1",
    "branch":"preserve","full_circle_policy":"reject_move"}],
  "max_arc_rebuilds":32,
  "max_work_units":200000,
  "max_output_bytes":16777216
}
```

所有配置层及 move/option 层严格拒绝未知字段；坐标为有限 WGS84 经纬度，不接受 bool。公开 moves 最多256项，ID唯一且须为当前真实 Rigid 点；至少一个 move 或 arc_option。`arc_options` 最多256项、跨度唯一，引用相邻 **Rigid骨架端点**，不是中间 Clamped/Sliding 分割出来的子腿。可选 `radius_m` 为0.001–1,000,000m，省略保留旧半径；`branch` 为 preserve/minor/major/left/right，始终保留原 sweep 正负方向；方向、小弧/大弧、中心侧的几何含义见 [ARC_EDIT_GEOMETRY_NOTES.md](ARC_EDIT_GEOMETRY_NOTES.md)。显式改分支或半径可以没有坐标 move。

默认 preserve 保留原小弧/大弧分支；旧数值不可分辨180°在实际编辑时须明确选择，不能猜中心。默认整圈 `reject_move`；`preserve_endpoint_center_bearing` 只允许两新端点为同一真实位置的联合移动，按旧端点到中心的 WGS84初始方位在新端点重建中心，保留±360°，不保留旧中心。整圈两新端点不同必拒绝；非preserve分支不适用于整圈。没有实际坐标/几何/沿线站变化时返回原工程的完整深拷贝与 `changed:false`。

## 曲线、约束与制造

所有 incident 圆弧一次重构；互相相连的多弧逐跨度独立求解，任一失败整笔无候选。六字段 `leg.geometry` 表达真实 WGS84测地等半径圆，未变的非incident腿保持原descriptor；不以屏幕圆、折线或加密点替代。实际弧長/KP来自 reduced-length 积分，不用 `R×角度`。半径保存不意味着接缝仍相切：`report.joins` 提供受影响点的 before/after 实际入切线、出切线及有符号转角，不能称自动维持C1连续。

原所有业务点ID必须保留；制造量边界可能需要真实新 `constraint-transition-*` marker，列于 `generated_marker_ids`，其用途与显示加密点不同。新marker不得偷提升旧 Clamped/Sliding 的身份，也不能删除其旧引用。刚性骨架变形后，Clamped/Flexible Sliding按真弧KP比例重建；Fixed Sliding按原实物站位重建。旧 `/api/constraints/edit` 的带圆弧分支调用同一服务，仍支持fraction、Fixed实物 `cable_kp_m`、自动移动Rigid禁令；公开人工端点moves不接受非Rigid点或这两字段。沿线坐标投影检查完整有向弧的多个局部最近点；存在等近歧义则要求显式fraction，不默认选一个。原无圆弧的旧约束编辑分支保持。旧入口的明确Fixed Sliding站编辑仍可按旧合同更新零长制造参考站，有限组件不能借此更改制造位置；公开Rigid端点moves不修改制造站。

结构重验复用 `constraints.reconcile_route_structure`，**不删除constraint_state后重新capture固定制造量**：Fixed保原制造快照、Path Links、基础/附加制造量、缆型量及组件实物站。stop_hours/extra_cost和事件随实际KP映射；有符号零长度body仍保其身份、载荷声明及站位，制造参考仍为零长。已声明assembly_item_id的link须同时满足真实组件/参考的几何KP与link point几何KP；replace组件与Clamped点分离整笔拒绝。additional体的leading实物KP可以与RPL post-insert缆KP不同，按实际几何同站判定，不靠数字相等误拒绝。普通无item非slack链接保冻结制造参考并报告实际/冻结base、几何KP和实物站偏移，不能把Rigid参考当新Slack-Change。

固定域不足由原域求解器报告 `CONSTRAINT_DOMAIN_SHORTAGE` error级warning及实际shortage，允许审查原库存不足的候选，绝不自动添库存或称已满足约束。无法布置实体、边界冲突或制造守恒失败则422。Flexible保原mode/slack/basis，独占装配按真实量更新；共享alternative的量变由 `WORKSPACE_SHARED_ASSEMBLY_CHANGED` 拒绝，不能静默fork/转Fixed。新水深未知；Flexible底余缆因失效剖面不能由人工坐标操作认证，须走实际地形重采样流程。

移动Rigid、真实重定位/新增marker水深为null；未变Rigid跨度中仅native反解舍入而位置未变的marker保留旧坐标、水深及原子腿descriptor。旧profile/side_slopes对象与旧签名保留，不贴新签名把旧测深认证为新结果；报告区分present与invalidated。主应用以完整document/config快照拒迟到响应，应用可作为一笔撤销，显式保存才有新数据库修订。

## 可审查结果与预算

`report` 提供 operation=`arc_edit`、规范config、changed、moved_point_ids、result_selection_point_id、arc_changes、joins、generated_marker_ids、before/after长度和点数、manufacturing及constraint_reconciliation、profile_invalidation/side_slopes_invalidation、budget。每个arc_change含Rigid两端ID、原实际子腿indexes、old/new六字段geometry、pure evidence/budget。evidence是真实半径/绑定误差、候选中心、所选分支、实际弧長与端切线；不能推断其是原厂容差证书。

`max_arc_rebuilds`默认32、hard256；`max_work_units`默认200,000、hard2,000,000，逐incident传剩余预算，累计pure native Direct/Inverse与RouteSegment实际积分/反解工作；这不是整个操作的CPU/墙钟上限、FLOPs或token。前置源工程准入、原段构造、制造重验、marker投影、接缝诊断、完整路线分析及JSON编码不计入该逻辑工作量，仍受各自已有保护。`max_output_bytes`默认16MiB/hard64MiB，限制完整direct候选有限UTF8 JSON；workspace包装另64MiB整体上限。超限整笔拒绝，不截断结果或只重建前几个弧。

Geometry有效但数值不可解/分支不清/预算不足均转换为具体 `ARC_EDIT_*` ValueError code，HTTP422；非法输入也422。无geometry的工程须用普通坐标编辑。纯模块的 `original_endpoints` 是服务从已绑定旧工程实际端点提供的内部证据，不能由HTTP配置替换；它解决Direct roundtrip数纳米差异，不吞掉用户实际移动。

## 本阶段独立验证

`tests/test_arc_edit.py` 用原生GEOD半径与独立GeographicLib reduced-length quadrature重建验收正负minor/major、日期线/高纬、多incident/非incident、显式分支/半径、整圈、Clamped/Sliding、冻结制造、linked replace拒绝与additional leading/post-insert可行例。`tests/test_arc_edit_workspace.py` 执行真实API、独占/共享策略、预览不写库、一笔应用后显式保存、关闭重开及revision冲突；非法枚举容器不产生500。专项执行记录由当前开发门禁提供，不能替代尚未执行的整版回归、PDF/发行与现场验收。
