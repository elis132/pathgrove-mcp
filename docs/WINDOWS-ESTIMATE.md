# Windows indexing: measurements and a completion estimate

**Planning guess: approximately 75 minutes for the tested C: + D: scope, about 7.94 million entries. This is a simulation-informed estimate, not a completed full-drive run of the optimized version.** An optimistic synthetic extrapolation gives about 19 minutes; a quadratic scenario gives about 124 minutes. These scenarios are not confidence bounds or guarantees.

## Implementation

- SQLite schema 3 maintains exact per-root counts through insert/delete/move triggers in the metadata transaction. Existing caches are backfilled atomically once. Progress refreshes no longer repeatedly count every file. Rollbacks, repeated upserts and multiple processes preserve the counters. Filtered counts remain exact SQL queries.
- Search and aggregate requests use SQL without packed-name candidates when another thread owns the filename-cache lock. Status returns optional memory metrics as `null`, with `memory_metrics_available: false`, instead of waiting for that lock.
- Queueing, candidate preparation, primary SQL and fallback share an absolute deadline. Fallback receives no new full budget. Indexed content searches return partial coverage if the deadline expires before SQL opens. Timed-out workers retain their concurrency slots until they finish.

Root scoping, link exclusions, inaccessible-subtree retention and partial-result notices remain enforced. Experimental NTFS enumeration remains disabled.

## Measured results

Tests used the Windows machine described in [PERFORMANCE.md](PERFORMANCE.md). No optimized Windows full-drive completion is claimed.

### Real metadata count refresh

An isolated backup of the **4,793,676-entry** cache was migrated to schema 3. The source was opened read-only. Three alternating samples on that same backup compared the old scoped `COUNT(*)` with the sum of transactional per-root counters. Every count agreed.

| Operation | Median |
|---|---|
| Old full-index count refresh | 734.0965 ms |
| New counter refresh | 0.0538 ms |

The refresh operation was **13,644.93 times faster**, not the entire index build. Opening/migrating the copied cache took 0.872 seconds, excluding the backup copy. [Raw samples, SQL and source hashes](benchmarks/windows-count-refresh.json).

### Synthetic indexing growth

Separate processes inserted generated metadata into fresh temporary caches through the production `_row`/`_upsert` path, with normal filename-buffer publication and batches of up to 256 rows. Baseline source: [`1460ce9`](https://github.com/elis132/pathgrove-mcp/commit/1460ce942f6ccc18d197c10eaec63293916baaf3). Optimized module hashes are recorded in its report.

The workload uses 64 files per generated directory, fixed timestamps/sizes and ASCII names such as `document-invoice-00000000.txt`. It creates metadata rows, **not one million real files**. Each version has one growth run with cumulative checkpoints, not repeated-trial medians. OS caches were not flushed; the machine remained active.

| Metadata entries | Baseline elapsed | Optimized elapsed |
|---|---|---|
| 100,000 | 11.199 s | 11.298 s |
| 250,000 | 30.159 s | 26.154 s |
| 500,000 | 64.083 s | 57.222 s |
| 1,000,000 | 151.227 s | 114.416 s |

At one million entries, time fell by **24.34%**, approximately **1.32 times faster** for this workload. Count refreshes consumed 42.529 seconds before and 0.034 seconds after; filename snapshot work remained substantial. The first checkpoint was slightly slower, so improvement is not promised for every scope. Raw checkpoints and hashes: [before](benchmarks/windows-growth-before.json), [after](benchmarks/windows-growth-after.json).

## Estimate and assumptions

The target **7,940,100 entries** is the largest observed accessible-entry count from the independent Windows traversals. Their median **221.645 seconds** is used as a filename-discovery proxy; it does not measure all stat, indexing and watcher work.

The old real index reached 4,756,213 entries after 7,200.333 seconds without finishing. Projecting that progress to the target gives **200 minutes at constant throughput**, or **334 minutes with quadratic growth**. These are estimates, not measured completion times.

The helper fits `t(n) = a*n + b*n*n` to the last two synthetic checkpoints, with nonnegative coefficients. The optimized checkpoints are effectively linear. Extrapolated insertion plus discovery is **1,130 seconds (19 minutes)**. Pure quadratic scaling from the final checkpoint gives **7,435 seconds (124 minutes)**.

The old synthetic fit predicts only about 1,543 seconds at the observed 4.76-million-entry milestone, versus 7,200 seconds in reality: a **4.666-fold gap**. Applying that historical factor to optimized synthetic insertion, then adding discovery, gives **4,460 seconds (74 minutes)**, rounded to a **75-minute planning guess**.

This calibration assumes that the old real/synthetic gap transfers to the optimized code. That assumption is unvalidated and may overestimate costs the fix removed. Names, directory distribution, permissions, watcher activity, SQLite/cache placement, I/O and live changes can change the outcome. The scenarios expose uncertainty; they do not establish an upper bound or deadline. [All model inputs and outputs](benchmarks/windows-indexing-estimate.json).

## Reproduce

With development dependencies installed, create a separate baseline checkout and run each experiment in its own process:

```sh
git worktree add --detach ../pathgrove-baseline 1460ce942f6ccc18d197c10eaec63293916baaf3
uv run --frozen python scripts/benchmark_growth.py --source ../pathgrove-baseline --entries 100000 250000 500000 1000000 --output before.json
uv run --frozen python scripts/benchmark_growth.py --entries 100000 250000 500000 1000000 --output after.json
```

The helper creates/removes its own cache and never touches an existing index. Its JSON checkpoints identify synthetic work, completion state, count/snapshot time and loaded module hashes. Its tested `forecast()` implements the linear, quadratic and fitted scenarios. Historical calibration is applied separately from the measured timings.

To repeat the count microbenchmark, back up your own cache, open only the copy with the new `LocalIndex`, and alternate the scoped SQL statements in its raw report. Use identical roots and verify identical counts. Do not replace scoped counts with an unscoped total in a cache shared by different roots. Keep caches and matching paths private.
