# 圆弧人工端点编辑界面 · 0.12 开发记录

本阶段在实际 WGS84 圆弧描述符上编辑 Rigid 端点。前端不计算替代圆弧、不发布显示采样点，也不以直弦暂代候选几何。原有 Clamped / Sliding 点继续使用制造域坐标、fraction 或实物站入口；含 Rigid 圆弧端点与非 Rigid 点的投影批次明确拒绝整体圆弧编辑，不部分应用。

## API 与组件接线

`ArcEditPanel` 接收 `ArcEditContext`：完整 `WorkspaceDocument` 引用和序列化 `contextKey`、当前实际选中点、`selectionRevision`、工作区 `pending`、选择回调及异步候选应用回调。`ArcEditIntent` 保存地图拖动、RPL 经纬度或真实投影转换生成的 WGS84 `moves`，以及新的入口 token。工程工具也提供独立圆弧编辑页面，可添加多个真实 Rigid 移动点，或显式载入当前路径的圆弧点。

`POST /api/workspace/arc-edit-preview` 仅提交 `{workspace,path_id,config}`。`config.moves` 使用 `{point_id,longitude,latitude}`；可显式提供逐骨架跨度的 `arc_options`，以实际相邻 Rigid 端点 ID 声明半径、`preserve/minor/major/left/right` 分支及完整圆策略。默认不发送额外半径或分支修改。选项按 Rigid 骨架跨度列出，不用制造边界 marker 子腿替代原跨度身份。

响应使用完整工作区 envelope，并核验 `operation.kind==='arc_edit'`、工作区 ID、保存修订、活跃 path ID、实际移动目标、完整分析和真实结果选中点。`tool_report.arc_changes` 列出旧新描述符、真实纯几何 `evidence` 和数值预算；制造核验、实际边界点、剖面与侧坡失效报告保留原始完整 JSON。

地图拖动先恢复原点位，RPL 数字提交返回 `false` 恢复原字段，之后打开完整候选面板。投影 X/Y 和投影地图先经过真实水平坐标变换，再将目标提交到同一圆弧候选入口；不会把只改了端点的旧描述符提交给旧坐标编辑。其他普通点保持原有入口和约束语义。

## 几何与测深展示

默认保留半径、未移动的另一端，以及原有向短弧或长弧分支；圆心随真实解变化。人工改变端点没有隐含保留原圆心或两端切线的过约束条件。候选表展示旧新圆心、半径、有向转角、真实弧长、端点绑定与径向残差、实际入出端切向。独立 `tool_report.joins` 表比较每个实际接缝点的前后入切向、出切向及有符号转角，入切向使用实际前段 arrival 而不推测为起始 bearing；接缝和制造域核验以服务报告为准，不声称自动保持光滑。

候选地图仅消费 `analysis.active_path_analysis.route_geometry` / `route_geometry_segments`。RPL 仅列存储的真实路线点和制造边界点。移动点的未测深 `null` 显示“—”，失效剖面不认证新路线；旧数据和失效签名在候选报告中可核对。完整圆单端不能自由保持闭合，默认 typed 拒绝；显式闭合端同移策略由真实后端求解，不用显示折线绕过拒绝。

## 异步事务保护

候选计算和应用绑定完整文档对象及内容、活跃路径、保存修订、选中点及其 revision、请求序号、组件 mounted 身份和输入签名。表单 `onInputCapture` 仅同步递增 raw revision ref，不在 capture 阶段 setState 改写受控输入；实际输入 onChange 驱动状态，因此未 blur 的数字或文本也能丢弃迟到响应。选择离开再回来、保存修订变化、路径切换或关闭面板使旧候选不可应用。投影转换另绑定原选中点及 selection revision。

应用仅提交当前候选路径的 `project` 到 `/api/workspace/action` 的 `update_path` + `assembly_policy:'auto_exclusive'`，不提交候选共享库存或整个候选工作区，不设 `update_shared:true`。响应再次核验身份、保存修订、活跃路径及结果点，随后完整文档原子替换并仅增加一次 undo。预览和应用均不写保存库，只有用户保存才产生保存修订。

## 版本与下载名称

`web/src/version.ts` 读取根维护的前端 `package.json`，统一普通/空工作区页眉、页脚、帮助及规则下载 fallback；不再保留固定 0.10 显示。真实导出响应优先解析 RFC5987 UTF-8 `filename*`，再用 ASCII `filename`；拒绝路径分隔符及控制字符，并安全降级。完整工程导出采用服务的 workspace scope，不硬标为单路径 project。

## 验证状态

当前已实际运行 TypeScript 编译（exit 0）与新 `dist-0.12-next` 构建。`web/tests/arc-edit.spec.ts` 已由 Playwright `--list` 实际列出 9 个专项场景：RPL 原子应用/undo/save/reopen、经纬地图拖动、真实投影 X/Y、投影地图拖动、相邻双弧及原负向长弧、整圈与不可达拒绝、迟到响应保护、共享库存语义、空 UI 与 scoped Unicode 导出。

首轮专项 `development_0.12_arc_edit_browser_first` 已实际执行 9 项：5 通过、4 失败，wall 154.935397 s，198 输入无变化。RPL/地理拖动/投影 X/Y/投影拖动及新版本导出通过；面板直接人工输入被 capture 阶段 setState 恢复为原值，服务真实返回 changed:false，导致相邻长弧、完整圆和两项异步/库存场景失败。此为具体前端缺陷，修正为 capture 阶段只更新 ref，实际 onChange 正常存储输入。首报告、失败上下文与截图保留，不把后端真实 no-op 当通过。两张首图实际查看还发现 modal 宽度被全局样式覆盖，已提高专属选择器优先级、表格顶部对齐及缩减主表证据；完整原始证据仍可展开。完整圆提示已更正为分支 preserve 与显式移动策略的区别。

修复轮 `development_0.12_arc_edit_browser_repair` 实际执行 9 项：8 通过、1 失败，wall 52.389657 s，198 输入无变化。8 个圆弧事务场景通过；剩余失败来自隔离开发库中前两轮使用相同 Unicode 示例工程名，打开对话框出现多个匹配项。测试工程名改为含其实际唯一 ID，继续验证中文、α、斜杠及服务 scoped filename，不修改生产代码或删除首轮证据。

最新专项 `development_0.12_arc_edit_browser_verified` 在同源 `http://127.0.0.1:8780` 的真实 0.12.0 API 与 `dist-0.12-next` 上实际执行：9 通过、0 失败、0 skipped、0 flaky，worker 1、retry 0，每项一次；Playwright 原生 53.848403 s，wall 54.054443417 s，Chrome 154.0.8037.93。执行前后 198 输入无变化，输入摘要为 `ced8dcb95754058636fe385e7528915e99c33c97016ab3ac6834521ad29e996a`。实际报告与 execution / inputs / log 保存在 `resources/validation/development_0.12_arc_edit_browser_verified*`；12 张实际 PNG 均在 `web/artifacts/dev-0.12/verified-arc-edit`，逐张 `view_image` 后记录于 `resources/validation/development_0.12_arc_edit_browser_visual_review.json`。

本结果仅是 9 项开发专项，不代替后续冻结源码、正式发行前端和新服务上的完整门禁。视觉审查仅确认截图可见视口，不能代替几何数值和事务断言；长报告下半部在滚动视口之外，超长合成库存名称在关系图中裁切已如实记录为非阻断观察。旧 0.11 归档、截图、报告、前端发行目录与专属 verifier 均未作为本阶段验证输出写入。
