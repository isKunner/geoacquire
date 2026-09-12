# GeoAcquire 项目报告

> 本文是按时间追加的工程历史记录，其中“当前”“最终”等说法只代表对应章节写作时的状态。
> 新人使用项目请看根目录的 [README.md](../README.md)，完整参数请看
> [REFERENCE.md](REFERENCE.md)，不要把本文早期章节当作当前配置说明。

## 1. 项目范围

GeoAcquire 用于按坐标范围或目标 GeoTIFF 获取 DEM、遥感影像和 LiDAR，并可将栅格拼接、裁剪、重投影和对齐到目标网格。

项目名称由 DEMRS 扩展为 GeoAcquire，因为架构稳定职责是 geospatial asset acquisition，不限定数据类型。

## 2. 设计结论

### 2.1 无注册器的 class_path 构造

YAML 直接声明 `class_path + init_args`，由 jsonargparse 动态导入、校验构造参数并实例化。新增 Source 不修改 `run.py`、Pipeline 或 central registry。

### 2.2 统一 Source 生命周期

```text
RegionProvider → BaseSource.acquire() → AcquisitionReport → Postprocessors
```

普通 URL Source 继承 HTTPSource，复用惰性请求、并行下载、续传与重试。CopDEM 保持认证客户端整体，直接实现 BaseSource。

### 2.3 Acquisition 与 Postprocess 分界

- JPG 写地理参考、LAZ 栅格化、CopDEM ZIP 解压属于 Source 产物落地；
- 面向目标 TIF 的 mosaic、crop、horizontal warp、resample 和 alignment 属于 Postprocess；
- Postprocessor 只使用当前 AcquisitionReport，不扫描目录。

### 2.4 配置分层

`configs/default.yaml`（英文）和 `configs/default_zh.yaml`（中文）集中列出全部参数，
两份实际配置值保持一致且 pipeline 默认禁用。运行时二选一作为基础；pipeline 按 name 合并，
postprocess 按位置合并，生产配置只写差异。命令行 `--set` 只允许修改已存在路径。

## 3. 当前实现

| 模块 | 能力 |
| --- | --- |
| Region | bbox、单 TIF、单层 TIF 目录、结果排除与完成跳过 |
| HTTP | generator 惰性请求、`workers × 4` 有界下载＋转换任务、续传、重试、事件式统计 |
| USGS LiDAR | 一次目录 WESM 查询、规则表、审计、flat 州级共享池、LAZ/DSM/DTM |
| USGS 1 m DEM | WESM、项目 link list、10 km tile 过滤、GeoTIFF |
| Google | XYZ、image/GeoTIFF、目标对齐 |
| Wayback | release 解析、XYZ、image/GeoTIFF、目标对齐 |
| CopDEM | CDSE 登录/刷新、Catalogue、ZIP、解压、DGED/DTED 30/90 |
| Postprocess | 窗口化 mosaic/crop/warp/resample、直接输出目录、垂直基准 warn/ignore |

LiDAR 只输出 HR；LR 下采样和训练配对明确留给后续超分项目。

## 4. 2026-08-30 LiDAR 整理

### 4.1 规则表

`configs/usgs_lidar_projects.yaml` 已补充由真实日志确认的：

- Alabama 2015/2016/2017/2020/2023/2024 相关批次；
- Arkansas Western/Eastern/NorthEast、Ouachita、North Corridor、NRCS、FEMA、
  Dardanelle、Benton、Upper Saline、Lake Conway、Languille/Cache，以及边界相交的
  Missouri Southwest 和 Mississippi Central Delta；
- Texas CentralEast、Panhandle、RedRiver、Lower San Bernard、Houston、South；
- West Virginia FEMAR3/FEMAHQ；
- WV 边界可能命中的 Pennsylvania WesternPA；
- Wyoming SouthCentral、Southwest、North Converse、Southeast、Sheridan、Laramie、FEMA East、Grand Teton、Yellowstone、Carbon；
- Massachusetts Western B24、Central/Eastern B21；
- Connecticut Statewide C16/C25，以及州界相交的 New York Southeast 4 County；
- 已有 Hawaii 和 Delaware 规则。

无法从日志安全建立空间关系的格式没有猜测规则，包括 StratMap serial、`FF232`、
`HE278`、`Goshen_0995`、Arkansas legacy 流水号和 MO/AR Game Fish 不规则项目块。
它们保留为 unmatched audit 项，避免一次错误匹配下载几千个文件。

Arkansas 规则使用用户提供的 46 项 audit 报告归并格式，并通过 30 次小范围 HTTP Range
读取抽查官方 LAZ header。规则覆盖其中 39 个 workunit，且其完整 link list 已全部解析
验证；剩余 7 个纯流水号/不规则项目有意保持 unmatched。

Massachusetts 三个最新 workunit 通过 9 次小范围 HTTP Range 读取确认：文件名保存公里级
MGRS 数字，实际 1500 m 瓦片左下角向上吸附到全局 1500 m 网格。网格模型新增可选的
`x_ceil_step/y_ceil_step`，不用固定偏移或扩大 padding 模拟周期性半格偏移。三个完整 link
list 共 10133 个文件均解析成功，未匹配数为 0。
使用全国 audit 中保存的 107 个 MA 目标范围执行离线选片复核，三个 workunit 分别选择
108、70、11 个 LAZ，共 189 个唯一文件；107 个目标的范围覆盖率均为 100%。

Connecticut 使用 15 个官方 LAZ header 样本确认两种规则：C16/C25 都把 5000 英尺母格
分成 `nw/ne/sw/se` 四个 2500 英尺象限，三位 X 坐标在一百万英尺处回绕；州界 New York
项目把十位数字拆成 UTM X/Y，末尾年份不参与坐标。十个优先 workunit 的 31547 个文件
全部解析，92 个 CT 目标共选择 267 个唯一 LAZ，范围覆盖率均为 100%。

### 4.2 正式规划

```text
遍历一个目标目录并建立全部 Region
→ 使用总包围范围执行一次 WESM query_many
→ 将候选 workunit 分配回实际相交的目标 Region
→ workunit 新到旧
→ link list + project rule
→ 在该 workunit 的目标范围合集上选择尚未被新数据覆盖的瓦片
→ 按最终文件名形成州级 flat 共享池并合并 target Regions
→ HTTP workers
→ 单次 LAZ rasterization + 多目标复用
→ 分别对齐回每个目标 TIF
```

例如 AL 目录有 121 个 TIF，某个 workunit 的 `targets=14 selected=28` 表示该项目只与
其中 14 个目标相交，并为这 14 个目标新增 28 个唯一物理 LAZ 请求；不是为单个 TIF
选择 28 个，也不是对全部 121 个目标选择 28 个。正式流程不读取远程 LAZ header。
当前 workunit 请求被消费时，一个规划线程预取下一 workunit link list。

### 4.3 审计短路径

Audit 现在直接执行 WESM、link list 和 filename rule parsing，不再重复正式下载所需的 tile selection、coverage union、destination-layout 校验或后处理。多个 link list 以四个以内的线程读取。

JSON 报告保留 WESM bounds/CRS、target bounds/CRS、filename examples 和 matched/partial/unmatched 统计。

### 4.4 输出可读性

- 大 Region 集合只打印总数、头尾样例，不再刷完整列表；
- 规则日志显示 `files/parsed/unmatched/selected`；
- HTTP 显示 workers、pending_limit、pending、submitted 和每 50 个完成进度；
- `targets` 是当前 workunit 相交的目标数，`selected` 是跨这些目标去重后的新增物理请求数；
- `pending` 是最多 `workers × 4` 个运行中或排队的文件任务，不是线程数；
- `max_workers=8` 是上限，selected 只有两个时只出现两个 `.part` 属于正常行为。

## 5. 本轮去除的重复与冗余

