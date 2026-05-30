# 中国香港市场数据资产维护

本页是中国香港市场数据资产生命周期的维护入口，主要覆盖港股日线、财务、估值、行业、日内和 tick-depth 等数据。下游研究或交易系统只消费这里发布的数据资产；下载、清洗、健康检查、current contract 和 release 由本仓库维护。

## 入口命令

推荐使用统一入口：

```bash
marketdata rqdata hk-assets -- --help
marketdata rqdata hk-depth -- --help
marketdata rqdata refresh-hk-current --help
marketdata rqdata refresh-hk-intraday --help
marketdata rqdata refresh-hk-depth --help
marketdata rqdata refresh-hk-fundamentals --help
```

安装本包后也提供兼容命令：

```bash
rqdata-hk-assets --help
```

## 命令矩阵

`marketdata rqdata hk-assets -- --help` 当前覆盖以下类别：

| 类别 | 命令 |
| --- | --- |
| 基础信息 | `info`, `quota`, `list-hk-financial-fields` |
| 原始资产镜像 | `export-hk-instruments`, `mirror-hk-daily`, `mirror-hk-valuation`, `mirror-hk-pit-financials`, `patch-hk-pit-financials`, `mirror-hk-financial-details`, `mirror-hk-ex-factors`, `mirror-hk-dividends`, `mirror-hk-shares`, `mirror-hk-exchange-rate`, `mirror-hk-announcement`, `mirror-hk-southbound`, `mirror-hk-instrument-industry`, `mirror-hk-industry-changes` |
| 派生资产 | `build-hk-pit-fundamentals`, `build-hk-industry-labels`, `build-hk-daily-clean-layer`, `build-hk-intraday-asset`, `sync-hk-intraday` |
| 检查与修复 | `inspect-hk-pit-coverage`, `inspect-hk-asset-health`, `inspect-hk-current-health`, `inspect-hk-data-assets`, `inspect-hk-intraday-health`, `rebase-hk-asset-metadata` |

较长的发布编排位于 `market_data_platform.release_tools`，常用入口是
`marketdata rqdata refresh-hk-current`、`refresh-hk-intraday`、
`refresh-hk-depth` 和 `refresh-hk-fundamentals`。

## 主要资产

平台侧负责以下中国香港市场数据资产：

* daily / daily_clean
* instruments
* PIT financials / financial details
* valuation、ex_factors、dividends、shares
* industry / industry_changes
* southbound / universe by date
* intraday 5m
* current contract、dataset registry、health、audit、release

资产应落在统一根目录：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

正式发布后，重建 current contract 和 registry：

```bash
marketdata contract build \
  --market hk \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260526
```

## 常用维护命令

```bash
marketdata migration status

marketdata rqdata hk-depth -- health --input <raw-depth-dir>
marketdata rqdata hk-depth -- aggregate-daily --input <raw-depth-dir> --output <daily.parquet>

marketdata rqdata hk-assets -- mirror-hk-daily <原 cstree rqdata 参数>
marketdata rqdata hk-assets -- build-hk-daily-clean-layer <原 cstree rqdata 参数>

marketdata rqdata inspect-hk-current \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260526
```

`marketdata rqdata hk-depth -- ...` 和 `marketdata rqdata refresh-hk-depth` 使用平台内 `market_data_platform.hk_depth` 实现；安装本包后也会提供兼容命令 `rqdata-hk-depth` 和 `rqdata-tick`。`marketdata rqdata hk-assets -- ...` 使用平台内 `market_data_platform.hk_assets` 实现；安装本包后也会提供 `rqdata-hk-assets` 命令。

## 日内数据

5m 日内数据刷新入口：

```bash
marketdata rqdata refresh-hk-intraday \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --start-date 20260526 \
  --end-date 20260526
```

底层实现位于 `market_data_platform.hk_assets.intraday_download`。旧的
`python -m cstree.research.hk_intraday_download` 是历史下游项目里的兼容 wrapper；新项目应直接使用 `marketdata rqdata refresh-hk-intraday`。

## 历史平台产物导入

如果历史数据平台产物还留在旧的下游研究仓库目录中，可以先 dry-run 查看迁移计划：

```bash
marketdata migration import-cross-artifacts \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --json
```

确认后执行：

```bash
marketdata migration import-cross-artifacts \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --apply
```

该命令只迁移平台归属的 assets、metadata、intraday cache、release 和中国香港市场 health/audit 报告；研究 runs、sweeps、live/export、benchmark 和 slippage 报告应留在原下游项目。
