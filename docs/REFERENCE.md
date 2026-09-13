# GeoAcquire 完整参考

本页保留完整参数、运行机制和维护细节。第一次使用项目请先看根目录的
[README.md](../README.md)，只需设置输入、下载目录和结果目录即可开始。

需要快速查找 RegionProvider、Source、Postprocessor 的完整类路径、构造参数和兼容组合时，
请看 [COMPONENT_MATRIX.md](COMPONENT_MATRIX.md)。

GeoAcquire 是一个由 YAML 驱动的地理空间数据获取框架。输入可以是坐标范围、Polygon 矢量文件、
单个目标 GeoTIFF 或目标 GeoTIFF 目录；输出既可以是下载后的源产品，也可以是拼接、裁剪并对齐到
目标网格的栅格。

当前内置数据源：

| Source | 数据 | Acquisition 产物 |
| --- | --- | --- |
| `USGSLidarSource` | USGS 3DEP LiDAR | LAZ、DSM、DTM |
| `USGSDEM1mSource` | USGS 3DEP 1 m DEM | GeoTIFF |
| `LINZDEM1mSource` | LINZ New Zealand LiDAR 1 m DEM | COG GeoTIFF |
| `CNIGMDTSource` | Spain CNIG/PNOA third-coverage 0.5 m DTM | COG GeoTIFF |
| `GoogleSource` | Google XYZ 影像 | JPG/PNG、GeoTIFF |
| `WaybackSource` | Esri Wayback 影像 | JPG/PNG、GeoTIFF |
| `PE3DSource` | PE3D 1 m MDT | 解压后的原生 MDT GeoTIFF |
| `CopDEMSource` | Copernicus DEM | 解压后的 DEM |

项目叫 GeoAcquire，是因为稳定职责是“获取地理空间资产”，并不限定 DEM 或遥感影像。

## 1. 快速开始

```bash
conda create -n geoacquire python=3.11 -y
conda activate geoacquire
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
python run.py -c configs/default.yaml --check
```

`configs/default.yaml` 是英文完整参数目录，`configs/default_zh.yaml` 是等值的中文版本；
两者都包含八个内置 pipeline、全部参数和选项说明，默认均为 `enable: false`。
每次选择其中一份作为第一份配置，不要同时加载两份。本文命令统一使用英文主配置。
中文版可以直接检查：`python run.py -c configs/default_zh.yaml --check`。

直接启用 Google：

```powershell
python run.py -c configs/default.yaml `
  --set pipelines.google.enable=true
```

指定目标 TIF 并运行 Google：

```powershell
python run.py `
  -c configs/default.yaml configs/examples/target_raster.yaml `
  --set region.init_args.source=E:/data/0.tif `
  --set pipelines.google.enable=true
```

当前训练数据示例会启用 LiDAR、Google、最新 Wayback 和 CopDEM：

```powershell
python run.py `
  -c configs/default.yaml `
     configs/examples/target_training_data.yaml `
     configs/private_copdem.yaml
```

下载与一个新西兰 bbox 相交的 LINZ 1 米 DEM 原生 COG：

```powershell
python run.py `
  -c configs/default.yaml configs/examples/linz_nz_dem_1m.yaml
```

第一次运行会并发读取 LINZ 静态 STAC 的轻量 Item JSON，并把图幅 footprint 和 COG URL
缓存到 `cache/linz/nz_dem_1m_stac.json`；后续运行直接使用缓存。无需 LINZ 账号、API Key
或 AWS 凭证。下载单位是完整的 LINZ 1:50,000 图幅 COG，不是服务器端裁剪后的 bbox。

CopDEM 账号密码按项目要求放在被 `.gitignore` 排除的私有 YAML 中。先复制仓库中的
`configs/private_copdem.example.yaml` 并重命名为 `configs/private_copdem.yaml`，再填写：

```yaml
pipelines:
  - name: copdem
    source:
      init_args:
        username: your_email
        password: your_password
```

PE3D 使用相同的私有配置约定：复制 `configs/private_pe3d.example.yaml` 为
`configs/private_pe3d.yaml` 后填写账号密码。验证码与当次会话绑定，运行时保存到
`cache/pe3d/captcha.png` 并由操作者在终端输入，不进入 YAML。下载 1 m MDT 示例：

```powershell
python run.py `
  -c configs/default.yaml `
     configs/examples/pe3d_dtm_1m.yaml `
     configs/private_pe3d.yaml
```

## 2. 配置文件分工

```text
configs/
├── default.yaml                 # 英文完整参数目录，默认主配置
├── default_zh.yaml              # 参数值完全相同的中文版本，二选一
├── usgs_lidar_projects.yaml     # 已审核的 USGS LiDAR 项目命名规则
├── private_copdem.example.yaml  # 可提交的凭证结构模板，仅含占位值
├── private_copdem.yaml          # 用户复制后填写的真实凭证，不提交
├── private_pe3d.example.yaml    # PE3D 凭证模板；CAPTCHA 不写入配置
├── private_pe3d.yaml            # 用户复制后填写的真实凭证，不提交
├── examples/                    # 可编辑的数据源下载示例和通用输入覆盖
│   ├── target_raster.yaml
│   ├── target_raster_directory.yaml
│   ├── target_training_data.yaml
│   ├── usgs_lidar.yaml / usgs_dem_1m.yaml
│   ├── google.yaml / wayback.yaml / pe3d_dtm_1m.yaml / copdem.yaml
│   ├── pe3d_dtm_1m_shapefile.yaml / pe3d_dtm_1m_point.yaml
│   ├── linz_nz_dem_1m.yaml / linz_nz_dem_1m_point.yaml
│   └── cnig_spain_mdt50cm.yaml / cnig_spain_mdt50cm_point.yaml
└── runs/                        # 实际生产场景
    ├── geodar_state_lidar.yaml
    └── geodar_state_lidar_audit.yaml
```

多份 YAML 按顺序合并。可以把路径都写在一个 `-c` 后，也可以为每份文件重复
`-c`；两种形式都保留声明顺序：

```powershell
python run.py -c configs/default.yaml -c configs/examples/cnig_spain_mdt50cm.yaml --check
```

- 普通 mapping 递归合并；
- `pipelines` 按 `name` 合并；
- `postprocess` 按列表位置合并，`[]` 表示清空；
- 其他 list 整体替换；
- 后面的文件只需写与 default 不同的值。

命令行 `--set PATH=VALUE` 可以继续覆盖已存在的配置项：

```powershell
python run.py -c configs/default.yaml `
  --set pipelines.google.enable=true `
  --set pipelines.google.source.init_args.zoom=17 `
  --set pipelines.google.acquire.max_workers=8
```

Pipeline 使用名称而不是列表序号。路径拼错会立即报错，不会静默创建无效参数。

## 3. 数据流和继承关系

```text
YAML class_path
    ↓ jsonargparse 动态构造
RegionProvider + Source + Postprocessors
    ↓
dict[str, Region]
    ↓
BaseSource.acquire(regions, context, options)
    → 每个文件在同一 A 线程内：下载 → 转换成可用资产
    → AcquisitionProgress 登记依赖 / 文件完成 / region 清单关闭
    → 一个 region 就绪即入队，由独立 B 线程运行它的 postprocess 链
    → 最后汇总 AcquisitionReport，不再整批重跑后处理
```

继承关系：

```text
BaseSource
├── HTTPSource
│   ├── LINZDEM1mSource
│   ├── XYZSource
│   │   ├── GoogleSource
│   │   └── WaybackSource
│   ├── PE3DSource
│   └── USGSHTTPSource
│       ├── USGSWorkunitHTTPSource
│       │   └── USGSDEM1mSource
│       └── USGSLidarSource
└── CopDEMSource
```

普通 URL 数据源复用 `HTTPSource`：

```text
plan_requests() → HTTPDownloadService worker 内 [download → materialize(one_file)]
```

PE3DSource 在 `HTTPSource` 上增加会话初始化、人工 CAPTCHA、按图幅链接发现和安全 ZIP 解包，
实际文件传输仍复用 `HTTPDownloadService` 的并发、重试与断点续传。CopDEM 需要 Token 刷新、
Catalogue、受权下载和 ZIP 解压，因此直接实现完整 `BaseSource.acquire()`。Pipeline 不判断
Source 类型，也没有注册器。

`RuntimeContext` 只是显式携带共享运行服务：

```python
context = RuntimeContext(
    workspace_dir=project_root,
    http=HTTPDownloadService(),
)
PipelineRunner(context).run(config.region, config.pipelines)
```

CopDEM 不会被强制走 HTTPDownloadService；它从 context 中不使用该服务，而是使用自己的 `CopDEMClient`。

### 下载与后处理如何并行

只有两组执行职责：A 获取可用数据，B 对就绪的 region 做后处理。**转换不再等整个州下载完，
也没有额外的转换线程池**。LiDAR 的同一个 A 线程连续完成 LAZ 下载、DSM/DTM 生成；
Google/Wayback 同线程下载并生成 GeoTIFF；CopDEM 保留客户端内的下载和 ZIP 提取。

