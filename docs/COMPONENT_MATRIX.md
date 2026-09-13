# GeoAcquire 组件组合与 YAML 速查表

本页回答三个问题：输入怎样变成 Region、每个下载器产出什么、哪一种后处理可以接在后面。
完整背景和运行机制见 [REFERENCE.md](REFERENCE.md)，数据源覆盖与坐标系见
[DATA_SOURCES.md](DATA_SOURCES.md)。

## 1. 三类组件是独立选择，不按行绑定

一个 pipeline 固定由下面三层组成：

```text
RegionProvider（输入与范围） → Source（检索与下载） → Postprocessor（零个或多个后处理）
```

下面三列分别是三个独立组件目录。**同一行没有对应关系**；实际组合由后面的适配表决定。

| 数据处理大类：RegionProvider | 下载器大类：Source | 后处理大类：Postprocessor |
| --- | --- | --- |
| bbox、目标TIF或TIF目录<br>`geoacquire.regions.bounds.BoundsRegionProvider`<br><br>Polygon/MultiPolygon<br>`geoacquire.regions.vector.VectorRegionProvider`<br><br>Point按米扩展<br>`geoacquire.regions.point.PointRegionProvider` | USGS LiDAR<br>`geoacquire.sources.usgs.lidar.USGSLidarSource`<br><br>USGS 1 m DEM<br>`geoacquire.sources.usgs.dem_1m.USGSDEM1mSource`<br><br>LINZ 1 m DEM<br>`geoacquire.sources.linz.dem_1m.LINZDEM1mSource`<br><br>CNIG MDT50 cm<br>`geoacquire.sources.cnig.source.CNIGMDTSource`<br><br>Google XYZ<br>`geoacquire.sources.rs.google.GoogleSource`<br><br>Esri Wayback<br>`geoacquire.sources.rs.wayback.WaybackSource`<br><br>PE3D<br>`geoacquire.sources.pe3d.source.PE3DSource`<br><br>Copernicus DEM<br>`geoacquire.sources.copdem.source.CopDEMSource` | 不处理<br>`postprocess: []`<br><br>Point原生网格固定米制窗口<br>`geoacquire.postprocess.native_point_crop.NativePointWindowPostprocessor`<br><br>目标TIF拼接、重投影和对齐<br>`geoacquire.postprocess.raster_align.RasterAlignToTargetPostprocessor`<br><br>只打印报告<br>`geoacquire.postprocess.print_step.PrintPostprocessor` |

三个内置 RegionProvider 都先输出统一的 `dict[str, Region]`；8个Source都通过统一的
`BaseSource.acquire(regions, context, options)` 接口接收它。Source类在YAML的 `source.class_path`
中选择。普通HTTP传输另由共享的 `geoacquire.services.http_download.HTTPDownloadService` 执行，
它在程序启动时注入，不是一个需要在每个pipeline中重复配置的Source。

### 最常用的三层组合

以下是推荐组合，不是Source的硬编码绑定：

