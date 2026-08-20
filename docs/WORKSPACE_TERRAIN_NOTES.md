# 共享地形、完整路线和制造量的原子更新预览

独立开发功能，基于公开手册 M129–144 的地形来源/剖面需求、M186–211 的固定与柔性 Path Link 制造域，以及当前 [共享工作区](WORKSPACE_NOTES.md)、[来源库](TERRAIN_SOURCES_NOTES.md) 和 [约束引擎](CONSTRAINT_NOTES.md)。它解决“先改共享源使柔性底余缆旧剖面失效，单路径再采样又无法校验整个工程”的实际更新次序问题。不修改冻结0.4发行物，不研究或读写原厂数据库事务/schema。

`oceanroute.workspace_terrain.preview_workspace_terrain` 从**旧有效完整工作区**及独立新来源草稿开始，在临时副本重采样、核算制造关系、最后统一 `validate_workspace`。它不按路径依次写入数据库，不暴露部分可应用工程，也不先删除旧剖面改用路线点水深。

## 实际接口和结果

`POST /api/workspace/terrain/preview`：

```json
{
  "workspace": {"schema_version":2,"id":"实际完整工作区，其余字段省略展示"},
  "sources": ["独立的新来源描述数组，使用terrain_sources既有schema"],
  "draft": {"path_id":"可选未提交路径ID","project":"可选完整schema1投影对象"},
  "config": {
    "spacing_m": 1000,
    "vertical_datum": "可选明确选择的来源基准名",
    "path_ids": ["可选路径选择，省略为全部"],
    "manufacturing_policy": "update_consistent",
    "max_total_query_points": 50000,
    "max_total_work_units": 60000000,
    "max_total_output_bytes": 33554432
  }
}
```

上面workspace/project/source的缩略展示文字不能直接发给API；真实调用必须使用完整实际对象。仅workspace/sources必填，draft/config可省略。未知字段拒绝。Python签名为 `preview_workspace_terrain(workspace,sources,config=None,*,draft=None)`。

结果：

```text
{model:'atomic-workspace-terrain-preview-v1',validation_status:'research',can_apply,
 input_signature,sources_signature,
 workspace:完整候选|null,project:候选活跃路径投影|null,analysis:完整工作区分析|null,
 paths:[{path_id,name,status,samples,quality,budget,before_summary,after_summary,
         constraint_report,warnings,error}],
 manufacturing:[{assembly_id,path_ids,status,old_total_m,new_total_m,delta_m,error}],
 errors,warnings,draft_report,before_summary,after_summary,budget,assumptions}
```

`paths.status` 是 `success/error/not_selected/not_run`；`samples` 为来源查询的真实完整站点（坐标、KP、depth/source_id/fingerprint、fallback及attempts），不是只给成功比例。没有执行的路径samples为空；失败路径若实际采样完成，保留全部缺测诊断。`error` 含稳定code、message、stage、适用时的path_id/assembly_id。stage为draft/selection/preflight/budget/path/manufacturing/final_validation。

制造status为unchanged/updated/updated_by_draft/error；共享分歧错误还含各path的 `path_totals_m`。成功后的总币种/采购金额/材料量在analysis中，不能把各替代路径相加称为制造库存。before_summary来自原workspace，after_summary为全部合入后的候选，不抹掉草稿造成的量变。

任一失败 `can_apply=false` 且workspace/project/analysis**全部null**，不返回旧工程假作候选。正常的缺测、库存分歧、采样预算等以200失败报告返回；无效旧工作区、非法来源/schema/config、无法在预算中容纳完整诊断的结果以422拒绝，错误不能被误当成功。全部JSON必须有限。

## 草稿与路径选择

旧workspace首先完整校验。可选draft只作结构准入：路径ID/schema1/WGS84须匹配；出现的共享cable_types/layers/terrain_sources必须与旧顶层库一致；workspace_context若出现，其工程/路径/装配角色/修订须一致；子路径不得带独立saved_revision。新sources只能通过独立参数传入。共享缆库或GIS也修改的草稿明确失败，不悄悄忽略其他未保存内容。

草稿几何变化允许让旧profile过期；本流程**先在新来源实际采样**，再完整分析柔性底余缆。因此可解决未配置约束路线的合法几何＋新源联合编辑。旧约束状态和Path Link不得删除、更改或重新捕获；直接改配置路线坐标会由真实约束签名校验拒绝，须另走约束编辑，不通过这项地形预览绕过Rigid/Clamped/Sliding规则。固定段必须保留原点对、缆型和固定制造量；含固定段时津贴、有限组件与实物参考也必须保留其库存字段。

