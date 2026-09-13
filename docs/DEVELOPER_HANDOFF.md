# GeoAcquire 开发交接文档

本文面向下一位维护者，说明稳定边界、目录职责、代码风格和 USGS LiDAR 新项目规则的处理方法。用户运行命令请先看根目录的 [README.md](../README.md)。

## 1. 不要破坏的设计边界

1. YAML 用 `class_path + init_args` 直接指定类，不增加 Source registry、scanner 或 `if source == ...`。
2. Pipeline 调用 `BaseSource.acquire()` 并接收通用的 region-ready 回调，不理解 URL、Token、ZIP、LAZ 或具体 Source。
3. 普通传输复用 `HTTPSource + HTTPDownloadService`；有状态客户端可以直接实现 `BaseSource`。
4. Source 的 materialization 负责把传输文件变成该数据源的可用本地产物，例如 JPG → GeoTIFF、LAZ → DSM、ZIP → DEM；与本文件下载在同一个 A 工作线程完成。
5. Postprocess 只处理 `Region + LocalAsset`，不理解远端协议，也不扫描输出目录猜资产。
6. 新增 Source 不修改 core；只有通用于多个 Source 的能力才进入 `services/`。
7. `RunStateTracker` 是统计状态的唯一写入模型；终端、log 和 JSON 只能由
   `RunReportingService` 从该模型渲染，Source 不自行维护计时器或总体计数。

## 2. 目录职责

```text
run.py                            CLI、配置加载、RuntimeContext 构造
geoacquire/core/
  models.py                      稳定数据契约及边界校验
  config.py                      YAML 合并、--set、jsonargparse 实例化
  source.py                      BaseSource/HTTPSource 生命周期
  acquisition.py                 共享文件依赖账本、清单关闭、region-ready 通知
  pipeline.py                    无数据源分支的 orchestration
  context.py                     显式共享服务
  run_state.py                   线程安全运行状态、TIF/LAZ 映射、耗时与线程积分
  geo.py                         水平 CRS 和 bounds 变换
geoacquire/regions/               外部范围/TIF → Region
geoacquire/services/              Source-neutral HTTP 下载能力
  http_download.py               有界流式 HTTP 调度、重试和文件级计时边界
  http_stream.py                 200/206/416 字节响应检查与 .part 写盘；不负责登录/重试
  run_reporting.py               终端、单 TIF log、原子 JSON 和轻量磁盘快照
geoacquire/sources/usgs/          WESM、项目规则、LiDAR/DEM
geoacquire/sources/linz/          LINZ 静态 STAC 索引、新西兰 1 m DEM
geoacquire/sources/cnig/          CNIG Polygon 查询、footprint 缓存与西班牙 0.5 m DTM
geoacquire/sources/rs/            XYZ、Google、Wayback、地理参考
geoacquire/sources/copdem/        CDSE stateful client
geoacquire/sources/pe3d/          PE3D 分幅、CAPTCHA 会话、链接过滤与 ZIP 解包
geoacquire/regions/vector.py      Polygon/MultiPolygon 矢量文件 Region Provider
geoacquire/regions/point.py       Point 矢量、米制查询范围与属性筛选 Region Provider
geoacquire/postprocess/           目标导向本地处理
  native_point_crop.py            原生网格固定米制 Point 窗口及可选的受限接缝补偿
configs/default.yaml              英文内置参数目录，默认主配置
configs/default_zh.yaml           与 default.yaml 等值的中文参数目录
configs/usgs_lidar_projects.yaml  LiDAR 已审核规则表
configs/examples/                 可编辑的数据源下载示例和通用输入覆盖
configs/runs/                     本机生产覆盖文件；新YAML默认忽略，已有GeoDAR模板继续跟踪
tests/test_core.py                离线回归
tests/test_streaming.py           A/B 调度、共享文件、失败隔离、早期真实 TIF 验收
tests/test_boundaries.py          续传完整性、缓存隔离、栅格网格等边界回归
tests/test_lidar_rules.py         文件头样本、命名边界和规则回归
tests/test_linz.py                静态 STAC 解析/缓存、footprint 选片回归
tests/test_cnig.py                门户 HTML、真实几何、UTM 分区去重与 20 文件上限
tests/test_pe3d.py                分幅命名、登录边界、链接过滤和安全解包回归
                                  含真实几何过滤、全局图幅批处理与错误 CRS 回归
tests/test_point_regions.py       地理/投影 Point 查询、原生裁剪、拼图和网格吸附边界
tests/test_reporting.py           三级输出、错误落盘、十分位和磁盘续接回归
tests/fixtures/lidar_headers.json 已核对的官方样本：来源、bounds、CRS 编号
tests/fixtures/lidar_headers_mn.json M–N 批次，包含独立复核和针对性补查样本
tests/fixtures/lidar_headers_ov.json OH–VT 批次，含跨格网和象限观测
tests/fixtures/lidar_headers_w.json WA/WI/WV/WY 批次，含首部与尾部 EVLR 的 CRS 证据
scripts/replay_lidar_audit.py     离线回放完整清单/旧目标 bounds，不读 TIF、不联网
scripts/sample_lidar_headers.py  维护时显式抽样文件头，正式下载不调用
```

## 3. 对象与调用关系

```text
BaseRegionProvider.get_regions()
    → dict[str, Region]

PipelineRunner.run()
    → source.acquire(regions, context, AcquireOptions)
    → context.on_region_ready(region_id, RegionResult) 仅入队
    → B worker: processor.process({region_id: region}, 单 region report, context)
    → acquire 与全部 B 任务结束后汇总 AcquisitionReport
```

`Region.metadata` 只保存跨层实际使用的信息：目标 TIF 场景使用 target_path、shape 和 transform；
Point 场景使用原始 point、point_crs 和 query_size_m。跨层键统一使用 `RegionMetadataKey`。常用读取
通过 `Region.target_path/target_shape/target_transform/point/point_crs` 属性完成，不在各模块重复
拼写字典键。

`DownloadRequest` 只存在于 HTTPSource 和下载服务之间。`LocalAsset` 才是后处理可消费的结果。不要让 Postprocessor 接收 URL，也不要让 Source 最终只返回 URL 列表。

### 3.1 资产契约

`core/models.py` 集中定义跨 Source 稳定词汇：

- `AssetKind`：封闭的处理/存储形态 `file/image/raster/point_cloud`；
- `ProductType`：内置 acquisition 产品类型 `generic/imagery/dem/laz/dsm/dtm`；
- `AssetStatus`：`success/skipped/reused/failed`；`skipped` 是磁盘已有，`reused` 是本次运行跨 Region 共用；
- `AssetSpec`：一个 Source/Postprocessor 声明的 `kind + product` 产出组合；
- `RegionMetadataKey`：确实跨 core、RegionProvider 和后处理使用的目标栅格、几何与 Point metadata 键。

`kind` 决定处理器是否能打开该存储形态，`product` 表示文件内容；二者不能合并。例如原始
XYZ 与地理配准 TIF 都是 `product=imagery`，但 kind 分别是 image/raster；DSM、DTM、DEM 和
imagery 都能是 raster，却不能混入同一 mosaic。`asset_id` 是单个逻辑实例身份，pipeline.name
是配置/日志身份，output_suffix 是文件命名，均不得借用 product 承担。

内置 Source 必须用上述常量并通过 `OUTPUT_SPECS` 或动态 `output_specs` property 声明
当前配置最终交给 Pipeline 的资产。PipelineConfig 沿后处理链调用 `validate_asset_flow()`；
已知契约无法满足 RasterAlign 的 kind/input_products 时，`--check` 必须直接失败。第三方 Source
仍可用非空自定义 product；确实无法静态声明时保留 `output_specs=None`，跳过契约预检。

