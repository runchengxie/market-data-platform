# 兼容层与清理计划

本页记录保留中的兼容入口、迁移入口和历史运行约定。每个兼容项都必须有明确用途、风险和清理条件。

## 当前兼容项

| 兼容项 | 当前用途 | 风险 | 推荐替代 | 清理条件 | 当前状态 | 审计证据 |
| --- | --- | --- | --- | --- | --- | --- |
| `hkdata` CLI | 旧命令名兼容，转发到 `marketdata` 实现 | 新用户可能继续复制旧命令 | `marketdata` | 下游脚本全部改用 `marketdata` 后，先文档标记 deprecated，再移除 | retained; downstream usage unknown | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `hk_data_platform.*` | 旧 Python 包名 re-export | 扩大 public API 面，类型检查价值低 | `market_data_platform.*` | repo-local 和下游 import 审计无使用后删除 | retained; downstream usage unknown | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `market_data_platform.rqdata_cn` / `tushare_cn` | 旧 provider module re-export | 旧路径永久化 | `market_data_platform.providers.rqdata_cn` / `market_data_platform.providers.tushare_cn` | 下游统一改用 `market_data_platform.providers.*` 后删除 | retained; downstream usage unknown | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `marketdata migration status` | 查看迁移后工作流归属 | 迁移完成后仍像正式功能 | `docs/migration-plan.md` 和原生 `marketdata rqdata ...` 入口 | `transition_status()` 长期为空后，改为 docs-only 记录 | retained; deprecation candidate | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `marketdata migration sync-hk-links` | 同步 `cross-sectional-trees` 兼容链接 | 继续强化跨仓库路径耦合 | 只读 current contract 和显式 artifacts root | 下游完全只读消费 current contract 后删除 | retained; downstream usage unknown | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `marketdata migration import-cross-artifacts` | 从旧仓库导入历史平台资产 | 一次性迁移命令留在主 CLI | `scripts/internal/import_cross_artifacts.py` 或归档文档 | 下游确认无 CLI 依赖后移除 wrapper，仅保留内部脚本/归档记录 | deprecated wrapper; execution archived internally | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md`; wrapper tests in `tests/test_transitions.py` |
| `rqdata-hk-depth` / `rqdata-tick` | 港股 depth 旧 console script 入口 | 新脚本可能绕过统一 `marketdata` CLI | `marketdata rqdata hk-depth -- ...` | 下游任务统一改用 `marketdata rqdata hk-depth -- ...` 后删除旧别名 | retained; downstream usage unknown | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `rqdata-hk-assets` | 中国香港市场 assets 旧 console script 入口 | 新脚本可能绕过统一 `marketdata` CLI | `marketdata rqdata hk-assets -- ...` | 下游任务统一改用 `marketdata rqdata hk-assets -- ...` 后删除旧别名 | retained; downstream usage unknown | `scripts/dev/compatibility_governance.py --check`; console smoke test in `tests/test_hk_depth.py` |
| HK release presets | 中国香港市场历史发布包复现，当前位于 `configs/presets/release/*.yml` | 历史 snapshot 名称仍需维护清理 | 当前发布 preset 与归档清单 | 定期归档过期 preset，只保留仍需复现或发布的配置 | retained; periodic review required | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `cstree data ...` | 下游研究仓库保留的标准层 catalog/materialize/query 兼容入口 | 用户可能继续误以为标准层生成归属研究仓库 | `marketdata data ...` | 下游脚本统一改用 `marketdata data ...` 后删除 wrapper | retained downstream wrapper | `cross-sectional-trees/scripts/dev/data_ops_boundary.py --check`; wrapper smoke in `cross-sectional-trees/tests/test_data_warehouse.py` |
| `cstree universe ...` | 下游研究仓库保留的中国香港市场 universe asset builder 兼容入口 | 用户可能继续在研究仓库维护 platform-owned universe 资产 | `marketdata rqdata hk-assets -- ...` 或平台中国香港市场 universe builder 模块 | 下游脚本统一改用平台入口后删除 wrapper | retained downstream wrapper | `cross-sectional-trees/scripts/dev/data_ops_boundary.py --check`; wrapper smoke in `cross-sectional-trees/tests/test_universe_tools.py` |
| `cstree backup-data` | 下游研究仓库保留的本地 snapshot 兼容入口 | 名称容易和平台数据资产备份混淆 | `marketdata backup-data` | 下游脚本统一改用 `marketdata backup-data` 后删除 wrapper 或改名为 research snapshot helper | retained downstream wrapper | `cross-sectional-trees/scripts/dev/data_ops_boundary.py --check`; wrapper smoke in `cross-sectional-trees/tests/test_backup_data.py` |
| `python -m cstree.research.hk_intraday_download` | 下游研究仓库保留的旧中国香港市场 intraday 下载模块路径 | 用户可能绕过 `marketdata rqdata refresh-hk-intraday` | `marketdata rqdata refresh-hk-intraday` | 下游脚本统一改用平台入口后删除 wrapper | retained downstream wrapper | `cross-sectional-trees/scripts/dev/data_ops_boundary.py --check`; wrapper smoke in `cross-sectional-trees/tests/test_hk_intraday_download.py` |

## 维护规则

1. 新代码优先使用 `marketdata`、`market_data_platform` 和
   `market_data_platform.providers.*`。
1. 新增兼容层时必须写入本表，说明用途和清理条件。
1. 迁移类命令不应继续承载新的业务能力；新能力应进入平台原生工作流。
1. 删除兼容项前先做 repo-local `rg` 审计，并确认下游脚本已经切换。
1. 下游研究仓库 wrapper 只用于兼容，不能承载新的下载、健康检查、
   current refresh、registry 或数据资产发布实现。

## 静态检查债务

当前 Ruff / Pyright 仍有目录级排除。常规检查是阻塞门禁：

```bash
uv run python -m pytest
uv run python -m ruff check .
uv run python -m pyright
```

债务可见性检查不作为阻塞门禁，但每次重构前后都建议跑：

```bash
uv run --extra dev python scripts/dev/quality_debt.py
uv run --extra dev python scripts/dev/quality_debt.py --complexity
uv run --extra dev python scripts/dev/maintainability_metrics.py
uv run --extra dev python scripts/dev/compatibility_governance.py --check
uv run --extra dev python scripts/dev/architecture_governance.py --check
```

治理优先级：

1. 先恢复低风险文件的 Ruff 覆盖，避免继续扩大 `extend-exclude`。
1. 将目录级排除收窄成 per-file ignore，并写清楚保留原因。
1. 对 contracts、paths、manifest、registry、current assets 这类边界模块优先提高
   Pyright 覆盖。
