# market-data-platform

HK / CN 研究数据资产的共享控制面。

本仓库负责把市场数据的生产、检查、发布和读取入口统一起来：定义数据契约、资产路径、注册表、健康巡检和发布工作流。大体量行情数据、缓存、报告和本地凭证不进入 Git。

## 这个项目解决什么问题

研究仓库需要稳定读取同一套 HK / CN 市场数据。`market-data-platform` 将这些公共能力集中维护：

- 定义资产键、共享目录结构和 current contract。
- 维护 HK RQData assets、HK tick-depth、CN RQData / TuShare 基础采集入口。
- 生成 `hk_current.json` / `cn_current.json` 和 `dataset_registry.csv`，让下游按契约读取确定的数据版本。
- 提供数据目录、标准层物化、DuckDB 查询、本地快照备份和质量治理脚本。

`cross-sectional-trees` 是策略研究下游，只读消费本仓库发布的数据资产。

## 快速开始

```bash
uv sync --extra dev
cp .envrc.example .envrc
cp .env.example .env.local
direnv allow
```

默认 `.envrc.example` 会把 `DATA_PLATFORM_ROOT` 指到仓库内的 `artifacts/`，便于本地开发。共享环境建议显式指定一个平台数据根目录：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

真实 provider credentials 写入未跟踪的 `.env.local`，或放在：

```text
~/.config/market-data-platform/secrets.env
```

命令入口：

```bash
marketdata --help
marketdata paths --market hk
marketdata contract build --market hk --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata registry build --artifacts-root "$DATA_PLATFORM_ROOT"
```

## 核心概念

### Artifacts Root

`DATA_PLATFORM_ROOT` 是共享数据产物根目录。平台工具向这里写入正式资产，下游研究仓库从这里读取已发布数据。

```text
<artifacts_root>/
  assets/
    rqdata/
      hk/
      cn/
    tushare/
      cn/
    universe/
  metadata/
    current_assets/
    dataset_registry.csv
  reports/
  standardized/
```

完整路径和资产键规范见 [docs/contracts.md](docs/contracts.md)。

### Current Contract

下游代码应优先读取 current contract：

```text
<artifacts_root>/metadata/current_assets/<market>_current.json
```

contract 记录每个资产的别名路径、解析后的真实路径、manifest、业务日期和 provider。它是下游选择数据版本的稳定入口，避免业务代码直接扫描 `latest` 别名目录。

### Dataset Registry

```text
<artifacts_root>/metadata/dataset_registry.csv
```

registry 是面向人工排查的数据目录摘要，由 current contract 和 manifest 推导生成。读取路径时以 current contract 为准。

## 常用工作流

### HK 数据维护

HK 日线、PIT、估值、行业、股票池、日内、tick-depth、current refresh 和发布工具都走 `marketdata`：

```bash
marketdata rqdata hk-assets -- --help
marketdata rqdata hk-depth -- --help
marketdata rqdata refresh-hk-current --help
marketdata rqdata refresh-hk-intraday --help
marketdata rqdata refresh-hk-depth --help
marketdata rqdata refresh-hk-fundamentals --help
```

维护说明见 [docs/hk-assets.md](docs/hk-assets.md)。

### CN 数据采集

CN 支持 RQData 与 TuShare 基础采集入口。TuShare 相关命令需要安装可选依赖并配置 token：

```bash
uv sync --extra dev --extra tushare
marketdata tushare verify-token
```

操作示例见 [docs/operations.md](docs/operations.md)，资产契约见 [docs/contracts.md](docs/contracts.md)。

### 数据目录与查询

```bash
marketdata data catalog --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata data query --artifacts-root "$DATA_PLATFORM_ROOT" --sql "select 1 as value"
```

DuckDB 查询需要额外安装 `duckdb` 可选依赖。更多说明见 [docs/data-warehouse.md](docs/data-warehouse.md)。

### 本地备份

```bash
marketdata backup-data --preset hk_current --name hk_current_20260526
marketdata backup-data --include-path configs/presets/release/hk_current.yml
```

备份命令会写入 snapshot 目录和 `manifest.yml`，已有 snapshot 不会被覆盖。细节见 [docs/operations.md](docs/operations.md)。

## 项目结构

```text
market-data-platform/
  src/market_data_platform/      平台主包和 marketdata CLI
  src/market_data_platform/hk_assets/
                                HK RQData assets 维护实现
  src/market_data_platform/hk_depth/
                                HK tick-depth 下载、检查、聚合与发布
  src/hk_data_platform/          旧包名兼容层
  configs/presets/               发布与 universe preset
  docs/                          契约、操作、迁移和治理文档
  scripts/dev/                   质量、兼容和架构治理脚本
  tests/                         单元测试与治理测试
```

兼容入口 `hkdata`、`rqdata-hk-depth`、`rqdata-tick` 和 `rqdata-hk-assets` 仍保留；新脚本优先使用 `marketdata` 与 `market_data_platform`。兼容层状态见 [docs/compatibility.md](docs/compatibility.md)。

## 本地开发

```bash
uv run --extra dev python -m pytest
uv run --extra dev python -m ruff check .
uv run --extra dev python -m pyright
```

CI 还会运行质量债务和架构治理检查。开发命令清单见 [docs/operations.md](docs/operations.md)，当前维护性快照见 [docs/maintenance-audit.md](docs/maintenance-audit.md)。

## 阅读路线

从 [docs/README.md](docs/README.md) 开始。新人通常按这个顺序阅读：

1. [docs/contracts.md](docs/contracts.md)：理解 artifacts root、asset key、current contract。
1. [docs/hk-assets.md](docs/hk-assets.md)：了解 HK 数据生产和发布入口。
1. [docs/operations.md](docs/operations.md)：配置凭证、运行 CN/TuShare、备份和本地开发命令。
1. [docs/integrations.md](docs/integrations.md)：了解下游项目如何只读接入。
