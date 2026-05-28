# HK 数据资产维护

本页是 HK 数据资产生命周期的维护入口。`cross-sectional-trees` 只消费这里发布的数据资产，不再负责下载、清洗、健康检查、current contract 或 release。

## 入口命令

推荐使用统一入口：

```bash
marketdata rqdata hk-assets -- --help
marketdata rqdata refresh-hk-current --help
marketdata rqdata refresh-hk-intraday --help
marketdata rqdata refresh-hk-fundamentals --help
```

安装本包后也保留兼容命令：

```bash
rqdata-hk-assets --help
```

## 主要资产

平台侧负责以下 HK 数据资产：

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

## Intraday

5m 日内数据刷新入口：

```bash
marketdata rqdata refresh-hk-intraday \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --start-date 20260526 \
  --end-date 20260526
```

底层实现位于 `market_data_platform.hk_assets.intraday_download`。旧的
`python -m cstree.research.hk_intraday_download` 只是 cross 仓库里的兼容 wrapper。

## 历史 cross 产物导入

如果历史数据平台产物还留在 `cross-sectional-trees/artifacts`，先 dry-run：

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

该命令只迁移平台归属的 assets、metadata、intraday cache、release 和 HK health/audit 报告；研究 runs、sweeps、live/export、benchmark 和 slippage 报告仍留在策略仓库。