Source 的 `output_products` 决定要生成或保留哪些产品；实际文件以同一值记录在
`LocalAsset.product` 中，RasterAlign 通过 `input_products` 选择。对齐只改变网格与文件名，
输出必须继承唯一输入产品，不提供 `output_role`/`output_product`。Source 专属的 mode、XYZ output_format、RasterAlign merge/vertical policy 等
由所属模块校验，不得因为它们也是稳定字符串就堆入 core models。

## 4. 配置规则

`deep_merge()` 的规则：

- pipeline 按 `name` 合并；
- postprocess 按位置合并；
- `postprocess: []` 清空默认步骤；
- 其他 list 替换；
- YAML 中同一 class_path 继承未覆盖的 init_args，换 class_path 则替换整个组件，不带入旧类参数；
- `--set` 只能修改已存在路径。

例如把默认的 RasterAlignToTargetPostprocessor 换为 PrintPostprocessor，覆盖该列表项的
`class_path` 和 `init_args: {message: check}` 即可；不需要把旧类的 output_suffix 等参数清空。
这个替换规则用于 YAML 合并；`--set ...class_path=...` 是单个叶值赋值，不会自动清理旧 init_args，
切换类型优先写一个小覆盖 YAML。

生产 YAML 应当是 default 的小覆盖，不复制整份 pipeline。英文 `default.yaml` 和中文
`default_zh.yaml` 只能选择一个作为基础；测试会检查两份实际参数完全一致，防止注释版本漂移。

所有 Source 构造函数必须显式声明参数，不使用宽泛 `**kwargs`；构造阶段必须无网络副作用，以保证 `--check` 安全。

## 5. HTTP 并发和内存

`HTTPDownloadService`：

- `max_workers` 是 A 的最大文件任务线程数，每个任务下载完成后在同线程调用 Source.materialize；
- 信号量限制最多 `max_workers × 4` 个运行/排队任务，取 generator 下一项前先申请空位；
- Request generator 在空位出现时才继续产生对象；
- 重试在同一 worker task 内完成；
- `.part` 支持续传；
- destination index 以共享目录内的 filename 为键；第一个请求执行下载和转换，后续请求复用结果；双方 URL 保存在 JSON/详情 log，不再抛异常；
- 同一 Region 不重复登记同一路径，避免 Postprocessor 重复消费；跨 Region 复用时登记 reused LocalAsset 别名。

不要把 Source 的全部 Request 转成 list。即使 generator 有界，最终报告仍保存每个 LocalAsset，这是后处理发现资产所需；百万文件场景应分区或改为磁盘 manifest，而不是删掉报告语义。

### 5.1 A/B 调度边界（2026-08-31 重构）

每条 pipeline 仅有 A（下载＋转换）和 B（region 后处理）两种执行职责，不另加转换池。
`PipelineConfig.postprocess_workers` 默认 1；设为 2 表示两个就绪 region 可同时后处理。
一条 region 的处理链始终按配置顺序执行，某一步失败便停止该 region 的后续步骤，其他 region 不受影响。
多个 Source pipeline 仍按顺序运行。CopDEM 的 A 保持客户端串行，下载和 ZIP 提取不拆开，但 B 独立。

HTTPSource 的 `materialize(report, context, options)` 保留原签名，执行时输入仅包含一个
物理文件。实现必须能并发处理不同文件，不把单次 raster 数组、reader 或路径存到 `self`。
LAZ→DSM 可能很耗内存，`max_workers=8` 也意味着最多同时有 8 个栅格化任务；不是固定 8 个网络传输。

共享文件只在 AcquisitionProgress 中分发。materialize 返回一个物理文件的产品，不能自己
再遍历 target_region_ids 克隆到多个 Region，否则公共账本会重复分发。旧 LiDAR 的
`_request_targets` / `_clone_asset` 已移除。RegionResult.asset_count 用于计数或就绪判断；
usable() 用于真正需要资产列表的调用，避免仅查看数量就复制列表。

`core/acquisition.py:AcquisitionProgress` 管理三个操作：

- `register(file_key, region_ids)`：建立依赖，重复登记同一依赖无副作用。
- `close_region(region_id)`：该 region 的清单已确定，不能再添加新文件。
- `complete(file_key, one_file_report)`：下载及转换已结束，成功或失败一次性通知所有使用者。

文件结果保留到本次 acquisition 结束，支持先完成文件、后发现共享 region。晚加入的 region
直接接收已生成产品的别名，不能重新读取已被 `keep_download=false` 删除的 LAZ/JPG。
完成通知和规划可来自不同线程；账本用一把短时锁保护依赖/计数，回调只提交任务，锁内不做后处理。
只有“清单关闭＋所有文件结束＋没有失败＋存在可用资产”才入队一次。最终统计与 B 任务用不同列表，
避免双方并发 append 同一 report；Pipeline 最后合并 B 的结果，不再重新执行全量后处理。

普通 HTTPSource 默认 `plan_requests()` 按 region 调用 `build_requests({id: region})`，
yield 完它的请求后自动关闭其清单。新 Source 通常仍只需写 build_requests 和可选 materialize。
需要批量 WESM 查询的 Source 覆盖 `plan_requests(regions, progress)`；HTTP 服务会登记每个
yield 的请求。**如果提前关闭清单，或在 Source 内去重而不再 yield 某个请求，就必须先显式 register**。
LiDAR 的 `_requests_for_selected(..., progress)` 正是这个入口：选片、登记整个 workunit 的
依赖后，才关闭已完全覆盖/到达最后候选 workunit 的 region，再把唯一请求逐个交给 A。

自定义客户端参考 CopDEMSource，核心写法如下（文件键必须在该 acquire 输出池内唯一）：

```python
progress = AcquisitionProgress(regions, context.on_region_ready)
seen = set()
for region_id, region in regions.items():
    files = plan_region_files(region)
    for item in files:
        progress.register(item.key, [region_id])
    progress.close_region(region_id)
    for item in files:
        if item.key in seen:
            continue
        seen.add(item.key)
        # 自己的客户端在这里下载、转换；成功产物或最终失败都放入单文件 report。
        one_file_report = acquire_file(item, region_id)
        progress.complete(item.key, one_file_report)
return progress.report
```

Source 不 import Postprocessor；`on_region_ready` 由 Pipeline 注入。旧自定义 BaseSource 若只
返回全量 report 仍可运行，但无法提前后处理；不要为了兼容旧 Source 给新内置 Source 加全量等待。

### 5.2 失败、复用和线程安全

- 下载重试仍在 A 的同一个任务内；转换失败记录 Failure，不把原始下载误报成可用的转换产物。
- 部分依赖失败不进入后处理，防止把已知缺文件的 region 当成完成；未知规则/清单缺失仍走现有 audit，
  不是像素覆盖质量检查。不要把“所有已选文件已完成”解释为“数据无空洞”。
- DSM、XYZ GeoTIFF 和 CopDEM 提取文件先写 `.part`，关闭成功后替换最终路径；异常清理该临时文件，
  不破坏已有完整产物。并发范围是一次 pipeline 运行，不新增跨进程文件锁；多进程仍须用户分配互不冲突的输出目录。
- `skip_existing` 按最终产物向前检查：Pipeline 批量扫描可推断的终端处理结果；Source 再检查
  LAZ→DSM/DTM、JPG→GeoTIFF 等派生产物；HTTP 最后检查原始目标。`keep_download=true` 时原始文件
  也属于必须存在的保留结果。自定义处理器无法声明 `expected_output()` 时可用 `skip_existing_suffixes`。
