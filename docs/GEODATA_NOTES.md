# 标准地理数据交换说明

输入约定为水平坐标和垂直基准分开管理。路线核心使用WGS84十进制度；所有导入完成后保存来源、格式、转换及缺测说明。

## KML

`geoformats.import_kml(text,name,kind)` 导入 Point、LineString、含洞Polygon及MultiGeometry，输出普通图层、警告和LineString路线候选。保留名称、说明、Data/SimpleData及高度；高度模式并不赋予潮位基准，路线候选水深一律null。忽略NetworkLink/影像/轨迹并告知，不请求远程内容，不把影像当测深。DTD/实体、坏坐标、未闭合环及无效几何拒绝。最大32MB。

接口：`POST /api/import/kml`，JSON `{text,name,kind}`。返回图层可直接加入工程，`route_candidates` 只供用户显式选择，不自动替换当前路线。[OGC KML标准](https://www.ogc.org/standards/kml/)定义了此处使用的几何。

## Shapefile

`geoformats.import_shapefile_zip(data,...)` 读取压缩包内同名shp/dbf及可选shx/prj/cpg。利用pyshp读取几何属性，PROJ将源坐标系转换为WGS84；Z坐标不当水深。缺prj必须提供crs_override，不能因为数值落在经纬范围内就猜WGS84。cpg优先；无cpg/编码声明时按UTF-8并警告。DBF中文可显式gbk；源记录字段保留。

接口：`POST /api/import/shapefile` multipart：`file,name,kind,crs_override,encoding,source_name`。ZIP多个shp时source_name须为包内路径或唯一文件名。上限压缩/展开128MB、1000文件、100000要素、1000000顶点。内存读取，不解包到磁盘；加密、非法路径和重复名称拒绝。无效几何拒绝，不在背后自动修补拓扑。[pyshp项目文档](https://github.com/GeospatialPython/pyshp)提供文件对象读取及标准几何接口。

## GeoJSON、XYZ、GeoTIFF

GeoJSON符合WGS84地理坐标约定；声明其他或未知CRS拒绝。解析每个Feature和几何，非有限坐标、范围越界、拓扑错误提供清晰异常。

XYZ 为 lon/lat/垂直值，沿线采样明确接受水深/高程正方向及 m/ft/fathom，再统一为内部米制正水深。DTM 的 XYZ 入口使用米制正水深；DTM与沿线采样采用局部AEQD及线性/IDW插值，凸包外和间隙过大为缺测。GeoTIFF按文件CRS采样第一波段，必须声明水深/高程方向、单位和垂直基准，NoData保持。DTM四波段为水深m、坡度deg、下坡方位deg、阴影0-1。

## Surfer 网格

`surfer.profile_from_surfer(project,data,source_crs=...,...)` 独立读取 DSAA 文本、DSBB little-endian 二进制和 DSRB 带节结构，节点行序从最小Y和最小X开始。按声明源 CRS 定位，再使用双线性或最近节点沿路线采样。源范围外为空，正权重的缺测节点不参与补值；恰好位于有效节点时零权重缺测邻点不会抹掉它。输入上限128MB/4,000,000节点，DSAA 文本额外32MB限制。

接口：`POST /api/terrain/surfer` multipart `file,project_json,source_crs,spacing_m,method,depth_positive,depth_units,vertical_datum`。坐标参考需要用户明确提供，不解析GSR2，不从数值猜投影。非零旋转拒绝；含断层的DSRB只允许最近节点，不声称重建断层两侧插值。版本1的BlankValue阈值与版本2的精确BlankValue区分。垂直单位、符号和潮位基准分别记录，陆上正高程转换后负水深的节点保留缺测。

格式依据为 Golden Software 的 [Surfer 6文本说明](https://grapherhelp.goldensoftware.com/subsys/ascii_grid_file_format.htm)、[Surfer用户指南](https://downloads.goldensoftware.com/guides/Surfer17UserGuide.pdf)的二进制布局、[Surfer 7节结构](https://grapherhelp.goldensoftware.com/subsys/surfer_7_grid_file_format.htm)与[GSR2坐标参考说明](https://surferhelp.goldensoftware.com/subsys/Golden_Software_Reference_File.htm)。这些是公开网格规格，并非原厂Makai工程文件结构。

## 验证与范围

回归覆盖WebMercator→WGS84、中文DBF编码、缺CRS、多个源、无效ZIP、KML洞/复合几何、外链不下载和高度不误作水深，以及三类Surfer节点/缺测/日期线/单位采样。自有JSON可保存完整工程；标准图形不能等同原厂专有项目无损交换。
