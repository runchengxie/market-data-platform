# 市场数据平台文档

若需将本代码库作为 HK / CN 共享数据控制面使用，请由此开始阅读。

## 文档导航

| 主题内容 | 对应文档 |
| --- | --- |
| 共享路径、资产键和数据契约 | `contracts.md` |
| HK 数据资产维护入口 | `hk-assets.md` |
| 数据目录、标准层与 DuckDB 查询 | `data-warehouse.md` |
| 存量项目接入指南 | `integrations.md` |
| 迁移记录与后续清理计划 | `migration-plan.md` |
| 兼容层、迁移入口与质量债务清理计划 | `compatibility.md` |
| Ruff / Pyright 覆盖、baseline 和维护性治理 | `quality-governance.md` |
| 当前维护性审计快照 | `maintenance-audit.md` |

## 当前执行准则

当前本代码库负责共享路径、资产键、当前数据契约、数据集注册表和统一数据维护 CLI，并已包含 CN 的 RQData / TuShare 基础采集 MVP。HK tick-depth 的下载、健康检查、聚合、对账和打包实现位于 `market_data_platform.hk_depth`，可经 `marketdata rqdata hk-depth -- ...` 或兼容命令 `rqdata-hk-depth ...` 调用。HK 日线、PIT、估值、行业、日内、资产健康巡检、current refresh 和发布工作流位于 `market_data_platform.hk_assets` 与 `market_data_platform.release_tools`，入口为 `marketdata rqdata hk-assets -- ...` 或 `rqdata-hk-assets ...`。

本地快照备份入口为 `marketdata backup-data`。它可冻结 cache、universe、配置文件，或按 `hk_current.json` 备份当前 HK 数据资产集合。
