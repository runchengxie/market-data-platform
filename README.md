# market-data-platform (多市场数据平台)

HK / CN 研究数据资产的共享控制平面（Shared control plane）。

本仓库是一个过渡项目，旨在将可复用的市场数据管理逻辑从各个策略仓库中解耦并剥离出来。未来，本仓库将集中统管数据契约（Contracts）、注册表规范（Registry conventions）、数据模式（Schema）、数据健康度巡检策略（Health policy）以及数据的打包与发布工作流。请注意，大体量的数据文件不会被纳入 Git 版本控制。

## 最终架构形态 (Target Shape)

```text
market-data-platform/
  daily（日频数据） / PIT（Point-in-Time数据） / valuation（估值） / industry（行业分类） / universe（标的池）
  intraday 5m（5分钟级日内数据）
  tick_depth raw（原始逐笔深度） / tick_depth_daily（日频逐笔深度）
  execution_cost_model（执行成本模型）
  current contract（当前数据契约） / dataset registry（数据集注册表） / health（健康度巡检） / reconcile（数据对账） / release（发布）

cross-sectional-trees/
  策略、特征、模型、回测、持仓
  作为 market-data-platform 的纯只读下游消费方

rqdata-hk-depth-snapshots/
  短期方案：独立的 tick-depth（逐笔深度）实现模块
  中期方案：将其作为 tick_depth 模块整合进 market-data-platform 中
```

## 第一阶段拆分边界 (Stage-1 Boundary)

目前，本仓库负责共享的数据契约和路径规范、CN 的 RQData / TuShare 基础采集
MVP，以及统一的数据维护 CLI 入口。HK 生产数据维护实现仍作为 transition
backend 保留在以下项目中，由 `marketdata rqdata ...` 转发执行：

- `cross-sectional-trees`：包含日频、PIT、估值、行业分类、标的池、当前数据契约、数据集注册表、数据健康度巡检及发布工具的实现。
- `rqdata-hk-depth-snapshots`：包含逐笔深度数据的下载、健康度巡检、日频聚合、数据对账及打包逻辑。

第一阶段的落地步骤是，将上述两个项目的数据输出指向同一个共享的产物根目录（Artifacts root）：

```bash
export DATA_PLATFORM_ROOT=/data/market-data-platform
```

`DATA_PLATFORM_ROOT` 是本仓库推荐使用的统一环境变量。`HK_DATA_PLATFORM_ROOT` 仍作为兼容旧 HK 调用方的 fallback 保留。只有当某个项目明确需要将其运行结果、缓存或报告等输出文件也集中放到该根目录下时，才需要使用 `CSTREE_ARTIFACTS_ROOT` 环境变量。通常情况下，各策略仓库应保持自己独立的输出目录，并统一通过 `DATA_PLATFORM_ROOT` 来读取已发布市场数据。

本地 provider credentials 以本仓库作为配置入口，但不得提交真实 secret。可复制
`.envrc.example` 为 `.envrc`，并将 token / 密码写入未跟踪的 `.env.local`，或写入
`~/.config/market-data-platform/secrets.env`：

```bash
cp .envrc.example .envrc
cp .env.example .env.local
direnv allow
```

`.env.example` 规定了 `TUSHARE_TOKEN`、`RQDATA_USERNAME`、`RQDATA_PASSWORD` 和
`RQDATA_URI` 等变量名；`.gitignore` 会阻止本地凭证文件进入 Git。

## 共享目录结构 (Shared Layout)

```text
<artifacts_root>/
  assets/
    rqdata/
      hk/
        daily/
        intraday/
        pit_financials/
        valuation/
        industry_changes/
        tick_depth/
        tick_depth_daily/
        execution_cost/
      cn/
        daily/
        pit_financials/
        valuation/
        industry/
        st_flags/
        suspend/
        limit_status/
    tushare/
      cn/
        instruments/
        trade_cal/
        daily/
        adj_factor/
        daily_basic/
        limit_status/
    universe/
  metadata/
    current_assets/
      hk_current.json
      cn_current.json
    dataset_registry.csv
  reports/
  standardized/
```

## 当前数据契约 (Current Contract)

共享的当前数据契约文件路径为：

```text
<artifacts_root>/metadata/current_assets/<market>_current.json
```

该文件记录了数据资产标识（asset keys）、别名路径（alias paths）、底层解析的绝对路径（resolved paths）、数据清单摘要（manifest summaries）以及数据业务日期（as-of dates）。各个策略仓库应当通过该契约文件来获取确定的底层数据路径进行读取，而不是通过扫描那些随时可能变动的 `latest` 别名目录来拉取数据。

常用命令：

```bash
marketdata paths --market cn
marketdata contract build --market cn --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata registry build --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata rqdata export-cn-instruments \
  --out "$DATA_PLATFORM_ROOT/assets/rqdata/cn/instruments/cn_all_instruments_latest.parquet"
```

