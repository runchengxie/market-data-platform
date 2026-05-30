# AGENTS.md

## 项目范围

本仓库用于维护多市场数据共享控制面。量化策略代码和海量市场行情数据不进入本仓库。

本仓库的核心职责包括：

- 定义中国香港市场和中国大陆市场的数据资产标识、数据契约及注册规范。
- 统一规范 RQData、TuShare、PIT、估值、行业、股票池、日内、Tick 级深度盘口等数据资产的共享目录结构。
- 维护平台内的数据健康检查、数据对账、打包构建和版本发布工作流。

## 数据管理规范

- **严禁**提交以下文件至代码库：Parquet 数据文件、压缩包分卷、数据源缓存、运行输出结果、分析报告、本地密钥和凭证。
- 大体积数据文件应统一存放在共享产物根目录、NAS、对象存储，或作为 Release 附件托管。
- Git 仓库只用于版本控制代码、文档、数据结构定义、小型测试数据集以及数据迁移记录。

## 市场称谓与表述口径

文档、注释、报错信息和面向用户的说明文字应使用清晰、稳妥的市场称谓：

- 优先写“中国香港市场”“港股”“港股通”“中国大陆市场”“A 股”等表述。
- 避免把中国大陆市场与中国香港市场写成政治或地域对立关系。
- 面向用户的正文先写业务含义；命令、路径、配置键、资产键和 provider API 示例只用于说明现有接口。
- 文档润色不要顺手重命名公开接口、路径或历史产物；命名变更应单独评估兼容影响。

## 当前阶段

本项目已经承接港股 tick-depth 与中国香港市场 RQData 资产的主要业务实现。下游研究或交易系统应只读消费本仓库发布的数据资产。新的数据生产、检查、current refresh 和发布修复应进入本仓库。

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
uv run --extra dev python scripts/dev/maintainability_metrics.py
uv run --extra dev python scripts/dev/compatibility_governance.py --check
uv run --extra dev python scripts/dev/architecture_governance.py --check
```

需要运行 DuckDB 查询时，额外安装查询依赖：

```bash
uv sync --extra dev --extra duckdb
```
