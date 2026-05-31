# A 股 TuShare Backfill Handoff - 2026-05-31

## Completion Update

The interrupted backfill was resumed later on 2026-05-31. The medium-window raw
history and derived clean snapshot are now complete and validated.

Validated window:

```text
20240101 to 20260529
```

Final raw snapshot summary:

| Dataset | Status | Rows | Symbols | Partitions | Failed segments |
| --- | --- | ---: | ---: | ---: | ---: |
| `daily` | `completed` | 3,128,352 | 5,611 | 580 | 0 |
| `adj_factor` | `completed` | 3,147,074 | 5,634 | 580 | 0 |
| `daily_basic` | `completed` | 3,128,352 | 5,611 | 580 | 0 |
| `limit_status` | `completed` | 4,115,535 | 7,771 | 580 | 0 |

The completed clean snapshot is:

```text
/home/richard/data/market-data-platform/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily_clean
```

Clean build summary:

```text
status: completed
rows: 3128352
symbols: 5611
files: 5611
duplicate_rows: 0
missing_tr_close: 0
```

The stronger clean validation passed with both valuation and limit-status
columns required:

```bash
.venv/bin/marketdata tushare validate-a-share-daily-clean \
  --daily-clean-dir /home/richard/data/market-data-platform/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily_clean \
  --min-rows 3000000 --min-symbols 5000 \
  --require-valuation --require-limit-status
```

Validation output:

```text
status: passed
rows: 3128352
symbols: 5611
duplicate_rows: 0
```

A matching dated trading-calendar asset was also mirrored:

```text
/home/richard/data/market-data-platform/assets/tushare/a_share/trade_cal/a_share_trade_cal_20240101_20260529.parquet
rows: 880
open_dates: 580
status: completed
```

## Publication Completed

Do not rerun the raw history download unless a new gap is found.

The repository now exposes canonical A 股 universe build and validation
commands:

```bash
.venv/bin/marketdata tushare build-a-share-universe
.venv/bin/marketdata tushare validate-a-share-universe
```

The completed clean history was used to replace the five-day sample universe.
The previous sample files were archived under:

```text
/home/richard/data/market-data-platform/metadata/archive/universe/a_share_sample_20260109_pre_backfill_20260531/
```

The canonical universe outputs are:

```text
/home/richard/data/market-data-platform/assets/universe/a_share_all_full_by_date.csv
/home/richard/data/market-data-platform/assets/universe/a_share_all_full_symbols.txt
/home/richard/data/market-data-platform/assets/universe/a_share_all_full_by_date.meta.yml
```

Universe build summary:

```text
rows: 151105
symbols_seen: 5611
symbols_selected: 5582
latest_symbols: 5499
trade_dates: 580
rebalance_dates_requested: 29
rebalance_dates: 28
first_rebalance_date: 20240229
last_rebalance_date: 20260529
duplicate_rows: 0
```

The first requested month end is intentionally excluded because the configured
30-trading-day minimum history window is not yet available.

Latest aliases now point to the completed `20240101` to `20260529` snapshots for
`daily`, `daily_clean`, `adj_factor`, `daily_basic`, `limit_status`, and
`trade_cal`.

The prior sample current contract was archived as:

```text
/home/richard/data/market-data-platform/metadata/archive/current_assets/a_share_current_20260109_sample_pre_backfill_20260531.json
```

The canonical current contract has been rebuilt at:

```text
/home/richard/data/market-data-platform/metadata/current_assets/a_share_current.json
```

The current-contract health gate passed with ten assets checked, zero missing
assets, zero stale assets, and zero issues. The JSON report is:

```text
/home/richard/data/market-data-platform/reports/a_share_current_health_20260529.json
```

## Original Handoff State

This note is for the next session. The active task is the medium-window A 股 TuShare raw history backfill into the external data root:

```text
/home/richard/data/market-data-platform
```

The target window is:

```text
20240101 to 20260529
```

Use 2026-05-29 because 2026-05-31 is a Sunday and 2026-05-29 is the latest completed weekday in this session.

## Do Not Start A Parallel Writer First

At handoff time, a backfill process was still running in the background:

```bash
ps -eo pid,ppid,stat,etime,pcpu,pmem,args | rg 'backfill-a-share-history|PID'
```

Observed process:

```text
PID 20728
.venv/bin/marketdata tushare backfill-a-share-history \
  --artifacts-root /home/richard/data/market-data-platform \
  --start-date 20240101 --end-date 20260529 \
  --dataset daily --dataset adj_factor --dataset daily_basic --dataset limit_status \
  --segment month --continue-on-error
```

Before doing anything else, check whether PID `20728` or another `backfill-a-share-history` process is still running. Do not start another writer against the same snapshot directories while it is active.

## Download Progress At Handoff

The first two attempts stopped on transient local proxy read timeouts:

```text
daily 20240801-20240831 timeout
daily 20241101-20241130 timeout
```

Then the command was restarted with `--continue-on-error` so later months and datasets could keep progressing.

Observed partition counts at handoff:

