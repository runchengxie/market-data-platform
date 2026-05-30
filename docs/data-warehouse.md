# 数据目录、标准层与 DuckDB 查询

`marketdata data ...` 负责基于数据清单的目录刷新、标准层物化和 DuckDB 查询。该能力由本平台统一维护；历史下游项目中的 `cstree data ...` 只是兼容入口。

默认产物根目录解析顺序为：

```text
--artifacts-root 参数
DATA_PLATFORM_ROOT
HK_DATA_PLATFORM_ROOT
CSTREE_ARTIFACTS_ROOT
artifacts/
```

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

查询功能需要安装 DuckDB：

```bash
uv sync --extra dev --extra duckdb
```

```bash
marketdata data query \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --sql "select 1 as value"
```

查询时会扫描标准层的数据清单并在 DuckDB 中注册视图。需要把结果写出时使用 `--format` 和 `--out`。