每条 pipeline 的配置可以分别控制两边：

```yaml
pipelines:
  - name: usgs_lidar
    acquire:
      max_workers: 8        # A：最多 8 个“下载＋转换”文件任务
    postprocess_workers: 1 # B：一个 region 的拼接、裁剪、对齐；也可设 2
```

命令行可增加 `--set pipelines.usgs_lidar.postprocess_workers=2`。8 个 A 线程不代表一直有
8 个网络传输：部分线程可能在生成 DSM；同时也可能有 8 个 DSM 转换，内存用量会随之增加。
B 默认 1 个线程，忙时后续就绪 region 排队，A 继续获取数据。

region 就绪必须满足：**清单已关闭、不再添加文件；全部依赖文件已完成转换，且没有获取失败**。
对于 LiDAR，已选较新瓦片完全覆盖目标，或已遍历该目标最后一个候选 workunit，便可关闭清单。
这只是已有命名规则下的计划完整性，不是对像素有效覆盖或规则正确性的额外认证；未匹配项目仍看 audit。
相邻 region 共享的 LAZ 只下载、转换一次；即使后面才发现另一个使用者，也复用已生成的 DSM。

同一个 region 的多个 postprocess 步骤仍按 YAML 顺序执行，不并行互相依赖的步骤；不同 region
可并行。失败的下载/转换会阻止受影响 region 的后处理，其他 region 继续。各数据源 pipeline
仍按配置顺序执行，这次没有同时运行 LiDAR、Google 和 CopDEM 等多个 Source。

## 4. 项目术语与固定定义

本节集中列出普通用户会在 YAML、终端、log、JSON 和代码接口中看到的项目词汇。定义按职责
归属：跨 Source 的稳定词汇放在 `geoacquire/core/models.py`，配置结构放在
`geoacquire/core/config.py`，某个 Source/后处理器独有的模式和策略留在所属模块。项目没有
一个含义含混的全局 `role`；文件的技术形态、内容语义、生命周期和命名分别由
`kind`、`product`、`status` 和 `output_suffix/filename_suffix` 表达。

### YAML 结构词汇

| 名称 | 定义 |
| --- | --- |
| `region` | 本次要覆盖的一组地理范围。可以来自 bbox、一个目标 TIF 或一个 TIF 目录。 |
| `class_path` | 要实例化的 Python 类完整导入路径；不是显示名称。 |
| `init_args` | 传给该 `class_path` 构造函数的参数。 |
| `pipelines` | 按 YAML 顺序执行的数据处理线列表；合并配置时按 `name` 匹配。 |
| `pipeline.name` | 配置合并、日志和 JSON 中使用的稳定 pipeline 标识。 |
| `pipeline.enable` | 是否执行该 pipeline。 |
| `source` | 负责查找、下载并把远端内容转成本地可用资产的组件。 |
| `acquire` | 所有 Source 共用的执行参数：输出目录、复用、重试、下载块和文件 worker。 |
| `postprocess` | Source 之后按顺序执行的处理器链；`[]` 表示明确清空整条链。 |
| `postprocess_workers` | 可并行处理的目标 TIF 数；不同于下载文件数 `acquire.max_workers`。 |
| `reporting` | 全局三级运行记录配置：终端摘要、人类 log、完整可续跑 JSON。 |

### 运行对象与常见地理词汇

| 名称 | 定义 |
| --- | --- |
| 目标 TIF | 用户已有、只用于给出目标范围/CRS/网格的输入 GeoTIFF，例如 `0.tif`。 |
| 最终 TIF | postprocess 生成的成果，例如 `0_lidar.tif`；不是新的目标输入。 |
| `Region` | 程序内部对一个目标范围的统一记录：`region_id + bounds + crs + metadata`。 |
| workunit | WESM 中的一条 USGS 项目/交付批次记录；一个 workunit 可涉及多个目标 TIF。 |
| link list | USGS 项目提供的远端文件 URL 文本清单，不是已经下载的 LAZ 集合。 |
| `DownloadRequest` | 一个准备交给下载服务的远端物理文件请求，可同时服务多个 Region。 |
| asset / `LocalAsset` | 已存在于本地且可继续处理的文件及其描述，不是文件副本或用户身份。 |
| `Postprocessor` | 只消费当前 pipeline 的 LocalAsset，执行拼接、裁剪、对齐等后处理。 |
| `AcquisitionReport` | Source 返回的按 Region 分组的成功、已有、复用和失败账本。 |
| LAZ | 压缩 LAS 点云文件；是 LiDAR 原始点云产品，不是高程栅格。 |
| DSM | 数字表面模型，通常保留建筑、树木等表面高度。 |
| DTM | 数字地形模型，本项目 LiDAR 实现只使用 LAS 地面分类 2。 |
| DEM | 通用数字高程模型产品；具体是表面还是裸地取决于数据源说明。 |
| imagery | Google/Wayback 等光学影像产品。 |

### 跨组件固定字段

| 字段 | 允许值与语义 | 权威定义 |
| --- | --- | --- |
| `region_id` | 一个目标范围的稳定名称，也是默认最终文件名前缀；不是下载顺序编号。 | `Region` |
| `bounds` / `crs` | 范围固定为 `(minx,miny,maxx,maxy)`，坐标值由同一 Region 的 CRS 解释。 | `Region` |
| `kind` | `file` 普通文件；`image` 未地理配准图片；`raster` 地理栅格；`point_cloud` 点云。 | `AssetKind` |
| `product` | 内置为 `generic/imagery/dem/laz/dsm/dtm`；自定义 Source 可声明其他非空字符串。 | `ProductType` |
| acquisition `status` | `success` 本轮产生；`skipped` 磁盘已有；`reused` 本轮共享；`failed` 最终不可用。 | `AssetStatus` |
| Region metadata | `target_path`、`shape`、`transform`，只在输入来自目标栅格时存在。 | `RegionMetadataKey` |
| `asset_id` | Source 定义的逻辑实例标识；不等于文件类型、product 或文件名。 | `LocalAsset/DownloadRequest` |
| `target_region_ids` | 同一个物理下载文件服务的所有 Region；下载服务据此传播成功或失败。 | `DownloadRequest` |
| `output_suffix` | 最终 TIF 的命名后缀，不表示内容语义；内容仍由 `product` 表示。 | `RasterAlignToTargetPostprocessor` |
| `filename_suffix` | Source 原生/中间文件的防冲突命名后缀，不改变 product。 | 各 Source |

### YAML 中有固定选择的参数

| 参数 | 允许值 | 归属/含义 |
| --- | --- | --- |
| USGS LiDAR `mode` | `download` / `audit` | 仅 `USGSLidarSource`；正式下载或下载前规则体检。 |
| USGS LiDAR `output_products` | `laz`、`dsm`、`dtm` 的非空组合 | 决定保留/生成哪些 LiDAR 产品。 |
| USGS DEM `hemisphere` | `north` / `south` | 解释文件名中的 UTM zone。 |
| XYZ `output_format` | `image` / `geotiff`；兼容别名 `jpg/raw` / `tif` | 保留普通图片或生成带地理参考 TIF。 |
| CopDEM `resolution` | 字符串 `"30"` / `"90"` | Copernicus DEM 分辨率系列。 |
| CopDEM `dem_format` | `DGED` / `DTED` | CDSE 产品格式。 |
| PE3D `product` | 当前仅 `dtm_raster` | 产品表已保留其他五类门户代码，但未核验前拒绝启用。 |
| PE3D `ca_bundle_path` | 默认 `null` | `null` 使用随代码提供的 PE3D ZeroSSL/Sectigo CA 链；也可指定自有 CA 文件。 |
| PE3D `min_intersection_fraction` | 默认 `0.0` | 相交面积占“Polygon 与图幅中较小者”的最低比例；可用 `0.05` 忽略已有图幅足迹的窄边缘重叠。 |
| RasterAlign `merge_method` | `first` / `last` | 重叠像元保留先出现或后出现的有效值。 |
| RasterAlign `vertical_datum_policy` | `ignore` / `warn_once` | 忽略或只警告一次；两者都不做高程基准转换。 |
| RasterAlign `resampling` | `nearest/bilinear/cubic/cubic_spline/lanczos/average/mode/max/min/med/q1/q3/sum/rms` | Rasterio 重采样方法。 |
| Reporting `overwrite` | `false` / `true` | 续接兼容 JSON，或覆盖同一路径并开始新报告。磁盘文件始终决定成果是否已存在。 |
| audit workunit `status` | `matched` / `partial` / `unmatched` | 全部文件名可解析、部分可解析、无可用规则或清单错误。 |