| 任务 | RegionProvider | 可选 Source | Postprocessor | 关系 |
| --- | --- | --- | --- | --- |
| Point生成原生网格固定米制GT | `geoacquire.regions.point.PointRegionProvider` | `geoacquire.sources.pe3d.source.PE3DSource`；`geoacquire.sources.linz.dem_1m.LINZDEM1mSource`；`geoacquire.sources.cnig.source.CNIGMDTSource`；`geoacquire.sources.usgs.dem_1m.USGSDEM1mSource`；产生DSM/DTM栅格的 `geoacquire.sources.usgs.lidar.USGSLidarSource`；`geoacquire.sources.copdem.source.CopDEMSource` | `geoacquire.postprocess.native_point_crop.NativePointWindowPostprocessor` | **强适配：后处理需要Point元数据**；默认不重采样，可选择受限缺口补偿；Source仍可更换 |
| 目标TIF严格网格对齐 | 目标TIF模式的 `geoacquire.regions.bounds.BoundsRegionProvider` | 任意输出 `kind=raster` 的Source，包括上列高程Source，以及GeoTIFF模式的Google/Wayback | `geoacquire.postprocess.raster_align.RasterAlignToTargetPostprocessor` | **强适配：后处理需要目标TIF的shape/transform** |
| Polygon按真实边界选片并保留原生文件 | `geoacquire.regions.vector.VectorRegionProvider` | `geoacquire.sources.pe3d.source.PE3DSource`；`geoacquire.sources.linz.dem_1m.LINZDEM1mSource`；`geoacquire.sources.cnig.source.CNIGMDTSource` 最适合；其他Source也接受Region但可能按bbox选片 | `postprocess: []` | 推荐组合，不是一一绑定 |
| bbox或任意Region只下载 | 任意RegionProvider | 覆盖该地理位置的任意Source | `postprocess: []` | 完全通用 |
| DEM GT对齐影像的第二次运行 | 目标TIF目录模式的 `geoacquire.regions.bounds.BoundsRegionProvider` | `geoacquire.sources.rs.google.GoogleSource` 或 `geoacquire.sources.rs.wayback.WaybackSource`，必须 `output_format: geotiff` | `geoacquire.postprocess.raster_align.RasterAlignToTargetPostprocessor` | 推荐组合：影像网格跟随第一次生成的DEM GT |

重要边界：

- 任意 RegionProvider 都可以给任意 Source 提供统一 Region；数据源实际覆盖范围仍由 Source 决定。
- `postprocess: []` 对所有 Source 都合法，表示只保留采集结果。
- `RasterAlignToTargetPostprocessor` 必须获得目标 TIF 的网格信息；坐标 bbox 和 Polygon 本身只有范围，
  没有目标行列数和仿射网格。
- `NativePointWindowPostprocessor` 必须获得 PointProvider 写入的原始点信息，而且上游必须输出栅格。
- 后处理通过资产的 `kind` 和 `product` 连接，不通过文件名猜测内容。
- 当前两个栅格处理器都会保留原资产并在报告中增加新资产。通常一个 pipeline 只配置其中一个；
  不要在同一 pipeline 中对同一种 `product` 连续串联二者，否则后一步可能同时选中原始栅格和前一步结果。

## 2. YAML 总体结构

```yaml
region:
  class_path: 完整的.RegionProvider.类路径
  init_args:
    # 该 RegionProvider 的构造参数

reporting:
  json_path: null
  log_path: null
  overwrite: false

pipelines:
  - name: 唯一名称
    enable: true
    postprocess_workers: 1
    source:
      class_path: 完整的.Source.类路径
      init_args:
        # 该 Source 的构造参数
    acquire:
      output_dir: ./output/raw
      skip_existing: true
      max_retries: 3
      max_workers: 4
      chunk_size: 1048576
      retry_delay: 2.0
    postprocess:
      - class_path: 完整的.Postprocessor.类路径
        init_args:
          # 该 Postprocessor 的构造参数
```

`class_path` 改成另一个类时，该组件原来的 `init_args` 会整体替换；保持同一个 `class_path` 时，
后加载 YAML 只需覆盖要改变的参数。

## 3. 输入与 RegionProvider

| 类与 `class_path` | 接受的输入 | 必填参数 | 可选参数与默认值 | 产生的 Region 信息 |
| --- | --- | --- | --- | --- |
| `BoundsRegionProvider`<br>`geoacquire.regions.bounds.BoundsRegionProvider` | 单 bbox、多个命名 bbox、单个 `.tif/.tiff`、直接包含 TIF 的目录 | `source` | `region: null`；`crs: EPSG:4326`；`exclude_suffixes: null`；`skip_existing_suffixes: null` | bbox 产生范围和声明 CRS；TIF 还产生 `target_path`、shape、transform，可供精确网格对齐 |
| `VectorRegionProvider`<br>`geoacquire.regions.vector.VectorRegionProvider` | GeoPandas 可读的 Polygon/MultiPolygon 文件，如 SHP、GeoJSON、GPKG | `source` | `id_field: null`；`crs_override: null`；`layer: null` | 每个要素一个 Region，保存真实几何和输入 CRS，不生成目标像元网格 |
| `PointRegionProvider`<br>`geoacquire.regions.point.PointRegionProvider` | GeoPandas 可读的 Point 文件，如 SHP、GeoJSON、GPKG | `source` | `id_field: null`；`query_size_m: 500.0`；`include_values: null`；`crs_override: null`；`layer: null` | 每个点一个米制查询 Region，同时保存原始点坐标、点 CRS、属性和 `query_size_m` |

