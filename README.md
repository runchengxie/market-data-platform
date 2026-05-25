# market-data-platform (多市场数据平台)

HK / CN 研究数据资产的共享控制平面（Shared control plane）。

本仓库是一个过渡项目，旨在将可复用的市场数据管理逻辑从各个策略仓库中解耦并剥离出来。未来，本仓库将集中统管数据契约（Contracts）、注册表规范（Registry conventions）、数据模式（Schema）、数据健康度巡检策略（Health policy）以及数据的打包与发布工作流。请注意，大体量的数据文件不会被纳入 Git 版本控制。

## 最终架构形态 (Target Shape)

```text
market-data-platform/
  daily（日频数据） / PIT（Point-in-Time数据） / valuation（估值） / industry（行业分类） / universe（标的池）
  intraday 5m（5分钟级日内数据）
  tick_depth raw（原始逐笔深度） / tick_depth_daily（日频逐笔深度）
  execution_cost_model（执行成本模型）
  current contract（当前数据契约） / dataset registry（数据集注册表） / health（健康度巡检） / reconcile（数据对账） / release（发布）

cross-sectional-trees/
  策略、特征、模型、回测、持仓
  作为 market-data-platform 的纯只读下游消费方

rqdata-hk-depth-snapshots/
  短期方案：独立的 tick-depth（逐笔深度）实现模块
  中期方案：将其作为 tick_depth 模块整合进 market-data-platform 中
```

## 第一阶段拆分边界 (Stage-1 Boundary)

目前，本仓库仅负责定义共享的数据契约和路径规范。具体的业务实现逻辑依然保留在以下项目中：

- `cross-sectional-trees`：包含日频、PIT、估值、行业分类、标的池、当前数据契约、数据集注册表、数据健康度巡检及发布工具的实现。
- `rqdata-hk-depth-snapshots`：包含逐笔深度数据的下载、健康度巡检、日频聚合、数据对账及打包逻辑。

第一阶段的落地步骤是，将上述两个项目的数据输出指向同一个共享的产物根目录（Artifacts root）：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

`DATA_PLATFORM_ROOT` 是本仓库推荐使用的统一环境变量。`HK_DATA_PLATFORM_ROOT` 仍作为兼容旧 HK 调用方的 fallback 保留。只有当某个项目明确需要将其运行结果、缓存或报告等输出文件也集中放到该根目录下时，才需要使用 `CSTREE_ARTIFACTS_ROOT` 环境变量。通常情况下，各策略仓库应保持自己独立的输出目录，并统一通过 `DATA_PLATFORM_ROOT` 来读取已发布市场数据。

## 共享目录结构 (Shared Layout)

```text
<artifacts_root>/
  assets/
    rqdata/
      hk/
        daily/
        intraday/
        pit_financials/
        valuation/
        industry_changes/
        tick_depth/
        tick_depth_daily/
        execution_cost/
      cn/
        daily/
        pit_financials/
        valuation/
        industry/
        st_flags/
        suspend/
        limit_status/
    universe/
  metadata/
    current_assets/
      hk_current.json
      cn_current.json
    dataset_registry.csv
  reports/
  standardized/
```

## 当前数据契约 (Current Contract)

共享的当前数据契约文件路径为：

```text
<artifacts_root>/metadata/current_assets/<market>_current.json
```

该文件记录了数据资产标识（asset keys）、别名路径（alias paths）、底层解析的绝对路径（resolved paths）、数据清单摘要（manifest summaries）以及数据业务日期（as-of dates）。各个策略仓库应当通过该契约文件来获取确定的底层数据路径进行读取，而不是通过扫描那些随时可能变动的 `latest` 别名目录来拉取数据。

常用命令：

```bash
marketdata paths --market cn
marketdata contract build --market cn --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata registry build --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata rqdata export-cn-instruments \
  --out "$DATA_PLATFORM_ROOT/assets/rqdata/cn/instruments/cn_all_instruments_latest.parquet"
```

`hkdata` 命令和 `hk_data_platform` Python 包名仍作为兼容层保留，新代码应优先使用 `marketdata` 和 `market_data_platform`。

## 本地开发 (Development)

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run pyright
```

关于数据契约的详细说明及迁移指南，请参阅 `docs/README.md`。