Source 独有的值不会为了形式统一搬进全局 models。例如 LiDAR `mode`、XYZ
`output_format`、RasterAlign 拼接/垂直策略只影响各自模块；跨 Source 共享的 `kind/product/status`
才是核心常量。

### JSON 运行状态值

| 层级 | 状态及含义 |
| --- | --- |
| 整次运行 | `running`、`complete`、`failed`、`interrupted`。 |
| pipeline | `planning`、`running`、`complete`、`failed`。 |
| 目标 TIF | `pending` 等待依赖、`ready` 可后处理、`building` 正在生成、`existing` 磁盘已有、`complete` 本轮完成、`failed` 已失败、`unavailable` 没有产生可用覆盖。 |
| LAZ 文件任务 | `planned`、`running`、`existing`、`complete`、`failed`。 |

这些是 `reporting.json_path` 中的程序状态，不等同于 acquisition 的
`success/skipped/reused/failed`：前者描述整个任务阶段，后者描述一个资产取得结果。

### `null`、`[]` 与 YAML list

YAML 的 `null` 没有全项目统一含义，必须看具体参数；`[]` 是合法空列表语法，也不自动等于
“使用默认值”。本项目中容易混淆的定义如下：

| 参数 | `null` | `[]` |
| --- | --- | --- |
| `exclude_suffixes` | 不排除后缀 | 不排除后缀 |
| `skip_existing_suffixes` | 不按人工后缀组合跳过 | 空组合，等同不启用该规则 |
| LiDAR `output_products` | 使用 Source 默认 `[laz]` | 非法，必须至少选择一个产品 |
| LiDAR `exclude_classes` | 使用默认噪声分类 `[7, 18]` | 不排除任何分类 |
| RasterAlign `input_products` | 使用当前报告中的全部 `kind=raster` 资产 | 非法，空白名单无法处理 |
| pipeline `postprocess` | 非法/不是该字段类型 | 清空全部后处理步骤 |
| `reporting.log_path/json_path` | 关闭对应持久文件 | 非法/不是路径类型 |

行内列表 `A: [x, y]` 与块列表完全相同：

```yaml
A:
  - x
  - y
```

### USGS LiDAR 规则 YAML

`configs/usgs_lidar_projects.yaml` 不是普通运行覆盖层，而是维护者审核过的“文件名到空间范围”
规则目录。`version` 是目录格式版本；每个 `projects` 项包含唯一 `name`、用于匹配 WESM
`workunit/project/lpc_link` 字段的正则 `match`，以及一个或多个 `schemes`。每个 scheme 的
`parser` 负责解码文件名，`crs` 为 `mgrs/workunit/EPSG:...`，`grid` 定义坐标缩放、偏移、
`min/max/center` 锚点、瓦片宽高、padding 和可选网格取整。完整 parser 名单直接维护在该
YAML 文件头和 `geoacquire/sources/usgs/lidar_catalog.py` 的 `PARSERS`，它们属于规则实现，
不是生产运行时让用户任意选择的数据产品。

### 核心数据对象示例

`Region` 是统一区域：

```python
Region(
    region_id='0',
    bounds=(minx, miny, maxx, maxy),
    crs='EPSG:4326',
    metadata={'target_path': '.../0.tif', ...},
)
```

`DownloadRequest` 是一次远端文件传输。一个 URL 对应一个对象；几十万瓦片会产生几十万个逻辑 Request，但通过 generator 惰性创建，下载器最多只保留 `max_workers × 4` 个在途 Future。

`LocalAsset`（本地资产）不是另一种文件，而是“一个已经落地、可被后处理消费的成果文件及其
描述记录”。例如一个 DSM GeoTIFF 的 `path` 指向实际文件，`region_id` 表示所属区域，
`asset_id` 是该文件的逻辑标识，`kind=raster` 表示处理方式，`product=dsm` 表示文件内容。
`AcquisitionReport` 按 Region 保存成功、跳过和失败的 LocalAsset。

重要类及字段的详细注释位于 `geoacquire/core/models.py`。

## 5. Region 输入

### 坐标范围

```yaml
region:
  class_path: geoacquire.regions.bounds.BoundsRegionProvider
  init_args:
    source: [-157.04, 21.15, -157.03, 21.16]
    region: example
    crs: EPSG:4326
```

### Polygon / MultiPolygon 矢量文件

```yaml
region:
  class_path: geoacquire.regions.vector.VectorRegionProvider
  init_args:
    source: ./examples/Brazil_PE3D/input/Brazil_PE3D_Example.shp
    id_field: name
    crs_override: null
    layer: null
```

每个要素成为一个 Region；`id_field` 选择稳定且唯一的名称字段，未设置时使用 `feature_0`、
`feature_1`。Provider 把完整 Polygon/MultiPolygon 以 WKB 保存在 Region metadata 中，Source 可做
真实几何相交，`bounds` 只用于候选范围预筛。缺少 CRS 时必须设置 `crs_override`；若 `.prj` 声明
经纬度而坐标超过合法经纬度范围，会直接报错。`crs_override` 只修正运行时解释，不改输入文件。

PE3D 的多 Polygon 模式会在登录前规划整个矢量文件，按图幅全局去重，并把一个物理下载结果关联
回所有相交 Region；因此一次运行只需一次 CAPTCHA，而不是每个要素一次。

LINZ 与 CNIG/PNOA 也读取同一份 WKB 真实几何。LINZ 对官方 STAC footprint 做一次全局空间索引；
CNIG 把 Polygon 分批提交给门户 POST 查询接口，再读取候选文件的 GeoJSON footprint 做本地精确
相交。两者都会把一个物理 COG 关联回所有相交 Region，并在一次运行中只下载一次。

### Point 矢量文件与米制窗口

```yaml
region:
  class_path: geoacquire.regions.point.PointRegionProvider
  init_args:
    source: E:/data/dams.shp
    id_field: ID
    query_size_m: 500
    include_values:
      ID: ['512989']
    crs_override: null
    layer: null
```

每个 Point 成为一个 Region。`query_size_m` 只决定 Source 选取哪些源图幅，并不提前生成目标
栅格；`include_values` 可按一个或多个属性字段精确筛选，省略时处理全部点。Provider 保存原始点、
输入 CRS、查询尺寸和属性，并在内存中建立查询外包框。投影 CRS 按轴单位换算米，地理 CRS 在点的
纬度处用测地距离估算；两种情况都不修改或强制转换输入矢量文件。

需要固定米制 GT 时，在 pipeline 中接 `NativePointWindowPostprocessor`：

```yaml
postprocess:
  - class_path: geoacquire.postprocess.native_point_crop.NativePointWindowPostprocessor
    init_args:
      size_m: 448
      input_products: [dtm]
      output_dir: E:/data/gt
      output_suffix: null
      min_valid_fraction: 1.0
      max_grid_snap_pixels: 0.5
      fallback_resampling: null
      max_resolution_mismatch_fraction: 0.001
      max_resampled_fraction: 0.25
      skip_existing: true
```

它先把原始点坐标转换到源栅格 CRS，然后把中心吸附到最近的原生像元网格，直接复制最接近
`size_m` 的整数行列。对 PE3D 1 m MDT，448 m 正常得到 448×448 像元。相邻图幅必须具有相同
CRS、分辨率和旋转；`max_grid_snap_pixels=0` 要求网格原点严格一致，设置为不超过 `0.5` 的值时，
允许次图幅原点吸附到主图幅最近的整数网格。默认 `fallback_resampling=null`，不插值或重算
高程样本，只直接复制原始像元。若显式设为 `bilinear`，程序仍先完成全部可行的原生复制，只对
剩余 NoData 使用方向一致、分辨率差不超过 `max_resolution_mismatch_fraction` 的相邻图幅；
不会覆盖已经复制的原生像元，且补偿比例超过 `max_resampled_fraction` 时整个目标失败。实际
吸附量、补偿比例、补偿图幅和最大分辨率差均写入输出标签。若源本身是地理栅格，则只在点附近
估算每个像元的米制大小，输出仍保持主图幅 CRS 和网格。

这个组合不依赖 PE3D。内置 Source 的兼容边界如下：

| Source | Point Region | `NativePointWindowPostprocessor` | `input_products` | 关键边界 |
| --- | --- | --- | --- | --- |
| `PE3DSource` | 支持 | 支持 | `[dtm]` | 1 m；示例允许原点吸附 ≤0.5 像元，并对极小分辨率误差启用受限接缝补偿 |
| `LINZDEM1mSource` | 支持 | 支持 | `[dem]` | 1 m NZTM2000；448 m 通常为 448×448 |
| `CNIGMDTSource` | 支持 | 支持 | `[dtm]` | 0.5 m；448 m 通常为 896×896；匿名最多 20 个源文件 |
| `USGSDEM1mSource` | 支持 | 支持 | `[dem]` | 1 m 原生 UTM 图幅，CRS 随位置变化 |
| `USGSLidarSource` | 支持 | 条件支持 | `[dtm]` 或 `[dsm]` | `output_products` 必须生成对应栅格；`laz` 不是栅格 |
| `CopDEMSource` | 支持 | 支持但近似尺寸 | `[dem]` | 30/90 m 不整除 448 m；保留原生像元时只能选择最近整数行列 |
| `GoogleSource` / `WaybackSource` | 支持 | 仅 `output_format=geotiff` 时可用 | `[imagery]` | EPSG:3857 地图米；推荐以第一阶段 DEM GT 作为第二阶段目标 TIF |

