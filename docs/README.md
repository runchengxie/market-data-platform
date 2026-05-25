# HK 数据平台文档

若需将本代码库作为 HK 共享数据控制面（Control Plane）使用，请由此开始阅读。

## 文档导航

| 主题内容 | 对应文档 |
| --- | --- |
| 共享路径与资产标识（Asset Key）数据契约 | `contracts.md` |
| 存量项目接入指南 | `integrations.md` |
| 分步迁移计划与执行顺序 | `migration-plan.md` |

## 第一阶段（Stage-1）执行准则

目前，本代码库仅用于定义数据契约。生产环境的实际数据处理工具仍保留在 `cross-sectional-trees` 和 `rqdata-hk-depth-snapshots` 项目中。
