# AGENTS.md

## 项目范围

本仓库专门用于维护多市场数据共享控制面（Control Plane）。这不是一个量化策略代码库，请勿将海量的市场行情数据直接提交到 Git 中。

本仓库的核心职责包括：

- 定义 HK / CN 资产标的数据契约（Data Contracts）及注册规范。
- 统一规范各类数据资产的共享目录结构，包含：米筐（RQData）日线、PIT（Point-in-Time / 截面快照）、估值、行业、股票池（Universe）、日内、Tick 级深度盘口，以及交易执行成本模型等数据。
- 承接从现有项目中迁移过来的各类工作流（Workflows），包括：数据健康度检查、数据对账、打包构建以及版本发布。

## 数据管理规范

- **严禁**提交以下文件至代码库：Parquet 数据文件、压缩包分卷、数据源缓存（Provider Caches）、运行输出结果、分析报告，以及本地密钥/凭证。
- 大体积数据文件应当统一存放在共享的构建产物目录（Artifacts Root）、NAS（网络附加存储）、对象存储，或是作为 Release 附件进行托管。
- Git 仓库应仅用于版本控制：代码、文档、数据结构定义（Schema）、小型测试数据集（Small Fixtures）以及数据迁移记录。

## 当前阶段

本项目已经承接 HK tick-depth 与 HK RQData assets 的主要业务实现。`cross-sectional-trees` 和 `rqdata-hk-depth-snapshots` 的旧入口进入兼容期；新的数据生产、检查、current refresh 和发布修复应优先进入本仓库。

## 常用命令

推荐的环境配置方式：

```bash
uv sync --extra dev
```

代码检查与测试：

```bash
uv run python -m pytest
uv run python -m ruff check .
uv run python -m pyright
```

质量债务可见性检查（非阻塞门禁）：

```bash
uv run --extra dev python scripts/dev/quality_debt.py
```
