# 贡献说明

当前稳定版冻结在 0.12.1。优先接受文档纠错、可复现的稳定性修复和安全问题处理；不默认开展新增产品功能。较大改动请先在 [Issues](https://github.com/Jane-o-O-o-O/openOceanRoute/issues) 说明问题与范围；安全问题使用[私密渠道](SECURITY.md)。

## 提交方式

1. 使用合成或可公开的最小用例说明问题、现有行为和期望行为，注明版本与环境。
2. 改动保持单一目的，保留单位、datum、NoData、预算、制造库存、完整候选和修订保护等既有合同。
3. 增补能够独立发现问题的必要回归；实际记录执行命令、结果和未验证范围，不把预期或旧轮次写成新通过。
4. 不改写已冻结发行包、原始报告、历史标签或其摘要。新证据使用新文件名，保留真实失败。

贡献须为你有权提交的材料，按项目原创部分的 [MIT License](LICENSE) 提供；第三方材料保留原许可与来源，不导入 Makai 受限程序、手册或私有工程数据。项目没有要求签署额外 CLA。

## 开发与验证命令

以下为 macOS／Linux shell 示例；Windows 用 `py -m venv .venv` 创建环境，并将 Python 路径替换为 `.venv\Scripts\python.exe`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[terrain,test]'
npm --prefix web ci
npm --prefix web run build
.venv/bin/python -m pytest
npm --prefix web run test:e2e
```

源码界面开发需要与 `web/package-lock.json` 兼容的 Node.js／npm（如 Node.js 20 或 22 系列）；浏览器测试使用已安装的 Google Chrome。先运行与改动相符的必要检查，是否执行完整门禁由改动范围决定。纯文档 PR 应如实注明未重跑产品测试。具体运行和模型说明见 [README](README.md)及[用户手册](docs/USER_MANUAL.md)。
