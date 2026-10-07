# 许可与第三方说明

## 原创部分

[MIT License](LICENSE) 适用于维护者有权授权的项目原创代码、原创项目文档和原创示意图，包括历史版本中的自有代码。使用、修改或再分发时，应保留 MIT 要求的版权及许可说明。

0.1–0.12.1 已冻结发行包保持原字节。本仓库新增的 LICENSE 与本说明是另行提供的许可补充；不声称原封存 ZIP、wheel、EXE 或 PDF 当时已经包含该 LICENSE 文件，也不据此改写原始摘要或验收记录。

项目截图中的自有界面不改变其所显示的第三方数据许可。六张项目页截图保留原截图内容与来源，不能把其中 NOAA 海图或其他第三方材料整体重新标为 MIT。

## 第三方组件与数据

项目和 Windows 安装包使用 Python、NumPy、SciPy、PROJ／pyproj、GeographicLib、Shapely／GEOS、GDAL／pyogrio、Rasterio 及其他依赖。它们保留各自版权和许可，不统一改为本项目的 MIT。例如安装包 Shapely 分发保留 `LICENSE_GEOS` 中的 GEOS LGPL 文本；具体版本、条件和第三方声明以实际分发内的许可文件为准。

Windows 包内相关文本通常位于 `runtime/Lib/site-packages/<distribution>.dist-info/licenses/`，另有运行时本身的许可文件。源码环境应查看实际安装的分发文件；不要只凭项目 LICENSE 推定依赖许可。

NOAA S-57 测试交换集保留原始字节、协议与来源。其 CC0 数据记录和再分发说明见[测试资料说明](tests/fixtures/s57/README.md)及[原始来源记录](resources/research/s57_sources.json)。这些资料不是官方 NOAA 航海产品，不表示 NOAA 背书，不保证当前海图有效性。

公开技术文档、学术资料和商标引用不因此取得新的再分发授权。本仓库不授予 Makai 软件、原始手册或受限资源使用权，亦不作第三方权利或不侵权保证。完整使用边界见 [DISCLAIMER](DISCLAIMER.md)。
