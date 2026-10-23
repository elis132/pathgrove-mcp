# Indexed search versus a fresh filesystem scan

This comparison answers a specific question: how much time does a reusable Pathgrove filename index save over independently walking the same real filesystem for each search?

It is **not** a before/after comparison of Pathgrove versions, or a benchmark against Everything, Spotlight, `find`, `fd`, `ripgrep` or another MCP server. The baseline is the independent Python standard-library scanner included in [benchmark_compare.py](../scripts/benchmark_compare.py).

## Equal work and correctness

- Both methods search filenames case-insensitively, use identical roots and return up to 50 entries sorted by Unicode-casefolded name and full path. Directories and links are included as entries; directory links and Windows reparse points are never followed.
- The baseline makes a **new complete traversal for each query and each trial** using `os.scandir`. It maintains a bounded top-50 page rather than sorting every matching entry. It does not stop at the first 50 hits, because that would give traversal order rather than the requested global alphabetical order.
- The baseline does not collect size or date metadata for every file. It is a filename-search baseline, not a repeated Pathgrove index build. It uses no Pathgrove query planner, candidate buffers or SQL to find matches.
- Pathgrove is called through the actual `pathgrove_search` MCP stdio tool. Timing includes transport, query execution, result formatting and diagnostics. The original private disk cache is opened read-only and backed up into an isolated database. Only background reconciliation/watchers are disabled in the benchmark server, keeping its indexed snapshot fixed. Production lifecycle behavior is unchanged.
- The first MCP call for every query is recorded separately, before measured repeated calls. The reported ratios compare medians after those calls. They exclude index construction, database backup, initialization and initial filename-buffer loading.
- Baseline/MCP order alternates between trials. OS caches are not flushed. A new traversal does **not** mean a cold OS cache.
- Every measured MCP page is compared with the baseline's exact ordered paths. A ratio is withheld for the entire query if any trial disagrees, reaches the MCP query budget or returns an error, or if any configured root's initial traversal was unfinished.
- Successful comparison establishes equality of the requested page, not equality of every matching entry or a complete filesystem snapshot. Inaccessible paths remain outside the available coverage, and live changes can cause mismatches. Skipped counts and observed entry counts are saved per trial.

The aggregate JSON reports contain timing, counts, equality and budget/error flags. Matching paths and file contents are never written to the public reports. The original caches are private and are not included in this repository.

## Results

The physical machines, volume scopes and initial indexing costs are described in [PERFORMANCE.md](PERFORMANCE.md). Comparison results are recorded separately from the original whole-volume latency measurements.

### Physical Mac mini M1: completed traversal

The frozen index contained **3,986,964 metadata entries** from the entire `/System/Volumes/Data` APFS data volume. Every independent traversal visited **3,987,009–3,987,013 entries** and skipped 253 entries/directories under ordinary account permissions. The live filesystem and fixed metadata snapshot differ slightly in entry count; only the requested ordered pages are verified as equal.

**All 25 measured ordered pages agreed, with no MCP errors or query-budget exhaustion.** Each query below has five independent traversal/MCP pairs. Ratios are the baseline median divided by the MCP median, not the best individual sample.

| Query | Fresh scan, median | MCP, median | Measured speed-up | First MCP call for this query |
|---|---|---|---|---|
| `invoice` | 77.060 s | 64.424 ms | 1,196.13× | 3,138.592 ms |
| `in` | 77.650 s | 2,394.593 ms | 32.43× | 2,456.095 ms |
| `o` | 79.029 s | 5.493 ms | 14,387.16× | 7.130 ms |
| `ext:pdf` | 79.399 s | 4.209 ms | 18,864.15× | 3.752 ms |
| Generated absent filename | 77.036 s | 4.112 ms | 18,734.34× | 15.772 ms |

These are intentionally different query shapes: the full traversal found 4,501 `invoice` matches, 1,095,276 `in` matches, about 2.15 million `o` matches and 5,191 PDF entries. A query that can use an indexed extension order is much faster than the broad two-character substring search. The absent-name query is not a substitute for the broad positive cases.

Initialization in the isolated snapshot server took 700.327 ms. Its first `invoice` call loaded filename buffers and took 3.139 seconds, materially slower than the repeated median. The first-call column is recorded separately for every query; later queries already share process/OS caches. These figures are not normal production restart/reconciliation measurements.