1. 规则选中 parser 后直接调用对应 parser，不再先运行所有 projected-name 正则再筛选。
2. Audit 使用独立短路径，不再构造下载请求或执行空间覆盖选择。
3. 删除未使用且与 `query_many()` 重复的 WESM 单区域 query 实现。
4. BoundsRegionProvider 只标准化坐标，长度/顺序/有限值统一由 Region 校验。
5. CopDEMClient 在 Source 构造时创建一次并复用，不在 acquire 时重复构造和重复校验。
6. CopDEM 与其他 Source 一样直接使用用户指定或默认解析出的 acquire 输出目录。
7. CRS transform 在统一 helper 中拒绝 `inf/nan`，避免错误值进入 GeoPackage SQL 后才报 `no such column: inf`。
8. GeoDAR 生产 YAML 是与操作系统无关的 default 小覆盖，不再复制完整 Source/Acquire/Postprocessor 配置。
9. USGS 共用基类与逐 workunit 模板分层，LiDAR 不再继承一个永远不会调用的 DEM planning hook。
10. LiDAR 从按 URL 去重改为按最终目标路径去重；同名请求合并 target Regions，避免不同 URL 写入同一文件。
11. HTTP 层重复目标从“抛异常终止批次”改为“保留首次传输并记录复用双方”；同 Region 不重复登记资产。
12. 无法读取的 USGS link list（包括旧项目 404）进入审计并跳过当前 workunit，不再中断整个州。
13. 删除后处理的 `output_location` 四模式；只保留 `output_dir`，null 跟随 acquire，
    非空直接写入指定目录。Linux 默认直接使用每州 GT 输入目录，不创建项目专用目录。
14. 删除 acquisition 的 `output_layout` 和 Region 子目录；相邻目标共享一个下载目录，
    统一按最终文件名去重。按州隔离由用户分别设置 `acquire.output_dir`。
15. WESM 候选查询对 WGS84（含 EPSG:9518 的水平分量）与 NAD83 使用等价经纬度
    envelope，避免离线 PROJ 请求 NOAA datum grid 后产生 `inf`；真实瓦片选择、栅格化
    和后处理投影不走该近似分支。
16. USGS link list 忽略没有瓦片编号、stem 以 `_` 结尾的项目占位名，并单独统计
    `ignored_placeholders`；`filename_count` 改为去重后的可用名称数，使
    `parsed + unmatched == files` 可以直接作为完整性判断。

公开可独立调用的边界仍保留必要的防御式校验，例如 AcquireOptions 和 HTTPDownloadService 的直接调用参数。这些不是同一内部路径上的重复业务检查。

## 6. 配置整理

Source 专用完整 examples 已删除，因为它们与 default 重复。保留：

```text
configs/examples/target_raster.yaml
configs/examples/target_raster_directory.yaml
configs/examples/target_training_data.yaml
```

GeoDAR 数据集的实际运行配置位于：

```text
configs/runs/geodar_state_lidar.yaml
configs/runs/geodar_state_lidar_audit.yaml
```

两者都必须放在 `configs/default.yaml` 后合并。

## 7. 关键历史修正

- WESM resume 仅在 HTTP 206 时追加，服务器忽略 Range 时从头覆盖；
- WESM 查询显式保留 collect_end；
- numeric horiz_crs 先试 EPSG，再试 ESRI；
- XYZ GeoTIFF 使用原生 EPSG:3857 pixel grid；
- CopDEM 登录重置 session start，整数最大边界采用半开 geocell；
- HTTP report 不再用 URL 作为唯一 key；最终路径冲突采用可审计的首次传输复用；
- LiDAR/DEM 的水平 CRS 与垂直 CRS 分开处理；
- 垂直基准不自动转换，LiDAR 输出保留源垂直 CRS。
- WESM 的 WGS84/NAD83 特例只服务于大范围候选 workunit 查询，不改变数据坐标或输出 CRS。

## 8. 验证记录

测试解释器：项目 Conda 环境中的 Python 3.11。

```text
python
```

首次整理时通过（最新验收见第 12 节）：

```text
Ran 44 tests
OK
```

覆盖包括：

- class_path 构造、named pipeline merge、postprocessor 局部覆盖和 `--set`；
- 单 TIF、单层目录 Region；
- 本地 HTTP 下载、有界预取、重试、flat layout 和重复目标复用；
- Google XYZ 规划；
- CopDEM 半开规划和本地 ZIP 提取；
- LiDAR full XY、MGRS token、axis pair、CT quadrant、网格向上吸附及 AL/AR/CT/MA/TX/WV/PA/WY rule catalog；
- Arkansas、Massachusetts、Connecticut 多种官方 header bounds 包含性回归；
- 未知项目审计、link list 404、清单/目标重复去重、audit 短路径、共享 LAZ、下一 workunit 预取、新旧覆盖；
- EPSG:9518/WGS84 目标在无 NOAA grid 环境下查询 NAD83 WESM；
- chunk-size invariant LAZ rasterization；
- 窗口化 mosaic、严格 transform/shape、target_scale 和直接输出目录；
- 垂直基准 warn_once/ignore 和源垂直 CRS 保留。

本轮还执行了三个真实范围的 audit，均未下载 LAZ：

| 范围 | workunit | 结果 |
| --- | --- | --- |
| Delaware | `DE_Statewide_1_B23` | 4779/4779 文件匹配；legacy `DE_Snds_2013` 保持 unmatched |
| Alabama | `AL_17Co_2_2020` | 7718/7718 文件匹配；legacy `AL_Clay_CleburneCo_2013` 保持 unmatched |
| Hawaii Island | `HI_Hawaii_Island_2017` | 8629/8629 文件匹配，issues=0 |

Hawaii Island 首次审计发现规则缺失后，使用项目内已有两个 LAZ header 只读确认了标准 MGRS 坐标、1000 m 瓦片和 `EPSG:6635` 水平 CRS，随后补规则、补离线测试并重新审计通过。本轮没有重复下载大型 LAZ。

## 9. 文档

- `README.md`：用户入口、命令、配置、日志解释和扩展示例；
- `DEVELOPER_HANDOFF.md`：目录职责、稳定边界、代码风格和新 LiDAR 规则处理步骤；
- `PROJECT_REPORT.md`：设计与验证记录。

## 10. 文件边界审计

本轮所有持久修改、创建和删除都位于：

```text
<项目目录>
```

没有修改、创建、覆盖或删除项目外文件。外部旧项目、用户数据和 Linux 路径仅作为历史上下文或只读检查对象。

## 11. 已知边界

1. 未审核 LiDAR 项目只进入 audit，不会下载；
2. 垂直基准不自动转换；
3. Wayback blank placeholder 尚未识别；
4. antimeridian 单 bbox 尚未支持；
5. 百万最终文件的 report/destination index 仍线性增长；
6. WESM 约 3.4 GB，换机器需单独复制 cache；
7. 当前 conda 环境可能显示 requests dependency 与 GDAL_DATA warning，它们不是本项目代码错误。

## 12. 2026-08-31 全国审计规则补全与复核

### 范围和实现

输入 `logs/usgs_lidar_USA_audit.json` 包含 705 个记录，按 workunit/link 合并后为
691 个 workunit，涉及 19 个州、2544 个目标。本轮使用审计保存的 bounds 和项目内
完整 link list 缓存复核，不要求当前挂载原 E: 盘，也没有重新下载整批 LAZ。

规则仍只由 `configs/usgs_lidar_projects.yaml` 选择；新语法只增加到 `lidar_tiles.py`
与 catalogue parser 声明，未修改 run.py、Pipeline 或输出目录约定。本轮重点新增：

- ME/MD/LA 的非标准 MGRS-like 坐标和跨百万米/100 km 边界；
- IL/IN/KY/GA 的 state-plane 坐标、奇数千英尺编码、取整和微小边界容差；
- AZ/ID/IA/KS 的 UTM、Albers、绝对坐标数字与固定 zone/band；
- FL 的 5000 ft FDEM 行列编号，CO 的 LD/轴坐标，CA 的多种地方格网；
- CA Upper Pit：MGRS 单元只是 state-plane 网格角点的定位信息，不能直接当成瓦片 CRS；
- ID Southern workunit 15 的两个格网字母拼写错误和零 easting 特例。

发现并修正了“名字能解析但位置不对”的初版规则，包括 ME 固定北坐标偏移、
IL 四位数字末尾零、McHenry 的百万位、IN Hamilton 网格、WESM 中少数不准确的 CRS。
这些修正以官方 header bounds 为依据，没有重新加入运行时自动猜测。

### 减少重复计算

profiling 显示，同一清单逐文件执行 `CRS.to_string()` 会重复查询 PROJ authority
数据库。Source 现在缓存最终 CRS 字符串；水平 CRS 和 Transformer 也采用 128 项
有界缓存。保留必要的有限值检查，不对每个文件重新构造相同投影关系。

官方 CO El Paso 清单中的 `https:/rockyweb...` 少斜杠问题，在清单入口进行幂等修正。
这不会更改其他域名，也不会覆盖用户文件。

### 验收结果与口径

完整清单回放：2,788,212 个去重后 workunit 内文件名，2,101,604 个可解析；
367 个 workunit 完整匹配，1 个 partial，323 个未匹配/缺清单。这里按 workunit
计数，不是全国唯一物理文件数，也不是“下载数量”。