### RegionProvider 参数含义

| 参数 | 使用类 | 含义 |
| --- | --- | --- |
| `source` | 全部 | bbox、矢量路径、目标 TIF 路径或 TIF 目录，具体格式由所选类决定 |
| `region` | Bounds | 单个 Region 名；目录输入可传名称列表；`null` 自动取文件名或 `region_0` |
| `crs` | Bounds | 只用于坐标 bbox；TIF 始终读取文件自身 CRS |
| `exclude_suffixes` | Bounds | 目录扫描时排除指定结果后缀，避免把输出再次当输入 |
| `skip_existing_suffixes` | Bounds | 目录中已同时存在指定兄弟结果时跳过该目标 TIF |
| `id_field` | Vector、Point | 用哪个属性字段生成 Region ID；必须唯一且非空 |
| `query_size_m` | Point | Source 检索范围的边长，单位为地面米；它不等于最终裁剪尺寸 |
| `include_values` | Point | 属性白名单，例如 `{size: [large], data_type: [test]}`；字段之间为 AND，同字段值之间为 OR |
| `crs_override` | Vector、Point | 仅在原文件 CRS 缺失或写错时强制声明真实 CRS；不会重投影坐标 |
| `layer` | Vector、Point | 多图层数据源中的图层名；普通 SHP 通常保持 `null` |

输入可以是地理坐标系或投影坐标系。PointProvider 对地理坐标使用局部测地估算扩展米制范围；
对投影坐标则依据 CRS 轴单位换算。`crs_override` 是修正错误声明，不是坐标转换工具。

## 4. Source 下载器

### 下载器与输出契约

| 下载器与 `class_path` | 覆盖/数据 | 输出 `kind:product` | 可接的栅格后处理 | 关键限制 |
| --- | --- | --- | --- | --- |
| `USGSLidarSource`<br>`geoacquire.sources.usgs.lidar.USGSLidarSource` | 美国 USGS 3DEP LiDAR | `point_cloud:laz`；可选 `raster:dsm`、`raster:dtm` | 只有配置了 `dsm` 或 `dtm` 才能接 | WESM 很大；栅格化规则随项目；`audit` 模式不下载 |
| `USGSDEM1mSource`<br>`geoacquire.sources.usgs.dem_1m.USGSDEM1mSource` | 美国 USGS 3DEP 1 m DEM | `raster:dem` | 两种栅格处理器均可 | 原生 CRS/高程基准随图幅位置变化 |
| `LINZDEM1mSource`<br>`geoacquire.sources.linz.dem_1m.LINZDEM1mSource` | 新西兰全国 LiDAR 1 m DEM | `raster:dem` | 两种栅格处理器均可 | 下载命中的完整 COG，不做服务器端 bbox 裁剪 |
| `CNIGMDTSource`<br>`geoacquire.sources.cnig.source.CNIGMDTSource` | 西班牙 PNOA 第三期 MDT 0.5 m | `raster:dtm` | 两种栅格处理器均可 | 匿名任务最多选择20个唯一源文件 |
| `GoogleSource`<br>`geoacquire.sources.rs.google.GoogleSource` | Google XYZ 影像 | `image:imagery` 或 `raster:imagery` | 仅 `output_format: geotiff` 时可接 | 落地 GeoTIFF 为 EPSG:3857；服务与授权状态需自行确认 |
| `WaybackSource`<br>`geoacquire.sources.rs.wayback.WaybackSource` | Esri World Imagery Wayback | `image:imagery` 或 `raster:imagery` | 仅 `output_format: geotiff` 时可接 | `date` 是发布版本日期，不一定是影像拍摄日 |
| `PE3DSource`<br>`geoacquire.sources.pe3d.source.PE3DSource` | 巴西伯南布哥州 PE3D | 当前为 `raster:dtm` | 两种栅格处理器均可 | 当前只实现 `product: dtm_raster`；账号和每次新会话 CAPTCHA |
| `CopDEMSource`<br>`geoacquire.sources.copdem.source.CopDEMSource` | 全球 Copernicus DEM 30/90 m | `raster:dem` | 两种栅格处理器均可 | 需 CDSE 账号；30/90 m 像元不能严格组成448 m原生窗口 |