这里的“Point Region 支持”表示 Source 能用扩展后的查询范围选择并去重源文件；不表示所有产品都能
在不重采样时严格得到 448 m。最终尺寸由 `size_m / 原生像元米制大小` 四舍五入成整数行列。
影像训练对齐应采用两次运行：先生成保持主图幅原生网格的 DEM GT，再把该 GT 或其目录交给
`BoundsRegionProvider`，由 `RasterAlignToTargetPostprocessor` 将影像对齐到完全相同的网格。

### 单个目标 TIF

```yaml
region:
  class_path: geoacquire.regions.bounds.BoundsRegionProvider
  init_args:
    source: E:/data/0.tif
    region: null
```

### TIF 目录

```yaml
region:
  class_path: geoacquire.regions.bounds.BoundsRegionProvider
  init_args:
    source: E:/data/GT/AL
    region: null
```

当前目录中直接包含的每个 `.tif/.tiff` 生成一个 Region，子目录不会被递归扫描。目录树由上层
脚本或命令接口逐个目录调用，使每次运行保持“一个输入目录对应一个输出目录”。

`exclude_suffixes` 防止 `_lidar.tif` 等结果被当成新目标；default 已排除内置的
`lidar/dsm/dtm/usgs_dem_1m/nz_dem_1m/cnig_mdt50cm/google/wayback/copdem/cop` 后缀。对这个参数，
`null` 和 `[]` 都表示“不排除任何后缀”；`[lidar, cop]` 与多行 YAML list 写法语义相同。
default 使用具体列表是为了避免把内置中间/最终结果再次识别成目标。
`skip_existing_suffixes` 是可选的目录级人工规则；Pipeline 还会根据实际启用的处理链自动
推断最终输出，完整结果已存在时在 Source 规划和下载之前复用。

目录输入不是依次完整处理 `0.tif`、再处理 `1.tif`。RegionProvider 会先遍历目录，
一次建立全部目标 Region；支持共享规划的 Source（当前主要是 USGS LiDAR）随后把这个
目录作为一个批次统一查询、筛选和去重。普通 XYZ 等 Source 仍可从同一 Region 集合惰性
地产生各自的下载请求。

## 6. 输出和目标后处理

`acquire.output_dir: null` 时，输入目标 TIF 的父目录就是下载目录，所有相邻目标
共享其中的源瓦片，不再创建 Region 子目录。例如输入 `/GT/AL/0.tif`：

```text
/GT/AL/source tiles
/GT/AL/0_google.tif
/GT/AL/0_cop.tif
```

纯 bbox 没有输入文件路径，null 直接使用项目下的 `./output`。多个目标跨越不同父
目录时 null 没有唯一含义，程序要求明确填写 `acquire.output_dir`。所有 Source 都直接
写入该目录；若需按州隔离，为各州指定不同的 `acquire.output_dir` 即可。

下载和后处理各自只有一个目录参数：

```yaml
acquire:
  output_dir: /data/downloads       # 下载和中间文件
postprocess:
  - init_args:
      output_dir: /data/results     # 最终拼接、裁剪、对齐后的 TIF
```

后处理 `output_dir: null` 时，结果放在该 pipeline 的 acquire 所生成的第一个
DSM、DEM 或 GeoTIFF 所在目录，也就是默认跟随 acquire；填写路径时直接放进该目录，
不再追加 Region 子目录。文件名始终为 `<region_id>_<output_suffix>.tif`。

启用 `acquire.skip_existing` 时，重跑按最终成果向前判断，而不是先看原始下载：Pipeline
先收集处理链可推断的终端文件，并按目录一次扫描到内存；同一 Region 的所有终端 product
（例如分别对齐的 DSM 和 DTM）都存在，才整体跳过 Source。若仍需 acquisition，XYZ/LiDAR
Source 再检查 GeoTIFF/DSM/DTM 等派生成果；最后才由 HTTP 层检查 JPG/LAZ/原生 TIF。
`keep_download=true` 时原始文件本身也属于要求保留的成果，缺失时仍会补下。

#### Asset 的 `kind` 与 `product`

每个 Source 都会把可用成果文件记录为 `LocalAsset`。`kind` 表示计算机怎样处理它，由
`AssetKind` 固定定义为 `file/image/raster/point_cloud`；`product` 表示文件内容是什么，例如
`dem/dsm/dtm/imagery/laz`。内置值集中定义在 `geoacquire.core.models.ProductType`；自定义
Source 仍可使用其他非空字符串。`product` 不从文件名推断，`output_suffix` 只负责命名。

LiDAR 的 `output_products` 是 Source 配置：决定需要保留或生成哪些产品。每个实际成果生成后，
同一个值写入 `LocalAsset.product`；RasterAlign 再用 `input_products` 选择要处理的产品。
对齐只改变网格与文件名，不改变数据内容，因此输出自动继承输入的 `product`，没有
`output_role` 或 `output_product` 参数。

内置 Source 的资产类型与产品语义如下：

| Source / 产品 | `kind` | `product` | RasterAlign 常用 `input_products` |
| --- | --- | --- | --- |
| USGS LiDAR 原始点云 | `point_cloud` | `laz` | 不适用，RasterAlign 不读取点云 |
| USGS LiDAR 表面/地形栅格 | `raster` | `dsm` / `dtm` | 分别使用 `[dsm]` 或 `[dtm]` |
| USGS 1 m DEM | `raster` | `dem` | `[dem]` |
| LINZ 1 m DEM | `raster` | `dem` | `[dem]` |
| CNIG/PNOA MDT50 cm | `raster` | `dtm` | `[dtm]` |
| Copernicus DEM | `raster` | `dem` | `[dem]` |
| Google / Wayback 影像 | `raster` | `imagery` | `[imagery]` |

`RasterAlignToTargetPostprocessor.input_products` 的类型和 YAML 语义为：

```yaml
input_products: [dsm]  # list[str]：只选 product=dsm
input_products: null   # 不按 product 筛选，使用当前报告中的全部 kind=raster 资产
# input_products: []   # 非法：空白名单没有可处理的输入，配置加载时报错
```

匹配是区分大小写的精确匹配，例如 `DSM` 不等于内置的 `dsm`。一次 RasterAlign 只能生成
一种语义明确的产品；若筛选结果同时包含 DSM 和 DTM，会直接报错，不能把两种高程含义拼成
一个文件。`null` 仅适用于上游确定只有一种 raster 产品的情况。

同一 LiDAR Source 若同时产出 DSM、DTM，可以配置两个 RasterAlign，分别使用 `[dsm]` 与
`[dtm]`；这才是两个模态/产品输出。Pipeline 重跑时要求这两个 product 各自链中最后一个
可推断结果都存在，才跳过该 Region。连续配置两个都选择 `[dsm]` 的 RasterAlign 是同一
product 的串行步骤，不是多模态；当前报告会保留上游 DSM，通常应拆成不同 pipeline，避免
第二步把原始 DSM 与第一步结果同时当作输入。

内置 Source 还通过 `output_specs: frozenset[AssetSpec]` 声明当前配置最终会产出的
`kind + product` 组合。配置加载会沿后处理链检查这些契约；例如 LiDAR 只配置
`output_products: [laz]` 却保留 `input_products: [dsm]`，或 XYZ 配置为只保留普通图片却继续
运行 RasterAlign，都会在 `--check` 阶段直接报错，而不是下载结束后才发现没有 TIF。

同一模型层还集中定义 `AssetStatus.SUCCESS/SKIPPED/REUSED/FAILED`：`SKIPPED` 表示磁盘上
已有完整文件，`REUSED` 表示本次运行中另一个 Region 共用了同一物理成果；二者不再混为一谈。
跨组件的目标栅格键使用 `RegionMetadataKey`。Source 自己的运行模式、影像输出格式、拼接策略和
垂直基准策略仍由所属模块定义，因为它们不是所有资产都共享的契约。

`RasterAlignToTargetPostprocessor` 只消费当前 pipeline 报告中的 Raster LocalAsset，不扫描目录，因此不会混入其他数据源。它以窗口方式完成：

```text
mosaic → horizontal reprojection → crop → optional resampling → GeoTIFF
```

主要参数：

