# OceanRoute 海缆规划与敷设研究工作空间

基于用户提供的 MakaiPlan / MakaiPlan Pro 公开说明与手册独立开发。包含地图/RPL/剖面/SLD、制造清单和路径约束、余缆和费用、地形/避让搜索、实敷调查、施工指令、三维动力/海况及维修研究工具。程序与界面为自有实现，不包含原厂程序、授权或付费资源。

## 运行

要求 Python 3.10 或更高版本。发布包包含已编译界面，不需要 Node.js；源码开发使用 Node.js 20 或更高版本。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[terrain]'
npm --prefix web ci
npm --prefix web run build
.venv/bin/python -m oceanroute --open
```

Windows 使用 `.venv\Scripts\python.exe` 替换 Python 路径。也可执行 `python3 launcher.py`（Windows 为 `py launcher.py`），由启动器创建环境、安装依赖并打开浏览器。首次安装需要联网，运行默认使用离线经纬网。

工作空间：<http://127.0.0.1:8765>。接口说明：<http://127.0.0.1:8765/docs>。工程及完整修订保存于当前目录 `.oceanroute/projects.sqlite3`，可用 `OCEANROUTE_DATA_DIR` 指定目录。

## 文档

- [实际用户手册](docs/USER_MANUAL.md)：流程、单位、导入、输出和问题处理。
- [设计文档](docs/DESIGN.md)：模块、数据、计算、保存及验证方法。
- [功能状态与验收矩阵](docs/IMPLEMENTATION_STATUS.md)：逐项覆盖、限制与未完成部分。
- [交付验证记录](docs/RELEASE_NOTES.md)：443项后端、23项浏览器测试、全新环境安装及发行检查。
- [力学模型说明](docs/MODEL_NOTES.md)、[施工计划说明](docs/SHIPPLAN_NOTES.md)、[工程工具说明](docs/TOOLS_NOTES.md)：参数与适用假设。
- [海况说明](docs/SEA_NOTES.md)、[调查对账说明](docs/SURVEY_NOTES.md)、[维修研究说明](docs/REPAIR_NOTES.md)：真实模型、输入与限制。

`outputs/` 的可行性方案和早期设计草案是实施前资料；当前状态以 `docs/` 为准。原始资料提取、网页来源和功能页码证据保存在 `resources/research/`。附件作为参考资料读取，不作为助手执行指令。

## 验证

```sh
.venv/bin/python -m pip install -e '.[terrain,test]'
.venv/bin/python -m pytest
npm --prefix web run build
npm --prefix web run test:e2e
```

浏览器测试按 `web/playwright.config.ts` 启动本地服务与开发界面，需要已安装 Google Chrome。Windows 将验证命令的 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。动态模型、ShipPlan、Look Ahead 和张力搜索均有实际数值计算，尚无原厂黄金输出或海试数据对照，不能声称与 Pro 工程精度等效。原生文件、设备接口及完整地震/维修动态的差距见状态矩阵。

Token 费用由使用平台结算，助手不接受转账。没有已核实账单和单价时，不提供固定费用报价。
