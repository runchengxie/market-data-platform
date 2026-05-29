# market-data-platform (多市场数据平台)

HK / CN 研究数据资产的共享控制面。

本仓库承接可复用的市场数据管理逻辑，统一维护数据契约、注册表规范、数据模式、健康巡检策略以及打包发布工作流。大体量数据文件不进入 Git。

## 目标架构

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
```

`execution_cost_model` 当前是预留的衍生资产键，路径规范和 current contract 已支持登记；正式构建流程尚未在平台内落地。

## 当前边界

目前，本仓库负责共享的数据契约和路径规范、CN 的 RQData / TuShare 基础采集
MVP、HK tick-depth 下载/健康检查/聚合/对账/打包实现，以及统一的数据维护 CLI
入口。HK RQData asset 生产维护实现位于 `market_data_platform.hk_assets`，由
`marketdata rqdata hk-assets -- ...` 原生执行。

- `market_data_platform.hk_assets`：包含日频、PIT、估值、行业分类、标的池、资产健康巡检、current refresh 及发布工具的实现。
- `market_data_platform.hk_depth`：包含逐笔深度数据的下载、健康度巡检、日频聚合、数据对账及打包逻辑。
- `cross-sectional-trees`：策略研究下游；只读消费平台发布的数据资产，HK 数据资产维护入口由本仓库提供。

当前落地方式是将平台内 HK depth / HK assets 工具的数据输出指向统一共享的产物根目录（Artifacts root）：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

`DATA_PLATFORM_ROOT` 是本仓库推荐使用的统一环境变量。`HK_DATA_PLATFORM_ROOT` 作为旧 HK 调用方的兼容变量保留。`CSTREE_ARTIFACTS_ROOT` 只用于明确需要把运行结果、缓存或报告也集中放到该根目录的场景。通常情况下，各策略仓库保持独立输出目录，并通过 `DATA_PLATFORM_ROOT` 读取已发布市场数据。

本地 provider credentials 以本仓库作为配置入口，但不得提交真实 secret。可复制
`.envrc.example` 为 `.envrc`，并将 token / 密码写入未跟踪的 `.env.local`，或写入
`~/.config/market-data-platform/secrets.env`：

```bash
cp .envrc.example .envrc
cp .env.example .env.local
direnv allow
```

`.env.example` 规定了 `TUSHARE_TOKEN`、`RQDATA_USERNAME`、`RQDATA_PASSWORD` 和
`RQDATA_URI` 等变量名；`.gitignore` 会阻止本地凭证文件进入 Git。

## 共享目录结构

```text
<artifacts_root>/
  assets/
    rqdata/
      hk/
        daily/
        instruments/
        intraday/
        pit_financials/
        valuation/
        ex_factors/
        dividends/
        shares/
        exchange_rate/
        southbound/
        financial_details/
        industry_changes/
        tick_depth/
        tick_depth_daily/
        execution_cost/
      cn/
        instruments/
        daily/
        valuation/
        industry_changes/
        industry/
        industry_citic/
        industry_sw/
        st_flags/
        suspend/
        limit_status/
        index_components/
        northbound/
    tushare/
      cn/
        instruments/
        trade_cal/
        daily/
        adj_factor/
        daily_basic/
        limit_status/
        daily_clean/
    universe/
  metadata/
    current_assets/
      hk_current.json
      cn_current.json
    dataset_registry.csv
  reports/
  standardized/
```

这是入口摘要；完整 asset key 与路径规范以 `docs/contracts.md` 和 `src/market_data_platform/paths.py` 为准。

## 当前数据契约

共享的当前数据契约文件路径为：

```text
<artifacts_root>/metadata/current_assets/<market>_current.json
```

该文件记录数据资产标识、别名路径、底层解析的绝对路径、数据清单摘要以及数据业务日期。各策略仓库应通过该契约读取确定的数据路径，避免直接扫描随时可能变化的 `latest` 别名目录。

常用命令：

```bash
marketdata paths --market cn
marketdata contract build --market cn --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata registry build --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata rqdata export-cn-instruments \
  --out "$DATA_PLATFORM_ROOT/assets/rqdata/cn/instruments/cn_all_instruments_latest.parquet"
```

## TuShare CN MVP

TuShare 是 CN 的并存 provider，不会替换现有 RQData 命令。安装可选依赖后，以环境变量提供 token：

```bash
uv sync --extra dev --extra tushare
export TUSHARE_TOKEN=...

marketdata tushare verify-token
marketdata tushare export-cn-instruments \
  --out "$DATA_PLATFORM_ROOT/assets/tushare/cn/instruments/cn_all_instruments_latest.parquet"
marketdata tushare mirror-cn-trade-cal \
  --start-date 20260101 --end-date 20260526 \
  --out "$DATA_PLATFORM_ROOT/assets/tushare/cn/trade_cal/cn_trade_cal_latest.parquet"
marketdata tushare mirror-cn-daily \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/cn/daily/cn_all_20260101_20260526_daily"
marketdata tushare mirror-cn-adj-factor \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/cn/adj_factor/cn_all_20260101_20260526_adj_factor"
marketdata tushare mirror-cn-daily-basic \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/cn/daily_basic/cn_all_20260101_20260526_daily_basic"
```

日频类 TuShare 镜像按开放交易日请求全市场并写入
`data/trade_date=YYYYMMDD/part.parquet`。完成数据校验并将 `*_latest`
alias 指向采用的 snapshot 后，使用以下命令发布当前 CN provider：

```bash
marketdata contract build --market cn --provider tushare \
  --artifacts-root "$DATA_PLATFORM_ROOT" --target-date 20260526