| 参数 | 含义 |
| --- | --- |
| `output_suffix` | `0 + google → 0_google.tif` |
| `input_products` | `list[str] \| null`；按 Source 写入的、区分大小写的 `LocalAsset.product` 精确筛选；`null` 选择全部 raster，`[]` 非法；实际输入必须只有一种产品 |
| `output_dir` | `null` 跟随 acquire 的 DSM/DEM/GeoTIFF 目录；填写路径则直接放入该目录 |
| `resample` | `true` 使用目标派生网格；`false` 使用源分辨率裁剪 |
| `target_scale` | 1 为目标网格；32 为同范围、32 倍像元尺寸 |
| `resampling` | `nearest/bilinear/cubic/average/...` |
| `window_size` | 单次读写窗口大小 |
| `vertical_datum_policy` | `ignore/warn_once`，均不执行高程转换 |

LiDAR 只生成对齐后的 HR，不在本项目中生成 LR。LiDAR 下采样和 HR/LR 配对留给后续超分项目。

## 7. GeoDAR 按州下载 LiDAR

程序运行时统一把人可读日志和运行状态 JSON 写入服务器的 `logs/`。从多台服务器把各州
结果汇总回本机时，可再手工归档到仓库根目录的 `geodar_log/`，它只是便于集中查看的归档
目录，不参与程序配置、自动写入或续跑判断。规则审计、header 观测和最终验证报告也仍放
在 `logs/`，文件名区分职责。

生产配置必须与 default 合并：

```bash
conda activate geoacquire
cd /mnt/disk/USA/GeoAcquire

python run.py \
  -c configs/default.yaml configs/runs/geodar_state_lidar.yaml \
  --check
```

正式后台运行：

```bash
mkdir -p logs
nohup python -u run.py \
  -c configs/default.yaml configs/runs/geodar_state_lidar.yaml \
  --set pipelines.usgs_lidar.acquire.output_dir=/mnt/disk/USA/SurfRef_Data/USA_GeoDAR/Lidir/AL \
  --set pipelines.usgs_lidar.postprocess.0.init_args.output_dir=/mnt/disk/USA/SurfRef_Data/USA_GeoDAR/GT/AL \
  >> logs/geodar_AL_launcher.log 2>&1 &
echo $!
```

生产 YAML 已设置 `reporting.log_path=./logs/geodar_AL.log` 和
`reporting.json_path=./logs/geodar_AL_run.json`。前者同时保存终端摘要和逐 TIF 详情，后者保存
完整可续接状态；`geodar_AL_launcher.log` 只补充捕获 Python 导入失败等 Reporting 启动前的
错误。`>>` 会在进程重启时追加而不是覆盖 launcher 历史；不要让 shell 重定向文件与
`reporting.log_path` 使用同一路径，否则两个写入者会造成重复和交错。

`USGSLidarSource.keep_download` 直接继承 default 的 `true`，因此 GeoDAR 正式运行会保留原始
LAZ 和生成的 DSM；各州命令不需要重复写 `--set ...keep_download=true`。

上面的生产命令把可复用的源数据和最终目标结果分开：

```text
/GT/AL/*.tif                         # 全部目标 TIF
    ↓ 统一规划、去重
/Lidir/AL/USGS_...laz + *_dsm.tif   # acquire 的州级共享池
    ↓ 再按每个目标 TIF 拼接、裁剪和对齐
/GT/AL/0_lidar.tif、1_lidar.tif...  # postprocess 最终结果
```

如果不覆盖两个 `output_dir`，生产 YAML 继承的 null 会让下载、中间栅格和最终结果都
使用 `/GT/AL`。两种方式的规划逻辑相同，区别只在文件放置位置。

切换 TX：

```bash
nohup python -u run.py \
  -c configs/default.yaml configs/runs/geodar_state_lidar.yaml \
  --set region.init_args.source=/mnt/disk/USA/SurfRef_Data/USA_GeoDAR/GT/TX \
  --set pipelines.usgs_lidar.acquire.output_dir=/mnt/disk/USA/SurfRef_Data/USA_GeoDAR/Lidir/TX \
  --set pipelines.usgs_lidar.postprocess.0.init_args.output_dir=/mnt/disk/USA/SurfRef_Data/USA_GeoDAR/GT/TX \
  --set reporting.log_path=./logs/geodar_TX.log \
  --set reporting.json_path=./logs/geodar_TX_run.json \
  >> logs/geodar_TX_launcher.log 2>&1 &
```

WY、WV 同理替换州名；一个州内部由 `max_workers` 控制 LAZ 下载及转换的并发。若不希望源文件
与 GT 共用目录，再显式覆盖 acquire 和 postprocess 各自的 `output_dir`。

## 8. USGS LiDAR 规划与审计

### 目标目录的完整数据流

以 `/GT/AL` 下 121 个 TIF 为例：

```text
遍历 /GT/AL/*.tif，建立 121 个 Region
    ↓
用全部 Region 的总包围范围读取一次本地 WESM.gpkg
    ↓
把候选 workunit 分配回真正相交的目标 TIF
    ↓
逐 workunit 读取 link list，并按规则解析每个 LAZ 的瓦片范围
    ↓
在该 workunit 覆盖的目标 TIF 范围合集上选择 LAZ
    ↓
跨目标、跨 workunit 按最终文件名统一去重
    ↓
8 个 HTTP worker 下载到一个州级共享目录
    ↓
每个物理 LAZ 至多栅格化一次，产物映射给所有相关 Region
    ↓
分别为 0.tif、1.tif...执行拼接、裁剪和对齐
```

这里不是每个目标 TIF 各自下载一套文件，也不是一个 workunit 永远对 AL 的全部 TIF
筛选。WESM 会先得到每个 workunit 实际相交的目标集合。例如：

```text
regions=121                         # AL 目录共有 121 个目标 TIF
workunit=AL_19Co_4_B24 targets=14  # 此项目只涉及其中 14 个
selected=28                        # 28 个唯一 LAZ 服务这 14 个目标
```

同一个 LAZ 同时覆盖相邻 TIF 时只传输一次，报告会把这一物理文件关联回所有需要它的
Region。其他 107 个目标由后续相交的 workunit 继续规划。

### 为什么需要规则表

USGS 不同项目的文件名不是同一套坐标编码。正式下载不读取远程 LAZ 头，也不在线猜测，而是使用：

```text
configs/usgs_lidar_projects.yaml
```

规则声明：

- 匹配的 `workunit/project/lpc_link`；
- filename parser；
- CRS；
- X/Y 比例、偏移、锚点、可选向上/向下/最近网格取整、瓦片尺寸和 padding。

规则表已补充本次 19 州审计中可确认的 ME、MD、LA、GA、IA、IL、IN、KS、KY、AZ、ID、
FL、CO、CA 项目，保留已有 MA、CT、AR、DE、HI 和 AL/TX/WV/WY 等规则。
这表示支持这些州中的特定项目，不是整个州的所有历史项目都已支持；具体结果看
`logs/lidar_audit_verified.json`，验证口径与缺口见 [PROJECT_REPORT.md](PROJECT_REPORT.md)。

后续 M–N 批次（MI/MN/MO/MS/MT/NC/ND/NE/NH/NJ/NM/NV/NY）另补了 **305 个
workunit** 的明确规则。13 份报告合并后共 494 个 workunit、1462 个目标；回放有
**1353 个目标名义范围完整覆盖，109 个仍有缺口**。MT、ND、NE、NJ、NY 的本批目标
全部覆盖，不代表这些州的所有历史项目都已支持。

这批最新结果看 `logs/lidar_MN_verified.json`，不要与前一批的
`lidar_audit_verified.json` 混用。`incomplete_targets` 保存剩余目标的路径、范围、覆盖
比例和关联项目；缺口分类、逐州统计及官方参考资料见项目报告第 13 节和交接文档第 9 节。
规则完全解析也不等于点云/栅格没有空洞；这里没有下载完整 LAZ 做点密度验收。

OH–VT 批次（OH/OK/OR/PA/PR/RI/SC/TN/TX/UT/VA/VT）新增 **241 个 workunit** 的
规则，12 份报告共 429 个 workunit、2144 个目标。最终回放 **274 个 matched、1 个
partial、154 个 unmatched/缺入口**；**1844 个目标名义范围完整覆盖，300 个仍有缺口**。
PR、RI、SC、TN、VT 的本批目标已全部覆盖，仍不代表全州历史项目均支持。
最新本批结果看 `logs/lidar_OV_verified.json`，逐州统计、未通过的候选和迁移文件见
项目报告第 15 节；新编码实例和维护方法见交接文档第 9 节。

最后 WA/WI/WV/WY 批次新增 **54 个 workunit、43 条规则表项**，包括 WV 的 `FF232`、
相邻 VA 的 `HE278` 字母格网，以及 WA/WI 的坐标缩写、偏移格网和象限后缀。四份报告
共 147 个 workunit；最终 **95 个 matched、52 个 unmatched/缺入口，无 partial**。

| 州 | 本批目标名义范围完整覆盖 | 剩余目标 |
| --- | ---: | ---: |
| WA | 63 / 85 | 22 |
| WI | 70 / 99 | 29 |
| WV | 85 / 85 | 0 |
| WY | 91 / 91 | 0 |