```text
daily:        562 part.parquet files
adj_factor:   495 part.parquet files
daily_basic:  575 part.parquet files
limit_status: 0 files; directory had not appeared yet
```

Commands used to check:

```bash
find /home/richard/data/market-data-platform/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily/data -name part.parquet | wc -l
find /home/richard/data/market-data-platform/assets/tushare/a_share/adj_factor/a_share_all_20240101_20260529_adj_factor/data -name part.parquet | wc -l
find /home/richard/data/market-data-platform/assets/tushare/a_share/daily_basic/a_share_all_20240101_20260529_daily_basic/data -name part.parquet | wc -l
find /home/richard/data/market-data-platform/assets/tushare/a_share/limit_status/a_share_limit_status_20240101_20260529/data -name part.parquet | wc -l
```

Manifests present at handoff:

```text
/home/richard/data/market-data-platform/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily/manifest.yml
/home/richard/data/market-data-platform/assets/tushare/a_share/adj_factor/a_share_all_20240101_20260529_adj_factor/manifest.yml
/home/richard/data/market-data-platform/assets/tushare/a_share/daily_basic/a_share_all_20240101_20260529_daily_basic/manifest.yml
```

`limit_status` manifest was not present yet at handoff.

## Original Recommended Recovery Steps

1. Check whether the existing backfill process is still running.

2. If it is still running, wait for it to finish and then summarize manifests. Do not interrupt unless the user explicitly asks.

3. If it finished with failures, rerun the same command. The command defaults to skip existing `trade_date` partitions, so it should only fill gaps:

```bash
.venv/bin/marketdata tushare backfill-a-share-history \
  --artifacts-root /home/richard/data/market-data-platform \
  --start-date 20240101 --end-date 20260529 \
  --dataset daily --dataset adj_factor --dataset daily_basic --dataset limit_status \
  --segment month --continue-on-error
```

4. After all four raw snapshots have `status: completed` and `segments_failed: 0`, build the new clean snapshot:

```bash
.venv/bin/marketdata tushare build-a-share-daily-clean \
  --daily-dir /home/richard/data/market-data-platform/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily \
  --adj-factor-dir /home/richard/data/market-data-platform/assets/tushare/a_share/adj_factor/a_share_all_20240101_20260529_adj_factor \
  --daily-basic-dir /home/richard/data/market-data-platform/assets/tushare/a_share/daily_basic/a_share_all_20240101_20260529_daily_basic \
  --limit-status-dir /home/richard/data/market-data-platform/assets/tushare/a_share/limit_status/a_share_limit_status_20240101_20260529 \
  --instruments-file /home/richard/data/market-data-platform/assets/tushare/a_share/instruments/a_share_all_instruments_latest.parquet \
  --out-dir /home/richard/data/market-data-platform/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily_clean \
  --min-rows 3000000 --min-symbols 5000
```

5. Validate clean before switching aliases:

```bash
.venv/bin/marketdata tushare validate-a-share-daily-clean \
  --daily-clean-dir /home/richard/data/market-data-platform/assets/tushare/a_share/daily/a_share_all_20240101_20260529_daily_clean \
  --min-rows 3000000 --min-symbols 5000 --require-limit-status
```

6. Only after raw and clean validation pass, update latest aliases and rebuild current contract. Do not switch current assets before validation.

## Recovery Runs Performed

The resumed session first confirmed that no host `backfill-a-share-history`
process remained. At that point the actual partition counts were:

```text
daily:        562
adj_factor:   495
daily_basic:  580
limit_status: 39
```

The first recovery run skipped the already complete `daily_basic` dataset:

```bash
.venv/bin/marketdata tushare backfill-a-share-history \
  --artifacts-root /home/richard/data/market-data-platform \
  --start-date 20240101 --end-date 20260529 \
  --dataset daily --dataset adj_factor --dataset limit_status \
  --segment month --continue-on-error
```

It completed `adj_factor`, advanced `daily` to 571 partitions, and advanced
`limit_status` to 576 partitions. Three transient local proxy timeouts remained.

The second recovery run retried only the incomplete datasets:

```bash
.venv/bin/marketdata tushare backfill-a-share-history \
  --artifacts-root /home/richard/data/market-data-platform \
  --start-date 20240101 --end-date 20260529 \
  --dataset daily --dataset limit_status \
  --segment month --continue-on-error
```

It completed both datasets with `segments_failed: 0`. The skip-existing
partition behavior worked as intended, so recovery did not rewrite completed
partitions.

## Notes

- `market-data-platform/.env.local` contains `TUSHARE_TOKEN`; the TuShare CLI now loads `.env.local` before token resolution.
- Token verification in this session returned `TUSHARE_TOKEN configured=true valid=true`.
- The backfill CLI was added earlier in this session and supports dry-run, month/year/all segmentation, skip-existing partitions, `--continue-on-error`, and optional `--sync-latest`.
- Previous small sample current contract for 20260105-20260109 had already been repaired and health inspected with `missing_assets=0`, `stale_assets=0`, `issue_count=0`.
