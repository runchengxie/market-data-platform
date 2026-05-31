# 操作手册

本页收录根 README 中展开会显得过长的日常操作：环境变量、provider credentials、中国大陆市场 / TuShare、中国香港市场 current refresh、备份和本地开发检查。

## 环境变量与凭证

推荐统一配置共享数据根目录：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

本地开发可使用仓库内的默认 `artifacts/`：

```bash
cp .envrc.example .envrc
cp .env.example .env.local
direnv allow
```

真实凭证写入未跟踪的 `.env.local`，或写入：

```text
~/.config/market-data-platform/secrets.env
```

支持的主要变量：

| 变量 | 用途 |
| --- | --- |
| `DATA_PLATFORM_ROOT` | 推荐的共享市场数据产物根目录 |
| `HK_DATA_PLATFORM_ROOT` | 旧中国香港市场调用方兼容变量 |
| `CSTREE_ARTIFACTS_ROOT` | 策略仓库运行产物根目录覆盖变量 |
| `TUSHARE_TOKEN` / `TUSHARE_TOKEN_2` | TuShare 中国大陆市场 provider token |
| `RQDATA_USERNAME` / `RQDATA_PASSWORD` / `RQDATA_URI` | RQData provider 凭证 |

## 中国大陆市场 TuShare MVP

TuShare 是中国大陆市场数据的并存 provider，当前主要用于 A 股基础数据采集。安装可选依赖后，以环境变量或未跟踪的 `.env.local` 提供 token；显式导出的环境变量优先级高于 `.env.local`：

```bash
uv sync --extra dev --extra tushare

marketdata tushare verify-token
```

常用导出和镜像命令：

```bash
marketdata tushare export-a-share-instruments \
  --out "$DATA_PLATFORM_ROOT/assets/tushare/a_share/instruments/a_share_all_instruments_latest.parquet"

marketdata tushare mirror-a-share-trade-cal \
  --start-date 20260101 --end-date 20260526 \
  --out "$DATA_PLATFORM_ROOT/assets/tushare/a_share/trade_cal/a_share_trade_cal_latest.parquet"

marketdata tushare mirror-a-share-daily \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/daily/a_share_all_20260101_20260526_daily"

marketdata tushare mirror-a-share-adj-factor \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/adj_factor/a_share_all_20260101_20260526_adj_factor"

marketdata tushare mirror-a-share-daily-basic \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/daily_basic/a_share_all_20260101_20260526_daily_basic"
```

日频类 TuShare 镜像按开放交易日请求全市场，并写入：

```text
data/trade_date=YYYYMMDD/part.parquet
```

raw 快照完整后，可构建并验证 `daily_clean`，再从 `daily_clean` 生成按调仓日维护的
A 股 full-market universe。universe builder 使用前置滚动中位成交额，避免在调仓日使用
当日成交额：

```bash
marketdata tushare build-a-share-daily-clean \
  --daily-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily" \
  --adj-factor-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/adj_factor/a_share_all_20240101_20260529_adj_factor" \
  --daily-basic-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/daily_basic/a_share_all_20240101_20260529_daily_basic" \
  --limit-status-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/limit_status/a_share_limit_status_20240101_20260529" \
  --instruments-file "$DATA_PLATFORM_ROOT/assets/tushare/a_share/instruments/a_share_all_instruments_latest.parquet" \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily_clean" \
  --min-rows 3000000 --min-symbols 5000

marketdata tushare build-a-share-universe \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --daily-clean-dir "$DATA_PLATFORM_ROOT/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily_clean" \
  --start-date 20240101 --end-date 20260529 \
  --rebalance-frequency M --lookback-days 60 --min-window-days 30 \
  --min-rows 100000 --min-symbols 5000 --min-rebalance-dates 20

marketdata tushare validate-a-share-universe \
  --by-date-file "$DATA_PLATFORM_ROOT/assets/universe/a_share_all_full_by_date.csv" \
  --latest-symbols-file "$DATA_PLATFORM_ROOT/assets/universe/a_share_all_full_symbols.txt" \
  --meta-file "$DATA_PLATFORM_ROOT/assets/universe/a_share_all_full_by_date.meta.yml" \
  --expected-as-of 20260529 \
  --min-rows 100000 --min-symbols 5000 --min-rebalance-dates 20
```