结果见 `logs/lidar_W_verified.json`；剩余 51 个目标的路径、范围和项目在 `incomplete_targets`。
这不代表全州历史项目全部支持，也不是完整 LAZ/DSM 的质量验收。WY Goshen、WA King、
WI 多个县的纯编号，以及 WA 旧 quad 分幅仍未确认，保留 unmatched；不会猜测后下载。
`stratmap...2998452a4` 等前批未确认编码也没有因此变成支持。详细证据见项目报告第 16 节。

### USGS LiDAR audit：先做规则体检，不下载 LAZ

这里的 `audit` 是 **USGS LiDAR 下载前的规则体检模式**，不是普通运行日志，也不是缩小版
下载。它回答的是：“WESM 找到的每个 workunit，其 link list 中的 LAZ 文件名，当前规则能否
全部解释成确定的空间范围？”结果分为 `matched`、`partial` 和 `unmatched`，用于在正式下载前
发现缺规则、部分文件名无法解析、link list 读取失败等问题。

它会联网获取或刷新 WESM 和很小的 link-list 文本，但不会选择或下载 LAZ，不会生成
DSM/DTM，不会执行 postprocess，也不能证明点云内容正确或目标范围没有数据空洞。
`audit_report_path` 是这次规则体检的专用诊断 JSON；它与正式 `mode=download` 使用的
`reporting.json_path`（完整、可续跑的下载状态）不是同一种文件。

`audit` 不是 GeoAcquire 的全局运行模式，而是 `USGSLidarSource` 的 Source 专属能力，所以
`audit_report_path` 只出现在 `pipelines.usgs_lidar.source.init_args`。原因不是其他 Source 漏写
了参数，而是它们没有同一种“项目文件名规则目录”：

| Source | 单文件空间范围从哪里得到 | 是否需要 LiDAR 规则 audit |
| --- | --- | --- |
| USGS LiDAR | WESM 给项目范围；每个 LAZ 的范围还要按大量项目专属文件名规则推断 | 需要 |
| USGS 1 m DEM | 使用统一的 USGS 1 m 文件名/UTM 公式 | 不需要独立规则目录 audit |
| LINZ DEM | STAC Item 直接提供 footprint 和 COG URL | 不需要 |
| Google / Wayback | XYZ 的 `z/x/y` 数学公式直接确定瓦片范围 | 不需要 |
| CopDEM | CDSE catalogue 和标准经纬度图幅名确定产品 | 不需要 |

因此 `audit_report_path` 应留在 USGS LiDAR Source，而不应搬成全局 `reporting` 参数。其他
Source 的正常下载、转换或网络错误仍统一写入三级 Reporting；以后若某个 Source 也引入了
一套需要单独诊断的外部规则，它可以在自己的模块中定义相应报告，而不是复用 LiDAR 的 JSON
结构。

```bash
python run.py \
  -c configs/default.yaml configs/runs/geodar_state_lidar_audit.yaml
```

审计只执行：

```text
一次州级 WESM 查询 → link list → 规则匹配 → JSON 报告
```

它不会执行覆盖 union、下载瓦片选择、输出目录检查、LAZ 下载或后处理。多个 link list 使用少量并行规划线程读取。

JSON 每项包含：

- `status: matched / partial / unmatched`；
- workunit、project、collect_end、horiz_crs；
- WESM bounds 及其 CRS；
- 去重后的可用文件名数、成功解析数、未解析数、重复/占位统计和文件名样例；
- 所有相关目标的路径、bounds 和 CRS；
- 具体错误。

### 每个新州遇到 unmatched 时怎么处理

这是新增州的固定流程，不把远程 LAZ 头探测放回正式下载逻辑。

1. 先给州目录运行 audit，并同时修改输入州和报告名：

```bash
python run.py \
  -c configs/default.yaml configs/runs/geodar_state_lidar_audit.yaml \
  --set region.init_args.source=/mnt/disk/USA/SurfRef_Data/USA_GeoDAR/GT/AR \
  --set pipelines.usgs_lidar.source.init_args.audit_report_path=./logs/usgs_lidar_AR_audit.json
```

2. 在 JSON 中按 `status != matched` 查看 `workunit`、`project`、`lpc_link`、
   `horiz_crs`、`filename_examples`、WESM bounds 和 target bounds。查看一个条目的命令：

```bash
python - ./logs/usgs_lidar_AR_audit.json AR_Western_4_B24 <<'PY'
import json, sys
items = json.load(open(sys.argv[1], encoding='utf-8'))
item = next(value for value in items if value['workunit'] == sys.argv[2])
print(json.dumps(item, ensure_ascii=False, indent=2))
PY
```

3. 如果文件名明显包含坐标，先从该项目抽取 2～3 个不同位置、必要时不同 workunit
   的官方 LAZ 头。JSON 中的 `lpc_link` 后加 `0_file_download_links.txt` 可取得完整 URL；
   只读取开头 256 KiB，不下载点数据：

```bash
curl -fsSL '<lpc_link>/0_file_download_links.txt' | grep -F '<filename>'
curl -fL --range 0-262143 --max-filesize 262144 \
  '<上一步得到的完整 LAZ URL>' -o /tmp/usgs_laz_header.bin

python - <<'PY'
import laspy
with laspy.open('/tmp/usgs_laz_header.bin') as reader:
    header = reader.header
    print('mins =', header.mins)
    print('maxs =', header.maxs)
    print('size =', header.maxs[:2] - header.mins[:2])
    print('crs  =', header.parse_crs())
PY
```

   如果 256 KiB 不足以包含全部 VLR，可适当增大 Range。这里只对每种未知格式抽样，
   不能逐个读取全项目几千个 LAZ 头。

4. 对比文件名坐标与 header `mins/maxs`，确定 parser、CRS、scale、offset、anchor、
   width/height。若坐标稳定落在下一条固定网格线上，使用 `x_ceil_step/y_ceil_step`；
   边界瓦片不完整时才用少量 `padding` 包住已观察到的 header bounds；
   不能从纯流水号建立空间关系的项目继续 unmatched。

5. 将最窄匹配规则写入 `configs/usgs_lidar_projects.yaml`。新增语法才改
   `geoacquire/sources/usgs/lidar_tiles.py` 和 `lidar_catalog.py`；不改主流程。
   把已核对的文件头样本加入 `tests/fixtures/lidar_headers.json` 或本批的
   `tests/fixtures/lidar_headers_mn.json` / `tests/fixtures/lidar_headers_ov.json` /
   `tests/fixtures/lidar_headers_w.json`，由
   `tests/test_lidar_rules.py` 验证唯一命中、坐标范围和 CRS 编号。

6. 运行完整离线测试，再重新运行该州 audit：

```bash
python -B -m unittest discover -s tests
```

   只有 `parsed_count == filename_count` 且 `unmatched_count == 0` 才算该 workunit 完整
   匹配。`partial` 必须继续检查；确实没有空间编码的流水号项目可以有意保留 unmatched，
   并在规则表、README 或交接文档记录原因。

本次 Arkansas 报告按这一流程将 46 个条目中的 39 个归入已校准规则，并已逐项验证这
39 个 workunit 的完整 link list；剩余 7 个是纯流水号或不规则项目块，仍然有意保持
unmatched。同步代码后仍建议在 Linux 上重跑 AR audit，确认当地缓存和当前官方清单。

### 已有审计报告，怎么复查规则

下面是 Windows CMD、PowerShell、Linux 都能直接执行的单行命令，从项目目录运行。
先用旧 audit 的目标 bounds 和本地 link list 缓存回放，不需要原 TIF，也不会访问网络：

```text
python scripts/replay_lidar_audit.py --audit logs/usgs_lidar_USA_audit.json --coverage --report logs/lidar_audit_verified.json
```

加 `--states ME IL` 可只复查这些 ID 前缀；不加就是审计报告中的全部区域。没有缓存的
清单会记入 `error`，不会偷偷联网补齐。`--coverage` 还执行正式的候选瓦片选择，
`incomplete_targets` 列出覆盖不全的目标；省略它只检查完整文件名清单。

必须区分三个结果：`matched` 是名字全部可解析；header 抽样是坐标规则经实物核对；
`covered` 是名义瓦片范围覆盖目标。三者都不能证明实际点云无空洞、栅格值有效或高程
基准一致。旧 audit 回放也不能发现后来新增的项目，更新目录仍需运行正常 audit。

维护者只针对需要确认的项目，手动抽取首、中、尾三个文件头：

```text
python scripts/sample_lidar_headers.py --audit logs/usgs_lidar_USA_audit.json --workunit "^ME_WesternMtns_1_B24$" --report logs/ME_headers.json
```

