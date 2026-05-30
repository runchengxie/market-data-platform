# market-data-platform

面向量化研究和交易系统的市场数据资产平台。

它负责把市场数据的采集、检查、发布和读取入口统一起来，让下游项目不用各自维护一套数据下载脚本、目录约定和质量检查逻辑。大体量行情数据、缓存、报告和本地凭证不进入 Git。

正文使用“中国香港市场 / 港股”和“中国大陆市场 / A 股”等业务称谓。A 股相关命令使用 `a-share`，路径、配置和资产键使用 `a_share`。

## 这个项目做什么

日常可以把它理解成共享数据控制面：

```text
provider 数据 -> 平台资产目录 -> current contract -> 下游只读消费
```

核心职责是：

- 定义共享数据根目录、资产键、manifest 和 current contract。
- 维护中国香港市场 RQData 资产、港股 tick-depth，以及中国大陆市场 RQData / TuShare 基础采集入口。
- 生成 `metadata/current_assets/hk_current.json`、`metadata/current_assets/a_share_current.json` 和 `metadata/dataset_registry.csv`。
- 提供数据目录查看、标准层物化、DuckDB 查询、本地快照备份和质量治理脚本。

下游研究、回测、交易或报表系统应优先读取 current contract，避免直接扫描某个 `latest` 目录。

## 快速开始

```bash
uv sync --extra dev
cp .envrc.example .envrc
cp .env.example .env.local
direnv allow
```

本地开发可以先使用仓库内的 `artifacts/`。共享环境建议显式指定平台数据根目录：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

真实 provider credentials 写入未跟踪的 `.env.local`，或放在：

```text
~/.config/market-data-platform/secrets.env
```

几个最小检查命令：

```bash
marketdata --help
marketdata paths --market hk
marketdata contract build --market hk --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata registry build --artifacts-root "$DATA_PLATFORM_ROOT"
```

## 最重要的概念

`DATA_PLATFORM_ROOT` 是共享市场数据产物根目录。平台工具向这里写入正式资产，下游项目从这里读取已发布数据。

```text
<artifacts_root>/
  assets/
  metadata/
    current_assets/
    dataset_registry.csv
  reports/
  standardized/
```

current contract 是下游读取数据版本的稳定入口：

```text
<artifacts_root>/metadata/current_assets/<market>_current.json
```

详细路径、资产键和 manifest 规则见 [docs/contracts.md](docs/contracts.md)。

## 常用入口

```bash
marketdata rqdata hk-assets -- --help
marketdata rqdata hk-depth -- --help
marketdata rqdata refresh-hk-current --help
marketdata data catalog --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata data query --artifacts-root "$DATA_PLATFORM_ROOT" --sql "select 1 as value"
marketdata backup-data --help
```

中国大陆市场 TuShare 入口需要可选依赖和 token：

```bash
uv sync --extra dev --extra tushare
marketdata tushare verify-token
```

日常操作说明见 [docs/operations.md](docs/operations.md)，数据目录与查询见 [docs/data-warehouse.md](docs/data-warehouse.md)。

## 下游边界

- `market-data-platform`：生产、检查和发布共享市场数据资产。
- `cross-sectional-trees`：只读消费平台资产，做研究、回测和目标持仓导出。
- `quant-execution-engine`：读取标准 `targets.json`，负责 dry-run、风控、执行和审计。

下游接入方式见 [docs/integrations.md](docs/integrations.md)。

## 文档导航

- 共享路径和数据契约：[docs/contracts.md](docs/contracts.md)
- 中国香港市场数据资产维护：[docs/hk-assets.md](docs/hk-assets.md)
- Provider 凭证、备份和本地开发：[docs/operations.md](docs/operations.md)
- 标准层与 DuckDB 查询：[docs/data-warehouse.md](docs/data-warehouse.md)
- 下游系统接入：[docs/integrations.md](docs/integrations.md)
- 兼容层说明：[docs/compatibility.md](docs/compatibility.md)
- 质量和维护性治理：[docs/quality-governance.md](docs/quality-governance.md)

如果不知道先读哪一页，从 [docs/README.md](docs/README.md) 开始。