- `postprocess_workers>1` 会并发调用同一个 Postprocessor 实例。工作变量必须局部化，少量共享状态需锁。
  当前 RasterAlign 的高程 warn-once 标记已有锁；每次调用独立打开/关闭 GDAL 数据集。
- 后处理应只读共享输入栅格，写自己的输出；不要在一个 region 完成后删除共享 DSM，后续 region 仍可能使用它。
- A 不等待 B future；B 不等待下载或同池其他 future，因此 B=1 也不会形成相互等待。

验证优先跑 `python -B -m unittest tests.test_streaming`，再跑完整 tests。新增 Source 应补：
下载/转换同线程、全部依赖成功后才回调、同名文件晚加入复用、失败不触发 B。不要用真实大 LAZ 下载作为调度测试的唯一依据。

### 5.3 三级 Reporting 与计时边界

`RuntimeContext.reporting` 是可选服务；`run.py` 正常运行时总会注入，直接单测或第三方调用不
注入时保留旧接口。职责固定如下：

- `core/run_state.py`：只维护内存状态和短锁，不做终端、log、JSON 或目录 I/O；
- `services/run_reporting.py`：统一格式化终端/log/JSON，并缓存每个实际目录的一次 `scandir`；
- `HTTPDownloadService`：记录请求发现、worker 开始/结束、传输/重试/转换时间；
- `PipelineRunner`：记录目标 TIF 等待依赖和完整后处理链耗时；
- Source/Postprocessor：只报告自己的结果，不各自累计总体时间。

终端只使用用户可理解的 TIF/LAZ 数量，不出现 Region/workunit/queue 等内部调度词；每条终端
行必须原样进入 log。log 额外保留 `[tif/success|existing|failed|unavailable]`、`[laz/failed]`
和规划问题；正常 LAZ 成功只在 JSON。JSON 保存完整 TIF↔LAZ 映射、URL/workunit、重试、
时间和错误，在 TIF 跨十分位、规划流结束、错误、中断和最终状态时用临时文件原子替换。

进度与 ETA 按目标 TIF 计算，不得为了得到准确 LAZ 总数提前 `list(request_generator)`。
LAZ 继续按有界流式调度，运行中只能叫 `discovered`；规划流消费完才标记精确总数。
线程利用率在 worker 状态变化时积分，不加轮询线程、barrier 或全体同步。续跑时磁盘是真相：
最终 TIF、派生 DSM 和 `.part` 都从同一个目录文件名快照判断；不逐个打开栅格或计算哈希。
最终结果合并必须显式区分 preflight 已完成 Region 与本次 Source 返回的 Region；不要把
`report.regions[id]` 写成 `dict.get()` 的默认参数，因为 Python 会提前求值并对已完成项触发 KeyError。

## 6. USGS LiDAR 当前流程

共享目录模式：

```text
遍历一个目标目录，先建立全部 Region
  → 用全部 Region 的总包围范围执行 WESMClient.query_many() 一次 GeoPackage bbox read
  → 将候选 workunit 分配回真正相交的 target Regions
  → workunit 去重并按 collect_end 新到旧排序
  → 项目规则解析 link list
  → link list 内按 filename 去重
  → 在当前 workunit 的所有目标剩余未覆盖范围上选择 LAZ
  → 每个请求显式携带 target_region_ids
  → HTTP 按最终目标路径统一去重并登记依赖
  → HTTP 下载
  → 每个 LAZ 只栅格化一次
  → 同一个 LocalAsset 映射回所有覆盖目标
  → postprocess.output_dir 指定目录内对齐输出
```

这是目录级统一规划，不是依次完整执行 `0.tif → 1.tif → ...`。例如一个州有 121 个
Region，某 workunit 日志为 `targets=14 selected=28`，表示它只涉及其中 14 个目标，
并在这 14 个范围的合集上新增 28 个唯一物理请求。一个 LAZ 覆盖多个相邻目标时只下载
一次；其 `target_region_ids` 在规划时登记到 AcquisitionProgress，转换完成后映射成各目标可消费的 LocalAsset。

目录规则保持简单：`acquire.output_dir=null` 时，目标 TIF 使用共同父目录，bbox 使用
`workspace/output`；跨多个目标父目录必须显式指定。所有 Source 直接写入这个共享目录，
不得重新创建 Region 子目录或 output_layout。postprocessor 的 `output_dir=null` 跟随
acquire 产生的首个栅格目录，非空则直接写入该目录。文件名只使用
`<region_id>_<output_suffix>.tif`，不得重新引入 output_location、目标路径推断或隐藏目录。

当前 workunit 的请求被下载器消费时，一个规划线程会预取下一 workunit link list。它不是多线程遍历所有 LAZ 头；正式代码完全不读取远程 LAZ header。

每个 scheme/CRS 组合只解析一次 CRS，并缓存最终字符串；不要把 `CRS.to_string()`
放回逐文件循环，它会重复查询 PROJ authority 数据库。`core/geo.py` 对水平 CRS 和
Transformer 使用有界缓存，复用大量瓦片比较中的相同转换关系。变换结果仍检查有限值。

规划和下载是惰性交错的：下载器先从 Source generator 取得最多 `workers × 4` 个唯一
请求，下载＋转换完成产生空位时再继续消费 generator。线程上限是 `workers`，HTTP worker
自己上报完成，慢 generator 不会挡住已完成文件的就绪通知。workunit/请求/转换细节进入
log/JSON；终端只在目标 TIF 跨十分位时输出总体数量、活动线程积分和保守 ETA。

`WESMClient._region_shape()` 对 WGS84（EPSG:4326，包括 EPSG:9518 提取出的水平部分）
与 WESM NAD83（EPSG:4269）只在候选 workunit envelope 查询时按等价经纬度处理。原因是
离线 PROJ 可能选择不可用的 NOAA datum grid 并返回 `inf`。这种米级 datum 差异不会
影响大型 workunit 候选筛选；不要把该特例推广到真实瓦片投影、栅格化或后处理。

重复职责分开但不重复维护账本：`duplicate_names` 是单个 link list 内同 filename 的输入清洗；
`HTTPDownloadService` 是所有 HTTP Source 唯一的物理目标去重和依赖登记点。
`[plan/duplicate]` 只证明本地路径冲突；首个请求和全部重复请求的 URL/metadata 必须保存在
JSON，详情 log 只保留可搜索的冲突摘要。

link list 的 404、连接失败等读取错误写入 audit 的 `error`，该 workunit 返回空计划并继续后续项目，不能让一个旧项目终止整个州。

`USGSHTTPSource` 只保存 WESM、link cache 和目录分批能力；`USGSWorkunitHTTPSource` 是 DEM 使用的逐 Region/workunit 模板。LiDAR 直接继承前者并实现州级共享规划，不要让它实现一个不会调用的 DEM hook。

Audit 走独立短路径：WESM → link list → rule parse → JSON。它不做 tile selection、coverage union、destination 检查或下载。

## 7. LiDAR 规则表结构

文件：`configs/usgs_lidar_projects.yaml`

```yaml
- name: readable_unique_name
  match:
    workunit: ^EXACT_WORKUNIT$
  schemes:
    - parser: mgrs
      crs: mgrs
      grid:
        x_scale: 1
        y_scale: 1
        x_offset: 0
        y_offset: 0
        x_anchor: min
        y_anchor: min
        width: 1000
        height: 1000
```

可匹配字段：`workunit`、`project`、`lpc_link`。全部是忽略大小写的正则；同一 workunit 同时匹配多个规则会报错。

Parser：

