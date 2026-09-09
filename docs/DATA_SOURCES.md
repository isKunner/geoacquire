# 数据来源与官方下载链接

这里汇总 GeoAcquire 已接入的数据源，以及可从官方页面另行下载、后续可以继续接入的数据。
“已接入”表示可以使用 README 中对应的 YAML 运行；“手动补充”表示当前只提供官方入口，尚未接入
GeoAcquire 的下载流程。链接与说明最后核对日期为 2026-09-09。

| 名称 | 类型 | 细分类型 | 分辨率 | 范围（国家/地区） | 链接 | 数据描述 | 数据来源 / 文献 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USGS 3DEP LiDAR Point Cloud | 高程 / 点云 | LiDAR、LAZ；可生成 DSM、DTM | 点云间距和密度随项目质量等级变化；GeoAcquire 派生栅格默认 1 m | 美国及其领地 | [The National Map 数据下载](https://www.usgs.gov/the-national-map-data-delivery/gis-data-download) | 3DEP 分类点云保留原始空间参考和单位；GeoAcquire **已接入**按范围选取 LAZ，并可生成与目标 TIF 对齐的 DSM、DTM。 | [USGS 数据目录与说明](https://data.usgs.gov/datacatalog/data/USGS%3Ab7e353d2-325f-4fc6-8d95-01254705638a) |
| USGS 3DEP 1 m DEM | 高程 | DTM / 裸地 DEM，GeoTIFF | 1 m | 美国及其领地 | [The National Map 数据下载](https://www.usgs.gov/the-national-map-data-delivery/gis-data-download) | 3DEP 标准 1 米地形高程产品；GeoAcquire **已接入**按范围选择项目 GeoTIFF。 | [USGS 3DEP 产品说明](https://www.usgs.gov/3d-elevation-program/about-3dep-products-services) |
| USGS WESM | 元数据 | 项目覆盖范围，GeoPackage | 不适用；矢量项目边界 | 美国及其领地 | [直接下载 WESM.gpkg](https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/metadata/WESM.gpkg) | Work Unit Extent Spatial Metadata，不是高程栅格；GeoAcquire 用它判断目标范围对应的 USGS 项目。文件超过 3 GB，建议浏览器下载到 `cache/wesm/WESM.gpkg`。 | [USGS 3DEP Spatial Metadata](https://www.usgs.gov/3d-elevation-program/3dep-spatial-metadata) |
| LINZ New Zealand LiDAR 1 m DEM | 高程 | LiDAR 派生 DTM / 裸地 DEM，COG | 1 m | 新西兰 | [LINZ 高程数据入口](https://www.linz.govt.nz/products-services/data/types-linz-data/elevation-data/access-elevation-data) | 自动更新的全国拼接产品，GeoAcquire **已接入**官方静态 STAC，按目标范围选取原生 COG。 | [Toitū Te Whenua LINZ](https://www.linz.govt.nz/products-services/data/types-linz-data/elevation-data) |
| Google Satellite | 遥感影像 | 卫星与航空影像瓦片 | 随地区和缩放级别变化；项目默认 z18，Web Mercator 赤道处约 0.60 m/像素 | 全球；可用层级因地区而异 | [Google Map Tiles API](https://developers.google.com/maps/documentation/tile/overview) | GeoAcquire 当前提供 XYZ 影像流程，但默认地址不是 Google 官方文档中的 Map Tiles API。官方服务需要 Cloud 项目、API key 和 session token；下载、缓存、离线分析及署名必须遵守其政策，不能把官方文档视为允许批量保存。 | [Map Tiles API 政策](https://developers.google.com/maps/documentation/tile/policies) |
| Esri World Imagery Wayback | 遥感影像 | 历史卫星 / 航空底图，WMTS | 随地区和缩放级别变化；项目默认 z18，Web Mercator 赤道处约 0.60 m/像素 | 全球；实际年代和可用层级因地区而异 | [World Imagery Wayback 应用](https://livingatlas.arcgis.com/wayback/) | 自 2014 年起的 World Imagery 发布版本档案；GeoAcquire **已接入**按版本和范围获取并配准影像瓦片。归档日期是底图发布日期，不一定是影像拍摄日期。 | [Esri Wayback 介绍](https://www.esri.com/arcgis-blog/products/arcgis-living-atlas/mapping/use-world-imagery-wayback) |
| Copernicus DEM | 高程 | DSM，GLO-30 / GLO-90 | 30 m 或 90 m | 全球陆地 | [Copernicus DEM 产品页](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM) | 表示包含建筑、基础设施和植被的地表高程，不是纯裸地 DTM；GeoAcquire **已接入** CDSE OData 下载，需要注册账号。 | [产品手册与访问说明](https://documentation.dataspace.copernicus.eu/Data/Others/CCM.html) |
| Kruger National Park elevation models and orthomosaics | 高程 + 遥感影像 | DSM、DTM、CIR 正射影像 | 0.25 m | 南非克鲁格国家公园 | [数据集 DOI](https://doi.org/10.5285/deab4235f1ef4cd79b73d0cbf2655bd7) | 由 2018 年 9—10 月 DMC 航空影像生成的亚米级全覆盖数据；CEDA 下载需要免费注册。当前为**手动补充**，尚未接入 GeoAcquire。 | [Heckel et al. (2021)](https://koedoe.co.za/index.php/koedoe/article/view/1679)；[论文 DOI](https://doi.org/10.4102/koedoe.v63i1.1679) |
| RGE ALTI® | 高程 | MNT / DTM | 1 m 与 5 m | 法国本土及海外领地；不含法属波利尼西亚、新喀里多尼亚、瓦利斯和富图纳，部分禁飞区为 NoData | [data.gouv.fr 下载页](https://www.data.gouv.fr/datasets/rge-alti-r) | 法国 IGN 的规则裸地高程网格，主要由 2010 年以来的 LiDAR 点云拼接而成；适合洪水、滑坡、林业和城市精细地形分析。当前为**手动补充**。 | [法国 IGN 数据源](https://geoservices.ign.fr/rgealti) |
| PNOA-LiDAR | 高程 / 点云 | LiDAR LAZ、DSM、DTM；多期覆盖 | 点云约 0.5–5 点/平方米；常见栅格为 0.5 m、2 m、5 m，依覆盖期和产品而定 | 西班牙全国 | [CNIG 下载中心](https://centrodedescargas.cnig.es/CentroDescargas/buscadorCatalogo.do?codFamilia=LIDAR) | 提供 2008 年以来多期 LiDAR 点云及派生产品；第三期包含约 5 点/平方米点云和 0.5 m DSM、DTM。当前为**手动补充**。 | [西班牙 IGN 产品说明](https://pnoa.ign.es/pnoa-lidar/productos-a-descarga) |

## 使用前注意

- 数据页面、账号要求、许可和服务接口会变化，正式使用或再发布前应重新查看各官方页面。
- 表中的空间分辨率是源产品规格；与目标 TIF 对齐不会提高源数据的真实精度。
- Google 影像有明确的缓存、离线使用和机器分析限制。公开发布或批处理前，应先确认当前实现、用途和账号协议均符合 Google Maps Platform 条款。
- 通过官方页面下载的数据默认不进入版本库；请放到项目外的数据目录，或确认保存位置已被 `.gitignore` 排除。
