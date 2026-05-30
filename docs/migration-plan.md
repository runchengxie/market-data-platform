# 迁移记录与后续清理计划

## 阶段一：共享数据契约 (Shared Contract)

**当前状态**：已建立 contract / registry / provider 基础能力，港股 tick-depth 和中国香港市场 assets 生产实现已由平台维护；下游系统已进入只读消费边界，旧的 tick-depth 快照仓库不再作为本项目依赖维护。

- 创建当前代码仓库。
- 现有项目中的数据处理实现逻辑保持不变。
- 使用统一的数据产物根目录 (artifacts root)。
- 在当前数据契约中，注册逐笔深度 (tick-depth) 和执行成本 (execution-cost) 的数据资产标识 (asset keys)。

## 阶段二：将逐笔深度数据资产发布至统一根目录

- 使用 `marketdata rqdata hk-depth -- emit-asset` 或 `rqdata-hk-depth emit-asset`，在统一的根目录下生成并写入正式的原始数据 (raw) 和日频数据 (daily) 资产。
- 引入 `tick_depth_raw` 和 `tick_depth_daily` 数据，重新生成 `hk_current.json` 文件。
- 将健康度检查报告和数据核对（对账）报告统一存放在 `reports/` 目录下。

## 阶段三：迁移控制面代码

优先将以下通用代码迁移至本仓库：

- 当前数据契约的辅助函数 (helpers)
- 数据集注册表的辅助函数
- 数据清单摘要 (manifest summary) 的辅助函数
- 公用的数据健康度策略
- 打包与发布流程的元数据规范
- 中国大陆市场 instruments / daily 的 RQData 镜像命令
- 中国大陆市场 instruments / trade calendar / daily / adj-factor / daily-basic / limit-status 的 TuShare
  基础镜像命令
- 共享 `.envrc.example` / `.env.example` provider secret 契约
- 港股 depth 与中国香港市场 RQData asset 的平台统一原生入口：
  `marketdata rqdata hk-depth -- ...` / `marketdata rqdata hk-assets -- ...`

`marketdata migration status` 会区分已在平台内实现的原生工作流和仍由历史相邻仓库提供实现的过渡后端。当前 `hk-depth` 与 `hk-assets` 都是原生工作流，没有过渡后端。

## 阶段三点五：吸收兼容仓库

- 旧的 `rqdata-hk-depth-snapshots` 已不再作为本项目依赖追踪。
- workspace 不支持 `rqdata_tick_data.*` 旧 Python import 路径。
- 新的下载、健康检查、聚合、对账和发布修复只进入 `market-data-platform`。

## 阶段三点六：迁移中国香港市场 assets 生产实现

本阶段已完成以下迁移：

1. 搬迁 provider/runtime、manifest、asset IO、shared path helpers。
2. 搬迁 daily、valuation、dated assets、financial/PIT、industry 和 intraday mirror。
3. 搬迁 asset health、current health、coverage、quality gate 和 audit。
4. 搬迁 package/release/current refresh workflow。

后续工作是把迁移来的平台代码逐步纳入平台原生 lint/type 检查，减少长期排除。

## 阶段四：确立策略层的只读边界

下游研究或交易系统未来应仅需依赖以下内容：

- 数据产物根目录
- `hk_current.json` / `a_share_current.json` 文件
- 已解析的资产路径 (resolved asset paths)
- 数据资产清单 (asset manifests)

下游项目只保留自己的策略、交易或报表逻辑，数据源资产更新、注册表生成和发布打包逻辑由本仓库维护。

历史遗留在下游研究仓库 `artifacts` 目录下的数据平台产物可通过
`marketdata migration import-cross-artifacts` 复制到 `market-data-platform/artifacts`。
默认不落盘；加 `--apply` 后才执行复制，并写入
`metadata/migration/cross_artifacts_import_*.json` 清单。该流程只迁移平台产物
（资产、metadata、intraday cache、release、中国香港市场 health/audit 报告），研究 runs、sweeps、
live runs、exports、benchmark attribution 和 slippage calibration 继续留在策略仓库。
