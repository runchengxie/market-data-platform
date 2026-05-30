# 系统集成 (Integrations)

本页说明下游研究、回测、交易和报表系统如何接入本平台发布的数据资产。下游系统应把本平台视为只读数据源，不需要了解平台内部的采集、清洗和发布实现。

## 下游系统接入边界

推荐定位：
* 下游系统负责自己的策略研究、特征工程、模型构建、回测、持仓管理或报告生成。
* 下游系统仅作为已发布数据资产的只读调用方。
* 中国香港市场数据的生产、检查和发布入口位于本仓库：`marketdata rqdata hk-assets -- ...` 或更高层的 `marketdata rqdata refresh-hk-*`。
* 中国大陆市场数据的基础采集入口也位于本仓库；当前主要覆盖 RQData / TuShare 原始层和 current contract 发布。

环境配置：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

这样配置可将下游系统的运行结果、缓存和报告输出保留在自身项目目录，同时将市场数据输入路径指向共享的数据根目录。下游项目如有自己的输出根目录配置，应只在确实需要把运行产物也写入平台根目录时使用。

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

## 港股 tick-depth

当前定位：
* 原始数据下载、数据质量监控（health checks）、日频数据聚合、数据对账（reconciliation）以及打包发布实现由 `market_data_platform.hk_depth` 承载。
* 推荐统一入口为 `marketdata rqdata hk-depth -- ...`；安装本包后也会提供 `rqdata-hk-depth` 兼容命令。
* 旧的 `rqdata_tick_data.*` Python import 路径不再作为公开接口维护；新项目应使用 `marketdata` CLI 或 `market_data_platform.hk_depth`。

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

## 规划中的交易成本模型

`execution_cost_model` 当前是预留的衍生资产键，路径规范和 current contract 已支持登记；正式构建流程尚未在平台内落地。后续交易成本模型应作为轻量级衍生数据资产提供，策略层不应直接读取底层 Tick Parquet 文件。该模型资产需明确记录以下元数据信息：

* 模型校准窗口期 (calibration window)
* 数据源依赖（所依赖的 Tick 级深度数据和日内数据资产）
* 适用的股票池/投资域 (usable universe)
* 核心假设条件（包括买卖价差、盘口深度、成交参与率、市场冲击以及数据质量的预设假设）
* 数据截止日期 (as-of date) 与版本号
