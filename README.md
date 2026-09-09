# GeoAcquire

GeoAcquire 根据指定的地理范围下载高程、LiDAR 和影像数据。输入支持三种形式：

1. 坐标范围 `[minx, miny, maxx, maxy]`；
2. 单个目标 GeoTIFF；
3. 直接包含多个目标 GeoTIFF 的目录。

目标 GeoTIFF 只提供覆盖范围、坐标系和目标网格，不会被修改。

## 目前能下载什么

每个数据源都有一份可以直接编辑的示例 YAML。新人推荐修改 YAML 中的输入输出路径，然后运行
对应 YAML；常规使用不需要写一长串命令行参数。

| 数据 | Pipeline 名称 | 下载内容 | 直接编辑的示例 YAML |
| --- | --- | --- | --- |
| USGS 3DEP LiDAR | `usgs_lidar` | LAZ、DSM、DTM | [`configs/examples/usgs_lidar.yaml`](configs/examples/usgs_lidar.yaml) |
| USGS 3DEP 1 m DEM | `usgs_dem_1m` | GeoTIFF DEM | [`configs/examples/usgs_dem_1m.yaml`](configs/examples/usgs_dem_1m.yaml) |
| LINZ New Zealand LiDAR 1 m DEM | `linz_nz_dem_1m` | 原生 COG DEM | [`configs/examples/linz_nz_dem_1m.yaml`](configs/examples/linz_nz_dem_1m.yaml) |
| Google XYZ 影像 | `google` | 影像及地理配准 GeoTIFF | [`configs/examples/google.yaml`](configs/examples/google.yaml) |
| Esri Wayback 影像 | `wayback` | 历史影像及地理配准 GeoTIFF | [`configs/examples/wayback.yaml`](configs/examples/wayback.yaml) |
| Copernicus DEM | `copdem` | 30 m 或 90 m DEM | [`configs/examples/copdem.yaml`](configs/examples/copdem.yaml) |

USGS 数据主要覆盖美国，LINZ 数据覆盖新西兰；Google、Wayback 和 Copernicus 的实际结果取决于
各服务的覆盖情况。Copernicus DEM 需要 CDSE 账号。

每个数据源的类型、源分辨率、国家或地区范围、官方下载入口和相关文献，统一整理在
[`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md)。其中既包括项目已经接入的数据，也包括暂时需要
从官方网页手动下载、以后可以继续接入的补充数据。

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
`[-109.860992, 38.112812, -109.855819, 38.116899]`。六份数据源 YAML 默认都读取这个文件，
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
| `region.init_args.source` | 输入 bbox、目标 TIF 或目标 TIF 目录 |
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

Copernicus DEM 需要把私有凭证配置放在最后加载：

```bash
python run.py -c configs/default.yaml configs/examples/copdem.yaml configs/private_copdem.yaml --check
```

## `default.yaml`、示例 YAML 和命令行的关系

程序按从左到右的顺序合并配置：

```text
configs/default.yaml
        ↓ 完整基础配置：Region、Reporting、六个 Pipeline 和全部参数
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

- `usgs_lidar.yaml`、`usgs_dem_1m.yaml`、`linz_nz_dem_1m.yaml`、`google.yaml`、
  `wayback.yaml`、`copdem.yaml`：每个数据源一份下载模板，已经包含输入和两个输出目录；
- `target_raster.yaml`：只演示单个目标 TIF 的 Region 输入；
- `target_raster_directory.yaml`：只演示目标 TIF 目录输入；
- `target_training_data.yaml`：一次启用 LiDAR、Google、Wayback 和 CopDEM 的多数据源示例。

这些文件都应加载在 `default.yaml` 后面。可以直接修改，也可以复制成自己的场景 YAML。

### `configs/runs/`

这里放实际生产场景，而不是入门模板：

- `geodar_state_lidar.yaml`：GeoDAR 按州正式下载 LiDAR；
- `geodar_state_lidar_audit.yaml`：只检查项目命名规则，不下载 LAZ。

### `configs/private_copdem.yaml`

这里保存 Copernicus DEM 的账号密码，已被 `.gitignore` 排除。使用 CopDEM 时把它作为最后一份
配置加载；不要把凭证写进 default、examples 或文档。

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
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | 数据目录：数据类型、源分辨率、国家范围、官方下载入口和来源文献 |
| [docs/REFERENCE.md](docs/REFERENCE.md) | 完整运行参考：参数、默认值、配置合并、日志、续跑和 LiDAR 审计 |
| [docs/DEVELOPER_HANDOFF.md](docs/DEVELOPER_HANDOFF.md) | 开发与继续扩展：架构边界、新增 Source/Postprocessor、并发约束、LiDAR 规则和提交检查 |
| [docs/PROJECT_REPORT.md](docs/PROJECT_REPORT.md) | 历史与证据：设计决定、规则补全、验证结果和未解决问题 |

如果任务是继续扩展 GeoAcquire，而不只是运行下载，应先阅读开发交接文档。项目通过 YAML 的
`class_path` 加载组件；新增数据源通常不需要修改 Pipeline 或建立新的注册表。
