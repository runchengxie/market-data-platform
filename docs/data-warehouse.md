# Data Warehouse 与标准层

`marketdata data ...` 负责 manifest-backed catalog、standardized layer 物化和 DuckDB 查询。该能力从 `cross-sectional-trees` 迁入平台仓库，cross 侧的 `cstree data ...` 仅保留兼容入口。

## 刷新 Catalog

```bash
marketdata data catalog \
  --artifacts-root "$DATA_PLATFORM_ROOT"
```

默认写入：

```text
<artifacts_root>/metadata/catalog.sqlite
<artifacts_root>/metadata/catalog_summary.csv
```

## 物化标准层

从 asset directory 物化：

```bash
marketdata data materialize \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --name hk_daily_panel \
  --market hk \
  --preset rqdata-daily \
  --asset-dir "$DATA_PLATFORM_ROOT/assets/rqdata/hk/daily/hk_all_daily_latest" \
  --frequency M
```

输出默认位于：

```text
<artifacts_root>/standardized/<market>/<dataset>/<name>/
```

## 查询标准层

```bash
marketdata data query \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --sql "select 1 as value"
```

查询时会扫描 standardized manifest 并在 DuckDB 中注册视图。需要把结果写出时使用 `--format` 和 `--out`。