### 所有 Source `init_args`

| Source | 参数（括号内为代码默认值） |
| --- | --- |
| `USGSLidarSource` | `output_products: null`（代码默认仅 LAZ；项目默认 YAML 为 `[laz, dsm]`）；`raster_resolution: 1.0`；`exclude_classes: null`（默认噪声类7、18）；`point_chunk_size: 2000000`；`keep_download: true`；`project_catalog_path: ./configs/usgs_lidar_projects.yaml`；`mode: download`；`audit_report_path: ./logs/usgs_lidar_audit.json`；`wesm_cache_dir: ./cache/wesm`；`link_cache_dir: ./cache/link_lists`；`link_cache_days: 10.0` |
| `USGSDEM1mSource` | `hemisphere: north`；`filename_suffix: usgs_dem_1m`；`wesm_cache_dir: ./cache/wesm`；`link_cache_dir: ./cache/link_lists`；`link_cache_days: 10.0` |
| `LINZDEM1mSource` | `collection_url`（官方1 m Collection）；`catalog_cache_path: ./cache/linz/nz_dem_1m_stac.json`；`catalog_cache_days: 10.0`；`catalog_workers: 16`；`filename_suffix: nz_dem_1m` |
| `CNIGMDTSource` | `product: mdt50cm`；`footprint_cache_path: ./cache/cnig/mdt50cm_footprints.json`；`query_batch_size: 100`；`footprint_workers: 4`；`request_timeout: 60.0` |
| `GoogleSource` | `server`（内置 XYZ 模板）；`zoom: 18`；`tile_ext: jpg`；`output_format: geotiff`；`keep_download: true` |
| `WaybackSource` | `date: null`；`version: null`；`release_date: null`；`server`（内置 WMTS 模板）；`zoom: 18`；`tile_ext: jpg`；`output_format: geotiff`；`keep_download: true` |
| `PE3DSource` | `username`、`password`（必填）；`product: dtm_raster`；`captcha_path: ./cache/pe3d/captcha.png`；`catalog_cache_path: ./cache/pe3d/quadriculas_pe.json`；`catalog_cache_days: 10.0`；`auth_attempts: 3`；`batch_size: 100`；`keep_archive: false`；`verify_tls: true`；`ca_bundle_path: null`；`quadrangle_mode: quad`；`min_intersection_fraction: 0.0` |
| `CopDEMSource` | `username`、`password`（必填）；`resolution: "30"`；`dem_format: DGED`；`keep_archive: false`；`filename_suffix: copdem` |

真实账号密码只放在 `.gitignore` 已排除的 `configs/private_*.yaml`，不要写入默认配置、示例或文档。

### 通用 `acquire` 参数

`acquire` 不是带 `class_path` 的组件，而是每个 pipeline 共用的下载执行参数：

| 参数 | 默认值 | 输入/输出含义 |
| --- | ---: | --- |
| `output_dir` | `null` | 原始下载/派生成果目录；`null` 时目标 TIF 场景跟随目标目录，其他场景使用工作区 `output` |
| `skip_existing` | `true` | 最终文件已完整存在时复用；`.part` 仍可用于续传 |
| `max_retries` | `3` | 首次失败后的额外重试次数 |
| `max_workers` | `4` | 并发“下载＋Source内部转换”的文件任务数；CopDEM 当前仍串行 |
| `chunk_size` | `1048576` | HTTP 流式读写块大小，单位字节 |
| `retry_delay` | `2.0` | 指数退避的基础秒数，`0` 表示立即重试 |

## 5. Postprocessor 后处理器

