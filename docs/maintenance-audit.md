# 维护性审计快照

审计日期：2026-05-29

本页记录当前维护事实、已接受的技术债、清理决策和下一轮重构优先级。数据来自本仓库治理脚本和 repo-local 搜索结果。

## 当前职责

本仓库维护中国香港市场和中国大陆市场的共享数据控制面，当前包含：

- 数据契约、路径规范、asset key、manifest、dataset registry 和 current contract。
- 中国大陆市场 RQData / TuShare 基础镜像入口。
- 港股 tick-depth 下载、health、aggregate、reconcile 和 release 工具。
- 中国香港市场 RQData assets 的 mirror、clean、PIT、valuation、industry、intraday、health、audit、current refresh 和 release 工具。
- `marketdata` 统一 CLI，以及保留中的旧命令和旧 import 兼容层。

下游策略、交易或报表系统应只读消费平台发布的数据资产。历史迁移记录保留在 `docs/migration-plan.md`；仍支持的旧入口记录在 `docs/compatibility.md`。

## 生命周期分类

| 范围 | 分类 | 维护决策 |
| --- | --- | --- |
| `src/market_data_platform/contract.py`, `paths.py`, `manifest.py`, `registry.py`, `current_assets.py`, `data_provider_contracts.py` | active | 平台核心边界，优先保持 Ruff / Pyright 覆盖 |
| `src/market_data_platform/providers/*`, `rqdata_cli_common.py`, `tushare_cli.py` | active | provider adapter 与 CLI parser，继续扩大类型覆盖 |
| `src/market_data_platform/hk_depth/*` | active / needs-refactor | 业务能力已归属平台；大模块继续拆分并逐步恢复 Pyright |
| `src/market_data_platform/hk_assets/*` | active / needs-refactor | 业务能力已归属平台；当前是最大维护热点 |
| `src/market_data_platform/release_tools/*` | active / needs-refactor | 发布编排活跃；优先拆分 planning、execution、reporting |
| `src/hk_data_platform/*`, `src/market_data_platform/rqdata_cn.py`, `src/market_data_platform/tushare_cn.py` | compatibility | 保留 re-export；删除前需 repo-local 和下游使用审计 |
| `marketdata migration status`, `sync-hk-links`, `import-cross-artifacts` | migration-only | 保留当前行为；不承载新业务能力 |
| `scripts/dev/*` | active governance | CI 使用的治理脚本，变更需配套测试 |
| `configs/presets/release/*.yml` | archival / retained | 保留历史发布复现能力；定期归档不再复现的 preset |
| `artifacts/`, `reports/`, `.pytest_cache/`, `.ruff_cache/`, `*.egg-info` | generated/cache | 不属于源码维护面，不提交 Git |

## 质量覆盖

本轮已将 `rebalance.py`、`pit_feature_stats.py`、`rqdata_runtime.py`、`data_providers.py`、`data_warehouse.py` 移出 Ruff 排除，并将 `rebalance.py` 移出 Pyright 排除。`data_providers.py` 拆出了 `provider_cache.py`，`data_warehouse.py` 拆出了 `warehouse_query.py`；两者仍暂留 Pyright 排除，因为 pandas-heavy 数据转换和可选 provider SDK 的类型噪声仍较高。`rqdata_runtime.py` 仍保留 Pyright 排除，因为它依赖可选 `rqdatac` 包；`pit_feature_stats.py` 仍保留 Pyright 排除，因为 pandas `groupby().groups` 的类型推断噪声较高。

当前 baseline：

| 工具 | 覆盖文件 | 覆盖行数 | 排除文件 | 排除行数 | 覆盖比例 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ruff | 62 / 118 | 16,347 / 47,241 | 56 | 30,894 | 34.6% |
| Pyright | 34 / 118 | 7,785 / 47,241 | 84 | 39,456 | 16.5% |

仍需优先收紧：

- Ruff：`release_tools`、`hk_assets`。
- Pyright：`data_providers.py`、`data_warehouse.py`、`pit_feature_stats.py`、`rqdata_runtime.py`、`release_tools`、`hk_assets`、大部分 `hk_depth` 模块。
- Pyright 收紧时优先处理纯路径、配置、parser、state、renderer 和 report 逻辑，再处理 provider SDK 与 pandas-heavy 数据转换逻辑。

## 维护性热点

当前 `scripts/dev/maintainability_metrics.py --json --limit 30` 输出：

| 指标 | 当前值 |
| --- | ---: |
| Python files | 135 |
| Python lines | 51,372 |
| Functions over 100 lines | 69 |
| Functions over 250 lines | 17 |
| Functions with 10+ args | 48 |
| Max file lines | 2,791 |
| Max function lines | 349 |
| Public facade exports | 50 |

最大文件：

| 文件 | 行数 | 建议 |
| --- | ---: | --- |
| `src/market_data_platform/hk_assets/asset_health.py` | 2,791 | 拆分采集、规则、summary、rendering、CLI adapter |
| `src/market_data_platform/release_tools/hk_asset_workflow.py` | 1,938 | 拆分 refresh step planning、execution、reporting |
| `src/market_data_platform/hk_assets/audit_assets.py` | 1,819 | 拆分 reference scan、prune decision、report rendering |
| `src/market_data_platform/hk_depth/downloader.py` | 1,767 | 已纳入 Pyright；继续拆分 provider client adapter 和 raw layout writer |
| `src/market_data_platform/hk_assets/mirror_financial.py` | 1,613 | 拆分 PIT patch、financial details、field resolution |
| `src/market_data_platform/hk_assets/mirror_workflow.py` | 1,538 | 拆分 dated dataset orchestration 与 common IO |
| `src/market_data_platform/hk_assets/coverage.py` | 1,454 | 拆分 PIT health section、selection logic、rendering |
| `src/market_data_platform/hk_assets/intraday_health.py` | 1,367 | 拆分 daily reconciliation 与 quality checks |
| `src/market_data_platform/hk_assets/build.py` | 1,215 | 拆分 PIT build、daily build、metadata helpers |
| `src/market_data_platform/hk_workflows.py` | 1,143 | 拆分 migration/archive helpers 与 HK workflow adapters |
| `src/market_data_platform/data_providers.py` | 1,123 | 已拆出 cache IO；继续拆 provider SDK adapters 与 pandas transforms |