| 州 | 目标范围被名义瓦片完整覆盖 / 总目标 |
| --- | ---: |
| AR | 218 / 218 |
| AZ | 63 / 63 |
| CA | 567 / 567 |
| CO | 308 / 309 |
| CT | 92 / 92 |
| DE | 4 / 4 |
| FL | 150 / 150 |
| GA | 189 / 200 |
| HI | 7 / 7 |
| IA | 157 / 157 |
| ID | 70 / 76 |
| IL | 165 / 165 |
| IN | 73 / 74 |
| KS | 57 / 57 |
| KY | 119 / 119 |
| LA | 61 / 61 |
| MA | 107 / 107 |
| MD | 23 / 23 |
| ME | 95 / 95 |

合计 **2525 / 2544** 个目标名义范围完整覆盖，规划为 5603 个唯一文件名。
另 19 个目标不能声称可完整下载：GA 11、ID 6、IN 1、CO 1。原因和对应项目见
交接文档第 9 节；机器可读报告 `logs/lidar_audit_verified.json` 保留具体目标与比例。
仍然未匹配的历史项目多数不影响本批目标，但没有把它们算作已支持。

新增 `tests/fixtures/lidar_headers.json` 保存 **152 个 workunit、403 个官方文件头样本**，
`tests/test_lidar_rules.py` 验证规则唯一性、范围包含、声明的水平 CRS 编号，以及
ME/IL/FL 边界和三个真实样本中心的选片。样本是最多读取 512 KiB 响应得到的观测值，
不是由当前 parser 生成的期望值；失败请求保存在 logs，未伪造结果。

这些检查不证明实际点云密度、栅格无空洞或高程基准一致，也不是完整 LAZ 下载和
栅格化验收。文件头中部分自定义 WKT 有小数截断/BoundCRS 包装；回归的 CRS 比较
核对声明的 EPSG 分区与单位，不替数据提供者认证 datum 参数。

最终验收：`unittest discover -s tests` **54 项通过**；44 个 Python 文件通过 AST
语法检查；`default.yaml --check` 通过；中英文 default 的实际参数完全一致。

### 新增维护入口和文件边界

```text
python scripts/replay_lidar_audit.py --audit logs/usgs_lidar_USA_audit.json --coverage --report logs/lidar_audit_verified.json
python -B -m unittest discover -s tests
python run.py -c configs/default.yaml --check
```

回放工具不会联网；`sample_lidar_headers.py` 只在维护者明确调用时联网抽样，不自动
修改规则。README 和交接文档说明了完整清单、header 抽样、目标覆盖三种验证的差别。
本轮新增/修改仍全部位于项目目录，未改项目外文件；
没有删除外部旧项目、用户数据、原始审计或下载成果。

主要改动文件：`configs/usgs_lidar_projects.yaml`、`core/geo.py`、USGS 的
`base.py/lidar.py/lidar_catalog.py/lidar_tiles.py`、`tests/test_core.py` 与新增的
`tests/test_lidar_rules.py`、fixture、两份 scripts，以及 README/本报告/交接文档。
最新汇总固定看 `logs/lidar_audit_verified.json`。早期的 `lidar_audit_replay*`、
`lidar_replay_corrected.json` 等过程记录已在最终 fixture 和 verified 报告稳定后清理。

## 13. 2026-08-31 M–N 州审计补全

### 本批范围与改动

输入是用户提供的 MI、MN、MO、MS、MT、NC、ND、NE、NH、NJ、NM、NV、NY 共 13 份
`usgs_lidar_<州>_audit.json`，合计 499 条记录、494 个唯一 workunit、1462 个目标。
只处理这批报告及其中相交的边界项目，没有把新出现的其他州报告混进本轮。
原始 JSON 未改；`logs/lidar_MN_combined_input.json` 给副本的目标 ID 加州前缀，
避免 MI 的 `0` 与 MN 的 `0` 被当成同一目标。这不影响下载/后处理的目录规则。

新增 97 条 catalogue 条目，精确限定到 305 个已核对的 workunit；不是用一个州级
通配符宣称全州支持。原有规则保留，新增语法也仍由 YAML 显式选择。

主要补全内容：

- MN 的不同精度 UTM 数字对，MI 的 2500 ft 网格和省略百万位；
- NJ 官方母格/象限/子格和 NC 官方 panel 编码；
- MT/NH 的压缩坐标、NE 的 state-plane 百万位偏移；
- ND 部分交付将 O 计入格网列字母的约定，仅修正已核对的批次；
- NM SouthEast 分段的 MGRS 方格内 X/Y，不能当绝对坐标读取；
- MO/NY 等项目的 MGRS-like、投影坐标与 1.5 km 格网取整；
- 轴坐标之间可带分隔符、坐标后带日期及 `_LAS_2019` 等已经实测的尾缀。

修复一个真实入口问题：NC 的 `..._LA_37_10380906_.laz` 是合法瓦片，原先会被
“末尾下划线”判断当作占位项丢掉，导致有些项目 `files=0`。现在保留六位以上纯数字
尾 token，仍过滤真正的 `USGS_LPC_Project_.laz`；测试覆盖清单入口，不只测 parser。

独立文件头复核发现 NY FEMA R2 的初版直读坐标会偏半公里。新增
`x_round_step/y_round_step` 还原最近网格角点，例如 `122478` 对应
`(121500, 478500)`。这与其他项目的 ceil 不同，不能加宽 padding 代替。Catalogue
在加载时检查同一轴不能同时指定 ceil/floor/round，正式逐瓦片解析不重复验证配置。

### 证据与测试

