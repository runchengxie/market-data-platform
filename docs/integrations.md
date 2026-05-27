# 系统集成 (Integrations)

## cross-sectional-trees

当前定位：
* 负责策略研究、特征工程（features）、模型构建、回测、持仓管理及报告生成。
* 仅作为已发布 HK / CN 数据资产的下游只读调用方。
* 其历史 HK 数据处理实现暂作为 transition backend 保留；新的操作入口为
  `marketdata rqdata hk-assets -- <原 rqdata 参数>`。

环境配置：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

这样配置可将策略的运行结果、缓存和报告输出保留在策略代码仓库本地，同时将市场数据输入路径指向共享的数据根目录。请注意：仅当您希望将策略项目的默认输出也写入该数据平台根目录时，才需要将 `CSTREE_ARTIFACTS_ROOT` 或 `paths.artifacts_root` 指向平台根目录。

覆盖默认输出路径：

```yaml
paths:
  artifacts_root: "/data/market-data-platform"
```

数据调用规范：
* 推荐通过 `metadata/current_assets/<market>_current.json` 结合各项资产的 manifest（清单文件）来读取数据。
* 严禁直接依赖（或硬编码）其他项目的工作目录。
* 运行低频策略时，请勿直接全量扫描原始的 Tick 级深度快照（tick-depth snapshots）数据。

---

## rqdata-hk-depth-snapshots

当前定位：
* 作为 HK tick-depth 兼容仓，保留历史命令名、包名、配置和执行记录。
* 原始数据下载、数据质量监控（health checks）、日频数据聚合、数据对账
  （reconciliation）以及打包发布实现由 `market_data_platform.hk_depth` 承载。
* 推荐统一入口为 `marketdata rqdata hk-depth -- <原 rqdata-hk-depth 参数>`；历史
  `rqdata-hk-depth ...` 命令继续可用。

推荐发布路径：

```text
<artifacts_root>/assets/rqdata/hk/tick_depth/<snapshot>/
<artifacts_root>/assets/rqdata/hk/tick_depth_daily/<snapshot>/
```

在发布正式的数据快照后，请务必更新或重新生成 `hk_current.json` 文件，以确保 `tick_depth_raw` 和 `tick_depth_daily` 字段正确指向最新采纳的数据资产。

不要将混合的 cache 目录、增量碎片目录或带 `_partial` 标记的聚合结果直接挂到
`hk_tick_depth_daily_latest`。该 latest alias 只能指向完整、去重并通过 health /
reconcile 验收的正式交付目录。候选或分片资产可以先发布到
`assets/rqdata/hk/tick_depth_daily/<snapshot>/` 下，但不要更新 current alias。

操作示例：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform

marketdata rqdata hk-depth -- emit-asset \
  --kind daily \
  --source artifacts/cache/rqdata/hk_tick_depth_daily/core_20250401_20260409/data.parquet \
  --output "$DATA_PLATFORM_ROOT/assets/rqdata/hk/tick_depth_daily/core_20250401_20260409"

ln -sfn core_20250401_20260409 \
  "$DATA_PLATFORM_ROOT/assets/rqdata/hk/tick_depth_daily/hk_tick_depth_daily_latest"

marketdata contract build \
  --market hk \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260409
```

---

## 规划中的交易成本模型 (Future execution_cost_model)

交易成本模型应当作为一种轻量级的衍生数据资产来提供，而不是让策略直接去读取底层原始的 Tick Parquet 文件。该模型资产需明确记录以下元数据信息：

* 模型校准窗口期 (calibration window)
* 数据源依赖（所依赖的 Tick 级深度数据和日内数据资产）
* 适用的股票池/投资域 (usable universe)
* 核心假设条件（包括买卖价差、盘口深度、成交参与率、市场冲击以及数据质量的预设假设）
* 数据截止日期 (as-of date) 与版本号