每个响应最多读 512 KiB，即使服务器忽略 Range 也不会下载完整 LAZ；失败记录在报告。
加 `--retry-errors` 只重试该报告中的失败样本。这里不会自动生成或修改规则。
项目跨多个 workunit、UTM 分区或百万米边界时，要跨边界补样本，不能只看三个名字就
扩展到全项目。更详细的添加步骤和 parser 表见 [DEVELOPER_HANDOFF.md](DEVELOPER_HANDOFF.md)。

M–N 批次已保留按州加前缀的合并副本，可直接离线复核，不需要挂载原始 TIF：

```text
python scripts/replay_lidar_audit.py --audit logs/lidar_MN_combined_input.json --coverage --report logs/lidar_MN_verified.json
```

OH–VT 这批的同类回放（只用本地清单缓存，不下载 LAZ）：

```text
python scripts/replay_lidar_audit.py --audit logs/lidar_OV_combined_input.json --coverage --report logs/replay_OV_check.json
```

最后四州同样可以离线回放（加 `--states WA WI` 可只检查两州）：

```text
python scripts/replay_lidar_audit.py --audit logs/lidar_W_combined_input.json --coverage --report logs/replay_W_check.json
```

首部抽样的 `header_crs=null` 不一定是文件缺 CRS：LAS 1.4 可把 WKT 放在文件尾 EVLR。
本轮明确补读了这些扩展记录；不是把 WESM 的 CRS 冒充成文件头观测，也没有把额外网络
读取放进正式下载规划。维护说明见交接文档第 9 节。

原始单州报告中的 `0`、`1` 会重名；合并副本使用 `MI__0`、`MN__0` 等 ID 防止目标互相
覆盖。这只用于回放统计，没有改变实际下载目录。单州原始 JSON 保留不动。
离线报告中的 `Cannot read link list` 可能表示本地没有缓存；本批联网读取结果另存
`logs/lidar_MN_link_fetch.json`，其中 10 个入口返回 404，不能把缺缓存直接当成缺命名规则。

### 运行输出怎么读

运行状态只维护一份，按用途渲染成三个层次；越往下越详细：

1. **终端**：只回答整个目录已经完成多少、还剩多少、下载线程利用情况和粗略 ETA；不打印
   TIF/LAZ 文件名、Region ID、workunit 或 URL。
2. **log**：完整保留每一条终端摘要，并额外记录单个目标 TIF 的输入、输出、依赖 LAZ 数量、
   等待时间、生成时间和最终错误；还汇总规划问题与失败 LAZ。
3. **JSON**：程序视角的完整状态，包括目标 TIF ↔ LAZ 依赖、URL、workunit、重试、耗时、
   状态和错误。终端与 log 的摘要都能从 JSON 重新推导。

生产配置中的路径是：

```yaml
reporting:
  log_path: ./logs/geodar_AL.log
  json_path: ./logs/geodar_AL_run.json
  overwrite: false
```

`overwrite: false` 是默认值：同一路径存在兼容 JSON 时续接，而不是清空。换州时必须一起修改
`region.init_args.source`、`reporting.log_path` 和 `reporting.json_path`；若确实要丢弃旧报告
重新统计，才设 `overwrite: true`。

#### 终端：只看总体进度

典型输出如下，实际文本为英文：

```text
2026-09-02T01:00:00Z [plan] TIF files: total=121, already complete=18, need processing=103; download threads=8
2026-09-02T06:12:00Z [progress] 30% TIF complete; TIF files complete=37/121, remaining=84, failed=0; LAZ files discovered=412, downloaded=380, already available=6, failed tasks=1, running tasks=8; download threads active=8/8, average while busy since last update=7.42; elapsed=5h 12m 00s, estimated remaining=11h 49m 00s
2026-09-02T18:40:00Z [final] TIF files: complete=101, already complete=18, failed=1, unavailable=1, total=121
2026-09-02T18:40:00Z [final] LAZ files: downloaded=684, already available=22, failed=2, total=708
```

`TIF files` 就是输入目录中用户认识的目标文件，不要求用户理解 Region。生产 overlay 把
`skip_existing_suffixes` 设为 null，让 Pipeline 先看到全部原始目标，再根据实际处理链批量
检查 `_lidar.tif`；因此 `total` 同时包含已完成和待处理目标，但已有最终结果不会进入 WESM
规划或下载。

进度每跨过目标 TIF 总数的 10% 输出一次，同时保存 JSON。小批次中一个 TIF 可能跨过多个
十分位，此时只输出本次达到的最高百分比，避免一瞬间刷十行。LAZ 仍按 workunit 流式发现，
所以运行中的 `discovered` 只是目前已规划数量，不冒充最终总数；规划流消费完后 JSON 中
`planning_complete=true`，最终摘要才给出这一轮精确的唯一 LAZ 总数。

ETA 使用刻意保守的公式：

```text
本轮已耗时 / 本轮新生成 TIF 数 * 仍未结束的 TIF 数
```

前面为了少量已完成 TIF 顺带下载、但尚未兑现成更多成品的 LAZ，耗时也全部计入分子；因此
估计通常偏慢、剩余时间偏长。后续这些 LAZ 直接推动更多 TIF 完成时，ETA 会自然下降。
`active=8/8` 是此刻 8 个下载＋转换线程都在工作；`average while busy` 通过线程开始/结束
事件积分得到，只计算至少有一个下载线程工作过的时段，不用轮询，也不会让线程同步等待。

#### log：按目标 TIF 查成功、失败和耗时

终端行会原样写入 `reporting.log_path`。此外可直接搜索这些英文标签：

```text
[tif/success]      最终 TIF 本轮成功生成
[tif/existing]     磁盘已有最终 TIF，本轮未下载
[tif/failed]       下载或后处理失败，未得到最终 TIF
[tif/unavailable]  没有可用覆盖或没有产出最终 TIF
[laz/failed]       某个 LAZ 最终失败，并列出受影响的 TIF
[plan/issue]       workunit 未匹配、部分匹配或 link list 读取错误
```

单个成功目标的 log 会包含 `input`、`outputs`、`laz_files`、`wait_for_laz`、`build_tif` 和
`total`。正常 LAZ 成功和 LAZ→DSM 不会逐个刷人类日志；它们的文件级状态在 JSON 中。
workunit 解析、重复 destination 和异常细节只进 log，不再污染终端。

#### JSON：完整记录、断点保存和磁盘续接

JSON 以 pipeline 为单位保存：

- `tifs`：每个输入/输出路径、所需/完成/失败 LAZ、状态、等待与生成时间、错误；
- `laz_files`：filename、URL、project/workunit/collect_end、目标 TIF、下载/转换耗时、
  尝试次数、断点文件、状态和错误；
- `planning_issues`：不能安全规划的 workunit 及少量例子；
- 根级 `errors`：本次运行的规划、LAZ、TIF 和致命错误汇总；续跑前的错误单独保留在
  `previous_errors`，不会与本轮缺漏混在一起；
- `summary`：终端所需的 TIF、LAZ、线程、ETA 和告警汇总。

JSON 在以下时机用临时文件加原子替换保存：规划流消费完、TIF 每跨 10%、每个最终错误、
用户正常中断、pipeline 结束和整轮结束。写 JSON 时先复制内存快照再释放状态锁；其他下载
线程继续工作，不会为了统计停到同一个时间点。

续跑时磁盘是真相，旧 JSON 只是历史与映射：

- 最终 `_lidar.tif` 存在：该目标已经完成，不再规划 Source；
- 当前 GeoDAR 配置的 `*_dsm.tif` 存在：对应 LAZ 文件任务可直接复用；
- `*.laz.part` 存在：保留并继续 Range 下载；
- JSON 写着成功但磁盘结果不存在：按待处理重新规划。

恢复检查对每个实际目录只做一次 `scandir`，把文件名集合放进内存；不逐个打开 TIF、
不算哈希，也不做复杂历史对账。已完成 TIF 的旧 LAZ 映射保留在 `historical_laz_files`，
但不计入本轮 LAZ 总数。

#### 为什么已经下载很多 LAZ，最终 TIF 仍然很少

一个目标目录先做一次批量 WESM 查询，只得到“每个目标可能涉及哪些 workunit”。共享
workunit 按 `collect_end` 从新到旧进入流式规划；目标编号、涉及目标数量都不决定优先级。
每到一个 workunit 才读取其 link list、计算覆盖、扣除新数据已覆盖范围，并产生 LAZ 请求。
HTTP 层按最终 filename 去重，并维持 `max_workers * 4` 个运行中或排队任务。

提交顺序是“从新到旧的 workunit → 当前 workunit 首次发现的唯一 LAZ”，完成顺序受文件
大小、网络和栅格化耗时影响。一个目标可能先得到部分 LAZ，直到更旧 workunit 才补齐；
同一个 LAZ 也可能同时服务多个相邻目标。只有某个目标的候选规划已经关闭，并且它依赖的
全部 LAZ 都完成下载和 Source 转换后，才进入拼接、裁剪和对齐。因此 LAZ 完成很多而最终
TIF 暂时很少，并不表示线程闲置；用终端的活动线程统计判断并发，用 log 的 `[tif/*]` 判断
单个成品，用 JSON 追查具体 TIF ↔ LAZ 关系。