| parser | 文件名坐标形式 |
| --- | --- |
| `mgrs` | 标准 MGRS，使用解码后的完整 UTM 坐标 |
| `mgrs_tokens` | 使用 MGRS 尾部原始 X/Y 数字，再由 grid 缩放 |
| `mgrs_absolute_tokens` | MGRS-like 前缀配绝对坐标数字，按格网字母恢复百万米北坐标；不能用固定 400/500 万偏移代替 |
| `mgrs_zero_x_next_100km` | LA Sabine 已确认的零 X token 跨 100 km 方格变体 |
| `mgrs_fixed_15s` | 指定固定 zone/band，解析省略前缀的 `TD8520` 等格网编号 |
| `mgrs_zone_11` | 仅在已审核规则内修正 Fresno 错写的 zone 前缀 |
| `full_xy` | 文件尾连接的完整 easting+northing |
| `axis_pair` | 文件尾轴字母加两个数字，例如 `E1234N5678` |
| `axis_named_pair` | E/W 数字作为 X、N/S 数字作为 Y；不由字母自行推断正负号 |
| `number_pair` | 文件尾两个下划线分隔数字 |
| `odd_km_pair` | IL 2 kft 奇数千英尺格网：`8970` 表示 897000，`1001` 表示 1001000 |
| `ct_quadrant_2500ft` | CT 三位 X/Y 母格编号加 `nw/ne/sw/se` 四象限 |
| `zone_tokens` / `zone_tokens_grid_od` | 只读 zone-like 前缀后的数字；后者限制 grid 字母，处理 Maryland 的非标准 OD |
| `single_number_split_N` | 将末尾数字在第 N 位切成 X/Y，可忽略 `_LAS_2019`、`_2022`、`_12` 尾缀 |
| `fl_fdem_5000ft` | Florida 5 kft 行列编号，支持 E/W/N 和已确认的现代/旧编号 |
| `il_mchenry_2500ft` | McHenry 省略百万位的 X/Y，注意 `00000000` 是合法瓦片，不是占位项 |
| `az_eastern_pima_5k` / `ca_san_diego_5k` | 两种已确认的 state-plane 坐标省略百万位约定 |
| `ca_eldorado_2500ft` | zone-like 前缀后实际存储 state-plane 千英尺坐标，再由 grid 向下取整 |
| `ca_solano_2640ft` | 还原省略末两位的偏移半英里格网坐标 |
| `ca_upper_pit_5k` | MGRS 100 m 单元定位 state-plane 5 kft 网格角点；使用本地同 datum 投影，不读网络 header |
| `id_southern_15` | 只为 ID Southern workunit 15 修复两个 TTM/QG 拼写及 NH 零 X token，不能推广到其他项目 |
| `mn_utm_pair` | MN 的 `327_5191`（km）或 `5315_48935`（100 m）；两段长度共同决定精度 |
| `mi_2500ft` / `mi_2500ft_wrapped` | MI 两个三位千英尺 token，还原 2500 ft 网格；跨百万位仅在指定 workunit 开启 wrapped |
| `nj_5000ft` | NJ 官方母格列/行、象限、子格编号，例如 `H7B14`；不是普通流水号 |
| `nc_panel_5000ft` / `nc_panel_2500ft` / `nc_panel_1250ft` | NC 官方八位 panel 编号；三种子格尺寸由末两位和项目规则限定 |
| `nh_2500ft` | NH 两个四位百英尺 token，还原省略的 X 百万位，Y 不额外偏移 |
| `mt_1000m_wrapped` | 指定 MT workunit 的省略百万位坐标，其他 MT 批次不能自动套用 |
| `mgrs_zone14_legacy_columns` | ND 3DEP/SD Eastern 部分交付把列字母 O 计入编号；仅对已验证的 workunit 修正 O/P/Q |
| `mgrs_separated_xy` | NM SouthEast 的 `13S_DR_78499_58000`，两个五位数是 MGRS 方格内坐标；其他分段数字不自动套用 |
| `axis_suffix_pair` | WI Oneida 的 `240E_200N`，轴字母在数字后；原始 token 的倍率仍在 YAML |
| `wi_sewrpc_10kft` | `LD15_2420_340` 是 (2420000,340000)，保留 LD15 限定，不能当任意两个数字 |
| `wi_2500ft_quadrants` | WI Iron/Florence：普通八位数恢复 2500 ft 格线，带 NE/NW/SE/SW 时拆 5000 ft 父格 |
| `wa_thurston_4500ft` | `w10035n53100` 与 `w99900n62550` 的 X 精度不同；仅用于已核验 Thurston 交付 |

CRS：

- `mgrs`：从 MGRS zone/band 解码；
- `workunit`：使用 WESM `horiz_crs`，纯数字先试 EPSG 再试 ESRI；
- `EPSG:xxxxx` / `ESRI:xxxxx`：明确固定 CRS。

Grid：

```text
coordinate = token × scale + offset
relative = coordinate - grid_origin                    # 默认 origin=0
x/y_ceil_step > 0 → coordinate = origin + ceil(relative / step) × step
x/y_floor_step > 0 → coordinate = origin + floor(relative / step) × step
x/y_round_step > 0 → coordinate = origin + round(relative / step) × step
anchor=min     → [coordinate, coordinate + size]
anchor=max     → [coordinate - size, coordinate]
anchor=center  → [coordinate - size/2, coordinate + size/2]
padding        → 对四边保守扩展，只用于避免漏选
```