最大函数：

| 函数 | 行数 | 建议 |
| --- | ---: | --- |
| `mirror_hk_instrument_industry` | 349 | 提取 request plan、frame normalization、persist summary |
| `mirror_hk_industry_changes` | 347 | 与 industry mirror 共用 dated mirror helper |
| `inspect_hk_asset_health` | 331 | 提取 rule aggregation 和 JSON report builder |
| `_build_refresh_steps` | 325 | 拆分为 asset-specific step builders |
| `mirror_hk_southbound` | 324 | 提取 fetch loop 与 merge logic |
| `build_hk_daily_clean_layer` | 312 | 拆分 cleaning rules、write plan、manifest output |
| `_build_pit_health_section` | 306 | 拆分 rule collection 与 rendering payload |
| `inspect_hk_pit_coverage` | 305 | 拆分 input resolve、metric calculation、output |

本轮已从 `cli.py` 提取 TuShare parser 构建逻辑到 `src/market_data_platform/tushare_cli.py`，并将 migration one-off `import-cross-artifacts` 的执行入口转到 `scripts/internal/import_cross_artifacts.py`。`cli.py` 保留 deprecated wrapper 和 warning，避免现有自动化直接断裂。

本轮也从 `data_warehouse.py` 提取 DuckDB standardized query registration 到 `src/market_data_platform/warehouse_query.py`，从 `data_providers.py` 提取 cache path、cache tag、legacy symbol cleanup 和 parquet cache write helper 到 `src/market_data_platform/provider_cache.py`。这两个大文件已恢复 Ruff 覆盖，但仍需要后续针对 pandas-heavy 类型噪声逐步恢复 Pyright 覆盖。

## 兼容层决策

`scripts/dev/compatibility_governance.py --json` 当前无 issues。保留项和本地使用证据：

| 兼容项 | source | tests | docs | scripts/other | 决策 |
| --- | ---: | ---: | ---: | ---: | --- |
| `hkdata` CLI | 1 | 2 | 1 | 4 | 保留；下游切换到 `marketdata` 后再标记 deprecated |
| `hk_data_platform.*` | 6 | 3 | 1 | 5 | 保留；先做下游 import 审计 |
| `market_data_platform.rqdata_cn` / `tushare_cn` | 2 | 2 | 1 | 1 | 保留；推荐 provider namespace |
| `marketdata migration status` | 1 | 1 | 2 | 2 | 保留为迁移状态查看；长期候选为 docs-only |
| `marketdata migration sync-hk-links` | 1 | 1 | 1 | 2 | 保留；等下游只读 current contract 后删除 |
| `marketdata migration import-cross-artifacts` | 1 | 2 | 3 | 2 | Deprecated wrapper；执行逻辑已归档到 `scripts/internal/import_cross_artifacts.py`，后续确认无下游依赖后移除 CLI wrapper |
| `rqdata-hk-depth` / `rqdata-tick` | 1 | 1 | 4 | 4 | 保留；新任务使用 `marketdata rqdata hk-depth -- ...` |
| `rqdata-hk-assets` | 1 | 1 | 4 | 3 | 保留；已补 console script smoke test |
| HK release presets | 0 | 0 | 1 | 2 | 中国香港市场发布配置；保留并定期归档历史 snapshot preset |

## 生成文件与数据产物

`.gitignore` 已覆盖 `.venv/`、`.pytest_cache/`、`.ruff_cache/`、`__pycache__/`、`*.py[cod]`、`*.egg-info/`、`artifacts/`、`data/`、`reports/` 和本地凭证。`git ls-files` 未发现已跟踪的 pycache、pytest cache、ruff cache、egg-info、artifacts reports 或 artifacts metadata 文件。

仓库工作区中可能存在未跟踪的本地运行产物，例如 `artifacts/metadata/*.csv`、`artifacts/reports/*.json`、`src/*.egg-info` 和 `__pycache__/`。这些不属于源码维护面，不应提交。

## 文档审计结论

根 README、`AGENTS.md` 和 `docs/*.md` 已同步当前状态：

- 当前入口以 `marketdata` 和 `market_data_platform` 为主。
- 兼容入口集中记录在 `docs/compatibility.md`。
- 迁移历史集中记录在 `docs/migration-plan.md`。
- 本地验证命令与 CI 保持一致。
- 文档保留 code-level English terms，如 CLI、provider、current contract、release、baseline、cache、artifacts、workflow，避免临时翻译造成歧义。

## 下一轮优先级

1. 从 `hk_assets/asset_health.py` 提取 report builder，并为 JSON payload 增加 focused tests。
1. 继续拆 `src/market_data_platform/data_providers.py` 的 provider SDK adapters 和 pandas transforms，逐步恢复 Pyright 覆盖。
1. 继续拆 `src/market_data_platform/data_warehouse.py` 的 catalog/materialize pandas-heavy 逻辑，逐步恢复 Pyright 覆盖。
1. 确认下游没有 `marketdata migration import-cross-artifacts` 依赖后，移除 CLI wrapper，仅保留 `scripts/internal/import_cross_artifacts.py` 和 archived docs。
1. 定期审查 `configs/presets/release/*.yml`，将不再用于复现的 snapshot preset 移入归档记录。
