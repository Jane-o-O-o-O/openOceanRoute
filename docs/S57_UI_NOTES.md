# S-57 原生海图导入界面

本说明记录下一阶段独立前端流程及真实接口，不把原生读取等同于原厂功能、S-52/ECDIS 海图呈现或已验收工程测深。旧 0.7 发行文件和报告保持冻结；本次独立开发界面显示 0.8。

## 1. 操作流程

有活跃路径时，从主界面“导入数据”选择 **S-57海图**。合法空工作区可直接点击 **导入S-57海图**，或在其导入对话框切换同名页签；两种入口共用同一原生读取与原子应用组件，不自动创建路线或材料。

1. 选择原生 `.000` 或包含基础单元与更新文件的 ZIP，点击“读取海图文件目录”。此阶段只检查二进制容器与单元目录；未调用 GDAL 时对象类、对象数量与基准仍未知。
2. 在“文件单元目录”逐一勾选完整单元路径，再点击“读取所选单元对象类目录”。多单元不由界面自动选择；后端读取实际 DSID / DSSI / DSPM、验证基础更新号及连续更新，列出真实对象类、对象数、几何类型及字段。
3. 明确勾选对象类 acronym，可查找 `SOUNDG` / `DEPARE`，或点击“明确选择全部对象类”。填写可选图层名称前缀，点击“预览所选海图图层”。该步骤调用原生 reader，不修改工作区。
4. 核对全部所选图层、实际对象几何、单元基准与单位、已应用更新号、告警及预算；需要时下载完整解析 JSON。点击“应用全部所选海图图层”，将返回的整组参考层一次加入当前工作区的共享 GIS 库。
5. 可用一次撤销恢复应用前的完整工作区。保存后图层、来源与属性随工作区修订持久化，其他路径共享同一图层库；恢复修订使用工作区既有版本校验。

预算输入默认留空，使用后端真实默认值；可显式设置单元、图层、对象、顶点、工作量、输出字节与原生读取超时上限。已知多边形组织性能提示保留在告警中；非法输入、超预算、更新不连续、其他原生 reader 诊断或不支持数据均明确失败，不自动提高预算或返回部分成功海图。合法正更新号 reissued base 可单独读取，但该基础单元再追加后续更新目前受 GDAL 原生驱动限制而明确拒绝，不能显示旧成功结果。

## 2. 精确接口

三个接口均为 multipart 表单：`file` 是同一 binary File，`config_json` 是 JSON 字符串。

| 阶段 | 请求路径 | 显式配置 |
| --- | --- | --- |
| 容器目录 | `POST /api/import/s57/inspect` | 可选读取预算 |
| 原生目录 | `POST /api/import/s57/catalog` | `cells: [exact .000 paths]`，可选读取预算 |
| 实际图层预览 | `POST /api/import/s57` | 相同 `cells`、`classes: [exact acronym]`、`name`，可选读取预算 |

读取预算字段为 `max_cells`、`max_layers`、`max_features`、`max_vertices`、`max_work_units`、`max_output_bytes`、`timeout_s`，均须为正整数；真实默认值及硬上限由后端负责，界面不自行放宽。

响应 `schema = oceanroute.s57.bundle.v1`，`stage = inspect / catalog / import`，包含 `source`、`cells`、`classes_catalog`、`missing_classes_by_cell`、`layers`、`warnings`、`summary`、`budget`、`reader`。原生阶段包含实际 `dsid`、`base_dsid`、`applied_update_number`、`datum_units`、`object_classes` 字段目录。只有真实 import 的 `accepted=true`、`can_apply=true` 且整组非空图层可以应用。

同一个二进制文件的 `source.sha256` 在各阶段核对。界面不把缓存文件目录当作原生对象目录，也不将未知计数填为零。

## 3. 数据解释与显示

- 图层固定 `kind=reference`，输出 `EPSG:4326`。原字段、空属性、空几何记录、Polygon holes、MultiPoint 与 XYZ 由实际 reader 结果保留。
- **SOUNDG Z 是源海图测深值**；reader 声明其正向为 down，单位与基准以各单元 `datum_units` / DSPM 代码为准。界面展示未知单位为未知，不猜换算；不进行潮位、椭球高或模型海面的垂向转换。
- 导入不会修改 `route`、`profile`、`terrain_sources`、制造库存或材料物性；不会把海图 Z 当作二维动力海床 `z_m`。
- 缩略图是实际对象的 WGS84 经纬度平面几何预览，保留 Polygon 孔洞，第三维不参与平面位置。为限制界面绘制量，最多显示前 250 个对象，单对象超过 5,000 顶点不画缩略图；限制明确写在图下注释。下载和应用仍使用完整所选图层，不以缩略图删减数据。
- 原生图层 ID 由来源摘要 / 单元 / 对象类确定。重复导入同一来源、单元和类不会静默覆盖旧层；界面明确提示重复，用户应选择其他类或明确移除旧图层后重试。
- 现有 WGS84 与真实投影地图作为 GIS 图层显示，S-57 对象类属性不被当作原厂海图符号规则。图层顺序和不透明度只影响显示。