| 后处理器与 `class_path` | 必需输入 | 输出 | 是否重采样 |
| --- | --- | --- | --- |
| `NativePointWindowPostprocessor`<br>`geoacquire.postprocess.native_point_crop.NativePointWindowPostprocessor` | PointProvider 元数据；同一种 `raster:dem/dtm/dsm/imagery` | `<region_id>[_<output_suffix>].tif`；保持输入产品语义 | 默认否。可显式启用受限接缝补偿，只插值原生复制后仍缺失的像元 |
| `RasterAlignToTargetPostprocessor`<br>`geoacquire.postprocess.raster_align.RasterAlignToTargetPostprocessor` | 目标 TIF 网格；同一种栅格产品 | `<region_id>_<output_suffix>.tif`；继承输入产品语义 | `resample: true` 时会；`false` 时只按范围裁剪并保留源网格 |
| `PrintPostprocessor`<br>`geoacquire.postprocess.print_step.PrintPostprocessor` | 任意报告 | 不创建文件，只打印并原样返回报告 | 否 |

### `NativePointWindowPostprocessor` 参数

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `size_m` | 必填 | 请求的最终窗口边长，单位地面米 |
| `input_products` | 必填、非空 | 只选择一种栅格产品，例如 `[dtm]`、`[dem]` 或 `[dsm]` |
| `output_dir` | `null` | `null` 跟随第一个输入栅格目录；也可指定独立 GT 目录 |
| `output_suffix` | `null` | `null` 输出 `<region_id>.tif`；例如 `gt` 输出 `<region_id>_gt.tif` |
| `min_valid_fraction` | `1.0` | 输出窗口中要求的最小有效像元比例，范围0到1 |
| `max_grid_snap_pixels` | `0.0` | 多源图幅原点允许的最大网格吸附量，范围0到0.5像元；非零必须显式接受 |
| `skip_existing` | `true` | 目标结果存在时跳过重写 |
| `fallback_resampling` | `null` | `null` 保持严格原生复制；`bilinear` 允许仅对剩余缺口进行双线性补偿 |
| `max_resolution_mismatch_fraction` | `0.001` | 补偿候选相对主图幅的最大分辨率差异；`0.001` 表示0.1%，仅在 fallback 启用时生效 |
| `max_resampled_fraction` | `0.25` | 输出中允许补偿的最大像元比例；超过时整个目标仍失败 |

`size_m / 原生像元尺寸` 必须转换成整数行列。因此1 m数据通常得到448×448；0.5 m数据得到
896×896；30 m CopDEM 最接近的是15像元，即约450 m，不可能在“保持主图幅原生网格”和
“严格448 m”之间同时满足两者。可选 fallback 只处理相邻图幅接缝，不改变这个尺寸计算。

### `RasterAlignToTargetPostprocessor` 参数

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `output_suffix` | 必填 | 输出文件后缀，生成 `<region_id>_<suffix>.tif` |
| `input_products` | `null` | 栅格产品白名单；建议显式写 `[dem]`、`[dtm]`、`[dsm]` 或 `[imagery]`；`[]` 非法 |
| `output_dir` | `null` | `null` 跟随第一个输入栅格；也可指定统一结果目录 |
| `resample` | `true` | `true` 对齐目标网格；`false` 保留源分辨率，仅按目标范围裁剪 |
| `target_scale` | `1` | 目标网格降采样整数倍；仅 `resample: true` 可使用大于1的值 |
| `resampling` | `bilinear` | 重采样方法，如 `nearest`、`bilinear`、`cubic`、`average`、`mode` |
| `merge_method` | `first` | 多源重叠时使用 `first` 或 `last` |
| `window_size` | `1024` | 分块读写边长，影响内存和 I/O，不改变空间结果 |
| `skip_existing` | `true` | 最终文件存在时跳过重写 |
| `vertical_datum_policy` | `ignore` | `ignore` 或 `warn_once`；只提示，不执行垂直基准转换 |
| `skip_without_target` | `true` | Region 没有目标 TIF 时跳过；`false` 则记录失败 |

连续高程与影像通常使用 `bilinear/cubic/average`；土地覆盖等类别栅格通常使用 `nearest/mode`。

## 6. 三层适配矩阵

### RegionProvider × Source

