# 迁移计划

## 阶段一：共享数据契约 (Shared Contract)

**当前状态**：进行中

- 创建当前代码仓库。
- 现有项目中的数据处理实现逻辑保持不变。
- 使用统一的数据产物根目录 (artifacts root)。
- 在当前数据契约中，注册逐笔深度 (tick-depth) 和执行成本 (execution-cost) 的数据资产标识 (asset keys)。

## 阶段二：将逐笔深度数据资产发布至统一根目录

- 配置 `rqdata-hk-depth-snapshots emit-asset` 命令，使其在统一的根目录下生成并写入正式的原始数据 (raw) 和日频数据 (daily) 资产。
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

在下游调用方完全迁移完毕之前，暂时在 `cross-sectional-trees` 中保留兼容适配层 (compatibility wrappers)。

## 阶段四：确立策略层的只读边界

`cross-sectional-trees` 项目未来应仅需依赖以下内容：

- 数据产物根目录
- `hk_current.json` / `cn_current.json` 文件
- 已解析的资产路径 (resolved asset paths)
- 数据资产清单 (asset manifests)

该项目将彻底剥离底层逻辑，不再负责数据源资产的更新拉取、注册表的生成以及发布的打包逻辑。
