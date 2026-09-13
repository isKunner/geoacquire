# GeoAcquire

GeoAcquire 根据指定的地理范围下载高程、LiDAR 和影像数据。输入支持五种形式：

1. 坐标范围 `[minx, miny, maxx, maxy]`；
2. 单个目标 GeoTIFF；
3. 直接包含多个目标 GeoTIFF 的目录；
4. 包含多个 Polygon/MultiPolygon 要素的 Shapefile 或其他 GeoPandas 可读矢量文件。
5. 包含多个 Point 要素的矢量文件；程序可按米扩展查询范围，再在原生 DEM 网格上裁剪固定大小。

目标 GeoTIFF 只提供覆盖范围、坐标系和目标网格，不会被修改。

## 目前能下载什么

每个数据源都有一份可以直接编辑的示例 YAML。新人推荐修改 YAML 中的输入输出路径，然后运行
对应 YAML；常规使用不需要写一长串命令行参数。

| 数据 | Pipeline 名称 | 下载内容 | 源数据坐标系 / 高程基准 | 直接编辑的示例 YAML |
| --- | --- | --- | --- | --- |
| USGS 3DEP LiDAR | `usgs_lidar` | LAZ、DSM、DTM | 随项目 / workunit 变化；通常为 NAD83 系列的 UTM、State Plane 或 Albers，也可能采用当地基准；以 WESM 和 LAZ 文件头为准 | [`configs/examples/usgs_lidar.yaml`](configs/examples/usgs_lidar.yaml) |
| USGS 3DEP 1 m DEM | `usgs_dem_1m` | GeoTIFF DEM | NAD83 / UTM，分区随位置变化；通常为 NAVD88 高程，美国部分领地采用当地高程基准；以 GeoTIFF 文件头为准 | [`configs/examples/usgs_dem_1m.yaml`](configs/examples/usgs_dem_1m.yaml) |
| LINZ New Zealand LiDAR 1 m DEM | `linz_nz_dem_1m` | 原生 COG DEM | NZTM2000（EPSG:2193）；NZVD2016 高程（EPSG:7839） | [范围示例](configs/examples/linz_nz_dem_1m.yaml) / [Point→448 m 示例](configs/examples/linz_nz_dem_1m_point.yaml) |
| Spain CNIG/PNOA MDT50 cm | `cnig_spain_mdt50cm` | 第三期原生 COG DTM | ETRS89 / 相应 UTM 分区；加那利群岛为 REGCAN95 / UTM 28N；正高 | [范围示例](configs/examples/cnig_spain_mdt50cm.yaml) / [Point→448 m 示例](configs/examples/cnig_spain_mdt50cm_point.yaml) |
| Google XYZ 影像 | `google` | 影像及地理配准 GeoTIFF | XYZ Web Mercator；GeoAcquire 落地 GeoTIFF 为 EPSG:3857 | [`configs/examples/google.yaml`](configs/examples/google.yaml) |
| Esri Wayback 影像 | `wayback` | 历史影像及地理配准 GeoTIFF | WMTS / Web Mercator；GeoAcquire 落地 GeoTIFF 为 EPSG:3857 | [`configs/examples/wayback.yaml`](configs/examples/wayback.yaml) |
| PE3D 1 m MDT | `pe3d_dtm_1m` | 1:5,000 原生 GeoTIFF 裸地高程图幅 | SIRGAS 2000 / UTM 24S 或 25S（EPSG:31984 / 31985）；垂直基准未声明 | [范围示例](configs/examples/pe3d_dtm_1m.yaml) / [多 Polygon SHP 示例](configs/examples/pe3d_dtm_1m_shapefile.yaml) / [Point→448 m 示例](configs/examples/pe3d_dtm_1m_point.yaml) |
| Copernicus DEM | `copdem` | 30 m 或 90 m DEM | WGS 84 经纬度（EPSG:4326）；EGM2008 正高（EPSG:3855） | [`configs/examples/copdem.yaml`](configs/examples/copdem.yaml) |

