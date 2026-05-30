# 市场数据平台文档

若需将本代码库作为独立的市场数据资产平台使用，请由此开始阅读。

平台当前主要覆盖中国香港市场数据（港股）和中国大陆市场数据（A 股）。文档正文尽量使用完整、清晰的中文表述；A 股相关命令使用 `a-share`，路径、配置和资产键使用 `a_share`。

## 新人阅读路线

1. 先读仓库根目录 `README.md`，确认项目定位、核心概念和常用入口。
1. 再读 `contracts.md`，理解共享数据根目录、资产键和 current contract。
1. 如果需要维护中国香港市场数据，继续读 `hk-assets.md`。
1. 如果需要配置 provider、运行中国大陆市场 TuShare 数据采集、备份或本地开发检查，读 `operations.md`。
1. 如果要接入下游研究、回测、交易或报表系统，读 `integrations.md`。

## 文档导航

| 主题内容 | 对应文档 |
| --- | --- |
| 共享路径、资产键和数据契约 | `contracts.md` |
| 中国香港市场数据资产维护入口 | `hk-assets.md` |
| Provider 凭证、中国大陆市场 / TuShare、备份和本地开发命令 | `operations.md` |
| 数据目录、标准层与 DuckDB 查询 | `data-warehouse.md` |
| 下游系统接入指南 | `integrations.md` |
| 迁移记录与后续清理计划 | `migration-plan.md` |
| 兼容层、迁移入口与质量债务清理计划 | `compatibility.md` |
| Ruff / Pyright 覆盖、baseline 和维护性治理 | `quality-governance.md` |
| 当前维护性审计快照 | `maintenance-audit.md` |

## 当前执行准则

当前本代码库负责共享路径、资产键、当前数据契约、数据集注册表和统一数据维护 CLI，并已包含中国大陆市场 RQData / TuShare 基础采集 MVP。港股 tick-depth 的下载、健康检查、聚合、对账和打包实现位于 `market_data_platform.hk_depth`，可经 `marketdata rqdata hk-depth -- ...` 或兼容命令 `rqdata-hk-depth ...` 调用。中国香港市场日线、PIT、估值、行业、日内、资产健康巡检、current refresh 和发布工作流位于 `market_data_platform.hk_assets` 与 `market_data_platform.release_tools`，入口为 `marketdata rqdata hk-assets -- ...` 或 `rqdata-hk-assets ...`。

本地快照备份入口为 `marketdata backup-data`。它可冻结 cache、universe、配置文件，或按 `hk_current.json` 备份当前中国香港市场数据资产集合。
