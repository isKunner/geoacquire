# USA San Juan 示例实例

本目录把示例输入、各数据源下载文件、对齐结果和运行日志集中在一起。根目录下
`configs/examples/*.yaml` 的数据源示例默认都指向这里，可以直接从项目根目录运行。

## 输入 TIF

```text
input/USA_SanJuan_Example.tif
```

该文件复制自：

```text
E:/Data/ResearchData/USA_SR_Data/CanyonTerrain_SanJuan/GT/CanyonTerrain_SanJuan_46_22.tif
```

文件信息：

| 项目 | 值 |
| --- | --- |
| CRS | `EPSG:26912`（NAD83 / UTM zone 12N） |
| 原始范围 | `[599849.999972, 4218950.000052, 600297.999972, 4219398.000052]` |
| WGS84 范围 | `[-109.860992, 38.112812, -109.855819, 38.116899]` |
| 栅格大小 | `448 × 448` |
| 像元大小 | `1 × 1` 米 |

## 目录结构

```text
USA_SanJuan_Example/
├── input/
│   └── USA_SanJuan_Example.tif
├── downloads/
│   ├── usgs_lidar/
│   ├── usgs_dem_1m/
│   ├── linz_nz_dem_1m/
│   ├── google/
│   ├── wayback/
│   └── copdem/
├── results/
│   └── 与 downloads 相同的数据源子目录
└── logs/
    └── 每个示例的人类日志和可续跑 JSON
```

`downloads/`、`results/` 和 `logs/` 会在运行时自动创建，并已加入 `.gitignore`；输入 TIF
和本说明保留为实例数据。

## 直接运行

先执行 `--check`，再去掉 `--check` 开始实际下载：

```bash
python run.py -c configs/default.yaml configs/examples/usgs_lidar.yaml --check
python run.py -c configs/default.yaml configs/examples/usgs_dem_1m.yaml --check
python run.py -c configs/default.yaml configs/examples/google.yaml --check
python run.py -c configs/default.yaml configs/examples/wayback.yaml --check
python run.py -c configs/default.yaml configs/examples/linz_nz_dem_1m.yaml --check
python run.py -c configs/default.yaml configs/examples/copdem.yaml configs/private_copdem.yaml --check
```

San Juan 位于美国，LINZ 数据源只覆盖新西兰。因此 LINZ 命令可以正常运行，但对这个输入不会
命中或下载 COG；要测试 LINZ 实际下载，必须把其 YAML 的 `source` 换成新西兰目标 TIF。