同一轴只选择一种取整方式；ceil/floor/round 不是重采样，而是还原文件名中省略的格网
坐标。NY FEMA R2 的 `122478` 对应 `(121500, 478500)`，必须向最近的 1500 网格还原，
不能同时 ceil，也不能靠固定偏移或加大 padding 掩盖。round 使用 Python 最近值取整；
本次确认的整千坐标与 1500 网格之间不存在恰好半格的情况。
尺寸、偏移、padding 全部使用该 scheme 的水平 CRS 单位，不一定是米。IL/KY/FL 的
5000 常常是英尺。FL 编码参考 [SWFWMD 官方格网说明](https://www45.swfwmd.state.fl.us/arcgis12/rest/services/OpenData/Boundaries/MapServer/layers)。
本实现将样本与其格网几何范围交叉核对，采用 E/W 每行 300 格、N 每行 540 格，现代编号
分别增加 200000/400000/600000；具体原点和样本记录于 parser 与测试 fixture。
不要把该 parser 用于其他州的流水号。

## 8. 遇到新项目规则怎么办

### 第一步：审计

先运行 `configs/runs/geodar_state_lidar_audit.yaml`。不要直接添加一个“看起来像”的通用正则。

每个州必须同时覆盖输入目录和报告路径，防止 AR 的结果写进 AL 报告名：

```bash
python run.py \
  -c configs/default.yaml configs/runs/geodar_state_lidar_audit.yaml \
  --set region.init_args.source=/mnt/disk/USA/SurfRef_Data/USA_GeoDAR/GT/AR \
  --set pipelines.usgs_lidar.source.init_args.audit_report_path=./logs/usgs_lidar_AR_audit.json
```

重点查看 JSON：

- `workunit/project/lpc_link`；
- `horiz_crs`；
- `workunit_bounds` 与 CRS；
- `filename_examples`；
- `filename_count/parsed_count/unmatched_count`；
- 相关 target bounds/CRS。

### 第二步：判断文件名是否真的包含空间坐标

可以直接建规则的情况：

- 标准 MGRS；
- 明确的 easting/northing；
- 已由可信日志或本地 LAS/LAZ 头样本确定 scale、offset、anchor、size。

不能猜的情况：

- `FF232`、`HE278` 等项目内部格网编码，但没有格网原点/步长；
- `Goshen_0995` 等纯流水号；
- StratMap 字母数字编号无法证明为坐标；
- 只有 WESM 项目总范围，没有单瓦片位置关系。

这些项目保持 unmatched 是正确行为。否则会错误地把几千个 LAZ 当成相关文件下载。

WESM bounds 只能证明项目与目标相交，不能证明单个 filename 的 footprint。没有已有校准
日志或本地样本时，从 `lpc_link/0_file_download_links.txt` 找到 2～3 个分布在不同位置
的完整 URL，只读取 LAZ 开头的小范围并检查 header：

```bash
curl -fsSL '<lpc_link>/0_file_download_links.txt' | grep -F '<filename>'
curl -fL --range 0-262143 --max-filesize 262144 \
  '<完整 LAZ URL>' -o /tmp/usgs_laz_header.bin

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

必要时增大 Range 以覆盖 VLR。只抽样未知格式，绝不能让正式下载重新逐文件读取远程
header。一个 project 含多个 workunit 时至少跨 workunit 抽样；名称坐标若稳定落在下一条
固定网格线上，用 `x_ceil_step/y_ceil_step` 表达，不要为每种余数堆 offset 或 padding。
只有边界瓦片物理范围不完整时才使用能覆盖样本的保守 padding，宁可少量多选，不能漏选。

### 第三步：建立最窄规则

优先精确匹配 workunit：

```yaml
match:
  workunit: ^TX_Example_B1_2020$
```

只有多个 workunit 已确认完全同构时才合并正则。不要用州名或项目前缀覆盖未经验证的后续批次。

唯一规则入口是 `configs/usgs_lidar_projects.yaml`；parser 实现在
`geoacquire/sources/usgs/lidar_tiles.py`，只有现有 parser 无法表达一种已确认的通用
坐标格式时才修改该文件。不要在 `lidar.py` 中添加州名分支，也不要改 core、Pipeline
或注册器。

### 第四步：补离线测试

至少验证：

1. `LidarProjectCatalog.find()` 命中唯一规则；
2. 一个代表 filename 能被指定 parser 解析；
3. `scheme.grid.bounds()` 得到已知瓦片范围；
4. 规则 footprint 完整包含抽样 header 的 X/Y bounds；
5. 一个目标范围只选择预期文件；
6. 未知/纯序号项目仍然 unmatched。

规则的真实 header 样本加入 `tests/fixtures/lidar_headers.json` 或对应批次的
`lidar_headers_mn.json` / `lidar_headers_ov.json` / `lidar_headers_w.json`：按 workunit 分组，保留
project、WESM horiz_crs、来源 URL 和样本的 filename/bounds/声明的水平 CRS 编号。
`tests/test_lidar_rules.py` 自动验证所有样本，特殊 token 边界也在该文件加单测；
编排/HTTP/配置回归仍放 `tests/test_core.py`。测试不依赖网络、缓存或 3.4 GB WESM。
当前测试显式读取这四份 fixture；另建新文件时也要在 `setUpClass` 中加入，不能
只放一个 JSON 就以为测试会自动发现它。规则初定后另取未参与定规则的文件头复核，
尤其是 1.5 km 格网的 ceil/round 和百万位边界。本批正是在独立样本中发现 NY 的半公里偏移。

### 第五步：重新审计

期望结果：

- `matched`：全部文件解析；
- `partial`：规则只能解释部分文件，正式下载会使用已解释部分，但必须人工确认缺失文件是否只是异常占位；
- `unmatched`：不进入下载选择。

完整解析的硬条件是 `parsed_count == filename_count` 且 `unmatched_count == 0`，但这不是
坐标正确的证明：还要核对 header bounds、CRS，并回放相关目标的瓦片选择。报告默认
只保留十个 filename 样例，它们全部通过不代表几千条完整 link list 全部通过，因此改完
规则必须重跑 audit。partial 不得直接当成成功；有意保留的 unmatched 要把原因记录在
本文件“当前有意未解决的命名”中。

Link list 入口会先忽略 stem 以 `_` 结尾、没有任何瓦片编号的项目占位名，并在
`ignored_placeholder_count` 中统计；随后按 filename 去重。因而 `filename_count` 是
去重后的可用名称数，必须满足 `parsed_count + unmatched_count == filename_count`。
不要为 `USGS_LPC_Project_.laz` 之类占位项添加假 scheme。
也不能仅凭末尾 `_` 就判断占位：NC 的 `..._LA_37_10380906_.laz` 是合法坐标编号。
入口会保留至少六位的纯数字尾 token，再由项目 parser 决定能否解释；回归同时检查入口
过滤和坐标解析，避免出现“单测正则正确、实际清单却提前丢掉”的情况。

已有全国审计的维护回放（在项目目录，单行命令兼容 CMD/PowerShell/Linux）：

```text
python scripts/replay_lidar_audit.py --audit logs/usgs_lidar_USA_audit.json --states ME IL --coverage --report logs/replay_ME_IL.json
python scripts/sample_lidar_headers.py --audit logs/usgs_lidar_USA_audit.json --workunit "^ME_WesternMtns_1_B24$" --report logs/ME_headers.json
python -B -m unittest discover -s tests
```

回放工具重用 `USGSLidarSource` 的真实解析/选片函数，只将清单读取换成本地缓存，缓存缺失
也不会联网。它按 `(lpc_link, workunit)` 合并 audit 中跨州重复条目，不把文件名样例当作
完整清单。`--coverage` 使用审计时的 bounds，**不会**打开 target_path，也不重新查询 WESM。
全量运行可去掉 `--states`。状态名前缀来自原报告的 Region ID；单州报告 ID 只有 `0` 等
名称时不要加 `--states`。

header 工具默认每个 workunit 首/中/尾 3 个；`--priority` 只取每个目标最新且有清单的
workunit，`--per-project` 每 project/CRS 只取一组，二者只适合初查，不能证明其他批次
同构。`--retry-errors` 复用 `--report` 中的成功样本，仅重试失败项。维护工具不会改 YAML。
`--filename "11TTM|11TNH000"` 可专门选择罕见名字，再从匹配结果抽样。
对跨 UTM 分区、400/500 万米或省略零的规则，另找边界样本加入 fixture；不要扩大正则
掩盖 partial。官方清单缺失、目标确无瓦片、CRS 信息异常、规则缺失要分别记录。

日志中的 `selected` 才是实际交给下载器的数量，`parsed` 只是规则可解释的全项目文件数量。
`targets` 是当前 workunit 真正相交的目标 Region 数；`selected` 是在这些目标范围合集上
去重后的新增物理请求数，而不是每个目标分别需要的数量。

## 9. 当前有意未解决的命名

根据现有日志，以下项目没有足够空间关系，未建猜测规则：

- TX Central/Coastal 的 StratMap quad/subtile 编码（不是普通 X/Y 数字对）；
- WA King/3 County 和 WI 8County/部分 Statewide 的纯编号；
- WA Olympic/Western 的 quad 分幅：有官方分幅说明，但交付边界反例仍未解释；
- WY Goshen 的纯编号；
- legacy AL/OH/PA 项目中未标定的格式；
- Arkansas/Missouri 边界的 `MO_AR_Game_Fish_WMA_2014` 不规则项目块；
- Arkansas legacy 的纯流水号项目（Lower St. Francis、Chicot/Desha、Dardanelle、
  PAGIS、Buffalo River）和相邻 Mississippi Yazoo serial。

2026-08-31 全国回放还留下这些需要继续收集证据的情况（完整条目在
`logs/lidar_audit_verified.json`，数量与覆盖表见 `PROJECT_REPORT.md`）：

- ID Payette NF 的 `44116F8402` 等 quad/subtile 编号：没有确认子格网划分；影响 6 个目标。
- GA 的 Savannah River 等内部编号、旧项目清单 404/读取失败：影响 11 个目标；
  先区分缺规则和缺清单。网络失败应先重跑 audit，不能写一条规则冒充读取成功。
- IN__34 已由新版/旧版已支持瓦片覆盖约 82.3%，余下范围没有已确认的瓦片；
  Brown County 旧版是流水号，需要官方 tile index。不能用加大 padding 伪造全覆盖。
- CO__297 的 `CO_SoPlatteRiver_Lot2b_2013` 在 WESM 中没有 `lpc_link`；
  应寻找官方可替代的清单入口，不是名称正则问题。
- CO Eastern 的一个 `_tst.laz` 未确认是否为正式瓦片，保持 partial；其他名字仍可正常选片。

未支持的历史 workunit 不一定造成目标缺口：新项目覆盖完整时可不依赖旧项目。
反过来 `matched` 也不保证目标覆盖完整，因此补规则后必须看 `incomplete_targets`。

其中一些旧 workunit 会因更新数据已完全覆盖而被跳过，但规则表仍不应伪造。

### M–N 批次的剩余问题（2026-08-31）

最新回放 `logs/lidar_MN_verified.json`：317 个 workunit 完整匹配、2 个 partial、
175 个 unmatched/缺入口；1353/1462 个目标名义范围完整覆盖。下列数量是目标缺口数，
不是未匹配项目数；一个目标可以同时关联多个新旧项目。

| 州 | 缺口目标 | 优先核查的项目 / 原因 |
| --- | ---: | --- |
| MI | 15 | Alpena/Montmorency 清单 404；Ottawa NF 混合编号，Saginaw/Calhoun serial，Allegan/Berrien/VanBurren 的特殊网格原点 |
| MN | 4 | Goodhue 和旧 Metro 的项目编号，需要官方 tile index |
| MO | 9 | StLouis 6 个、Boone 3 个；不能把其他 MO workunit 的规则直接套过来 |
| MS | 27 | NRCS East 的分段 MGRS-like 名称存在分区/坐标含义问题；Lauderdale 等旧 serial 需索引 |
| NC | 20 | Phase5 Cherokee/Jackson/Haywood/Graham/Polk 的 5 个入口 404；旧 Phase3 尚无已确认规则 |
| NH | 27 | CT River P2/P3 的 WESM `lpc_link` 为空，先找官方入口，不是正则问题 |
| NM | 2 | MRCOG 编号和 SouthernSanLuis 网格仍需证据 |
| NV | 5 | Reno Carson 的部分格网字母异常、Clark County 编号；保留 unmatched |

MT、ND、NE、NJ、NY 的本批目标均已覆盖。NJ 仍有两个真实异常名：Southern 的
`__16B16.laz` 缺母格列字母、NW6Co 的 `D4CC7.laz` 多字母，分别保留在 partial 的
`filename_examples` 中；其余可解释名字可正常选片，本批目标不依赖这两个异常名。
不要把它们改为占位项，也不要为了把报告变绿而猜测删字母。

继续处理时按以下顺序，避免重复慢查询：

1. 在 `incomplete_targets` 找目标，再按 `workunits` 找项目，先解决影响目标的规则。
2. 本批离线回放中的“缺缓存”与 `logs/lidar_MN_link_fetch.json` 联查：10 个入口实测
   404，另 2 个 NH workunit 没有入口。先在官方项目目录/元数据寻找替代清单或 tile index，
   不要用补 YAML 假装修好了网络入口。
3. 有清单、有坐标语义：显式抽 header，验证新规则，修改
   `configs/usgs_lidar_projects.yaml`。只有出现新语法才改 `lidar_tiles.py`，并在
   `lidar_catalog.py` 的 `PARSERS` 中声明。无坐标语义的 serial 要先找官方索引。
4. 加观测 fixture 和测试，最后回放目标覆盖。生产运行仍不调用抽样工具，也不会自动学习规则。

例如检查剩余 MI 特殊网格（单行，项目目录下运行）：

```text
python scripts/sample_lidar_headers.py --audit logs/lidar_MN_combined_input.json --workunit "^MI_AlleganCo_2015$" --report logs/MI_Allegan_headers.json
python scripts/replay_lidar_audit.py --audit logs/lidar_MN_combined_input.json --states MI --coverage --report logs/replay_MI.json
```

NC panel 根据[官方技术规范第 27–29 页](https://it.nc.gov/documents/files/north-carolina-technical-specification-lidar-base-mapping/download?attachment=)
确定交错数字和子格顺序；NJ 根据[官方格网元数据](https://www.arcgis.com/sharing/rest/content/items/4ae00a8b5d9f40f690941a08d509a3bb/info/metadata/metadata.xml)
确定 40 kft 母格、象限和 4×4 子格。两者均另用 USGS 实际文件头核对，不能只按名称猜。

`logs/lidar_MN_combined_input.json` 是 13 份报告的副本，合并时仅给目标 ID 增加州前缀，
防止各州 `0` 互相覆盖；不改变生产目录。`lidar_MN_verified.json` 是最新结论，
header/holdout/extra JSON 保存原始观测。旧的 before/after/proposal/approved 快照和本批
review/fit/approve 临时工具已清理；常规维护只使用 `scripts/` 中的两个公开工具。

### OH–VT 批次的维护入口（2026-08-31）

本批是 OH/OK/OR/PA/PR/RI/SC/TN/TX/UT/VA/VT 共 12 份报告，不仅是 UT/VA/VT。
新增 89 条 catalogue 条目覆盖 241 个 workunit，最终 274 matched、1 partial、154
unmatched/缺入口；1844/2144 个目标名义范围完整覆盖，剩余 300 个：OH 18、OK 1、
OR 1、PA 8、TX 181、UT 2、VA 89。PR/RI/SC/TN/VT 的本批目标均完整覆盖。
使用 `logs/lidar_OV_verified.json` 查看最终回放；旧的改前基线和过程快照已清理。
`lidar_OV_combined_input.json` 只给副本的 target ID 增加州前缀，不改目录或原始报告。
官方清单读取结果另存 `lidar_OV_link_fetch.json`；404 和空 `lpc_link` 不能靠名称规则解决。

本轮仍只改两个层次：**已知语法加 YAML 条目，新语法才加 parser**。没有把远程
header 校验、自动拟合或逐文件检索放回正式下载。重要新增编码如下：

| 命名 | 处理位置与含义 |
| --- | --- |
| OH `BN13270395` | `single_number_split_4` + YAML 的 1000 倍和 1250 ft 向上取整；落在 (1327500,395000)，不是 (1327000,395000) |
| TN `2248661NE` | `tn_7000x4000ft`；父格省略固定尾数 (503,378)，NE 再加 (7000,4000)，子格是矩形 |
| TX Neches `15SUR3405520` | `single_number_split_3`；X 三位千米、Y 四位百米，加 3000000；X 恢复到 1500 m 格线，不按标准 MGRS 解析 |
| TX 部分项目 `15STS000390` / `15SUS000390` | `mgrs_zero_x_columns_t` 仅给 T 列的零坐标进位，U 列保持原值；两者都可能表示 X=300000，不能一见 `000` 就进位 |
| TX LowerRioGrande `14rop420700_b` | `tx_lower_rio_500m` 修正已核验的 O 列，再按 a=NW/b=NE/c=SE/d=SW 取 500 m 子格；无字母的交付用独立 1 km 规则 |
| OR `10TER000511500005026500` | `zone_full_xy`；两个各九位补零的完整 UTM 坐标 |
| OR `421088_942808_20180917` | `number_pair_date`；最后八位是日期，不是 northing |
| PA Luzerne / Dauphin | `pa_luzerne_2500ft` 恢复省略的百万位；`pa_dauphin_5000ft` 交换 Y/X；两者 Y 都代表上边，YAML 使用 `y_anchor: max` |
| VT 旧版 `N4557E527` | `vt_1400m_legacy_center`；标签顺序反了，E 省略十万米位，用唯一 1400 m 中心格线恢复 (455700,252700)；新版 Statewide 仍用正常 `axis_named_pair` |
| VA Southwest `AA100` | `letter_row_number_column`；数字为列，字母为行（A=0，Z=25，AA=26）；原点和步长写 YAML，不套到其他字母编号项目 |
| SC `3431-01` | `sc_5000ft_quadrants`；四位交错母格数字 + 01=NW/02=NE/03=SW/04=SE；YAML 恢复 X 的百万位 |
| SC Savannah/PeeDee `11508021` | X/Y 千英尺数字交错；`sc_savannah_5000ft` / `sc_savannah_2500ft` 区分交付格网，固定小数偏移写 YAML；规则由实际交付文件头核对，不推广成 SC 全州规范 |

每条规则只匹配明确核验的 workunit。**名称能解析不代表坐标正确**，尤其是零坐标、
跨 100 km 格网、UTM 分区错误、四舍五入/截断混用；这些要单独抽样。不允许用一个
大 padding 把错位的观测包起来。个别已核对交付的很小边缘越界会显式记在 YAML，
单位与源 CRS 一致，也计入名义范围；这不能证明该边缘有足够点。

离线样本保存在 `tests/fixtures/lidar_headers_ov.json`，已加入 `test_lidar_rules.py`
的加载列表；测试同时核对 bounds、声明水平 CRS、真实样本中心选片和非法名称。
`logs/lidar_OV_headers.json` 保存初查、独立 holdout、跨前缀/象限补样观测，
`lidar_OV_rejected.json` 保留验证失败的候选，不代表所有未支持项目的完整清单。
完整未匹配列表始终从 `lidar_OV_verified.json` 的 workunits 查。

本轮没有启用这四个候选：`TX_Pecos_Dallas_B2b_2018`（偏移后的 1500 m 格网）、
`TX_West_Central_B11_2018` 和 `VA_West_Chesapeake_B1_2017`（零 northing 的网格行
写错，普通名称通过但边界差 100 km）、`UT_StatewideSouth_3_2020`（部分 zone/grid
前缀与实际坐标不符）。优先按失败 JSON 中的原始 URL 和 bounds 补证据，不要将
这些候选当成已有支持。特别是 `mgrs_zero_x_columns_*` 只修正 X，不能顺手改所有 Y=0。

剩余缺口中：OH 的 Phase1_7、VA NShenandoah_1 入口 404；TX StratMap、部分错区
MGRS、VA SouthCentral/Sandy/Chesapeake 的混合编码、OR Wallowa 的 quad 编号及
旧 PA/OH serial 还需要项目索引或额外坐标证据。缺少瓦片覆盖的目标可能只有已支持
workunit，因此始终先看 `incomplete_targets`，不要只按 unmatched 数量排优先级。
本批唯一 partial 是原有 `TX_CentralEast_1_A23`：部分 O 列命名仍未确认，未伪装成占位项。

同步到 Linux 时，至少同步 `configs/usgs_lidar_projects.yaml`、`lidar_tiles.py`、
`lidar_catalog.py`；不能只复制 YAML，否则旧代码不认识新 parser。建议同时同步 tests 和文档。
运行 YAML、default、目录、并发参数不变；旧进程不会热加载这些修改。

常规维护命令使用公开脚本；本轮一次性 fit/approve/patch 工具已在结果固化后清理：

```text
python scripts/replay_lidar_audit.py --audit logs/lidar_OV_combined_input.json --states OH TN TX --coverage --report logs/replay_OV_check.json
python scripts/sample_lidar_headers.py --audit logs/lidar_OV_combined_input.json --workunit "^TX_Neches_B5_2016$" --filename "15SUR" --report logs/TX_Neches_check.json
python -B -m unittest discover -s tests
```

### 2026-08-31 最后四州维护补充（WA/WI/WV/WY）

最终结果以 `logs/lidar_W_verified.json` 为准：147 个 workunit 中 95 个 matched、52 个
unmatched/缺入口，无 partial；360 个目标中 309 个名义范围完整覆盖。WA 余 22 个、
WI 余 29 个；WV/WY 本批目标全部覆盖。新增 43 个表项精确匹配 54 个 workunit，
其中包括跨州相交的 MN CentralMissRiver 和 VA Northeast，不能只按报告州名过滤项目。

这次没有调整 Source、HTTP、A/B 并行、输出目录或 default。规则仍集中在
`configs/usgs_lidar_projects.yaml`；仅四种新语法进入 `lidar_tiles.py` 并在
`lidar_catalog.py` 的 parser 白名单列出。原有语法只补 YAML。

新增 `x_grid_origin/y_grid_origin` 默认为 0，仅给取整格网一个固定原点，不是新输出
参数。例：WI 12County 的 `738365` 先得到 (738000,365000)，再以 4500 ft 步长、
原点 (530,1490) 向上还原成 (738530,365990)。不同县的原点、CRS 不同，不能用
`x_offset` 或大 padding 替代，也不能把一县规则套全州。未设取整步长时 origin 不改变坐标。

WV `FF232` 已确认采用 `letter_row_number_column`，X=232，Y=FF 的零起始行号 161；
YAML 步长 1500、原点 (259090.17,4045879.91)，得左下角 (607090.17,4287379.91)。
VA Northeast 同语法，但原点 (259090,4045878)，与先前 Southwest 也不同；各自单独
匹配 workunit。WV 文件尾声明 NAD83/UTM17，而 WESM 写 NAD83(2011)/UTM17；本轮保存
实际交付的水平 WKT（含其 bound transformation），不强行替成 WESM 的 EPSG:6346。

WI Iron 的 52 个 `NE/NW/SE/SW` 名称是在完整清单回放中发现的，现已补四象限独立
样本并改用 `wi_2500ft_quadrants`；不是占位文件，不允许为了消掉 partial 而忽略。
644 个新增真实样本保存在 `tests/fixtures/lidar_headers_w.json`，均有范围与声明 CRS；
每个新增 workunit 至少包含初查、独立 holdout 和补样。完整测试 96 项通过。

**文件头没有 CRS 时怎么查：**公开 `sample_lidar_headers.py` 只读文件首部最多 512 KiB，
`header_crs=null` 不能证明完整 LAZ 没有 CRS。LAS 1.4 可能将 WKT 存在尾部 EVLR；
固定头偏移 235/243 记录 EVLR 起点/数量。维护时可明确请求该字节范围，检查 HTTP 206
和 Content-Range 起点，再找 `LASF_Projection` 的 2112 记录。保留单次字节上限，
服务器忽略 Range 就停止，不能为验证 CRS 隐式下载整文件。本轮曾用批次专用检查工具
完成 476 个明确的补查，并在 `lidar_W_headers.json` 记下 `crs_storage` /
`crs_evlr_offset`；正式规划不执行该操作。批次工具已清理，新州维护使用公开脚本。

继续处理缺口时：

1. 在 `lidar_W_verified.json.incomplete_targets` 选目标，再按 `workunits` 找项目入口。
2. WA King/3County、WI 多县及 WY Goshen 是纯编号，应查官方 tile index / metadata，
   不能按当前 link list 排序号拟合空间位置。WY Goshen 的未匹配不影响本批 91 个目标。
3. WA quad 可参考 [DNR 分幅规范第 52 页](https://www.dnr.wa.gov/publications/ger_wa_lidar_plan_2020.pdf)，
   但 `lidar_W_quad_review.json` 记录了 `q49122A7325` 的数千米级反例，以及其他样本的
   小偏差；本轮没有安装该猜测规则。还需核对交付的 tile index、经纬度基准和边界命名。
4. `WA_PSLC_2000` 清单实测 404，`WA_Colville_Part1_5A_2014` 没有 lpc_link；先处理入口，
   不是补 parser。结合 `lidar_W_link_fetch.json`，不要把缺缓存都称为名称不支持。
5. 新规则仍按第 8 节：最窄 YAML 匹配 → 独立 header/CRS → fixture → 完整清单回放与覆盖。

`lidar_W_verification.json` 记录原始四份审计 SHA256 未变、样本数和选中文件同名检查。
合并副本只添加州前缀 ID，不改变实际目录。本批选中集合未发现同州同名不同 URL，
这不是对全部未来项目的保证。同步 Linux 至少带上规则 YAML、`lidar_tiles.py`、
`lidar_catalog.py`，并建议同步 tests/文档；旧进程不会热更新。

```text
python scripts/replay_lidar_audit.py --audit logs/lidar_W_combined_input.json --coverage --report logs/replay_W_check.json
python scripts/sample_lidar_headers.py --audit logs/lidar_W_combined_input.json --workunit "^WI_Iron_5_2019$" --filename "NE|NW|SE|SW" --report logs/WI_Iron_quadrants.json
python -B -m unittest discover -s tests
```

## 10. 后处理维护注意事项

`RasterAlignToTargetPostprocessor`：

- 只使用当前报告中的 raster asset；
- 先建立输出文件，再按 source/窗口合并；
- `resample=true` 使用目标 transform/shape 派生网格；
- `target_scale>1` 要求目标宽高可整除；
- warp 只使用水平 CRS；
- 如果源有垂直 CRS，输出保留源垂直 CRS，不冒充目标垂直基准；
- `warn_once` 每个处理器实例至多一条垂直基准汇总。

不要把 LiDAR LR 下采样加回 GeoAcquire；这是下游超分训练项目的职责。

### LAZ 网格与重复读取的边界

LidarRasterizer 的两遍分块读取有不同目的：先计算分类过滤后的范围，再按该范围聚合点。
不能为了少一遍读取而直接用未经筛选的文件头范围，或一次把所有点放进内存。输出数组仍
在内存中，point_chunk_size 只控制点批次，不限制栅格数组大小。

写出的 transform 必须与点分桶使用同一个 raster_resolution。2.4 单位宽、resolution=1
时宽度为 3，实际覆盖宽度为 3，而非用 from_bounds 把每格改成 0.8。单点数据也保留真实
非零分辨率。DSM 默认排除分类 7/18，exclude_classes=[] 明确不过滤；DTM 始终取分类 2 的
每格最低点。该修正不自动覆盖已经存在的 DSM/最终 TIF，重算时应显式配置或使用新目录。

CRS 以完整 LAZ 头的声明为第一优先级；只有 `parse_crs()` 确认无声明时，materialize 才把
规则规划阶段已经审核并写入 asset metadata 的 `source_crs` 交给 Rasterizer。两者都没有时
仍然失败，不能按文件名猜 CRS。该回退用于 `TX_RioGrand_FTWhit_2014`：实际下载的 17 个
LAZ 均无头 CRS，而 WESM/规则计划明确为 EPSG:6342；已有 LAZ 重跑时无需再次下载。

### 下载字节与缓存的边界

- HTTPDownloadService、WESMClient 和 CopDEMClient 共用 services/http_stream.py，不共享认证或调度逻辑。
- 请求使用 identity 编码；206 检查 Content-Range 起点，200 表示服务器忽略 Range，应重写；
  416 仅在已存字节数与服务器总长度完全一致时认定完成。长度已知但不一致时保留 .part，不发布最终文件。
- WESM 新下载也先写 WESM.gpkg.part，成功后替换；旧版本留下的小型不完整 WESM.gpkg 可迁入 .part 续传。
  现有大于阈值的旧缓存仍沿用原兼容策略，本次不会对 3.4 GB 缓存做全库扫描。
- 链接缓存使用独立临时文件，避免不同州进程抢同一个 .tmp；Windows 并发替换被拒绝而目标缓存已存在时，
  保留另一个完整缓存，当前调用继续使用已取到的清单。这不是下载目录的跨进程文件锁。
- workunit 计划缓存键包含 URL、workunit 名、水平 CRS 和规则名；相同 URL 的不同 CRS 不复用解析结果。
- LINZ 静态 STAC Collection 只有 Item 链接，没有集中式 tile bbox。首次运行并发读取 Item，
  把 footprint 和 COG URL 原子写入单个索引；过期刷新失败时只回退到能完整解析的旧缓存。
  选片使用 Item 的真实 geometry，不要退化成仅比较会在倾斜图幅角落误选的 bbox。
- sample_lidar_headers.py 只读首部最多 512 KiB，不追读远端 EVLR。header_crs=null 可能表示 CRS 在尾部，
  不能因此猜 CRS 或直接认为项目无效；维护者需按前文的 EVLR/元数据检查办法补证据。

## 11. 代码风格

- Python 文件保留统一 header；
- 标识符、代码注释、docstring、异常信息使用英文；
- 文件/function/variable 使用 `snake_case`，class 使用 `CamelCase`；
- 显式类型标注重要边界；
- 网络 I/O 放在运行方法，不放构造函数；
- 使用 `os.path` 保持现有风格；
- 对用户输入在稳定边界校验一次，内部 helper 不重复校验同一事实；
- 公开可独立调用的组件可以保留自身防御式校验；
- 大集合使用 generator/分块，不建立无界 list；
- Reporting 标签保持 `[plan] / [progress] / [final] / [tif/*] / [laz/failed] / [plan/issue]`；
  不在 Source 中新增会污染生产终端的普通 `print()`。

本次没有按州拆分 parser，也没有合并 Source、调度和后处理：lidar_catalog 管配置校验，
lidar_tiles 管纯命名数学，lidar_rasterizer 管本地点云。三个职责分别变化，保持分开更容易维护。
长测试文件暂不机械拆分，新增边界回归单独放 test_boundaries.py，正式代码不依赖 tests 或 scripts。
保留规则 fixtures、合并审计输入、原始 header/失败观测、最终 verified 报告和规范 PDF；
重复州级 audit、过程快照和一次性研究脚本已在证据固化后清理。
根目录 test.py 是个人坐标修正脚本，不属于 unittest 测试集，也不应被批量执行。

## 12. 提交前检查

```powershell
python -B -m unittest discover -s tests -b
python run.py -c configs/default.yaml --check
python run.py -c configs/default_zh.yaml --check
```

再确认：

- `run.py` 和 Pipeline 没有具体 Source 分支；
- 新 Source 不要求修改 core；
- Source 返回的 LocalAsset path/kind/product 正确；
- 没有把私有账号写进 README、example 或测试；
- 生产 YAML 仍与 default 合并；
- LiDAR 新规则有离线 filename/footprint 测试；
- README、PROJECT_REPORT 和本交接文档同步更新。

测试数量会随回归用例增加而变化，以完整测试命令的实际输出为准；历史增量见 PROJECT_REPORT。
测试临时目录使用
`.test_core_*` / `.test_stream_*` / `.test_boundary_*`，每次仅清理自己新建的目录。
不要再固定写入并递归删除用户可能已有的 `.test_tmp`。
