# AGENTS.md

## 项目范围

本仓库用于维护多市场数据共享控制面。它不是量化策略代码库，请勿将海量市场行情数据提交到 Git。

本仓库的核心职责包括：

- 定义 HK / CN 资产标识、数据契约及注册规范。
- 统一规范 RQData、TuShare、PIT、估值、行业、股票池、日内、Tick 级深度盘口等数据资产的共享目录结构。
- 承接从现有项目迁移来的工作流，包括数据健康检查、数据对账、打包构建和版本发布。

## 数据管理规范

- **严禁**提交以下文件至代码库：Parquet 数据文件、压缩包分卷、数据源缓存、运行输出结果、分析报告、本地密钥和凭证。
- 大体积数据文件应统一存放在共享产物根目录、NAS、对象存储，或作为 Release 附件托管。
- Git 仓库只用于版本控制代码、文档、数据结构定义、小型测试数据集以及数据迁移记录。

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

需要运行 DuckDB 查询时，额外安装查询依赖：

```bash
uv sync --extra dev --extra duckdb
```
