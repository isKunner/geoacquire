# 数据来源与官方下载链接

这里汇总 GeoAcquire 已接入的数据源，以及可从官方页面另行下载、后续可以继续接入的数据。
“已接入”表示可以使用 README 中对应的 YAML 运行；“手动补充”表示当前只提供官方入口，尚未接入
GeoAcquire 的下载流程。链接与说明最后核对日期为 2026-09-11。

| 名称 | 类型 | 细分类型 | 分辨率 | 时间（采集/发布） | 范围（国家/地区）                                                               | 坐标系（源数据） | 链接 | 数据描述                                                                                                                                                                                                    | 数据来源 / 文献 |
| --- | --- | --- | --- | --- |-------------------------------------------------------------------------| --- | --- |---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------| --- |
| USGS 3DEP LiDAR Point Cloud | 高程 / 点云 | LiDAR、LAZ；可生成 DSM、DTM | 点云间距和密度随项目质量等级变化；GeoAcquire 派生栅格默认 1 m | 2004 年至今；具体时间随项目而异 | 美国及其领地                                                                  | 随项目 / workunit 变化；通常为 NAD83 系列的 UTM、State Plane 或 Albers，也可能采用当地基准；美国本土高程通常为 NAVD88；以 WESM 和 LAZ 文件头为准 | [The National Map 数据下载](https://www.usgs.gov/the-national-map-data-delivery/gis-data-download) | 3DEP 分类点云保留原始空间参考和单位；GeoAcquire **已接入**按范围选取 LAZ，并可生成与目标 TIF 对齐的 DSM、DTM。                                                                                                                               | [USGS 数据目录与说明](https://data.usgs.gov/datacatalog/data/USGS%3Ab7e353d2-325f-4fc6-8d95-01254705638a) |
| USGS 3DEP 1 m DEM | 高程 | DTM / 裸地 DEM，GeoTIFF | 1 m | 持续更新；每个瓦片的源项目时间不同 | 美国及其领地                                                                  | NAD83 / UTM，分区随位置变化；通常为 NAVD88 高程，部分领地采用当地高程基准；以 GeoTIFF 文件头为准 | [The National Map 数据下载](https://www.usgs.gov/the-national-map-data-delivery/gis-data-download) | 3DEP 标准 1 米地形高程产品；GeoAcquire **已接入**按范围选择项目 GeoTIFF。                                                                                                                                                    | [USGS 3DEP 产品说明](https://www.usgs.gov/3d-elevation-program/about-3dep-products-services) |
| USGS WESM | 元数据 | 项目覆盖范围，GeoPackage | 不适用；矢量项目边界 | 每日生成；记录各项目的采集与发布日期 | 美国及其领地                                                                  | NAD83 经纬度（EPSG:4269）；各 workunit 的 `horiz_crs` 另行记录实际产品 CRS | [直接下载 WESM.gpkg](https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/metadata/WESM.gpkg) | Work Unit Extent Spatial Metadata，不是高程栅格；GeoAcquire 用它判断目标范围对应的 USGS 项目。文件超过 3 GB，建议浏览器下载到 `cache/wesm/WESM.gpkg`。                                                                                      | [USGS 3DEP Spatial Metadata](https://www.usgs.gov/3d-elevation-program/3dep-spatial-metadata) |
| LINZ New Zealand LiDAR 1 m DEM | 高程 | LiDAR 派生 DTM / 裸地 DEM，COG | 1 m | 持续更新；各分区 LiDAR 采集年份不同 | 新西兰                                                                     | NZTM2000（EPSG:2193）；NZVD2016 高程（EPSG:7839） | [LINZ 高程数据入口](https://www.linz.govt.nz/products-services/data/types-linz-data/elevation-data/access-elevation-data) | 自动更新的全国拼接产品，GeoAcquire **已接入**官方静态 STAC，按目标范围选取原生 COG。                                                                                                                                                  | [Toitū Te Whenua LINZ](https://www.linz.govt.nz/products-services/data/types-linz-data/elevation-data) |
| Pernambuco Tridimensional (PE3D) | 高程 / 点数据 / 遥感影像 | 正射影像；MDE / DSM 栅格与 XYZI；MDT / DTM 栅格与 XYZ；强度—分层设色合成图 | 全州 1:5,000 产品：正射影像 0.5 m，MDE / MDT 栅格 1 m，合成图 1 m；局部 1:1,000 产品：正射影像 0.12 m，MDE / MDT 栅格与合成图 0.5 m | 航测与激光扫描始于 2014 年，项目于 2016 年发布；不同区块的具体航飞时间应以文件元数据为准 | [巴西](https://www.ibge.gov.br/en/geosciences/territorial-organization/territorial-meshes/18890-municipal-mesh.html?edicao=46274&t=downloads)伯南布哥州；1:5,000 级覆盖约 98,146 km² 的全州，1:1,000 级覆盖 26 个市镇的约 870 km² 城区 | SIRGAS 2000 / UTM 24S 或 25S（EPSG:31984 / 31985）；官网未明确说明垂直高程基准 | [PE3D 下载地图](https://pe3d.pe.gov.br/mapa.php) | 航空摄影测量与 LiDAR 成果，提供六类可下载产品；下载免费但需要注册、登录和 CAPTCHA。网站要求成果使用时注明伯南布哥州政府及州经济发展主管部门。GeoAcquire **已接入 1 m MDT Raster 下载**，其他五类产品暂未开放。 | [PE3D 官方说明](https://pe3d.pe.gov.br/)；[州政府项目页](https://www.srhs.pe.gov.br/programas-acoes/pe3d) |
| Google Satellite | 遥感影像 | 卫星与航空影像瓦片 | 随地区和缩放级别变化；项目默认 z18，Web Mercator 赤道处约 0.60 m/像素 | 影像采集时间因地区和图源而异；服务持续更新 | 全球；可用层级因地区而异                                                            | XYZ Web Mercator；GeoAcquire 落地 GeoTIFF 为 EPSG:3857 | [Google Map Tiles API](https://developers.google.com/maps/documentation/tile/overview) | GeoAcquire 当前提供 XYZ 影像流程，但默认地址不是 Google 官方文档中的 Map Tiles API。官方服务需要 Cloud 项目、API key 和 session token；下载、缓存、离线分析及署名必须遵守其政策，不能把官方文档视为允许批量保存。                                                              | [Map Tiles API 政策](https://developers.google.com/maps/documentation/tile/policies) |
| Esri World Imagery Wayback | 遥感影像 | 历史卫星 / 航空底图，WMTS | 随地区和缩放级别变化；项目默认 z18，Web Mercator 赤道处约 0.60 m/像素 | 归档覆盖 2014 年至今；版本日是发布日，不一定是拍摄日 | 全球；实际年代和可用层级因地区而异                                                       | WMTS / Web Mercator；GeoAcquire 落地 GeoTIFF 为 EPSG:3857 | [World Imagery Wayback 应用](https://livingatlas.arcgis.com/wayback/) | World Imagery 发布版本档案；GeoAcquire **已接入**按版本和范围获取并配准影像瓦片。                                                                                                                                                 | [Esri Wayback 介绍](https://www.esri.com/arcgis-blog/products/arcgis-living-atlas/mapping/use-world-imagery-wayback) |
| Copernicus DEM | 高程 | DSM，GLO-30 / GLO-90 | 30 m 或 90 m | TanDEM-X 主要采集于 2011–2015 年；缺口可能使用更早数据 | 全球陆地                                                                    | WGS 84 经纬度（EPSG:4326）；EGM2008 正高（EPSG:3855） | [Copernicus DEM 产品页](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM) | 表示包含建筑、基础设施和植被的地表高程，不是纯裸地 DTM；GeoAcquire支持需要账号的CDSE最新交付整包，也支持无需账号的AWS Open Data 2021 COG。 | [产品手册与访问说明](https://documentation.dataspace.copernicus.eu/Data/Others/CCM.html) / [AWS Open Data](https://registry.opendata.aws/copernicus-dem/) |
| Kruger National Park elevation models and orthomosaics | 高程 + 遥感影像 | DSM、DTM、CIR 正射影像 | 0.25 m | 2018 年 9–10 月采集；2021 年发布 | 南非克鲁格国家公园                                                               | WGS 84 / UTM 36S（EPSG:32736）；高程以 WGS 84 椭球为垂直基准 | [数据集 DOI](https://doi.org/10.5285/deab4235f1ef4cd79b73d0cbf2655bd7) | DMC 航空影像生成的亚米级全覆盖数据；CEDA 下载需要免费注册。当前为**手动补充**，尚未接入 GeoAcquire。                                                                                                                                          | [Heckel et al. (2021)](https://koedoe.co.za/index.php/koedoe/article/view/1679)；[论文 DOI](https://doi.org/10.4102/koedoe.v63i1.1679) |
| RGE ALTI® | 高程 | MNT / DTM | 1 m 与 5 m | 主要汇编 2010 年以来的 LiDAR；该产品于 2024 年停止更新 | 法国本土及海外领地；不含法属波利尼西亚、新喀里多尼亚、瓦利斯和富图纳，部分禁飞区为 NoData                        | 随地区变化；法国本土为 RGF93 / Lambert-93（EPSG:2154）及 IGN69 正常高，科西嘉为 IGN78，海外领地采用各自法定 CRS | [data.gouv.fr 下载页](https://www.data.gouv.fr/datasets/rge-alti-r) | 法国 IGN 的规则裸地高程网格；适合洪水、滑坡、林业和城市精细地形分析。当前为**手动补充**。                                                                                                                                                       | [法国 IGN 数据源](https://geoservices.ign.fr/rgealti) |
| PNOA-LiDAR | 高程 / 点云 | LiDAR LAZ、DSM、DTM；多期覆盖 | 点云约 0.5–5 点/平方米；常见栅格为 0.5 m、2 m、5 m，依覆盖期和产品而定 | 第一期 2008–2015；第二期 2015–2021；第三期 2022–2025 | 西班牙全国；第三期 0.5 m MDT 覆盖仍在补充                                              | 西班牙本土、巴利阿里群岛、休达和梅利利亚为 ETRS89 / 相应 UTM 分区；加那利群岛为 REGCAN95 / UTM；采用正高 | [CNIG MDT50 cm](https://centrodedescargas.cnig.es/CentroDescargas/modelo-digital-terreno-mdt50cm) | 多期 LiDAR 点云及派生产品；第三期包含约 5 点/平方米点云和 0.5 m DSM、DTM。GeoAcquire **已接入 MDT50 cm 小范围下载**：按真实 Polygon 查询并保存原生 COG；当前遵守匿名 20 文件上限，账号登录尚未接入。 | [西班牙 IGN 产品说明](https://pnoa.ign.es/pnoa-lidar/productos-a-descarga) |

## PE3D 补充说明与下载器参考

### 当前可下载产品

PE3D 官网在 2026-09-11 仍提供以下六个产品选项：

1. **Ortoimagem**：正射影像，GeoTIFF；全州产品为 0.5 m、8 bit，局部精细产品为 0.12 m、8 bit。
2. **Modelo Digital de Elevação (RASTER)**：包含建筑、树木、桥梁等地表目标的 MDE，即 DSM；GeoTIFF + TFW；全州产品为 1 m、32 bit，局部精细产品为 0.5 m、32 bit。
3. **Modelo Digital de Elevação (XYZI)**：激光点的 X、Y、Z 和回波强度 I；官网称作 MDE ASCII。
4. **Modelo Digital de Terreno (RASTER)**：剔除植被、建筑、桥梁等地物后的 MDT，即 DTM；GeoTIFF + TFW；全州产品为 1 m、32 bit，局部精细产品为 0.5 m、32 bit。
5. **Modelo Digital de Terreno (XYZ)**：裸地点的 X、Y、Z；官网称作 MDT ASCII。
6. **Intensidade-Hipsometria / MDE Composição**：激光回波强度与分层设色高程的合成影像；GeoTIFF + TFW，另带 PNG 色标；全州产品为 1 m、8 bit，局部精细产品为 0.5 m、8 bit。

所有产品使用 SIRGAS 2000 投影坐标，分属 UTM 24S 或 25S。官网称全州 1:5,000 正射影像满足 PEC A 级，激光高程误差优于 25 cm；1:1,000 局部产品的激光高程误差优于 10 cm。全州激光扫描约含 750 亿个点，平均约每 1.3 m² 一个点。

### 覆盖与可用性

- 1:5,000 级航空摄影与激光扫描覆盖伯南布哥州全境，官网列出的完成面积为 98,146 km²。
- 2026-09-11 实测官网下载地图的 `quadriculas_pe.json` 含 16,816 条“图幅—市镇”记录；同一图幅跨越
  多个市镇时会重复。按门户 `id_quad` 与图幅号去重后共有 12,962 个当前可选的 1:5,000 图幅。
- 1:1,000 级精细成果覆盖约 870 km² 的城区。PE3D 官网列出的 26 个市镇是：Arcoverde、Belo Jardim、Brejo da Madre de Deus、Carpina、Caruaru、Chã Grande、Escada、Garanhuns、Lajedo、Limoeiro、Nazaré da Mata、Paudalho、Pesqueira、Petrolina、Pombos、Ribeirão、Sanharó、Santa Cruz do Capibaribe、São Bento do Una、São Caetano、Surubim、Tacaimbó、Timbaúba、Toritama、Tracunhaém、Vitória de Santo Antão。
- 官网还说明 Compesa 另以相近精度完成 Goiana 和 Recife 都会区共 15 个市镇的测绘；不能在没有核对实际下载清单前，假定这些成果全部包含在 PE3D 门户的 1:1,000 下载范围内。
- 下载地图可按一个完整市镇或一个/多个图幅格网选择。深绿色图幅表示六类文件齐全，浅绿色表示除正射影像外的文件可用，因此“全州已测绘”不等于每个图幅的六类文件当前都能下载。
- 两份官方材料对精细测绘的市镇数存在差异：PE3D 门户列出 26 个市镇，而州政府现行项目页称 17 个城市。接入时应以门户实际图幅和文件清单为准，不应硬编码任一汇总数字。

### 访问方式与参考实现

- 数据免费下载，但下载阶段需要注册账号、提交登录凭据和 CAPTCHA。官网要求使用成果时注明伯南布哥州政府及州经济发展主管部门；门户没有给出可直接等同于开放数据许可的标准许可证，因此再分发前仍需核对授权范围。
- 门户当前只发送站点证书而遗漏 ZeroSSL 中间证书，浏览器会自动补链，但 Requests/OpenSSL 不会。
  GeoAcquire 为这个数据源提供经过核对的 ZeroSSL/Sectigo CA 链，并在登录、链接发现和文件传输
  三个阶段继续执行证书、主机名及有效期校验；没有用 `verify=False` 绕过问题。
- 下载流程是：选择一种产品，再选择一个完整市镇或一个/多个图幅；登录成功后，网站向 `baixararquivo.php` 提交产品代码、选择项和选择模式，并从返回页面取得实际 ZIP 文件链接。
- 门户没有独立的比例尺或分辨率选择项，但实际 1 m 样本链接采用
  `arquivos/1_5000/BLOCO-*/4_MDT_RASTER/MDT-<图幅号>.zip`。GeoAcquire 同时核对产品代码 `4`、
  路径段 `1_5000/4_MDT_RASTER` 和 `MDT-` 文件名前缀，只接受 1:5,000 的 1 m MDT；不会把
  `1_1000` 的 0.5 m 城区产品混入结果。
- 当前实现把任意输入范围转换到 WGS84，按巴西系统分幅规则计算相交的 1:5,000 图幅，再用官网
  `quadriculas_pe.json` 核对实际可用性并取得门户要求的 `id_quad - 市镇` 选择值。目录采用 10 天
  本地缓存；刷新失败时可继续使用最后一份有效缓存。下载和解压后的原生 GeoTIFF 保持约
  3.55 km × 2.42 km 的完整图幅及其边缘重叠；首版不拼接、裁剪、重采样或重投影。
- 可参考的 QGIS 插件：[weraclitof/qgis-pe3d-downloader](https://github.com/weraclitof/qgis-pe3d-downloader)（v1.0.0、MIT）。它实现了会话初始化、CAPTCHA 登录、按市镇与产品代码获取链接、最多四路并行下载 ZIP，以及解压并加载栅格，可作为站点协议和流程参考。
- 该插件适合验证下载思路，但不能原样作为 GeoAcquire 的生产实现：当前代码关闭 TLS 证书校验、用字符串切分解析 `iframe`、在多个下载线程间共享同一个 `requests.Session`、直接 `extractall`，且缺少失败闭环、重试、断点续传、内容校验和安全解压。正式接入时应保留协议认知，重新实现传输与落地层。

## 使用前注意

- 数据页面、账号要求、许可和服务接口会变化，正式使用或再发布前应重新查看各官方页面。
- “坐标系（源数据）”记录下载文件自身的水平 CRS，并在已知时同时列出垂直高程基准。最终对齐结果采用目标 TIF 的水平 CRS；GeoAcquire 当前不会自动完成垂直基准转换。
- 表中的空间分辨率是源产品规格；与目标 TIF 对齐不会提高源数据的真实精度。
- Google 影像有明确的缓存、离线使用和机器分析限制。公开发布或批处理前，应先确认当前实现、用途和账号协议均符合 Google Maps Platform 条款。
- 通过官方页面下载的数据默认不进入版本库；请放到项目外的数据目录，或确认保存位置已被 `.gitignore` 排除。