默认选择全部路径。库摘要变化时，**所有来源绑定剖面的路径及所有柔性底余缆路径**都是必选，漏选整笔失败、尚不开始查询；带draft的路径也必选。可另外显式选旧手动剖面路径，将它改为新来源实采剖面。库摘要不变且没有draft的路径可以不选，保持原剖面。改名/数组重排不改变库摘要，其余内容/方向/单位/基准/采样/开关/priority改变的保守规则同来源库。

## 固定域和共享制造规则

新剖面必须连续完整，签名匹配几何和库，实际底距可用。缺测不能外推、补零或从路线点水深救回。配置Path Link的路线调用现有 `solve_constraints` 无移点复核；几何若发生变化则拒绝，固定制造守恒仍由真实引擎验证，柔性链接更新为当前实际实物KP。固定长度不会为新海床自动加长；core的CABLE_SHORTAGE及域不足都阻断候选，错误不会被百分比截为零。

`manufacturing_policy:'update_consistent'` 默认允许柔性实际量更新：独占装配更新；共享装配只有**全部关联路径**所需的制造缆型/顺序/量、组件及参考实物位置相同才整套更新一次。保留assembly ID、association、deployment/alternative角色；能对齐的原缆分区ID保留，有限组件/参考使用原实体ID。多路径存在不同量时明确 `WORKSPACE_TERRAIN_SHARED_DIVERGENCE`，不自动fork、换主路径或悄悄复用实物。

`manufacturing_policy:'preserve'` 要求现有制造实物不变；柔性底余缆量变也拒绝，预览保留实际delta供决定。不同制造方案需要先明确处理独立装配关系，再重新预览。本功能不支持以新来源更新为名扩大固定库存、改变制造参考或清除约束。

## 预算与保存

先预估**所有选择路径的实际恒向/测地采样点总数**，不按旧草稿长度猜点数。默认总50,000、硬上限200,000；每路径仍遵守既有50,000点。spacing为1..100,000m。总逻辑query工作默认60M、上限200M，单路径仅拿到剩余额度；预算失败后其他路径not_run，不继续重复昂贵查询。来源预处理即使命中缓存仍按来源库既有保守量计入。

budget.work_units为完整查询记账加预算失败时对剩余额度的保守保留，`completed_query_work_units`只记录已返回完整query结果的量；二者不是全部CPU FLOPs。旧workspace及最终校验仍受现有32MiB工作区、100路径、累计路线点/加密几何/制造条目/约束规模限制；本接口不声明硬RSS、精确执行时间或把所有几何/JSON验证成本都计入query量。

完整输入摘要限制64MiB；总结果默认32MiB、范围64KiB..64MiB。查询前用每采样点约1200字节＋工作区/来源字节作保守预检，最终严格校验真实结果字节，不裁掉来源诊断来通过预算。实际candidate还须满足32MiB工作区保存上限。

`input_signature` 是完整workspace＋sources＋config＋draft的规范JSON SHA256，包含saved_revision及全部未提交数据，同值int/float/−0一致，不作小数舍入。它用于预览输入一致性，非安全认证。UI也应比较整个编辑文档快照，任意改动使旧候选不可应用。

预览无持久化副作用，成功candidate保留旧saved_revision。显式应用替换完整工作区后，仍用既有 `POST /api/workspaces` 一次SQLite事务保存；并发旧revision会冲突。恢复一次旧完整修订同时恢复来源、所有剖面、制造实体及关系，不分别恢复几个路径。

## 可复制的真实小例及验收

1. GET `/api/terrain/sources/example`，将project.route设为 `slack_basis:'bottom',slack_pct:2`，POST `/api/terrain/profile` spacing200取得有效底余缆工程。
2. POST `/api/workspace/migrate`，再用 `/api/workspace/action` 的copy_path、assembly_policy:'alternative'生成两个共享路径。
3. 只在独立sources草稿将example-background的priority由10改1000；POST本预览 spacing200。两路线真实重采样成功，一套制造装配长度实际下降，delta明确，仍计一次采购。
4. sources=[]实测得到NoData；返回两路线逐点null与失败，旧workspace全部保持。单路径草稿末点longitude增加.003可重新采样；若仅一条共享方案变长则制造分歧拒绝。

`tests/test_workspace_terrain.py` 实测包含底余缆/共享及独立制造/priority、无缺测补救、总点/工作/输出预算、来源基准冲突、域外、固定库存/津贴保护、真实Path Link求解与不足、合法几何草稿及约束/共享/修订失败、输入签名、HTTP预览不写＋一次保存、两个SQLite并发写仅一个成功、重开和完整修订恢复。新增32项及相关workspace/terrain/constraints联合139项通过（4.13s）；包含旧工程显式 `profile:null` 的兼容回归。