在公开接口上，8个Source都接收相同的 `dict[str, Region]`。下面的差异是选片精度和地理覆盖，
不是方法签名不同。

| Source完整 `class_path` | Bounds bbox/目标TIF | Vector Polygon | Point扩展Region | 实际选片方式 |
| --- | :---: | :---: | :---: | --- |
| `geoacquire.sources.usgs.lidar.USGSLidarSource` | ✓ | ✓ | ✓ | WESM与项目规则按Region范围选片；是否有数据取决于USGS覆盖 |
| `geoacquire.sources.usgs.dem_1m.USGSDEM1mSource` | ✓ | ✓ | ✓ | WESM与10 km原生图幅按Region范围选片 |
| `geoacquire.sources.linz.dem_1m.LINZDEM1mSource` | ✓ | ✓ | ✓ | Vector保留真实Polygon相交；其他输入使用Region矩形 |
| `geoacquire.sources.cnig.source.CNIGMDTSource` | ✓ | ✓ | ✓ | Vector保留真实Polygon相交；其他输入使用Region矩形 |
| `geoacquire.sources.rs.google.GoogleSource` | ✓ | ✓ | ✓ | 全部转换为WGS84 bbox后计算XYZ瓦片；不按Polygon形状裁瓦片 |
| `geoacquire.sources.rs.wayback.WaybackSource` | ✓ | ✓ | ✓ | 全部转换为WGS84 bbox后计算XYZ瓦片；不按Polygon形状裁瓦片 |
| `geoacquire.sources.pe3d.source.PE3DSource` | ✓ | ✓ | ✓ | Vector使用真实Polygon过滤；Point使用按米扩展后的矩形；仅有PE州覆盖 |
| `geoacquire.sources.copdem.source.CopDEMSource` | ✓ | ✓ | ✓ | 按WGS84 bbox选择1°原生瓦片，不按Polygon形状裁瓦片 |

`BoundsRegionProvider` 的目标TIF模式虽然携带额外网格信息，但Source仍只把它当一个Region范围；
shape/transform留给后处理使用。PointProvider的原始点元数据同样不会把Source绑定到Point模式，
Source看到的下载范围仍是普通Region。

### RegionProvider × Postprocessor

这里列的是当前三个内置RegionProvider产生的元数据。它清楚标出真正接近一一对应的两组关系。

| RegionProvider输入模式 | `postprocess: []` | `NativePointWindowPostprocessor` | `RasterAlignToTargetPostprocessor` | `PrintPostprocessor` |
| --- | :---: | :---: | :---: | :---: |
| Bounds：坐标bbox/多个bbox | ✓ | ✗，没有原始点 | ✗，没有目标网格 | ✓ |
| Bounds：单个目标TIF/TIF目录 | ✓ | ✗，没有原始点 | **✓ 最适配** | ✓ |
| Vector：Polygon/MultiPolygon | ✓ | ✗，没有原始点 | ✗，没有目标shape/transform | ✓ |
| Point：按 `query_size_m` 扩展 | ✓ | **✓ 唯一内置适配输入** | ✗，没有目标shape/transform | ✓ |

因此：

- `PointRegionProvider → NativePointWindowPostprocessor` 是内置组件中的强适配关系。最终裁剪范围由
  Point输入的原始中心、`size_m` 和源栅格原生像元网格共同决定；`query_size_m` 只负责预留下载容差。
- “目标TIF模式的 `BoundsRegionProvider` → RasterAlignToTargetPostprocessor”是另一组强适配关系。
  最终范围、CRS、行列数和transform由目标TIF决定。
- Source夹在中间可以更换，只要它覆盖目标区域并输出后处理需要的栅格产品。

### Source × Postprocessor

