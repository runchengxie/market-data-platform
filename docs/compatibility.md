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
| `marketdata migration import-cross-artifacts` | 从旧仓库导入历史平台资产 | 一次性迁移命令留在主 CLI | 归档文档或内部迁移脚本 | 迁移归档完成后移到 archive 文档或内部脚本 | retained; archive candidate | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `rqdata-hk-depth` / `rqdata-tick` | HK depth 旧 console script 入口 | 新脚本可能绕过统一 `marketdata` CLI | `marketdata rqdata hk-depth -- ...` | 下游任务统一改用 `marketdata rqdata hk-depth -- ...` 后删除旧别名 | retained; downstream usage unknown | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |
| `rqdata-hk-assets` | HK assets 旧 console script 入口 | 新脚本可能绕过统一 `marketdata` CLI | `marketdata rqdata hk-assets -- ...` | 下游任务统一改用 `marketdata rqdata hk-assets -- ...` 后删除旧别名 | retained; downstream usage unknown | `scripts/dev/compatibility_governance.py --check`; console smoke test in `tests/test_hk_depth.py` |
| HK release presets | 历史发布包复现，当前位于 `configs/presets/release/*.yml` | 历史 snapshot 名称仍需维护清理 | 当前发布 preset 与归档清单 | 定期归档过期 preset，只保留仍需复现或发布的配置 | retained; periodic review required | `scripts/dev/compatibility_governance.py --check`; usage counts in `docs/maintenance-audit.md` |

## 维护规则

1. 新代码优先使用 `marketdata`、`market_data_platform` 和
   `market_data_platform.providers.*`。
1. 新增兼容层时必须写入本表，说明用途和清理条件。
1. 迁移类命令不应继续承载新的业务能力；新能力应进入平台原生工作流。
1. 删除兼容项前先做 repo-local `rg` 审计，并确认下游脚本已经切换。

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