NC 按[官方技术规范第 27–29 页](https://it.nc.gov/documents/files/north-carolina-technical-specification-lidar-base-mapping/download?attachment=)
的 panel 图核对行列顺序，NJ 按[官方格网元数据](https://www.arcgis.com/sharing/rest/content/items/4ae00a8b5d9f40f690941a08d509a3bb/info/metadata/metadata.xml)
核对原点、象限和子格；所有新规则再与 USGS 官方文件头的真实 bounds 对比。

初定后另取 316 个文件头独立复核，并为取整歧义等情况追加 77 次针对性检查。
最终新增 fixture `tests/fixtures/lidar_headers_mn.json` 保存 305 个 workunit 的
1306 个不重复观测样本（并非全部抽样请求都会成为可支持规则）。与上一批 403 个
样本合计 1709 个；范围、声明 CRS、真实文件中心选片和格式边界均在离线测试中核对。
运行时没有恢复远程文件头标定；只有本次维护显式读取少量响应，每次上限 512 KiB。

最终 `python -B -m unittest discover -s tests`：**62 项通过**。
44 个 Python 文件通过 AST 语法检查，`default.yaml --check` 通过；中英文 default
参数一致，未修改两份默认配置。
完整清单回放共 1,981,389 个 workunit 内去重文件名，1,740,942 个可解析；
317 个 workunit 完整匹配、2 个 partial、175 个 unmatched/缺入口。
这些数字不是本批要下载的 LAZ 数量；实际选片计划为 **2976 个唯一文件名**。

### 目标覆盖结果

| 州 | 名义瓦片完整覆盖 / 目标总数 | 剩余缺口 |
| --- | ---: | ---: |
| MI | 111 / 126 | 15 |
| MN | 161 / 165 | 4 |
| MO | 180 / 189 | 9 |
| MS | 68 / 95 | 27 |
| MT | 78 / 78 | 0 |
| NC | 101 / 121 | 20 |
| ND | 67 / 67 | 0 |
| NE | 155 / 155 | 0 |
| NH | 34 / 61 | 27 |
| NJ | 38 / 38 | 0 |
| NM | 82 / 84 | 2 |
| NV | 45 / 50 | 5 |
| NY | 233 / 233 | 0 |

合计 **1353 / 1462**；同批目标在本轮修改前只有 47 个可被旧规则完整覆盖。
剩余 **109 个** 不能声称已完整解决。这里检查的是审计保存的目标 bounds 和规则推算
的名义瓦片范围，不是实际点密度、栅格空洞、高程基准或完整 LAZ 下载验收。

### 剩余问题不能混作一类

1. **缺入口/清单失效**：本次联网读取 10 个清单返回 404，另 2 个 NH workunit 的
   `lpc_link` 为空。其中 NC 的 5 个 Phase5 入口影响 20 个目标，NH 两个入口影响
   27 个目标；需要寻找官方替代清单/目录，不能靠猜正则修复。
2. **有清单但坐标语义不足**：MI 部分网格/serial、MN Goodhue、MO StLouis/Boone、
   MS NRCS East/Lauderdale、NM MRCOG/SanLuis、NV Reno/Clark 等。先按剩余目标关联
   的项目查官方 tile index 或补抽 header，再加精确规则。
3. **已支持项目中的异常名字**：NJ Southern 的 `__16B16` 与 NW6Co 的 `D4CC7`
   各一个仍为 partial；其余名字可用且本批目标已覆盖，不伪造异常名字的范围。

未支持的旧 workunit 不一定阻塞目标：已有新数据覆盖时可以不依赖旧数据；反之
文件名全部 parsed 也不能替代目标覆盖检查。逐目标原因及下一步入口见交接文档第 9 节。

### 结果文件和交付边界

- `logs/lidar_MN_verified.json`：本批最终规则/选片/覆盖结果，优先看这个。
- `logs/lidar_MN_combined_input.json`：原始报告的命名空间隔离副本。
- `logs/lidar_MN_link_fetch.json`：清单读取结果，包括 10 个真实 404；离线回放缺缓存
  只会报告本地读不到文件，要与本记录结合判断，不能直接断言网络状态。
- `logs/lidar_MN_headers.json`、`lidar_MN_holdout_headers.json`、
  `lidar_MN_extra_headers.json`：原始观测与失败记录，留作后续复核。

`before/after/proposals/approved` 等过程快照已经清理；它们不属于运行输入，最终规则由
fixture、combined input、原始观测和 verified 报告共同复核。

本批代码改动：`geoacquire/sources/usgs/lidar.py`、`lidar_tiles.py`、`lidar_catalog.py`、
`configs/usgs_lidar_projects.yaml`、`tests/test_lidar_rules.py` 与新 fixture；同步更新
README、交接文档和本报告。迁移 Linux 时要同步这三份 Python 文件和规则表，不能只
拷贝 YAML，因为新 parser 及 round 参数需要对应代码。

M–N 命名规则补全阶段没有修改 run.py、core、HTTP 下载并发、输出目录参数或默认配置。该阶段所有新增/修改都在
项目目录内；项目外文件、原始审计 JSON、既有下载成果均未改。
没有下载完整 LAZ，也没有运行新的后处理或写入用户的 GT 数据目录。

## 14. 下载与 region 后处理并行重构（2026-08-31）

### 已实现的执行方式

按最终确认的两组职责落地：A 的每个工作线程连续完成一个文件的下载和转换，再获取下一个文件；
B 独立执行就绪 region 的后处理。没有独立的转换池，没有把 LAZ→DSM 移到 B。

- HTTP A 由 `acquire.max_workers` 控制；LiDAR、Google/Wayback 复用同一个文件任务模板。
- CopDEM 保留客户端串行下载＋ZIP 提取，逐文件发布完成，未改认证及下载协议。
- B 由新增的 pipeline 级 `postprocess_workers` 控制，默认 1，可设 2；同 region 的步骤有序，
  不同 region 可并行，不等整个州完成才开始。不同 Source pipeline 仍顺序运行。
- `AcquisitionProgress` 仅管理依赖和就绪，不理解具体 Source 或栅格算法；Pipeline 的回调只入队。
- region 清单先关闭，再在所有已选文件转换成功后发布一次。LiDAR 保留州级 WESM 查询和新旧
  workunit 选择；新瓦片覆盖目标后可提前关闭，不必等待无关旧 workunit。
- 共享文件只下载/转换一次。后发现的 region 也能拿到已完成产物；同一 region 重复请求不重复登记。
- HTTP worker 自己上报结果，下一条请求规划慢不会阻挡已就绪 region。A 不等待 B 的处理任务。
- 下载/转换失败只阻止相关 region；后处理失败停止该 region 的后续步骤，不中断其他 region。
- DSM、XYZ TIF、CopDEM 提取产物先写 `.part`，完整关闭后替换最终文件；失败不留下假完成文件。

### 参数与兼容边界

中英文 default 的五条 pipeline 均列出 `postprocess_workers: 1`，支持：

```text
--set pipelines.usgs_lidar.postprocess_workers=2
```

生产 GeoDAR 配置仍继承 default，原命令继续可用。`max_workers=8` 现在是 8 个“下载＋转换”
文件任务，可能同时产生 8 份 DSM 栅格化内存负载，不意味着始终维持 8 路网络传输。
目录、文件后缀、规则表、注册方式以及栅格算法不变；没有添加 region 子目录或隐藏下载目录。

旧自定义 BaseSource 若不发布 region-ready，Pipeline 仍能在其返回后执行后处理。
新 HTTPSource 不需改 main/core：build_requests 与可选 materialize 即可；批量规划或自定义
客户端的三个进度调用点和线程安全约定已写入交接文档第 5 节。

### 验收

- 完整离线测试：**79 项通过**（原 62 项＋新增 17 项）。
- 新增 17 项并发测试额外连续执行 5 轮，全部通过；44 个 Python 文件 AST 语法检查通过。
- 新调度测试重点覆盖：同线程下载转换、慢规划、慢后处理、晚加入共享文件、并发完成只触发一次、
  下载/转换失败隔离、后处理步骤顺序和失败隔离、两个 B 线程、已有原文件复用，以及 CopDEM 逐瓦片通知。
- 真实小型 LAZ 冒烟：3 个目标 region、2 个物理 LAZ，其中一个被两个 region 共用，实际生成 DSM 并
  窗口化对齐；3 个最终输出的 CRS/transform/shape 与目标相同，并有有效像素。最后一个下载任务被
  刻意阻塞时，首个 region 的最终 TIF 已经存在；两份 LAZ 只转换两次。
- 真实 JPEG → Web Mercator GeoTIFF 测试通过；原 CopDEM ZIP 提取和现有规则样本测试保留通过。
- 英文、中文 default 的 `--check` 均通过；配置参数值等价测试通过。
- 本次不进行整州网络速度测试，也没有用生产账号重新验证 CDSE 登录下载；CopDEM 的调度用替身测试，
  ZIP 提取由本地真实归档测试验证。原有 requests 依赖版本和 GDAL_DATA 警告仍存在，未修改 Conda 环境。

### 本次文件变更清单

- 新增：`geoacquire/core/acquisition.py`、`tests/test_streaming.py`。
- 调度与接口：`geoacquire/core/source.py`、`context.py`、`pipeline.py`、`config.py`、`models.py`，
  `geoacquire/services/http_download.py`。
- 数据源接入及完整写入：`geoacquire/sources/usgs/base.py`、`lidar.py`、`lidar_rasterizer.py`，
  `geoacquire/sources/copdem/source.py`、`client.py`，`geoacquire/sources/rs/georeference.py`。
- 后处理契约和 warn-once 并发保护：`geoacquire/postprocess/base.py`、`raster_align.py`。
- 配置：`configs/default.yaml`、`configs/default_zh.yaml`、`configs/runs/geodar_state_lidar.yaml`（仅补注释）。
- 文档：`README.md`、`DEVELOPER_HANDOFF.md`、`PROJECT_REPORT.md`。

**项目外文件修改：无。** 未修改原项目 DEMRSDataDownload、用户 GT、既有下载数据、原始审计 JSON、
凭证配置或 Conda 环境。测试生成的数据仅在 GeoAcquire 内的临时测试目录中，完成后由测试清理。
Linux 副本未直接改动：部署时需同步此次 Python 文件（包括新 acquisition.py）及配置；旧进程不会热更新。

## 15. OH–VT 十二份审计补全（2026-08-31）

### 范围、实现与证据

处理用户提供的 OH、OK、OR、PA、PR、RI、SC、TN、TX、UT、VA、VT 共 12 份 JSON，
合计 432 条记录，按 `(lpc_link,workunit)` 去重后 429 个 workunit、2144 个目标。
原始报告保持不变；合并副本仅给目标 ID 加州前缀，避免各州的 `0` 相互覆盖。

新增 **89 条 YAML 条目，精确覆盖 241 个 workunit**。主要包括 Ohio 1250 ft 截断坐标、
Tennessee 7000×4000 ft 象限、Texas Neches 不等长坐标与特定列零坐标、LowerRioGrande
500 m 子格、Oregon 补零坐标/日期后缀、Pennsylvania 坐标顺序与百万位、Vermont 旧版
中心格网、Virginia 字母行号、South Carolina 交错数字和子格，以及已核对的标准 MGRS。
现有可表达的语法继续用原 parser 加 YAML；只有新语法新增确定性 parser，均有注释和示例。

正式运行仍只查静态规则，不读远程 header、不在线拟合。A 的“下载＋转换同线程”、
B 的 region 就绪后并行后处理、输出目录、参数和运行配置均未更改。

本次显式维护抽样共得到 **2622 个成功文件头观测**：初查、独立 holdout、不同网格前缀、
零坐标、象限及 SC 专项补样。每次最多读取 512 KiB，不下载完整 LAZ。
过程中代理连接失败的 433 个补样使用仅限该维护进程的直连重试，全部成功；
没有改系统代理、环境变量或生产网络策略。

最终 fixture `tests/fixtures/lidar_headers_ov.json` 包含 **241 个 workunit、2362 个观测**。
全部新增 workunit 均有未参与初定规则的 holdout；原两份 fixture 保留，合计 **4071 个观测**。
这是抽样坐标与声明水平 CRS 核验，不保证未抽到的每个文件名或供应方 CRS 元数据都无错误。

### 最终回放与测试

最终报告：`logs/lidar_OV_verified.json`。对缓存的完整清单逐名解析，而不只看审计样例；
共 2,031,618 个可用文件名，其中 1,728,851 个已解析，按文件名去重选中 4399 个候选文件；
该计数没有实际下载，也不表示对不同 URL 的字节内容做了哈希去重。
状态为 **274 matched、1 partial、154 unmatched/缺入口**。改前完整覆盖 257 个目标，
改后 **1844/2144 个目标名义范围完整覆盖，300 个仍有缺口**。

| 州/地区 | 名义范围完整覆盖 / 本批目标 | 仍有缺口 |
| --- | ---: | ---: |
| OH | 189 / 207 | 18 |
| OK | 361 / 362 | 1 |
| OR | 89 / 90 | 1 |
| PA | 245 / 253 | 8 |
| PR | 29 / 29 | 0 |
| RI | 14 / 14 | 0 |
| SC | 61 / 61 | 0 |
| TN | 114 / 114 | 0 |
| TX | 574 / 755 | 181 |
| UT | 42 / 44 | 2 |
| VA | 77 / 166 | 89 |
| VT | 49 / 49 | 0 |

“覆盖”是名称推导瓦片范围的几何覆盖，不是点云密度、DSM 无洞或高程精度验收；
本次没有将完整州 LAZ 下载、转换，也没有重新读取用户 GT。少量显式 padding 是已观察到的
源坐标边缘越界：OH Phase2_6 的 X 5 ft，以及两份 TN 交付的 0.5 ft；没有用大 padding
掩盖格网错位。YAML 内单位跟源 CRS，不能一概当米。

验收：完整离线测试 **90 项通过**；新增 11 项语法/边界/限制范围测试，文件头 bounds、
声明 CRS 和真实中心选片回归通过。原 17 项并发测试另跑一轮通过；其中有真实小 LAZ/JPEG
转换与目标网格对齐。中英文 default 的 `--check` 均通过。
原有 requests 版本/GDAL_DATA 警告仍存在，本轮未修改 Conda 环境。

### 不能强行通过的情况

独立补样发现了“普通文件通过、边界文件错位”的反例；验证失败的 4 个候选未安装：

- `TX_Pecos_Dallas_B2b_2018`：偏移格网上的取整尚未统一；
- `TX_West_Central_B11_2018`：`14SKA985000` 的实际 northing 比通常解码多 100 km；
- `VA_West_Chesapeake_B1_2017`：`17SPB015000` 同样有 northing 格网行错误；
- `UT_StatewideSouth_3_2020`：`11SQB3564` 的实际 easting 与所写 zone/grid 不符。

反例保存在 `lidar_OV_rejected.json`。Texas RedRiver 等已修成 `mgrs_zero_x_columns_*`，
只修复核验过的列，不再对所有 `000` 统一进位。

唯一 partial 是已有 `TX_CentralEast_1_A23`，其中 O 列变体未核实；其余支持名称仍可选片。
其他未解决项主要是 TX StratMap quad/subtile、错区 MGRS、VA SouthCentral/Sandy/旧格网、
OR Wallowa quad、PA/OH 等旧流水号。先查 `incomplete_targets` 找实际受影响目标，再找
官方 tile index、项目元数据或少量 header；不能凭看起来像坐标就加入规则。

清单读取另有 **8 个真实 404**：NC Phase5 Polk、OH Phase1_7、VA NShenandoah_1，
以及 TX 旧 Brazoria/Jackson/Matagorda/San Patricio/Victoria。另有一条没有 `lpc_link`。
这些在 `lidar_OV_link_fetch.json` 和合并输入中核对；离线回放会显示缺缓存，不能误当成
只有名称规则未补。后续处理步骤和新增 parser 的位置见交接文档第 9 节。

### 文件清单与部署边界

- 生产修改：`configs/usgs_lidar_projects.yaml`、`geoacquire/sources/usgs/lidar_tiles.py`、
  `geoacquire/sources/usgs/lidar_catalog.py`。
- 测试修改：`tests/test_lidar_rules.py`；新增 `tests/fixtures/lidar_headers_ov.json`。
- 文档：`README.md`、`DEVELOPER_HANDOFF.md`、本报告。
- 维护证据：`logs/lidar_OV_combined_input.json`、`lidar_OV_headers.json`、
  `lidar_OV_rejected.json`、`lidar_OV_link_fetch.json`、`lidar_OV_SC_extra.json` 和最终
  `lidar_OV_verified.json`，以及 `cache/link_lists/` 中可离线复用的清单缓存。

批次专用的 fit/approve/patch 脚本及 `before/after/proposals/approved` 快照已经清理；公开
维护入口固定为 `scripts/`。最终 verified 报告仍是结论来源。
**GeoAcquire 以外的文件修改：无。** 未动原项目、GT、已有生产下载、凭证或 Conda 环境。
测试只在项目内部临时目录生成/清理数据，未删除用户数据。

Linux 机器未直接修改。同步至少包括规则 YAML、`lidar_tiles.py`、`lidar_catalog.py`，
新 parser 与 YAML 必须一起部署；建议同步 tests 和三份文档。无需修改 default、州级运行
YAML、输出目录或并发参数。已运行的旧进程不会热更新，重启后才使用新规则。

## 16. 最后四州规则更新：WA / WI / WV / WY（2026-08-31）

### 输入、修改与验收范围

本轮读取用户提供的四份 `logs/usgs_lidar_<州>_audit.json`，共 147 个独立 workunit、
360 个目标。只在合并副本 `lidar_W_combined_input.json` 给 region_id 加州前缀，
原始四份 JSON 的 SHA256 均未变化，记录见 `lidar_W_verification.json`。

新增 **43 个 YAML 规则表项，明确覆盖 54 个 workunit**：WA 24、WI 27、WV 1，
以及与本批目标相交的 MN 1、VA 1。WY 的 27 个已有规则保留，未给 Goshen 纯编号造规则。
新语法只有四个：`axis_suffix_pair`、`wi_sewrpc_10kft`、`wi_2500ft_quadrants`、
`wa_thurston_4500ft`；其余使用已有 parser 和显式 grid 参数。

WI 部分县的 4500 ft 格网不经过坐标原点，因此 `ProjectedGrid` 增加两个默认 0 的
`x_grid_origin/y_grid_origin`：取整前减 origin，取整后加回；与 token 的 offset 区分。
例如 `738365` 恢复到 (738530,365990)，不是简单乘千，也不能靠大 padding 补救。
新增字段只用于已确认项目的规则表，没有增加 default/运行配置参数。

### 最终回放结果

最终报告为 `logs/lidar_W_verified.json`；早期改前基线已在最终报告稳定后清理。

| 州 | 目标数 | 改前完整覆盖 | 改后完整覆盖 | 仍有缺口 |
| --- | ---: | ---: | ---: | ---: |
| WA | 85 | 0 | 63 | 22 |
| WI | 99 | 4 | 70 | 29 |
| WV | 85 | 79 | 85 | 0 |
| WY | 91 | 91 | 91 | 0 |
| 合计 | 360 | 174 | 309 | 51 |

workunit 从 **41 matched / 106 unmatched** 提升到 **95 matched / 52 unmatched**，
最终无 partial。累计核对完整清单中 738,497 个可用文件名，606,108 个可解析；按本批
目标范围选出 716 个唯一文件名。本次没有发现选中集合内同州同名不同 URL 的冲突。
这些是名称推导 footprint 的名义几何覆盖，不是完整点云/DSM 无空洞、点密度或高程精度验收。
未重新读取用户 GT，未下载整州 LAZ，未改变已有生产下载。

### 样本与重要发现

- 新 fixture `tests/fixtures/lidar_headers_w.json` 保存 **54 个 workunit、644 个真实
  LAZ 范围和水平 CRS 观测**。初查首/中/尾之外，另有未参与定规则的 holdout，以及跨
  MGRS 格网、X/Y 零值、取整余数、百万位和象限补查。
- 其中 476 个文件的 WKT 存在尾部 EVLR，首部抽样没有 CRS 并非文件缺失投影。
  本轮明确补读 EVLR；每个补查任务最多 512 KiB，非零起点必须校验 206 和 Content-Range。
  `lidar_W_headers.json` 保留 `crs_storage`、`crs_evlr_offset`，未把 WESM 当作实测声明。
- WV East 的 `FF232` 与 VA Northeast 的 `HE278` 可复用字母行/数字列 parser，
  但原点分别是 (259090.17,4045879.91) 与 (259090,4045878)，不能通用同一偏移。
  WV 的实际 WKT 声明 NAD83 / UTM17（带 bound transformation），与 WESM 的
  NAD83(2011) 不同；规则保留交付声明。WA 若干交付也保留原始水平 WKT，不强改 EPSG。
- 完整清单回放发现 WI Iron 有 52 个象限后缀，初版普通 split parser 漏掉了它们；
  补采四象限后与 Florence 一起使用 `wi_2500ft_quadrants`，最终 parsed=files。
- 原有测试曾将 WV East 当作“不应命中”的例子，本轮已换成真正未审核的 workunit；
  真实 `FF232` 则通过官方 bounds/CRS 与固定坐标测试。

最终完整测试 **96 项通过**（新增 6 项）；四份 fixture 合计 **4715 个观测**。
还包括原有 17 项并发测试、小 LAZ/JPEG 转换与目标对齐；中英文 default 的 `--check`
均通过。没有修改 Conda 环境；原 requests/GDAL_DATA 警告仍存在。

### 未解决项与下一步

51 个缺口目标均列在 `lidar_W_verified.json.incomplete_targets`，含路径、范围、CRS、
覆盖比例和关联 workunit。优先解决影响这些目标的项目，不要仅按 unmatched 数量排序。

- **WA 22 个目标**：King County 7 个、3 County 1 个、Olympic/Western 分幅 14 个。
  King/3County 是纯编号，需要官方 tile index；不能按链接清单的行号还原坐标。
- **WI 29 个目标**：8County 各县、Monroe、Jefferson、Price、Clark、GreenLake、
  Sawyer/Washburn 等以纯流水号交付，同样需要项目索引，未猜测其规则。
- **WY Goshen** 仍是未解析项目，但本批 91 个目标已有其他已支持数据覆盖。
  后续可从 [USGS Goshen 官方 metadata 目录](https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/LPC/projects/WY_GoshenCounty_2017_C20/WY_GoshenCounty_B1_2017/metadata/)
  查 tile 编号与范围；本轮未批量抓取这些 XML 或引入运行时逐文件查询。
- **WA quad** 已找到 [DNR 分幅规范](https://www.dnr.wa.gov/publications/ger_wa_lidar_plan_2020.pdf)，
  但规范本身不能证明旧交付全都照此编码。明确检查的 `q49122A7325` 与简单解释相差
  数千米，其他样本也有小偏差，详见 `lidar_W_quad_review.json`。先核对官方 tile index、
  经纬度基准和边界例外，不能加大 padding 让候选通过。本轮没有安装该规则。
- `WA_PSLC_2000/0_file_download_links.txt` 实测 **404**；
  `WA_Colville_Part1_5A_2014` 没有 `lpc_link`。这两项是入口问题，不是 parser 问题。
  读取状态见 `lidar_W_link_fetch.json`。

查找、补规则、文件头/EVLR 验证与离线回放的具体位置和命令，已同步至交接文档第 8、9 节。
维护工具仍不会自动安装规则；正式下载规划不新增远程文件头探测。

### 改动文件及部署边界

- 生产代码：`configs/usgs_lidar_projects.yaml`、
  `geoacquire/sources/usgs/lidar_tiles.py`、`geoacquire/sources/usgs/lidar_catalog.py`。
- 测试：`tests/test_lidar_rules.py`、`tests/test_core.py`；新增
  `tests/fixtures/lidar_headers_w.json`。
- 文档：`README.md`、`DEVELOPER_HANDOFF.md`、本报告。
- 维护观测：`logs/lidar_W_combined_input.json`、`lidar_W_headers.json`、
  `lidar_W_link_fetch.json`、`lidar_W_quad_review.json`、`lidar_W_verification.json`、最终
  `lidar_W_verified.json`，以及缺失清单的 `cache/link_lists/` 缓存。批次专用研究脚本
  已在结果固化后清理；它们不参与正式下载，也不是用户入口。

**GeoAcquire 以外修改的文件：无。** 原始四份审计、用户 GT、原项目、凭证、Conda 环境、
已有生产输出均未修改。测试只在项目内生成/清理自身的临时数据。

## 17. 最终代码检查与整理（2026-08-31）

本轮检查正式运行代码、公开维护脚本、配置、测试及三份说明文档，不再补新州规则。
此前章节是阶段记录；当前验收以本节为准。整体架构保持 class_path 构造、Source 适配、
A 下载并同线程转换、B 按就绪 Region 后处理，以及 acquire/postprocess 各一个 output_dir。

### 已修正的重复和边界问题

1. 移除 core/geo.py 中 raise 后永远不可达的重复实现；修正 CRS 示例为 EPSG:9518 → 4326。
   RasterAlign 复用公共水平 CRS 解析，不再维护另一套相同算法。
2. 移除 LiDAR 的旧 `_request_targets` / `_clone_asset` 多 Region 分发。materialize 只返回
   一个物理文件的产品，AcquisitionProgress 唯一负责共享、失败传播和就绪通知。
3. RegionResult 增加 asset_count，计数/日志不再创建完整资产列表；Pipeline 对已经提前入队的
   Region 不再复制最终报告快照。仍保留 A/B 各自的列表，避免并发修改同一报告。
4. 目录扫描先筛选再检查文件，只对最终 TIF 列表排序一次；规则正则在加载时编译并保留，
   不依赖反复查询的全局正则缓存。WESM 的 CRS 文本在一批查询中只生成一次。
5. 三个下载入口共用 services/http_stream.py，仅复用 HTTP 响应写盘这一事实。登录、搜索、
   客户端生命周期与重试仍分别由原模块负责，没有引入注册器、新线程池或目录配置。
   检查 Range 起点及已知总长度，200 重写、206 正确续传、完整 .part 遇到 416 可完成提交；
   异常不把不完整字节发布成最终文件。WESM 新下载也使用 .part 后原子替换。
6. 链接缓存改用唯一临时文件，处理不同州进程同时替换缓存的 Windows 冲突。workunit 计划
   缓存键补入 workunit 和水平 CRS，避免不同元数据行误用相同 URL 的解析结果。
7. CopDEM 不在 acquire 开头重复登录；客户端需要搜索时才认证，因此空输入和本地 DEM 复用
   无需网络。已有 ZIP 的完整性检查仍保留，不把必要的数据校验当成无用重复。
8. YAML 更换 class_path 时替换对应组件，防止旧 init_args 混入新类；同类的小覆盖照常合并。
9. 修复 LAZ 分桶分辨率和 TIF transform 不一致：非整倍数范围向上取整像元数，保持配置的
   实际像元大小；单点范围不产生零分辨率。exclude_classes=[] 现在真正表示不排除 DSM 分类，
   None 仍用默认 [7,18]；重复 output_products 只生成一次。
10. 公开文件头抽样显式关闭尾部 EVLR 读取，首部读取保持 512 KiB 上限；保留可读 bounds，
    不因截断首部无法访问 EVLR 而丢掉整个观测。CRS 缺失仍需维护者补证据，不自动猜测。

### 注释、模块和测试文件的取舍

- 补充 Region 栅格 metadata 的 shape/transform 顺序、DownloadRequest 的共享含义、
  LocalAsset metadata 的只读约定、ProjectedTile/Grid 的输入输出示例、LAZ→DSM 和倍率对齐示例。
- 补齐正式模块顶层公开类/函数的 docstring，保持英文代码注释、明确类型边界和现有 header 风格。
- lidar_catalog、lidar_tiles、lidar_rasterizer 分别负责声明校验、命名数学、本地点云，不合并；
  Source/调度/Postprocess 也不按“少文件”强行放在一起。
- LAZ 两遍读取有意保留：第一遍测量分类过滤后的范围，第二遍分块聚合。点缓冲有界，但
  输出栅格数组仍占内存，max_workers 仍同时控制下载和转换任务数量。
- 全部正式回归测试和四份 fixture 保留；新增边界测试单列 test_boundaries.py，不继续堆入
  test_core.py。测试改为唯一临时目录，不再递归删除可能事先存在的固定 .test_tmp。
- 根目录 test.py 是个人坐标修正脚本，未运行、未修改、未删除；
  `tmp/pdfs/nc_lidar_spec.pdf` 是 NC 规则证据，继续保留原路径。
- 2026-09-02 清理了可再生的 `__pycache__`、旧联调输出、规则标定片段、重复州级 audit、
  过程快照和批次专用研究脚本。正式 tests/scripts、WESM、link-list 缓存、合并审计输入、
  原始 header/失败观测和最终 verified 报告均保留。

### 验证结果

- 最终离线测试 **114 项通过**：原 96 项及新增 18 项边界回归。
- 其中 17 项调度测试继续通过，包含小 LAZ/JPEG 转换、共享文件只处理一次、首次最终 TIF
  早于整批下载结束、两个后处理线程、失败隔离和目标网格对齐。
- **48 个 Python 文件**通过 AST 语法检查；本轮静态检查未发现未使用 import、tab/行尾空白，
  正式模块顶层公开类/函数均有 docstring。未安装额外 formatter/linter，也未更改 Conda。
- 中英文 default 的 --check 均通过；测试确认实际参数值一致。默认值、生产运行覆盖 YAML、
  已审核规则 YAML 和四份规则 fixture 没有改变。
- 最后四州完整离线回放：147 workunit，95 matched / 52 unmatched，716 个唯一选中文件；
  WA 63/85、WI 70/99、WV 85/85、WY 91/91，合计 309/360，与本轮整理前一致。
  输出为 logs/final_review_W_replay.json；这不是一次新的整州 LAZ 网络下载或像素质量认证。
- 原环境仍有 requests 依赖组合和 GDAL_DATA 警告；不在本轮修改用户的 geo-torch 环境。

重现检查：

```text
python -B -m unittest discover -s tests -b
python -B run.py -c configs/default.yaml --check
python -B run.py -c configs/default_zh.yaml --check
python -B scripts/replay_lidar_audit.py --audit logs/lidar_W_combined_input.json --coverage --report logs/final_review_W_replay.json
```

### 保留的边界与交付范围

旧 DSM/最终 TIF 不会因代码更新被自动重算；需要采用修正后网格时，先对小区域和新输出
目录验证，再明确配置 skip_existing。原始 LAZ/JPG 被 keep_download=false 删除后，跨运行
重启仍可能重新下载；没有为此新增另一套缓存清单/调度协议。同一次运行的共享复用有回归保证。
未审核规则、剩余覆盖缺口、垂直基准、跨进程同一下载目录锁等边界不因本轮检查自动消失。

新增正式工具模块 services/http_stream.py 和测试 tests/test_boundaries.py；修改的正式代码
集中在 core、HTTP/WESM/CopDEM 写盘、LiDAR materialize/rasterizer/cache，以及上述输入输出
注释。同步 README、交接文档、两份 default 注释、测试临时目录与 .gitignore；未更改
run.py、生产 YAML、账号文件、规则表、原始 audit、现有数据输出或 Conda 环境。
**GeoAcquire 之外没有修改或删除文件。**
未修改 Linux 机器。同步到 Linux 至少要一起复制规则 YAML、`lidar_tiles.py`、
`lidar_catalog.py`，建议连同 tests 和文档同步；不能只复制 YAML。
并行流程、default、州级运行 YAML、输出目录保持不变，已运行进程不会热加载新规则。

## 18. LINZ New Zealand LiDAR 1 m DEM 接入（2026-09-01）

新增 `geoacquire/sources/linz/`，通过官方公开静态 STAC 选择并下载新西兰全国 LiDAR
1 米 DEM COG。数据源继承 `HTTPSource`，不修改 Pipeline、core 或下载服务，不需要 LINZ
账号、API Key 或 AWS 凭证。

静态 Collection 包含 Item 链接但不集中提供每个图幅的空间范围。`LINZStaticSTACCatalog`
在首次运行时并发读取轻量 Item JSON，提取 WGS84 footprint、相对 COG URL、采集时间和校验和，
再原子写入单个本地索引。后续按缓存直接规划；缓存过期且远端刷新失败时可回退到已有完整索引。
Source 将任意 Region bounds 变换到 WGS84，先做 bbox 短路，再以 STAC geometry 精确相交，
避免倾斜的 NZTM 图幅仅按 envelope 判断时在角落多下载文件。

下载产物保留原生 NZTM2000（EPSG:2193）COG，元数据记录 1 米分辨率、NZVD2016
（EPSG:7839）、采集时间、Item URL、checksum 和 CC BY 4.0。默认文件名增加
`_nz_dem_1m` 后缀，避免共享 acquire 目录冲突。当前下载单位仍是完整 LINZ 1:50,000
图幅，不提供服务器端 bbox 裁剪，小目标也可能下载较大的源 COG。

两份 default 新增等值的 `linz_nz_dem_1m` pipeline，示例为
`configs/examples/linz_nz_dem_1m.yaml`，README 同步运行方法和容量边界。四项 LINZ
离线测试覆盖精确选片、投影 Region、相对 STAC 链接、缓存复用和离线旧缓存回退；完整回归
**118 项通过**。真实联网冒烟读取官方当前 424 个 Item，Wellington 示例范围精确命中
`BQ31.tiff`；该验证只建立临时 STAC 索引，没有下载大型 DEM 图幅，临时索引随后清理。

## 19. 目标目录单次快照优化（2026-09-01）

`BoundsRegionProvider.get_regions() -> dict[str, Region]` 契约保持不变。目录输入改为通过
`os.scandir()` 一次枚举当前目录的全部真实文件，同时保存原始路径和按父目录划分的
文件名集合；扩展名、排除后缀和完成后缀均在内存中判断。`_has_completed_siblings()` 不再为
每个目标、每个后缀调用 `os.path.isfile()`。递归扫描及其 `recursive/region_id_mode` 参数已
移除；目录树由上层接口逐个目录运行，保持一个输入目录对应一个输出目录。只有未完成目标
继续进入 `read_raster_region()`。

## 20. 资产 kind/product 契约集中化（2026-09-01）

资产是一个可用本地成果文件及其结构化描述，不是额外的数据实体。Source 在 DownloadRequest
写入 product，HTTP 服务只传递，materialize 可以把 laz 转成 dsm/dtm，RasterAlign 按
`kind=raster + input_products` 筛选。product 表示成果内容，不承担技术形态、实例身份、
文件命名或 pipeline 日志身份。

`core/models.py` 集中 `AssetKind/ProductType/AssetStatus/AssetSpec` 以及跨层 metadata key。
内置 Source 不再散落书写 kind/product 裸字符串，并声明当前配置的最终资产组合；PipelineConfig
在 `--check` 时沿后处理链验证契约，能够提前拒绝“只输出 laz 却要求 dsm”或“只保留普通
图片却运行 RasterAlign”等配置。自定义 Source product 仍允许非空字符串，未知动态产出可声明
为 None，未把扩展接口封闭成只能使用内置枚举。

`output_role` 已删除：RasterAlign 输出继承唯一输入的 product，`output_suffix` 只决定文件名；
同时选择 DSM、DTM 等不同产品会直接报错，避免把不同语义的数据拼进同一输出。Region 目标
栅格 metadata 键同步集中，并通过模型属性读取。
Source 专属 mode、RasterAlign 策略和 XYZ 格式仍留在各自模块，没有把所有稳定字符串都堆入
models。

新增 3 项回归覆盖枚举/模型边界及两类错误资产流；完整离线测试 **122 项通过**，中英文
default 的 `--check` 均通过并打印各 Source 的 `kind:product` 产出契约。

## 21. 三级运行输出与轻量续接（2026-09-02）

新增 `core/run_state.py` 和 `services/run_reporting.py`，把原先散落在 Pipeline、HTTP 与 LiDAR
Source 中且难以解释的 `print()` 分为三个稳定层次：终端只显示目标 TIF/LAZ 总体数量、十分位、
活动下载线程积分和保守 ETA；log 原样保留终端并按单个目标 TIF 记录输入、输出、依赖数量、
等待/生成耗时和错误；JSON 保存完整 TIF↔LAZ、URL/workunit、重试、时间、状态和错误。

进度按一开始即可确定的目标 TIF 总数估算，不为获得准确 LAZ 总数而展开整个请求 generator。
workunit 仍从新到旧流式规划，HTTP 仍维持 `workers × 4` 有界窗口，文件完成后仍立即推动已满足
目标进入后处理。运行中的 LAZ 数称为 discovered；请求流消费完后才标记精确总数。线程利用率
只在线程开始/结束事件上积分，不增加轮询、barrier 或同步等待。

`reporting.overwrite=false` 默认续接兼容 JSON。磁盘是真相：Pipeline 批量识别最终 TIF，Source
复用 DSM，HTTP 保留 `.part` 续传；JSON 成功但磁盘缺失时重新规划。恢复检查通过 Reporting
缓存每个实际目录的一次 `scandir`，RasterAlign 和 LiDAR materialize 也复用该内存文件名集合，
不为每个目标反复 `isfile()`、不开 TIF、不算哈希。JSON 在启动、规划问题、请求流结束、TIF
每跨十分位、最终错误、中断和结束时原子替换保存；状态锁只保护内存快照，其他下载线程继续运行。

混合续跑时，preflight 已完成 TIF 不进入 Source report。最终合并不再使用会提前计算默认参数的
`completed.get(id, report.regions[id])`，而是显式分支选择两份结果；回归测试覆盖“部分已有、部分
本次处理”，避免已完成 Region 在收尾阶段触发 KeyError。

GeoDAR 生产 overlay 将程序 log/JSON 写到 `logs/`，并让 Pipeline 看到全部原始目标以统计已有
与待处理 TIF；根目录 `geodar_log/` 仍只是用户从各服务器手工汇总日志的归档目录，程序不自动
写入。新增 Reporting 回归覆盖三级职责、错误落盘、磁盘续接、中断保存和事件式线程利用率。

## 22. GeoDAR 旧日志错误复核（2026-09-02）

只按结构读取 `geodar_log/` 的旧日志与旧版 audit JSON。旧日志中 68 个曾显示无规则的唯一
workunit 已被当前规则表覆盖（TX 67、AL 1），无需重复加规则；其余主要是文档中已保留的旧
流水号、StratMap/quad/错区 MGRS 等未核实项目，未猜测扩展规则。唯一保留的 partial 是
`TX_CentralEast_1_A23` 的 201 个未核实 O 列名称。SC/TN/VT 的实际文件失败是网络连接超时，
旧项目的 404/缺 `lpc_link` 也不是正则问题，均留给重跑或后续数据源核查。

TX 有 17 个已下载 LAZ 在栅格化时因完整文件头没有 CRS 而失败，均属于
`TX_RioGrand_FTWhit_2014`。Rasterizer 现仅在头 CRS 缺失时使用规划阶段保存的已审核
`source_crs`（EPSG:6342），正常头声明仍优先。连同混合续跑回归，完整离线测试 133 项通过。

## 23. PE3D 1 米 MDT 下载接入（2026-09-11）

新增 `geoacquire/sources/pe3d/`。纯规划器把任意 Region 转到 WGS84 后按巴西系统分幅计算
1:5,000 图幅，再以带过期和故障回退的官网 `quadriculas_pe.json` 缓存核对可用性，并取得下载表单
需要的门户 ID。2026-09-11 的目录含 16,816 条图幅—市镇记录，去重后为 12,962 个图幅。三个用户
下载样本的实际 GeoTIFF 中心分别正确反解为
`SC-24-X-B-IV-1-NE-D-I`、`SC-24-X-B-IV-1-NE-D-III` 和
`SC-25-V-A-II-4-NO-C-IV`。样本 ZIP 的来源地址确认 1 米 MDT 使用
`1_5000/BLOCO-*/4_MDT_RASTER/MDT-*.zip`，因此链接发现同时核对产品代码、比例尺目录、
产品目录、文件前缀、请求图幅和官方主机，不靠下载后的像元大小猜分辨率。

`PE3DClient` 的构造保持离线；只有未缓存图幅需要查询时才初始化会话、保存 CAPTCHA 并等待
人工输入。账号密码沿用 CopDEM 的 ignored private YAML 约定，验证码不持久化。登录后的实际
ZIP 传输复用 `HTTPDownloadService` 的并发、重试、续传和 reporting；Cookie 值不写入运行状态，
只记录请求含有的 header 名。解包只提取精确匹配的 TIF/TFW/aux.xml，通过 `.part` 写入且最后
发布 TIF，不使用 `extractall`。

门户 TLS 响应只包含叶证书，遗漏 `ZeroSSL ECC DV SSL CA 2` 中间证书。新增 PE3D 专用、已核验的
ZeroSSL/Sectigo CA 链，并把 `DownloadRequest.verify_tls` 扩展为可接收 CA 文件路径，使登录、目录
请求和共享 HTTP 下载线程使用同一验证链；主机名、证书签名和有效期校验仍保持启用。

六类门户产品集中在 `products.py`，当前仅开放 `dtm_raster`（代码 4、DTM raster）；未来类型
通过补充已核验的 archive convention 复用相同会话、分幅和下载流程。默认与示例 pipeline
只返回原生 1 米 MDT 图幅，不拼接、裁剪、重采样或重投影。

为大量小 Polygon 增加 `VectorRegionProvider`：每个 Polygon/MultiPolygon 要素形成一个 Region，
完整几何以 WKB 保存，边界框只负责候选预筛。PE3D 规划改为在认证前处理全部 Region，集中生成
唯一图幅集合，再登录一次并跨要素按 `batch_size` 批量查询；同一图幅通过 `target_region_ids`
关联回所有 Polygon，只发生一次物理下载。可选 `min_intersection_fraction` 用于忽略已有 PE3D
栅格足迹越过名义图幅边界的窄重叠。

用户提供的两要素测试 SHP 原 `.prj` 声明 EPSG:4326，但坐标与样本栅格一致，实际为 EPSG:31984；
现已把 `.prj` 修正为 SIRGAS 2000 / UTM 24S。Provider 仍会拒绝不可能的经纬度坐标，并允许其他
错误或缺失 CRS 的输入显式使用 `crs_override`。专用示例直接读取修正后的 CRS，并以 5% 相交阈值
精确规划为 `SC-24-X-B-IV-1-NE-D-III` 和 `SC-24-X-B-IV-1-NE-D-I` 两个预期图幅。

## 24. Polygon 几何复用、LINZ 精确选片与 CNIG MDT50 cm 首版（2026-09-11）

把 Vector Region 的 WKB 解析、坐标转换和正面积相交判断集中到
`geoacquire/core/region_geometry.py`。bounds 与目标 TIF 输入仍回退到历史外包矩形语义，只有
确实携带 WKB 的矢量输入使用真实 Polygon/MultiPolygon；PE3D、LINZ 和 CNIG 共用这层几何边界。

LINZ Source 由逐 Region 外包矩形查询改为一次规划全部 Region：目标真实 Polygon 与官方 STAC
Item footprint 精确相交，同一 COG 汇总全部 `target_region_ids` 后只生成一个下载请求。仍保存
原生 EPSG:2193 / NZVD2016 COG，不增加服务器端裁剪或重投影。

CNIG 官网的 MDT50 cm 页面已通过真实 HTTP 探测确认三个 POST 端点：`archivosSerie` 接受 WGS84
Polygon/MultiPolygon 并分页返回候选文件，`localizarCoordsSec` 返回文件 GeoJSON footprint，
`descargaDir` 直接返回原生 COG TIFF。新增 `geoacquire/sources/cnig/`，把门户 HTML 解析封装在
client 边界，按真实几何复核、跨 Region 去重，并在同一地理图幅同时提供相邻 UTM 分区副本时优先
选择目标质心所属分区。共享 HTTP 请求契约增加 GET/POST 与表单 body 支持，原 GET 行为不变。

CNIG 公开的 OGC API Coverages 当前最高仅提供 5 米 MDT，不含第三期 0.5 米产品；本实现使用的
是门户内部接口而非公开 OGC API。官网 FAQ 规定匿名最多下载 20 个文件，因此首版在规划阶段主动
拒绝超过 20 个唯一 COG，账号登录尚未接入。联网冒烟仅查询塞维利亚小范围并核对得到唯一首选
`MDT50CM-ETRS89-H30-0984-5-6-COB3-V1.tif`，没有下载约 128 MB 的实体文件。

同时修复 `run.py` 重复使用 `-c` 时丢失前面配置文件的问题；现在“单个 `-c`
后写多个路径”和“每份 YAML 重复一次 `-c`”都按顺序合并。新西兰示例也从美国占位
TIF 替换为惠灵顿真实小范围。两个示例的 `--check`、Python 编译检查和完整离线回归均
通过，最终为 **155 项测试通过**。