完成校验并将 `*_latest` alias 指向采用的 snapshot 后，发布当前中国大陆市场 provider：

```bash
marketdata contract build --market a_share --provider tushare \
  --artifacts-root "$DATA_PLATFORM_ROOT" --target-date 20260526
```

发布后可用通用 current contract 检查入口生成机器可读报告：

```bash
marketdata contract inspect --market a_share --provider tushare \
  --artifacts-root "$DATA_PLATFORM_ROOT" --target-date 20260526 \
  --fail-on-severity error --format json \
  --out "$DATA_PLATFORM_ROOT/reports/a_share_current_health_20260526.json"
```

`marketdata tushare mirror-a-share-limit-status` 可镜像 `stk_limit` 接口形成 `limit_status`
raw 资产。当前范围包含 raw layer、`daily_clean`、full-market universe 和 contract 发布入口；
自动化 current refresh、发布打包与更完整的修复工作流仍需后续补齐。

### A 股历史 backfill 编排

长历史下载应先用编排入口生成分段计划，再按月或按年续跑，不建议直接把 10-15 年历史一次性下完。默认按月分段、跳过已存在的 `trade_date` 分区；加 `--dry-run` 只打印计划，不访问 provider：

```bash
marketdata tushare backfill-a-share-history \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --start-date 20240101 --end-date 20260531 \
  --dataset daily --dataset adj_factor --dataset daily_basic --dataset limit_status \
  --segment month \
  --dry-run
```

确认计划后去掉 `--dry-run` 执行。每个 raw asset 会写到一个 range snapshot 目录，例如 `assets/tushare/a_share/daily/a_share_all_20240101_20260531_daily/`，内部仍按 `data/trade_date=YYYYMMDD/part.parquet` 分区，并在 snapshot 根目录写 `manifest.yml` 汇总实际覆盖、跳过分区和失败分段。全部成功后可用 `--sync-latest` 将 canonical latest alias 指向本次 snapshot。

## 中国香港市场 current refresh

常用中国香港市场 current contract 和增量刷新入口：

```bash
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
```

`refresh-hk-current` 会调用平台内中国香港市场 refresh workflow，并在成功后重建 `hk_current.json` 与 `dataset_registry.csv`。`refresh-hk-intraday`、`refresh-hk-depth` 和 `refresh-hk-fundamentals` 分别封装 5m 增量刷新、tick-depth download/health/aggregate/publish、PIT patch 与 financial details 刷新。

## 历史产物迁入

如果需要把历史下游研究仓库 `artifacts` 中遗留的平台产物迁入平台根目录，先查看计划：

```bash
marketdata migration import-cross-artifacts \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --json
```

确认后执行复制：

```bash
marketdata migration import-cross-artifacts \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --apply
```

该命令只处理 `assets/rqdata`、`assets/style`、`assets/universe`、`metadata`、`cache/intraday`、`releases` 以及中国香港市场 health/audit 类报告；不会复制研究 runs、sweeps、live/export 产物、benchmark attribution 或 slippage calibration 报告，也不会删除源文件。

## 本地快照备份

`marketdata backup-data` 用于冻结本地 cache、universe、配置文件，或按 `hk_current.json` 备份当前中国香港市场数据资产集合。该命令写入 snapshot 目录和 `manifest.yml`，不会覆盖已有 snapshot。

```bash
marketdata backup-data --preset hk_current --name hk_current_20260526
marketdata backup-data --include-path configs/presets/release/hk_current.yml
```

## 本地开发与治理检查

常规开发：

```bash
uv sync --extra dev
uv run --extra dev python -m pytest
uv run --extra dev python -m ruff check .
uv run --extra dev python -m pyright
```

DuckDB 查询依赖：

```bash
uv sync --extra dev --extra duckdb
```

CI 治理检查：

```bash
uv run --extra dev python scripts/dev/quality_debt.py --skip-ruff --check-baseline
uv run --extra dev python scripts/dev/maintainability_metrics.py --check-baseline
uv run --extra dev python scripts/dev/compatibility_governance.py --check
uv run --extra dev python scripts/dev/architecture_governance.py --check
```