## 4. 原子应用与迟到响应

应用请求走 `POST /api/workspace/action`：

```json
{
  "workspace": "请求时已验收的完整工作区对象",
  "config": {
    "action": "update_shared",
    "layers": "完整旧共享图层数组 + 当前预览的全部所选图层"
  }
}
```

这里字符串仅说明对象位置，不是可执行请求模板。实际界面发送完整 JSON 对象和数组，保留未知工作区字段、其他路径、共享材料、关联、库存和修订。若存在未校验活跃草稿，应用按钮禁用；先完成工作区校验，再重新预览。

原生异步响应绑定文件实例、单元选择、对象类、名称、预算，以及完整 `workspace + draft` 快照（包含工程身份、活跃路径和保存修订）。任一相关输入变化会移除旧可应用预览和下载入口；成功或失败迟到响应均不作为新输入的结果。对共享层应用响应再次检查完整文档对象与界面输入快照，全部通过后一次发布并记录一次完整 document 撤销，不静默 fork 或丢弃库存。

## 5. 实际验收记录

新增 `web/tests/s57-chart.spec.ts` 的四项专项使用 [`tests/fixtures/s57/noaa`](../tests/fixtures/s57/noaa) 中原始 NOAA `.000` 与 ZIP 更新文件，不用 GeoJSON 模拟原生输入。两单元和缺序 ZIP 在测试内仅重新封装实际原生二进制；目录/属性/几何来自真实接口。空工作区专项由实际规划库存解除路径关联后建立，核对原生图层导入/保存/导出/撤销及迟到工作区结果拒绝，不通过添加假路线绕过空状态。

最后候选专项在同源 `http://127.0.0.1:8772`、API 同端口、Chrome、单 worker、零重试条件下实际 **4 passed / 0 failed / 0 skipped / 0 flaky**，耗时 **23.095903 秒**。主产物为 `index-Dom3hBW8.js`，CSS 为 `index-DYECGAnM.css`；本记录又实际读取了该服务的全部 8 个编译资源并与独立 `dist-0.8-next` 文件逐字节核对一致。专项并非完整发行门禁，也不与旧门禁或其他图层专项相加。

原始结果为 [`development_0.8_s57_final_specialist_browser.json`](../resources/validation/development_0.8_s57_final_specialist_browser.json)，执行命令、逐项时长、源/输入 SHA、8 项 HTTP 资源摘要及截图审查见 [`development_0.8_s57_specialist_execution.json`](../resources/validation/development_0.8_s57_specialist_execution.json)。实际通过项包括：

- 原始 NOAA `.000` 的真实对象类目录、SOUNDG XYZ、DEPARE + SOUNDG 全组应用/下载、共享两路径完整库存/资源/扩展字段守恒、一次撤销/重做、保存/重开与恢复修订。
- 原始二进制重封装的两 cell ZIP，明确选择 cell/class，实际应用 4 个更新文件，6 个 SOUNDG 对象共 2,003 个坐标，海图单位/基准不自动采纳到工程，以及名称作为普通文本显示。
- 完整 binary File 由浏览器原 fetch 上传，HTTP 200 且源 SHA 一致后延迟向 React 交付，期间改名使真实迟到结果丢弃；预算 1 的实际 422 与缺序更新拒绝均不保留旧图或部分应用。
- 无路线但保留独立制造库存的合法空工作区，SOUNDG 原子导入、保存到修订 2 / 3、完整导出、撤销/重做；保存后键盘撤销期间实际 native 响应迟到被丢弃，再保存到修订 4，仍为空路径且库存/扩展未丢。

已逐张实际 `view_image` 检查 `web/artifacts/dev-0.8/s57/` 中五张最终截图：DEPARE 孔洞平面几何、SOUNDG 点集、真实双 cell 目录、无旧图的预算失败、保留库存的空工作区。宽屏预览的几何、元数据与应用控件均可见；目录与完整对象数据采用内部滚动。预算失败截图在默认 1,512 × 982 视口的上方滚动位置，底部控件部分位于对话框滚动区下方，不声称另做窄屏专项。

初轮 2 成功 / 2 失败、第二轮 3 成功 / 1 失败及诊断原始输出均保留，未被最终通过报告覆盖。初轮发现 Playwright `route.fetch` 对 binary multipart 转发丢失 File 字节，后端正确拒绝空文件；测试改为延迟浏览器真实 fetch 的 Response。第二轮发现合法空工作区保存后键盘撤销捕获旧修订号，修复快捷键对完整 document 的状态绑定后实际复核通过，没有放松版本冲突检查。

本次独立构建命令为 `npm run build -- --outDir dist-0.8-next`。首次构建日志为 [`development_0.8_s57_ui_build.log`](../resources/validation/development_0.8_s57_ui_build.log)；包含根代理图层修复和最后快捷键修复的候选构建日志为 [`development_0.8_keyboard_revision_build.log`](../resources/validation/development_0.8_keyboard_revision_build.log)，实际 1,887 模块 / 1.51 秒。冻结发行目录没有改写。