规划细节例如下面一行只写入 log/JSON：

```text
[plan/workunit] workunit=AL_19Co_4_B24 rule=AL_standard_1km_MGRS files=8528 parsed=8528 unmatched=0 target_tifs=14
[plan/workunit] workunit=AL_19Co_4_B24 selected=28
```

`files/parsed/unmatched` 描述整个 link list，`target_tifs` 是它涉及的目标数，`selected`
才是当前 workunit 新增的请求数。`duplicate_names` 是同一清单内部重名；`[plan/duplicate]`
是跨请求最终 destination 重复，后者由 HTTP 层复用首次任务并把结果分发给所有依赖目标。

### 性能模型

- 一个目标目录只读取一次 WESM bbox；
- 同一 workunit link list 只读取、解析一次；
- 新 workunit 先规划，旧 workunit只补未覆盖区域；
- 下载当前请求时，单独规划线程预取下一个 workunit；
- flat 州级池按最终目标文件名去重，并合并使用该 LAZ 的目标 Region；
- HTTP 下载器再次按最终路径兜底去重，漏检也不会覆盖或终止批次；
- HTTP Request/Future 队列有界；
- LAZ 栅格化按 `point_chunk_size` 两遍分块处理，紧接本文件下载，在同一 A 线程内完成；
- B 独立处理就绪 region；慢查询不会阻塞 A 上报已完成文件；
- 转换产物先写 `.part`，成功关闭后改成最终文件名，失败不发布半成品。

## 9. 其他数据源

Google 和 Wayback 继承 `XYZSource`，共享瓦片规划和 JPG/PNG → EPSG:3857 GeoTIFF 落地。Wayback 的 `date: null` 选择最新 release；指定日期选择最接近的 release，并不代表影像实际拍摄日期。

USGS 1 m DEM 通过 WESM 获取项目链接并下载原生 GeoTIFF。`hemisphere` 必须明确为 `north` 或 `south`。

LINZ New Zealand LiDAR 1 m DEM 通过官方公开静态 STAC 选择图幅，并直接下载 NZTM2000
（EPSG:2193）COG；高程采用 NZVD2016。首次建立的 STAC 索引按
`catalog_cache_days` 刷新，刷新失败时可回退到已有旧缓存。一个很小的 bbox 仍可能命中并下载
完整的 1:50,000 图幅，因此正式批量运行前应先用 Source 规划日志和磁盘容量评估范围。

CNIG/PNOA MDT50 cm Source 使用门户实际采用的 POST 接口查询 Polygon/MultiPolygon、分页读取候选
文件，并通过 `localizarCoordsSec` 返回的真实 footprint 在本地复核；最终以 POST 下载原生 COG。
该接口不是公开 OGC API，因此客户端解析边界集中在 `geoacquire/sources/cnig/client.py`。官网规定
匿名最多下载 20 个文件，当前 Source 主动执行该限制；账号登录尚未接入，不能用于超过 20 个唯一
COG 的生产批次。

CopDEM 的 Source 自己维护一个 CDSE 客户端会话。支持 `DGED/DTED × 30/90 m`；
`max_workers` 对它无效，当前按瓦片串行保持认证状态简单，产品直接写入
`acquire.output_dir`。每个瓦片提取完成后都会更新依赖它的 region；独立 B 线程可在后续瓦片
下载时处理已就绪的 region。共享瓦片在一次运行内只获取一次。

## 10. 扩展新数据源

普通 HTTP 数据源通常只需新建一个类：

```python
from geoacquire.core.models import AssetKind, AssetSpec, DownloadRequest, ProductType


class ExampleSource(HTTPSource):
    OUTPUT_SPECS = frozenset({AssetSpec(AssetKind.RASTER, ProductType.DEM)})

    def __init__(self, server: str):
        self.server = server

    def build_requests(self, regions):
        for region_id, region in regions.items():
            for item in self._query(region):
                yield DownloadRequest(
                    region_id=region_id,
                    asset_id=item['id'],
                    url=item['url'],
                    filename=item['filename'],
                    kind=AssetKind.RASTER,
                    product=ProductType.DEM,
                )
```

YAML 直接写类路径：

```yaml
source:
  class_path: geoacquire.sources.example.ExampleSource
  init_args:
    server: https://example.com
```

不需要修改 `run.py`、Pipeline、注册器或 core。内置/可声明的 Source 应设置
`OUTPUT_SPECS`；输出随构造参数变化时覆盖 `output_specs` property。无法提前声明的
动态自定义 Source 可以沿用 `None`，此时跳过静态资产流检查。下载后若需要解包/格式落地，
覆盖 `materialize()`；需要完整认证客户端时直接继承 `BaseSource` 并实现 `acquire()`。

`materialize(report, context, options)` 在 HTTP worker 内调用，输入是**一个物理文件**，
不同文件可并发进入同一个 Source 实例。每次调用使用独立变量/文件句柄，输出关闭后才返回。
普通 HTTPSource 默认逐 region 调用 `build_requests()`，自动关闭该 region 的清单。
需要批量查询的 Source 覆盖 `plan_requests(regions, progress)`，例如现有 USGS 模板。
自定义客户端用 `AcquisitionProgress` 的 `register/close_region/complete` 发布进度，具体例子
看 `DEVELOPER_HANDOFF.md`。旧 BaseSource 只返回最终 report 也兼容，但不能提前后处理。

新增 Postprocessor 同样只需继承 `BasePostprocessor` 并在 YAML 中写 `class_path`。

## 11. 测试

```powershell
python -B -m unittest discover -s tests
```

当前离线测试覆盖：class_path 构造、配置合并、命令行覆盖、Region 目录、惰性 HTTP 调度与重试、CopDEM 规划/解压、XYZ 规划、LiDAR 规则与审计、州级共享池、新旧 workunit 覆盖、窗口化拼接对齐、倍率重采样以及垂直基准策略。

并行重构额外覆盖 `tests/test_streaming.py`：下载转换同线程、最后一个下载结束前产生首个最终
TIF、慢规划/慢后处理互不阻塞、共享文件晚加入、并发完成只触发一次、失败隔离、两个 B 线程
保持各自步骤顺序，以及真实小型 LAZ/JPEG 的转换和目标网格对齐。测试不需要生产账号或下载整州。
当前测试集覆盖最终产物预检、派生产物复用、资产状态和上下游契约回归；具体测试数量和结果
以运行下面的完整测试命令为准。四份规则 fixture 合计 **4715 个真实
文件头观测**。`tests/test_boundaries.py` 新增 18 项检查，覆盖续传起点/长度、缓存并发、
无网络本地复用、类切换参数隔离、LAZ 实际像元大小及有限文件头抽样。测试只在项目内的
唯一临时目录写数据，完成后清理自身目录；不运行根目录个人脚本 `test.py`。

本轮还回放了 WA/WI/WV/WY：147 个 workunit、95 matched / 52 unmatched，716 个唯一
选中文件，覆盖 309/360 个目标，与规则更新后的统计一致。详情在
`logs/final_review_W_replay.json`；这仍是文件名范围覆盖检查，不代表像素质量全部合格。

最终整理保留现有 A/B 并行与两处 output_dir 配置，仅修正具体问题：共享文件的 Region
分发统一由进度账本负责；三个下载入口共用字节响应写盘工具；目录扫描只排序一次；
规则正则只编译一次。LAZ 的分桶与写盘现在使用相同分辨率；例如 2.4 个单位的范围在
resolution=1 时生成 3 个一单位像元，不再把它们压回原范围。

已有 DSM/最终 TIF 不会被自动重算。若需要重新生成旧栅格，先选择一个小区域和新的输出
目录验证，再明确设置 acquire 和 postprocess 的 skip_existing；本次没有改动已有下载结果。

详细设计、维护规则和接手步骤见 [DEVELOPER_HANDOFF.md](DEVELOPER_HANDOFF.md)，历史建立与验证记录见 [PROJECT_REPORT.md](PROJECT_REPORT.md)。

## 12. 已知边界

- 垂直基准只警告或忽略，不自动转换；
- 未审核的 LiDAR 文件名不会下载；
- Wayback 无覆盖区的空白占位图尚未自动剔除；
- 单个 bbox 暂不支持跨越 ±180°；
- 百万级最终文件仍会使 `AcquisitionReport` 和 destination 索引线性增长，届时应按区域分区或引入磁盘 manifest；
- 自定义 Postprocessor 若无法实现 `expected_output()`，Pipeline 无法在 acquisition 前自动复用它的最终文件；仍可使用 `skip_existing_suffixes` 提供目录级规则；
- `cache/wesm/WESM.gpkg` 很大且被忽略，迁移项目时需单独复制。
