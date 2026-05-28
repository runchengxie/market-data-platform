# 质量治理与维护债务

本页记录 Ruff / Pyright 覆盖、维护性指标、兼容层生命周期和架构边界的本地治理命令。
这些检查的目标是让历史债务可见，并阻止新增债务继续扩大；它们不是要求一次性修完
所有迁移遗留问题。

## 常规门禁

常规门禁仍然是：

```bash
uv run --extra dev python -m pytest
uv run --extra dev python -m ruff check .
uv run --extra dev python -m pyright
```

## 债务可见性

静态检查覆盖和非阻塞 Ruff debt：

```bash
uv run --extra dev python scripts/dev/quality_debt.py
uv run --extra dev python scripts/dev/quality_debt.py --complexity
uv run --extra dev python scripts/dev/quality_debt.py --json --skip-ruff
```

维护性指标：

```bash
uv run --extra dev python scripts/dev/maintainability_metrics.py
uv run --extra dev python scripts/dev/maintainability_metrics.py --markdown
```

兼容层和架构边界：

```bash
uv run --extra dev python scripts/dev/compatibility_governance.py --check
uv run --extra dev python scripts/dev/architecture_governance.py --check
```

## Baseline 更新

只有在变化是有意接受或有意改善时才更新 baseline：

```bash
uv run --extra dev python scripts/dev/quality_debt.py --skip-ruff --write-baseline
uv run --extra dev python scripts/dev/quality_debt.py --skip-ruff --check-baseline

uv run --extra dev python scripts/dev/maintainability_metrics.py --write-baseline
uv run --extra dev python scripts/dev/maintainability_metrics.py --check-baseline
```

`--check-baseline` 会阻止以下回退：

- Ruff / Pyright checked source lines 下降。
- Ruff / Pyright excluded source lines 上升。
- Ruff / Pyright source exclude 列表新增但 baseline 未更新。
- 大文件、长函数、超长参数列表或 public facade export 数量超过已接受 baseline。

## 分阶段准入计划

当前优先级：

1. Ruff：先移出低风险单文件 exclude。第一批已将 `src/market_data_platform/symbols.py`
   纳入 Ruff 覆盖。
1. Ruff：继续处理 `data_provider_contracts.py`、`rqdata_cli_common.py`、
   `config_utils.py` 这类边界模块，把目录级排除收窄成具体文件问题。
1. Pyright：优先处理 contracts、paths、manifest、registry、current assets 等边界模块。
1. Pyright：provider contract 使用 `Protocol`、`TypedDict` 或 dataclass 稳定接口后再扩大覆盖。
1. HK assets / HK depth / release workflows：先通过 maintainability metrics 锁定长函数，
   再按 plan/config、fetch、transform、validate、persist、manifest、report 拆分。

## 兼容层规则

新增 console script alias、旧 import re-export 或 migration-only command 前，必须先更新
`docs/compatibility.md`，记录用途、风险、推荐替代、清理条件、当前状态和审计证据。
迁移类命令不承载新的平台业务能力；新能力应进入原生 `marketdata rqdata ...` 工作流。
