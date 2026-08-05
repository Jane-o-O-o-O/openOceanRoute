# 可运行合成示例

这里的坐标、深度、材料、价格、RAO和测量均为合成演示，不能替代实测资料。

1. 在“导入→完整工程”打开 `short-route.oceanroute.json`，等待计算完成，查看 RPL、剖面和 SLD。
2. 在制造装配导入粘贴 `manufacturing-relative.csv`，使用 relative 模式、replace 操作，明确选择 surface_fraction 映射，预览后应用。缆型 LW/DA 来自示例库。制造量可能改变余缆，需看警告。
3. `sea-research.json` 是 `/api/sea/simulate` 的 config，不是完整工程。海况页可输入其中的 `heave_rao`，动力设置按 `simulation`，或通过本地 API 使用。RAO 数值仅用于解析验证。
4. 完整动态结果的 checkpoint 可在仿真界面下载并上传续算；不要只复制动画帧。详细参数和限制见交付手册与模型说明。
5. “实敷调查”上传 `survey-observations.csv`，它对应上述短路线，含约11m合成纬度偏移。默认水深基准未对齐，因此不计算深差；只有明确接受共同基准才启用。实测缆KP来自示例，不代表实际放缆。
6. `repair-research.json` 分别保存四个 `/api/repair/{kind}` 的 config；不是工程文件。回收例得到水平250N、垂向600N、总张力650N。浮标例只完成静水垂向选型，缺少水平约束，整体平衡必须显示false。

完整工程与 API 示例分开保存，避免把工况配置误当项目 JSON 导入。所有长度为米，水深向下为正，三维 z 向上为正。
