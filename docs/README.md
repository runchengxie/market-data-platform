# Market 数据平台文档

若需将本代码库作为 HK / CN 共享数据控制面（Control Plane）使用，请由此开始阅读。

## 文档导航

| 主题内容 | 对应文档 |
| --- | --- |
| 共享路径与资产标识（Asset Key）数据契约 | `contracts.md` |
| 存量项目接入指南 | `integrations.md` |
| 分步迁移计划与执行顺序 | `migration-plan.md` |

## 当前执行准则

当前本代码库负责共享路径、资产键、current contract、dataset registry 和统一数据维护 CLI，并已包含 CN 的 RQData / TuShare 基础采集 MVP。HK tick-depth 的下载、健康检查、聚合、对账和打包实现已迁入 `market_data_platform.hk_depth`，可经 `marketdata rqdata hk-depth -- ...` 或兼容命令 `rqdata-hk-depth ...` 调用。HK 日线、PIT、估值、行业、intraday、资产健康巡检和发布工作流仍由 `cross-sectional-trees` 作为 `hk-assets` transition backend 承载，入口为 `marketdata rqdata hk-assets -- ...`。