Initial index construction cost **1,029.470 seconds (17.16 minutes)** in the earlier real-volume run. Dividing that cost by the measured repeated `invoice` saving gives roughly **14 searches** to amortize indexing under these conditions. This is an estimate across the two runs, excludes ongoing reconciliation costs and does not imply a benefit for a one-off search. Content-search speed-ups were not measured.

Raw results, including every trial: [macos-scan-comparison.json](benchmarks/macos-scan-comparison.json).

### Windows: correctness control with an unfinished index

The existing whole-drive `C:` + `D:` cache contained **4,793,676 entries** when copied, after the original two-hour indexing run and its cached restart. Neither root had a completed initial traversal in the cache. The independent traversals visited **7,924,398–7,940,100 entries**, with 371–372 skipped entries/directories. The live machine changed between traversals.

One trial per query was run as a control; these single observations are not five-trial medians. All five exhaustive baseline traversals finished. **No Windows speed-up ratio is valid or reported.**

| Query | Fresh filesystem scan | Cached MCP | Identical ordered page? | Speed-up |
|---|---|---|---|---|
| `invoice` | 383.265 s | 1,386.102 ms | No | Withheld |
| `in` | 190.481 s | 2,827.279 ms | No | Withheld |
| `o` | 177.662 s | 14.387 ms | No | Withheld |
| `ext:pdf` | 233.381 s | 14.357 ms | No | Withheld |
| Generated absent filename | 221.645 s | 13.904 ms | Yes, both empty | Withheld: initial indexing unfinished |

The first, separately recorded `invoice` MCP call also reached its query budget. The later measured call returned 50 paths without reaching that budget, but it still disagreed with the exhaustive traversal. A fast response from an incomplete index is **not** a like-for-like performance win. The empty absent-name result does not establish full index coverage either.

Raw results: [windows-scan-comparison.json](benchmarks/windows-scan-comparison.json).

## Evidence provenance

Both runs used [benchmark implementation `648f170`](https://github.com/elis132/pathgrove-mcp/commit/648f17062cd92cebae750e2529270a372a5d7844). The benchmark does not change production server code. The reports record the benchmark script hash, all 11 server-module hashes and the private input database backup's hash. Source hashes use UTF-8 text with LF line endings. The installed Mac runtime hashes were checked against the previously published whole-volume report and match; the local Windows runtime was checked likewise.

The tests require identical ordered pages with more than 50 hits, preserve directory-link exclusions, verify public-report privacy and withhold invalid ratios. [The implementation CI](https://github.com/elis132/pathgrove-mcp/actions/runs/40003265339) passed Windows/Linux/ARM macOS/Intel macOS on Python 3.10 and 3.14 plus lint. GitHub CI uses isolated fixtures; it does not rerun these private physical-volume performance measurements.

## Reproduce on your own data

First build a cache over the exact roots you want to compare. Keep the server running until `pathgrove_index_status` reports that the initial traversal ended, then stop it. Check coverage warnings as well: readiness does not grant access to protected folders.

Run the comparison from the checkout against that cache. For example, on macOS:

```sh
uv sync --frozen --extra dev
uv run --frozen python scripts/benchmark_compare.py \
  --database /absolute/path/to/private/index.sqlite3 \
  --roots /System/Volumes/Data \
  --trials 5 \
  --output comparison.json
```

The Windows control used one trial for each of the five queries:

```powershell
uv run --frozen python scripts/benchmark_compare.py --database C:/path/to/private/index.sqlite3 --roots C:/ D:/ --trials 1 --output comparison.json
```

Use the existing cache's exact configured roots. Avoid overlapping roots. The benchmark copies the metadata database and never deliberately writes the original cache; allow approximately its size again in temporary storage. The comparison does not build or complete an unfinished cache for you.

Each full traversal can take minutes and is repeated for every query. The benchmark has no traversal time limit; the MCP query budget remains 10 seconds by default. Run it on a machine and scope where this work is acceptable. An absent-name query demonstrates an exhaustive accessible filename traversal by the baseline; it says nothing about file contents or inaccessible paths.

## Interpretation

This measures the benefit of **paying indexing cost once and reusing it**, rather than repeatedly discovering filenames. For a small project, MCP overhead can exceed a fast direct traversal. For a large volume, repeated filename lookups can amortize the initial indexing cost. Neither result implies faster initial indexing or faster content searches.

The steady-state comparison deliberately suppresses background filesystem work. Production startup/reconciliation costs and first cached queries are separately reported in [PERFORMANCE.md](PERFORMANCE.md), including the slow first Windows query and its unfinished two-hour initial scan. Do not apply a ratio from a completed Mac volume to the incomplete Windows catalog.