USGS 数据主要覆盖美国，LINZ 数据覆盖新西兰，CNIG/PNOA 数据覆盖当前已发布的西班牙区域；
Google、Wayback 和 Copernicus 的实际结果取决于各服务的覆盖情况。默认Copernicus配置下载
CDSE最新交付整包并需要账号；也可改用无需账号的AWS Open Data 2021 COG Source。CNIG当前允许
匿名下载最多20个文件，账号登录尚未接入，因此首版用于小范围验证。

表中的坐标系是下载或落地后的**源数据坐标系**。如果配置了目标 GeoTIFF，最终对齐结果使用
目标 GeoTIFF 的水平坐标系和网格；当前流程只做水平重投影，不会自动转换不同的垂直高程基准。

每个数据源的类型、源分辨率、采集或发布时间、国家或地区范围、官方下载入口和相关文献，统一整理在
[`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md)。其中既包括项目已经接入的数据，也包括暂时需要
从官方网页手动下载、以后可以继续接入的补充数据。

如果要判断“哪一种输入可以接哪一个下载器、下载结果又能接哪一种后处理”，直接查看
[`docs/COMPONENT_MATRIX.md`](docs/COMPONENT_MATRIX.md)。该表列出了全部内置类的 `class_path`、
`init_args`、输入输出产品和推荐组合。

## 1. 创建 Conda 环境

下面创建项目专用的 `geoacquire` 环境，并根据 `requirements.txt` 安装依赖：

```bash
conda create -n geoacquire python=3.11 -y
conda activate geoacquire
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

以后重新进入项目时，只需要执行 `conda activate geoacquire`。

## 2. 首次使用 USGS：提前下载 `WESM.gpkg`

USGS LiDAR 和 USGS 1 m DEM 都需要 WESM（Work Unit Extent Spatial Metadata）来查询某个范围
对应哪些 USGS 项目。这个 GeoPackage 超过 3 GB，而且 USGS 会持续更新。由于文件很大，推荐先用
浏览器下载，不要等程序第一次运行时临时下载。

官方下载：

- [`WESM.gpkg` 直接下载链接](https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/metadata/WESM.gpkg)
- [USGS 3DEP Spatial Metadata 说明页](https://www.usgs.gov/3d-elevation-program/3dep-spatial-metadata)

下载完成后，确认文件名严格为 `WESM.gpkg`，并保存到项目目录：

```text
GeoAcquire/
└── cache/
    └── wesm/
        └── WESM.gpkg
```

例如项目位于 `D:/GeoAcquire` 时，完整位置是：

```text
D:/GeoAcquire/cache/wesm/WESM.gpkg
```

如果 `cache/wesm/` 不存在，可以手动创建。不要把文件放在 `downloads/`、项目根目录或
`configs/` 中。项目只会在 `cache/wesm/WESM.gpkg` 查找它。

不提前下载也可以：程序发现文件不存在时会自动从同一地址下载到
`cache/wesm/WESM.gpkg.part`，完整后再改名为 `WESM.gpkg`，中断后可以续传。但大文件通过浏览器
下载通常更直观，也更容易确认进度和失败。`cache/` 已加入 `.gitignore`，不需要上传 WESM。

## 3. 内置的 San Juan 示例

项目已经包含一个可直接运行的目标 TIF：

```text
./examples/USA_SanJuan_Example/input/USA_SanJuan_Example.tif
```

它位于美国 San Juan，CRS 为 `EPSG:26912`，WGS84 范围约为
`[-109.860992, 38.112812, -109.855819, 38.116899]`。美国适用的数据源 YAML 默认读取这个文件，
下载、结果和日志统一放在 `examples/USA_SanJuan_Example/` 下。完整目录说明见
[`examples/USA_SanJuan_Example/README.md`](examples/USA_SanJuan_Example/README.md)。

## 4. 在 YAML 中修改输入输出

以 [`configs/examples/usgs_lidar.yaml`](configs/examples/usgs_lidar.yaml) 为例，需要修改的主要内容
已经直接写在文件中：

```yaml
region:
  init_args:
    source: ./examples/USA_SanJuan_Example/input/USA_SanJuan_Example.tif
    region: null
    crs: EPSG:4326

pipelines:
  - name: usgs_lidar
    enable: true
    acquire:
      output_dir: ./examples/USA_SanJuan_Example/downloads/usgs_lidar
    postprocess:
      - init_args:
          output_dir: ./examples/USA_SanJuan_Example/results/usgs_lidar
```

| YAML 位置 | 含义 |
| --- | --- |
| `region.init_args.source` | 输入 bbox、目标 TIF/TIF 目录，或由所选 RegionProvider 读取的 Polygon/Point 矢量文件 |
| `pipelines.<名称>.acquire.output_dir` | LAZ、源影像、DEM 和中间文件的下载目录 |
| `pipelines.<名称>.postprocess[0].init_args.output_dir` | 与目标 TIF 对齐的最终结果目录 |

输入可以直接改成以下任意一种：

```yaml
# 坐标范围，顺序固定为 [minx, miny, maxx, maxy]
source: [116.38, 39.90, 116.40, 39.92]
region: beijing_bbox
crs: EPSG:4326

# 单个目标 TIF
source: ./examples/USA_SanJuan_Example/input/USA_SanJuan_Example.tif
region: null

# 目标 TIF 目录
source: ./examples/USA_SanJuan_Example/input
region: null
```

Point 文件要同时更换 RegionProvider；`query_size_m` 是下载查询范围，最终尺寸由后处理的
`size_m` 决定：

```yaml
region:
  class_path: geoacquire.regions.point.PointRegionProvider
  init_args:
    source: E:/data/dams.shp
    id_field: ID
    query_size_m: 500
    include_values: null
    crs_override: null
    layer: null
```

纯 bbox 没有目标 TIF 网格，因此只把选中的源数据写入 `acquire.output_dir`，并跳过目标对齐；
此时结果目录不会产生对齐文件。使用目标 TIF 或 TIF 目录时，最终结果写入
`postprocess[0].init_args.output_dir`。

## 5. 检查并开始下载

内置示例路径已经配置好，可以直接检查：

```bash
python run.py -c configs/default.yaml configs/examples/usgs_lidar.yaml --check
```

检查通过后，去掉 `--check` 开始下载：

```bash
python run.py -c configs/default.yaml configs/examples/usgs_lidar.yaml
```

其他数据源只需要换成表格中的示例 YAML。例如 Google：

```bash
python run.py -c configs/default.yaml configs/examples/google.yaml --check
python run.py -c configs/default.yaml configs/examples/google.yaml
```

Copernicus DEM 第一次使用时，先复制
[`configs/private_copdem.example.yaml`](configs/private_copdem.example.yaml)，将副本重命名为
`configs/private_copdem.yaml`。然后只修改副本中的两个值：

```yaml
pipelines:
  - name: copdem
    source:
      init_args:
        username: your_cdse_email
        password: your_cdse_password
```

运行时把私有凭证配置放在最后加载：

```bash
python run.py -c configs/default.yaml configs/examples/copdem.yaml configs/private_copdem.yaml --check
```

需要更稳定的公开COG下载、且可以接受AWS Open Data提供的2021发布版时，可在运行覆盖中把
`copdem` pipeline 的 Source 换为：

```yaml
source:
  class_path: geoacquire.sources.copdem.public_cog.CopDEMPublicCOGSource
  init_args:
    resolution: "30"
    base_url: null
    filename_suffix: null
```

该模式不加载 `private_copdem.yaml`，并通过共享HTTP服务并发下载、跨Region去重及安全续传。

PE3D 同样把账号密码放在私有配置中。复制
[`configs/private_pe3d.example.yaml`](configs/private_pe3d.example.yaml) 为
`configs/private_pe3d.yaml`，只在副本里填写真实账号。运行时程序会把当次 CAPTCHA 保存到
`cache/pe3d/captcha.png` 并在终端等待人工输入；验证码不会写入 YAML 或日志：

```bash
python run.py -c configs/default.yaml configs/examples/pe3d_dtm_1m.yaml configs/private_pe3d.yaml --check
python run.py -c configs/default.yaml configs/examples/pe3d_dtm_1m.yaml configs/private_pe3d.yaml
```

多个 Polygon 必须放在同一次运行中，才能共用一次 CAPTCHA 会话并对重复图幅全局去重。Brazil
PE3D 测试 SHP 的 CRS 已修正为 SIRGAS 2000 / UTM 24S（EPSG:31984），示例会直接读取 `.prj`：

```bash
python run.py -c configs/default.yaml configs/examples/pe3d_dtm_1m_shapefile.yaml configs/private_pe3d.yaml
```

每个要素会保留真实 Polygon/MultiPolygon，而不是只使用外包矩形。PE3D 会先汇总全部相交图幅、
全局去重，再登录一次并按 `batch_size` 分批取得链接；一个图幅即使服务多个 Polygon 也只下载一次。
`min_intersection_fraction` 默认 0，表示保留所有正面积相交；已有 PE3D 图幅边缘本身带窄重叠时，
可像 SHP 示例一样设为 `0.05`，忽略不足较小几何面积 5% 的边缘条带。

Point 输入的 Entremontes 示例只选择 `ID=512989`，以原始点为中心建立约 500 m × 500 m 的下载
查询范围，再从 PE3D 原生 MDT 中直接复制最接近 448 m × 448 m 的整数像元窗口：

```bash
python run.py -c configs/default.yaml -c configs/examples/pe3d_dtm_1m_point.yaml -c configs/private_pe3d.yaml
```

输入 SHP 可以使用地理或投影坐标系，不会改写源文件。地理坐标按点所在纬度用测地距离估算 500 m；
投影坐标按 CRS 的线性单位换算。最终结果保持 PE3D 主图幅的 UTM CRS、原生网格和像元值；中心
允许吸附到最近的原生像元网格。PE3D 示例显式允许次图幅原点向主图幅网格吸附最多0.5个像元。
若相邻官方图幅仅因导出舍入产生不超过0.1%的分辨率差，处理器可只对原生复制后仍为空的接缝
执行双线性补偿，最多占结果的25%，并且绝不覆盖已复制的原生高程；超过任一阈值仍明确失败。
补偿比例和来源会写入输出标签。原始图幅写入 `raw/`，最终 `<ID>.tif` 写入 `gt/`。同一次运行
中的全部 Point 仍只登录和验证一次，并对重复图幅全局去重。

当前 PE3D pipeline 只下载并安全解压 1:5,000 的 `MDT Raster`（产品代码 4）。它通过下载链接中的
`1_5000/4_MDT_RASTER/MDT-*.zip` 三项约束排除 0.5 m 城区产品。范围和 Polygon 示例保留完整
原生图幅；Point 示例另行执行原生优先的拼接/裁剪，仅对满足严格阈值的剩余接缝启用局部补偿。
PE3D 服务器当前没有发送完整的 ZeroSSL 中间证书链，因此浏览器能访问时，Requests 仍可能报
`CERTIFICATE_VERIFY_FAILED`。下载器默认使用随代码提供的 PE3D 专用 ZeroSSL/Sectigo CA 链，
登录、目录查询和 ZIP 下载都保持 TLS 校验；如需使用自己的 CA 文件，可设置 `ca_bundle_path`，
不应把 `verify_tls` 改为 `false`。

LINZ 和 CNIG/PNOA 同样会使用矢量输入中的真实 Polygon/MultiPolygon，并把多个要素命中的同一
源文件合并成一次物理下载。LINZ 从公开静态 STAC 取得图幅 footprint；CNIG 通过门户的 POST
查询接口取得当前 MDT50 cm 文件及其 GeoJSON footprint。CNIG 首版保留原生 COG，不裁剪、拼接、
重采样或重投影，并遵守官网注明的匿名 20 文件上限。

## Point → 固定米制 GT 的通用性

完整的三层组件组合、全部类路径和参数速查见
[`docs/COMPONENT_MATRIX.md`](docs/COMPONENT_MATRIX.md)。

Point 输入与数据源解耦：`PointRegionProvider` 只负责把每个点扩展成米制查询 Region；任何接受
普通 Region 的 Source 都能直接使用。`NativePointWindowPostprocessor` 只接收 Source 已落地的
栅格，因此切换数据源时主要改变 `input_products` 和原生分辨率，不复制点扩展逻辑。

| 数据源 | Point SHP 查询 | 原生窗口 | 448 m 的典型结果 / 限制 |
| --- | --- | --- | --- |
| PE3D 1 m MDT | 可以 | 可以 | 448×448；原生复制优先，示例对极小图幅分辨率误差启用受限接缝补偿 |
| LINZ 1 m DEM | 可以 | 可以 | 448×448，输出保持 EPSG:2193 / NZVD2016 |
| Spain CNIG 0.5 m MDT | 可以 | 可以 | 896×896；一次匿名任务仍受最多 20 个源文件限制 |
| USGS 1 m DEM | 可以 | 可以 | 通常 448×448；CRS 随原生 UTM 图幅变化 |
| USGS LiDAR | 可以 | 有条件 | 必须让 Source 生成 `dtm` 或 `dsm` 栅格；只下载 `laz` 时不能直接使用栅格窗口处理器 |
| Copernicus DEM | 可以 | 可以但只能近似 | 30/90 m 原生像元不能整除 448 m；程序选最接近的整数行列，不重采样，因此实际尺寸不会严格等于 448 m |
| Google / Wayback | 可以 | 技术上可以但不推荐 | 原生 GeoTIFF 是 EPSG:3857，地图米不等于严格地面米；训练数据应采用下面的两次运行 |

推荐流程是两次运行：第一次由高程数据源读取 Point 文件并生成原生网格 GT；第二次把生成的 GT
目录作为目标 TIF 输入，再运行 Google/Wayback。这样影像会重投影、拼接和重采样到 DEM 的精确
范围、CRS、行列数与仿射网格。除非像 PE3D 示例一样显式开启受限接缝补偿，否则 DEM 不会重采样；
即使开启也只填补缺失像元。新西兰和西班牙的可运行 Point 示例自带
一个 WGS84 GeoJSON 点；把其中 `source` 换成任意带正确 CRS 的 Point SHP 即可。

## `default.yaml`、示例 YAML 和命令行的关系

程序按从左到右的顺序合并配置：

```text
configs/default.yaml
        ↓ 完整基础配置：Region、Reporting、八个 Pipeline 和全部参数
configs/examples/某个数据源.yaml
        ↓ 本次运行：输入、输出目录、启用哪个 Pipeline
--set 配置路径=新值
        ↓ 可选的临时命令行覆盖
最终运行配置
```

`configs/default.yaml` 必须先加载，因为示例 YAML 只写本次运行需要改变的内容。
`configs/default_zh.yaml` 是中文注释等值版，可以替代英文版，但两份不要同时加载。

推荐把长期使用的输入输出路径直接写进 YAML。`--set` 只适合临时换路径、批处理脚本或不想修改
文件的单次运行，例如：

```bash
python run.py -c configs/default.yaml configs/examples/google.yaml --set region.init_args.source=E:/temporary/target.tif --set pipelines.google.acquire.output_dir=E:/temporary/google
```

## `configs` 目录里分别是什么

### `configs/examples/`

这里放的是可以复制、编辑和运行的示例覆盖配置，不是第二份 default：

- `usgs_lidar.yaml`、`usgs_dem_1m.yaml`、`linz_nz_dem_1m.yaml`、
  `linz_nz_dem_1m_point.yaml`、`cnig_spain_mdt50cm.yaml`、`cnig_spain_mdt50cm_point.yaml`、
  `google.yaml`、`wayback.yaml`、`pe3d_dtm_1m.yaml`、`pe3d_dtm_1m_shapefile.yaml`、
  `pe3d_dtm_1m_point.yaml`、`copdem.yaml`：每个数据源
  一份下载模板；PE3D 首版只设置
  下载目录，其他示例同时设置下载和结果目录；
- `target_raster.yaml`：只演示单个目标 TIF 的 Region 输入；
- `target_raster_directory.yaml`：只演示目标 TIF 目录输入；
- `target_training_data.yaml`：一次启用 LiDAR、Google、Wayback 和 CopDEM 的多数据源示例。

这些文件都应加载在 `default.yaml` 后面。可以直接修改，也可以复制成自己的场景 YAML。

### `configs/runs/`

这里放本机实际生产场景，而不是入门模板。目录中新建的 YAML 默认被 `.gitignore` 排除，避免
把绝对路径、任务批次和运行报告位置上传。仓库目前只保留两个历史上已经纳入版本控制的 GeoDAR
通用运行模板：

- `geodar_state_lidar.yaml`：GeoDAR 按州正式下载 LiDAR；
- `geodar_state_lidar_audit.yaml`：只检查项目命名规则，不下载 LAZ。

### `configs/private_copdem.example.yaml` 与 `configs/private_copdem.yaml`

仓库上传 `private_copdem.example.yaml`，让用户看到凭证的 YAML 位置，但其中只有占位值。使用时复制
并重命名为 `private_copdem.yaml`，再填写真实的 CDSE 账号密码。真实文件已被 `.gitignore` 排除；
使用 CopDEM 时把它作为最后一份配置加载，不要把凭证写进 default、examples 或文档。

### `configs/private_pe3d.example.yaml` 与 `configs/private_pe3d.yaml`

用途与 CopDEM 私有配置相同。PE3D 的 CAPTCHA 与当前 HTTP 会话绑定，因此不保存在私有 YAML；
真正需要远程查询时由程序生成图片并要求人工输入。已经解压到下载目录的图幅可以直接复用，不会登录。

### `configs/usgs_lidar_projects.yaml`

这不是普通运行配置，也不要把它写在 `-c` 后面。USGS 历史 LiDAR 项目的文件名格式并不统一，
程序需要这张审核过的规则表，才能从文件名推导每个 LAZ 的空间范围并安全选片。
`USGSLidarSource` 会自动读取它。

新项目如果没有匹配规则，会出现在 audit 报告中，而不是冒险下载。补充或修改规则前，应按
[`docs/DEVELOPER_HANDOFF.md`](docs/DEVELOPER_HANDOFF.md) 中的流程采样文件头、核对坐标并补测试。

## 文档分别解决什么问题

| 文档 | 读者和用途 |
| --- | --- |
| [README.md](README.md) | 新人入口：选择数据源、修改 YAML 中的输入输出并开始下载 |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | 数据目录：数据类型、源分辨率、时间、国家范围、官方下载入口和来源文献 |
| [docs/COMPONENT_MATRIX.md](docs/COMPONENT_MATRIX.md) | 组件组合速查：类路径、参数、输入输出产品以及 Region、Source、Postprocessor 兼容矩阵 |
| [docs/REFERENCE.md](docs/REFERENCE.md) | 完整运行参考：参数、默认值、配置合并、日志、续跑和 LiDAR 审计 |
| [docs/DEVELOPER_HANDOFF.md](docs/DEVELOPER_HANDOFF.md) | 开发与继续扩展：架构边界、新增 Source/Postprocessor、并发约束、LiDAR 规则和提交检查 |
| [docs/PROJECT_REPORT.md](docs/PROJECT_REPORT.md) | 历史与证据：设计决定、规则补全、验证结果和未解决问题 |

如果任务是继续扩展 GeoAcquire，而不只是运行下载，应先阅读开发交接文档。项目通过 YAML 的
`class_path` 加载组件；新增数据源通常不需要修改 Pipeline 或建立新的注册表。
