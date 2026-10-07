# 来源与致谢

感谢公开技术资料、开源科学计算与地理工具的作者，以及报告问题和核验边界的贡献者。引用不表示原作者与本项目存在合作、授权、背书或工程认证关系。

- [MakaiPlan](https://www.makai.com/cable-software/makaiplan/)与[MakaiPlan Pro](https://www.makai.com/cable-software/makaiplan-pro/)的公开产品说明，以及用户提供的参考手册，用于理解规划工作流与需求；原始手册 PDF 和原厂程序不在本仓库分发。
- [NOAA ENC资料](https://repository.library.noaa.gov/view/noaa/71555)提供原生 S-57 研究样例。保留原交换集及协议，见[样例来源](tests/fixtures/s57/README.md)；这些样例不用于航海，不表示 NOAA 认可本软件。
- Python、NumPy、SciPy、PROJ／pyproj、GeographicLib、Shapely／GEOS、GDAL／pyogrio、Rasterio、FastAPI、React、Vite、Playwright及其他依赖支持本项目。各自许可保留，不能统一视为本项目 MIT。
- 独立数学核对、解析用例、数值细化、原生格式对照和界面核验帮助记录真实实现范围；它们不能代替原厂黄金结果或现场验证。

本项目原创示意图不是工程地图；项目页六张截图来自实际测试截图，图中的第三方数据仍保留其来源与许可。许可边界见 [NOTICE](NOTICE.md)，历史重建性质见 [HISTORY_RECONSTRUCTION](HISTORY_RECONSTRUCTION.md)。