```

`marketdata tushare mirror-cn-stk-limit` 还可镜像 `stk_limit` 接口形成
`limit_status` raw 资产；`mirror-cn-limit-status` 是同一操作的兼容别名。当前 MVP
不包括 clean layer、修复、质量门禁或发布打包。

## HK 数据维护入口

HK tick-depth 代码位于 `market_data_platform.hk_depth`；HK RQData asset
生产、检查、current refresh 和发布工作流位于 `market_data_platform.hk_assets`
与 `market_data_platform.release_tools`。统一入口如下：

```bash
marketdata migration status

marketdata rqdata hk-depth -- health --input <raw-depth-dir>
marketdata rqdata hk-depth -- aggregate-daily --input <raw-depth-dir> --output <daily.parquet>

marketdata rqdata hk-assets -- mirror-hk-daily <原 cstree rqdata 参数>
marketdata rqdata hk-assets -- build-hk-daily-clean-layer <原 cstree rqdata 参数>

marketdata migration sync-hk-links --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata migration import-cross-artifacts --artifacts-root "$DATA_PLATFORM_ROOT" --json
marketdata migration import-cross-artifacts --artifacts-root "$DATA_PLATFORM_ROOT" --apply
marketdata rqdata inspect-hk-current \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260526

marketdata rqdata refresh-hk-current \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260526 \
  --refresh-asset daily --refresh-asset daily_clean \
  --inspect-asset daily --inspect-asset daily_clean

marketdata rqdata refresh-hk-intraday \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --start-date 20260526 \
  --end-date 20260526

marketdata rqdata refresh-hk-depth \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --start-date 20260526 \
  --end-date 20260526 \
  --symbols-file "$DATA_PLATFORM_ROOT/assets/rqdata/hk/daily/hk_all_daily_clean_latest/symbols.txt" \
  --name hk_tick_depth_increment_20260526

marketdata rqdata refresh-hk-fundamentals \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260526

marketdata data catalog --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata data query --artifacts-root "$DATA_PLATFORM_ROOT" --sql "select 1 as value"
```

`marketdata rqdata hk-depth -- ...` 和 `marketdata rqdata refresh-hk-depth` 使用平台内
`market_data_platform.hk_depth` 实现；安装本包后也会提供兼容命令
`rqdata-hk-depth` 和 `rqdata-tick`。`marketdata rqdata hk-assets -- ...` 使用平台内
`market_data_platform.hk_assets` 实现；安装本包后也会提供 `rqdata-hk-assets` 命令。
`refresh-hk-current` 是平台侧 HK current wrapper：它会调用平台内 HK refresh workflow，
并在成功后由 `market-data-platform` 重新生成 `hk_current.json` 与
`dataset_registry.csv`。如果需要让 `cross-sectional-trees` 在本地研究配置中读取同一套数据，
可使用 `marketdata migration sync-hk-links` 同步 artifacts 兼容链接和 registry；这是数据路径兼容，cross 不拥有数据维护代码。
如果需要把 `cross-sectional-trees/artifacts` 中历史遗留的数据平台产物迁入平台根目录，
先运行 `marketdata migration import-cross-artifacts --json` 查看计划，再加 `--apply`
执行复制。该命令只处理 `assets/rqdata`、`assets/style`、`assets/universe`、`metadata`、
`cache/intraday`、`releases` 以及 HK health/audit 类报告；不会复制研究 runs、sweeps、
live/export 产物、benchmark attribution 或 slippage calibration 报告，也不会删除源文件。
`inspect-hk-current` 提供同一根目录下的 current contract 健康度检查。
`refresh-hk-intraday`、`refresh-hk-depth` 和
`refresh-hk-fundamentals` 分别封装 5m 增量刷新、tick-depth download/health/aggregate/
publish、PIT patch 与 financial details 刷新，并同样在成功后重建 current contract。
`marketdata migration status` 会将 `hk-assets` 与 `hk-depth` 都标为 `native`。
`marketdata data ...` 承载数据清单目录、标准层物化和 DuckDB 查询；查询功能需要安装 `duckdb` 可选依赖，例如 `uv sync --extra dev --extra duckdb`。
`cross-sectional-trees` 中的 `cstree data ...` 仅作为兼容入口保留。

## 本地快照备份

`marketdata backup-data` 用于冻结本地 cache、universe、配置文件，或按 `hk_current.json` 备份当前 HK 数据资产集合。该命令写入 snapshot 目录和 `manifest.yml`，不会覆盖已有 snapshot。

```bash
marketdata backup-data --preset hk_current --name hk_current_20260526
marketdata backup-data --include-path configs/presets/release/hk_current.yml
```

`hkdata` 命令和 `hk_data_platform` Python 包名作为兼容层保留，新代码应优先使用 `marketdata` 和 `market_data_platform`。

## 本地开发 (Development)

```bash
uv sync --extra dev
uv run --extra dev python -m pytest
uv run --extra dev python -m ruff check .
uv run --extra dev python -m pyright
```

如需运行 DuckDB 查询：

```bash
uv sync --extra dev --extra duckdb
```

CI 还会运行以下治理检查：

```bash
uv run --extra dev python scripts/dev/quality_debt.py --skip-ruff --check-baseline
uv run --extra dev python scripts/dev/maintainability_metrics.py --check-baseline
uv run --extra dev python scripts/dev/compatibility_governance.py --check
uv run --extra dev python scripts/dev/architecture_governance.py --check
```

当前维护性快照见 `docs/maintenance-audit.md`。关于数据契约、兼容层和迁移记录，请参阅 `docs/README.md`。