| Source完整 `class_path` | 无后处理 `[]` | `NativePointWindow` | `RasterAlignToTarget` | 对应 `input_products` |
| --- | :---: | :---: | :---: | --- |
| `geoacquire.sources.usgs.lidar.USGSLidarSource`，仅 `[laz]` | ✓ | ✗ | ✗ | 无栅格产品 |
| `geoacquire.sources.usgs.lidar.USGSLidarSource`，含 `dsm` | ✓ | ✓ | ✓ | `[dsm]` |
| `geoacquire.sources.usgs.lidar.USGSLidarSource`，含 `dtm` | ✓ | ✓ | ✓ | `[dtm]` |
| `geoacquire.sources.usgs.dem_1m.USGSDEM1mSource` | ✓ | ✓ | ✓ | `[dem]` |
| `geoacquire.sources.linz.dem_1m.LINZDEM1mSource` | ✓ | ✓ | ✓ | `[dem]` |
| `geoacquire.sources.cnig.source.CNIGMDTSource` | ✓ | ✓ | ✓ | `[dtm]` |
| `geoacquire.sources.rs.google.GoogleSource` / `geoacquire.sources.rs.wayback.WaybackSource`，`output_format: image` | ✓ | ✗ | ✗ | 输出只有 `image:imagery` |
| `geoacquire.sources.rs.google.GoogleSource` / `geoacquire.sources.rs.wayback.WaybackSource`，`output_format: geotiff` | ✓ | 可用但不推荐直接作GT | ✓ | `[imagery]` |
| `geoacquire.sources.pe3d.source.PE3DSource`，`product: dtm_raster` | ✓ | ✓ | ✓ | `[dtm]` |
| `geoacquire.sources.copdem.source.CopDEMSource` | ✓ | 可用但448 m只能近似 | ✓ | `[dem]` |

矩阵中的 `NativePointWindow` 还要求 Region 来自 PointProvider；`RasterAlignToTarget` 还要求 Region
来自单个/目录目标 TIF。仅仅产品类型相符，不代表输入 Region 已包含后处理所需元数据。

## 7. 三个最常用的 YAML 片段

### Point → 原生 DEM/DTM → 固定米制 GT

```yaml
region:
  class_path: geoacquire.regions.point.PointRegionProvider
  init_args:
    source: E:/data/points.shp
    id_field: ID
    query_size_m: 500.0

pipelines:
  - name: pe3d_dtm_1m
    enable: true
    source:
      class_path: geoacquire.sources.pe3d.source.PE3DSource
      init_args:
        # username/password 由 private_pe3d.yaml 覆盖
        product: dtm_raster
    acquire:
      output_dir: E:/data/raw_dem
    postprocess:
      - class_path: geoacquire.postprocess.native_point_crop.NativePointWindowPostprocessor
        init_args:
          size_m: 448.0
          input_products: [dtm]
          output_dir: E:/data/gt
          max_grid_snap_pixels: 0.5
          fallback_resampling: bilinear
          max_resolution_mismatch_fraction: 0.001
          max_resampled_fraction: 0.25
```

### Polygon → 只下载原生数据

```yaml
region:
  class_path: geoacquire.regions.vector.VectorRegionProvider
  init_args:
    source: E:/data/areas.shp
    id_field: ID

pipelines:
  - name: linz_nz_dem_1m
    enable: true
    source:
      class_path: geoacquire.sources.linz.dem_1m.LINZDEM1mSource
      init_args: {}
    acquire:
      output_dir: E:/data/raw_dem
    postprocess: []
```

### 目标 DEM TIF → 下载并对齐影像

```yaml
region:
  class_path: geoacquire.regions.bounds.BoundsRegionProvider
  init_args:
    source: E:/data/gt
    exclude_suffixes: [google, wayback]

pipelines:
  - name: google
    enable: true
    source:
      class_path: geoacquire.sources.rs.google.GoogleSource
      init_args:
        zoom: 18
        tile_ext: jpg
        output_format: geotiff
        keep_download: false
    acquire:
      output_dir: E:/data/google_raw
    postprocess:
      - class_path: geoacquire.postprocess.raster_align.RasterAlignToTargetPostprocessor
        init_args:
          output_suffix: google
          input_products: [imagery]
          output_dir: E:/data/google_gt
          resample: true
          target_scale: 1
          resampling: bilinear
```

完整默认值仍以 [default.yaml](../configs/default.yaml) 或中文注释版
[default_zh.yaml](../configs/default_zh.yaml) 为准。可运行场景位于
[`configs/examples/`](../configs/examples/)。
