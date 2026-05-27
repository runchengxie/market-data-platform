# 迁移计划

## 阶段一：共享数据契约 (Shared Contract)

**当前状态**：已建立 contract / registry / provider 基础能力，HK tick-depth 已迁入平台，HK assets 迁移进行中

- 创建当前代码仓库。
- 现有项目中的数据处理实现逻辑保持不变。
- 使用统一的数据产物根目录 (artifacts root)。
- 在当前数据契约中，注册逐笔深度 (tick-depth) 和执行成本 (execution-cost) 的数据资产标识 (asset keys)。

## 阶段二：将逐笔深度数据资产发布至统一根目录

- 使用 `marketdata rqdata hk-depth -- emit-asset` 或 `rqdata-hk-depth emit-asset`，在统一的根目录下生成并写入正式的原始数据 (raw) 和日频数据 (daily) 资产。
- 引入 `tick_depth_raw` 和 `tick_depth_daily` 数据，重新生成 `hk_current.json` 文件。
- 将健康度检查报告和数据核对（对账）报告统一存放在 `reports/` 目录下。

## 阶段三：迁移控制面 (Control Plane) 代码

优先将以下通用代码迁移至本仓库：

- 当前数据契约的辅助函数 (helpers)
- 数据集注册表的辅助函数
- 数据清单摘要 (manifest summary) 的辅助函数
- 公用的数据健康度策略
- 打包与发布流程的元数据规范
- CN instruments / daily 的 RQData 镜像命令
- CN instruments / trade calendar / daily / adj-factor / daily-basic / limit-status 的 TuShare
  基础镜像命令
- 共享 `.envrc.example` / `.env.example` provider secret 契约
- HK depth 原生入口与 HK RQData asset 的平台统一 transition 入口：
  `marketdata rqdata hk-depth -- ...` / `marketdata rqdata hk-assets -- ...`

`marketdata migration status` 会区分已在平台内实现的 `native` 工作流和仍由 sibling
repo 提供实现的 `transition_backend`。当前 `hk-depth` 是 `native`；`hk-assets`
仍由 `cross-sectional-trees` 提供 transition backend。`rqdata-hk-depth-snapshots`
保留历史包名、命令名、配置和执行记录，兼容入口委托平台内实现。

## 阶段三点五：吸收兼容仓库

- `rqdata-hk-depth-snapshots` 的 Python 模块保留 `rqdata_tick_data.*` import 路径。
- 兼容模块委托 `market_data_platform.hk_depth.*`，避免 depth 业务实现双写。
- 历史文档、执行记录和 universe 切片配置继续保留到归档策略确定。
- 新的下载、健康检查、聚合、对账和发布修复只进入 `market-data-platform`。

## 阶段三点六：迁移 HK assets 生产实现

后续按以下顺序迁移 `cross-sectional-trees` 中的 HK 数据维护代码：

1. 搬迁 provider/runtime、manifest、asset IO、shared path helpers。
2. 搬迁 daily、valuation、dated assets、financial/PIT、industry 和 intraday mirror。
3. 搬迁 asset health、current health、coverage、quality gate 和 audit。
4. 搬迁 package/release/current refresh workflow。
5. 将 `cross-sectional-trees` 的 `cstree rqdata ...` 保留为只读研究侧兼容入口或移除。

## 阶段四：确立策略层的只读边界

`cross-sectional-trees` 项目未来应仅需依赖以下内容：

- 数据产物根目录
- `hk_current.json` / `cn_current.json` 文件
- 已解析的资产路径 (resolved asset paths)
- 数据资产清单 (asset manifests)

该项目将彻底剥离底层逻辑，不再负责数据源资产的更新拉取、注册表的生成以及发布的打包逻辑。
