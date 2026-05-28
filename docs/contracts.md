# 共享数据契约

## 数据产物根目录

共享的数据产物根目录用于划分数据工具与策略代码库之间的存储边界。

推荐的环境变量配置：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

`DATA_PLATFORM_ROOT` 是推荐使用的跨项目通用环境变量，用于指定共享市场数据输入路径。`HK_DATA_PLATFORM_ROOT` 仍作为旧 HK 调用方的兼容变量保留。`CSTREE_ARTIFACTS_ROOT` 用于覆盖策略输出根目录；下游项目只有在需要把运行记录、缓存或报告也写入共享根目录时才应使用它。

## 当前数据契约

```text
<artifacts_root>/metadata/current_assets/<market>_current.json
```

必需的顶层 JSON 结构：

```json
{
  "contract": {
    "name": "hk_current",
    "market": "hk",
    "provider": "rqdata",
    "version": 1,
    "artifacts_root": "/data/market-data-platform",
    "target_date": "20260409"
  },
  "assets": {
    "daily_clean": {
      "alias_path": ".../hk_all_daily_clean_latest",
      "resolved_path": ".../hk_all_2000_20260409_daily_clean_refetched_latest",
      "manifest_path": ".../manifest.yml",
      "as_of": "20260409"
    }
  }
}
```

可通过标准的数据资产别名（Asset Aliases）来生成该契约文件：

```bash
marketdata contract build \
  --market hk \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260409
```

默认情况下，该命令会同时合并已存在的 HK/CN current contracts 生成
`metadata/dataset_registry.csv`。如只需写入 JSON 契约，可加 `--no-registry`。

## 数据资产键名

| 资产键名 (Asset key) | 产物根目录下的默认路径 |
| --- | --- |
| `daily` | `assets/rqdata/hk/daily/hk_all_daily_latest` |
| `daily_clean` | `assets/rqdata/hk/daily/hk_all_daily_clean_latest` |
| `intraday` | `assets/rqdata/hk/intraday/hk_intraday_latest` |
| `tick_depth_raw` | `assets/rqdata/hk/tick_depth/hk_tick_depth_latest` |
| `tick_depth_daily` | `assets/rqdata/hk/tick_depth_daily/hk_tick_depth_daily_latest` |
| `execution_cost_model` | `assets/rqdata/hk/execution_cost/hk_execution_cost_model_latest` |
| `etf_daily` | `assets/rqdata/hk/daily/hk_etf_daily_latest` |
| `etf_daily_clean` | `assets/rqdata/hk/daily/hk_etf_daily_clean_latest` |
| `etf_instruments` | `assets/rqdata/hk/instruments/hk_etf_instruments_latest.parquet` |
| `valuation` | `assets/rqdata/hk/valuation/hk_all_valuation_latest` |
| `instruments` | `assets/rqdata/hk/instruments/hk_all_instruments_latest.parquet` |
| `pit` | `assets/rqdata/hk/pit_financials/hk_all_2000_2025_full_market_latest` |
| `ex_factors` | `assets/rqdata/hk/ex_factors/hk_all_ex_factors_latest` |
| `dividends` | `assets/rqdata/hk/dividends/hk_all_dividends_latest` |
| `shares` | `assets/rqdata/hk/shares/hk_all_shares_latest` |
| `exchange_rate` | `assets/rqdata/hk/exchange_rate/hk_exchange_rate_latest` |
| `southbound` | `assets/rqdata/hk/southbound/hk_connect_southbound_latest` |
| `financial_details` | `assets/rqdata/hk/financial_details/hk_financial_details_latest` |
| `industry_changes` | `assets/rqdata/hk/industry_changes/hk_all_industry_changes_latest` |
| `universe_by_date` | `assets/universe/hk_all_full_by_date.csv` |
| `universe_symbols` | `assets/universe/hk_all_full_symbols.txt` |
| `universe_meta` | `assets/universe/hk_all_full_by_date.meta.yml` |

CN 使用同一套 asset key 语义，但路径落在 `assets/rqdata/cn/...`，并额外预留
`st_flags`、`suspend`、`limit_status`、`index_components`、`industry_citic`、
`industry_sw`、`northbound` 等 A 股数据资产键。

CN contract 可通过 `--provider tushare` 显式选择 TuShare raw 资产。此模式下
`cn_current.json` 的 `contract.provider` 为 `tushare`，当前支持的路径为：

| 资产键名 (Asset key) | TuShare CN 默认路径 |
| --- | --- |
| `instruments` | `assets/tushare/cn/instruments/cn_all_instruments_latest.parquet` |
| `trade_cal` | `assets/tushare/cn/trade_cal/cn_trade_cal_latest.parquet` |
| `daily` | `assets/tushare/cn/daily/cn_all_daily_latest` |
| `adj_factor` | `assets/tushare/cn/adj_factor/cn_all_adj_factor_latest` |
| `daily_basic` | `assets/tushare/cn/daily_basic/cn_all_daily_basic_latest` |
| `limit_status` | `assets/tushare/cn/limit_status/cn_limit_status_latest` |
| `daily_clean` | `assets/tushare/cn/daily/cn_all_daily_clean_latest` |

不传 `--provider` 的 CN contract 继续使用原有 `rqdata` 布局。单个
`cn_current.json` 只表示当前采纳的 provider，不汇总多个 provider 的 raw 快照。

## 数据集注册表 (Dataset Registry)

```text
<artifacts_root>/metadata/dataset_registry.csv
```

该注册表是一个专为人类阅读设计的精简索引文件，其内容由 `hk_current.json` / `cn_current.json` 和各项数据资产的数据清单推导生成。当前数据契约才是下游读取路径时的权威入口。

也可以单独重建注册表：

```bash
marketdata registry build \
  --artifacts-root "$DATA_PLATFORM_ROOT"
```

## 数据清单规范 (Manifest Rule)

每一个正式发布的数据资产目录中，都必须包含一个 `manifest.yml` 文件；对于单文件形式的数据资产，则应在其同级目录下提供一个对应的 `*.manifest.yml` 文件。数据清单需要透出足够丰富的字段，以便下游代码能够快速获取该数据集的概况信息，包括：数据集状态、数据行数、标的代码（Symbol）数量、日期范围以及数据血缘（Lineage）。