## TuShare CN MVP

TuShare 是 CN 的并存 provider，不会替换现有 RQData 命令。安装 optional extra 后，以环境变量提供 token：

```bash
uv sync --extra dev --extra tushare
export TUSHARE_TOKEN=...

marketdata tushare verify-token
marketdata tushare export-cn-instruments \
  --out "$DATA_PLATFORM_ROOT/assets/tushare/cn/instruments/cn_all_instruments_latest.parquet"
marketdata tushare mirror-cn-trade-cal \
  --start-date 20260101 --end-date 20260526 \
  --out "$DATA_PLATFORM_ROOT/assets/tushare/cn/trade_cal/cn_trade_cal_latest.parquet"
marketdata tushare mirror-cn-daily \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/cn/daily/cn_all_20260101_20260526_daily"
marketdata tushare mirror-cn-adj-factor \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/cn/adj_factor/cn_all_20260101_20260526_adj_factor"
marketdata tushare mirror-cn-daily-basic \
  --start-date 20260101 --end-date 20260526 \
  --out-dir "$DATA_PLATFORM_ROOT/assets/tushare/cn/daily_basic/cn_all_20260101_20260526_daily_basic"
```

日频类 TuShare 镜像按开放交易日请求全市场并写入
`data/trade_date=YYYYMMDD/part.parquet`。完成数据校验并将 `*_latest`
alias 指向采用的 snapshot 后，使用以下命令发布当前 CN provider：

```bash
marketdata contract build --market cn --provider tushare \
  --artifacts-root "$DATA_PLATFORM_ROOT" --target-date 20260526
```

`marketdata tushare mirror-cn-stk-limit` 还可镜像 `stk_limit` 接口形成
`limit_status` raw 资产；`mirror-cn-limit-status` 是同一操作的兼容别名。当前 MVP
不包括 clean layer、修复、质量门禁或发布打包。

## HK 迁移入口

HK depth 与既有 HK RQData asset 代码尚未物理迁入本包。为先固定操作入口与 secret
运行上下文，平台提供 transition backend 调度：

```bash
marketdata migration status

marketdata rqdata hk-depth -- health --input <raw-depth-dir>
marketdata rqdata hk-depth -- aggregate-daily --input <raw-depth-dir> --output <daily.parquet>

marketdata rqdata hk-assets -- mirror-hk-daily <原 cstree rqdata 参数>
marketdata rqdata hk-assets -- build-hk-daily-clean-layer <原 cstree rqdata 参数>

marketdata migration sync-hk-links --artifacts-root "$DATA_PLATFORM_ROOT"
marketdata rqdata inspect-hk-current \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260526

marketdata rqdata refresh-hk-current \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260526 \
  --refresh-asset daily --refresh-asset daily_clean \
  --inspect-asset daily --inspect-asset daily_clean

marketdata rqdata refresh-hk-intraday \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --start-date 20260526 \
  --end-date 20260526

marketdata rqdata refresh-hk-depth \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --start-date 20260526 \
  --end-date 20260526 \
  --symbols-file "$DATA_PLATFORM_ROOT/assets/rqdata/hk/daily/hk_all_daily_clean_latest/symbols.txt" \
  --name hk_tick_depth_increment_20260526

marketdata rqdata refresh-hk-fundamentals \
  --artifacts-root "$DATA_PLATFORM_ROOT" \
  --target-date 20260526
```

在当前 workspace 中，调度器优先使用 sibling repo 的 `.venv/bin/python -m ...`
运行 backend 模块，避免依赖可能过期的 console-script shebang。在其他部署环境中，可通过
`MARKETDATA_HK_DEPTH_COMMAND` / `MARKETDATA_HK_ASSETS_COMMAND` 指定 backend
可执行命令。`refresh-hk-current` 是平台侧 HK current wrapper：它会先把
`cross-sectional-trees` 的过渡资产链接指向统一 artifacts root，再调用既有
HK refresh workflow，并在成功后由 `market-data-platform` 重新生成
`hk_current.json` 与 `dataset_registry.csv`，并同步一份 registry 给过渡期
`cross-sectional-trees`。`inspect-hk-current` 提供同一根目录下的 current contract
健康度检查。`refresh-hk-intraday`、`refresh-hk-depth` 和
`refresh-hk-fundamentals` 分别封装 5m 增量刷新、tick-depth download/health/aggregate/
publish、PIT patch 与 financial details 刷新，并同样在成功后重建 current contract。
这是迁移期间的兼容入口；物理源码迁移完成前，
`marketdata migration status` 会将两个 HK backend 标为 `transition_backend`。

`hkdata` 命令和 `hk_data_platform` Python 包名仍作为兼容层保留，新代码应优先使用 `marketdata` 和 `market_data_platform`。

## 本地开发 (Development)

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run pyright
```

关于数据契约的详细说明及迁移指南，请参阅 `docs/README.md`。
