# Whole-volume performance

This report measures real filesystem discovery and actual MCP stdio calls. It does not substitute synthetic SQLite insertion timings for a disk scan.

For the separate comparison of indexed filename searches against independent repeated filesystem traversal, including result-equality checks and speed-up ratios, see [COMPARISON.md](COMPARISON.md).

## Method

- Use a new private SQLite cache for each machine's run. No previous metadata index is reused for the initial scan.
- Start the normal MCP server, with watchers enabled and the default 10-second query budget.
- Poll status approximately once per second. Record initialization latency, the first observed searchable batch and the initial traversal completion time or the readiness deadline if unfinished.
- Query the populated index through the MCP SDK: `invoice` by name and modified date, `in`, `o`, `ext:pdf`, and a generated absent name. Return at most 50 results per call.
- Make five sequential calls per query. Report the first, median and maximum elapsed time; retain every sample and its partial/budget/error flags in the JSON reports.
- Search for a generated absent literal across `txt`, `md`, `py`, `ts` and `json` files, both directly and through indexed content candidates. These are bounded calls over the configured volume scope, not exhaustive content scans.
- Sample the server process's resident memory during indexing and after each query. This is a sampled maximum, not a guaranteed OS peak or total system memory usage.
- Stop the process, restart it with the populated cache and measure MCP initialization and the first cached filename query while reconciliation runs in the background.

Elapsed query time includes the MCP round trip, server work and response formatting. It excludes model/agent reasoning and the initial scan. Initial scan time includes process startup. First-batch time is the first observation at the polling interval, not an exact first-commit timestamp.

The OS filesystem cache was not flushed and neither machine was rebooted for this run. Both machines remained normal working systems. Repeated calls benefit from warm caches. Watcher work and live filesystem changes can affect results; the filesystem is not frozen during measurement.

## Scopes and hardware

| Machine | Scope | Hardware |
|---|---|---|
| Windows | Entire `C:\` and `D:\` roots, approximately 1.5 TB combined logical capacity | Intel Core i5-10600K, 48 GiB RAM; SATA and NVMe SSDs |
| Physical Mac mini | Entire `/System/Volumes/Data` APFS data volume, approximately 245 GB capacity | Apple M1, 8 GiB RAM, internal SSD; macOS 26.2 |

The Mac scope covers the data volume, including user data, applications and writable system data. It does not include the separate sealed macOS system volume. These different datasets and machines are not a controlled hardware comparison. Linux/WSL correctness is validated separately; no physical Linux whole-volume performance run is claimed.

The Windows cache is written to the default temporary directory on `C:` (Samsung 850 EVO SATA SSD); `D:` is a Kingston SFYRS1000G NVMe SSD. At the start, `C:` had approximately 40 GB free and `D:` approximately 536 GB free. The Mac cache is on its internal SSD. Cache placement and available space are part of these measurements, not controlled constants across machines.

Ordinary account permissions apply. Neither Windows permissions nor macOS privacy controls were changed. A finished traversal can still have incomplete coverage when paths are inaccessible. Names are measured against the resulting metadata snapshot; a content miss under any budget or access restriction does not establish absence.

## Results

Measured on 2026-10-23 through actual MCP stdio calls, with five calls per query and at most 50 results.

**Windows did not finish its configured `C:` + `D:` traversal within two hours.** The initial crawl was still in `D:`; `C:` had not yet been indexed in that phase. Its filename timings describe that committed partial index while scanning continued, not a complete catalog of both drives. The Mac traversal finished with incomplete access coverage (253 skipped entries/directories). All filename calls on both machines retained a partial/freshness notice.

| Metric | Windows, configured `C:` + `D:` | Mac mini M1, full data volume |
|---|---|---|
| Metadata entries when queries began | 4,756,213 | 3,986,964 |
| Initial traversal | Not finished after 120 min | 17.16 min |
| First observed searchable batch | 2.644 s | 1.329 s |
| Metadata cache | 1.22 GiB | 1.18 GiB |
| Sampled maximum server RSS | 508.0 MiB | 453.6 MiB |
| Fresh MCP initialization | 1.597 s | 0.315 s |
| Cached MCP initialization | 3.288 s | 1.350 s |
| First cached filename query | 13.397 s (budget-limited) | 3.614 s |

Median MCP round-trip latency:

| Query | Windows | Mac mini M1 |
|---|---|---|
| `invoice`, name order | 157.906 ms | 64.402 ms |
| `invoice`, newest first | 102.542 ms | 62.875 ms |
| `in`, name order | 6,652.803 ms (3/5 budget-limited) | 2,545.185 ms |
| `o`, name order | 11.013 ms | 4.738 ms |
| `ext:pdf`, name order | 10.552 ms | 4.026 ms |
| Generated absent filename | 10.829 ms | 3.929 ms |
| Direct content search (bounded) | 10,004.997 ms (5/5 budget-limited) | 5,182.775 ms (5/5 budget-limited) |
| Indexed-candidate content search (bounded) | 10,018.482 ms (5/5 budget-limited) | 10,008.347 ms (5/5 budget-limited) |

Both content-search methods returned no hits and reached a budget in all five calls on each machine. **These were not exhaustive content scans, and the misses do not establish absence.** The Windows cached first query also used a bounded fallback and returned one result; its 13.397 s total must not be treated as a full-result lookup.

The 10-second query budget is cooperative, not a hard end-to-end MCP deadline. Queueing, buffer work and diagnostics can add to the request time. OS caches were not flushed, and the datasets, cache placement and machines differ; these results are not a controlled hardware comparison.


Raw reports contain aggregate timing, memory, counts and coverage flags. They omit matching file paths, file contents and detailed error messages. Private metadata caches and stderr logs remain on the tested machines and must not be uploaded with the reports.

## Reproduce

Install the development dependencies from the checkout:

```sh
uv sync --frozen --extra dev
```

Windows, full selected drives:

```sh
uv run --frozen --extra dev python scripts/benchmark_disk.py --roots C:/ D:/ --trials 5 --wait-seconds 7200 --output docs/benchmarks/windows-full-disks.json
```

macOS, the full data volume:

```sh
uv run --frozen --extra dev python scripts/benchmark_disk.py --roots /System/Volumes/Data --trials 5 --wait-seconds 7200 --output docs/benchmarks/macos-full-data-volume.json
```

The script creates a fresh cache in the OS temporary directory. Budget enough time and disk space for a full scan. `--wait-seconds` limits waiting for initial readiness; a timeout is recorded as an unfinished scan. It does not remove content-read budgets or grant additional filesystem permissions. Query and shutdown time are additional to that readiness limit.

Use a smaller explicit root to measure a project or home directory. Such results must be labeled with that scope, rather than compared as if they covered the entire volume.

`scripts/benchmark_index.py` remains available for generated-row query experiments. Those timings exclude filesystem discovery and content reads and are not the whole-volume figures above.

## Detailed samples

Full aggregate reports: [Windows](benchmarks/windows-full-disks.json) and [Mac mini](benchmarks/macos-full-data-volume.json). Runtime source hashes use UTF-8 text normalized to LF line endings. No matching file names or contents are published.

### Windows

| Query | First ms | Median ms | Maximum ms | Budget-limited calls | Tool errors |
|---|---|---|---|---|---|
| `invoice`, name order | 333.404 | 157.906 | 333.404 | 0/5 | 0/5 |
| `invoice`, newest first | 98.774 | 102.542 | 158.280 | 0/5 | 0/5 |
| `in`, name order | 6,743.506 | 6,652.803 | 6,743.506 | 3/5 | 0/5 |
| `o`, name order | 18.162 | 11.013 | 18.162 | 0/5 | 0/5 |
| `ext:pdf`, name order | 9.864 | 10.552 | 11.930 | 0/5 | 0/5 |
| Generated absent filename | 29.589 | 10.829 | 29.589 | 0/5 | 0/5 |
| Direct content search (bounded) | 10,004.997 | 10,004.997 | 10,120.706 | 5/5 | 0/5 |
| Indexed-candidate content search (bounded) | 10,029.763 | 10,018.482 | 10,029.763 | 5/5 | 0/5 |

### Mac mini M1

| Query | First ms | Median ms | Maximum ms | Budget-limited calls | Tool errors |
|---|---|---|---|---|---|
| `invoice`, name order | 133.325 | 64.402 | 133.325 | 0/5 | 0/5 |
| `invoice`, newest first | 62.875 | 62.875 | 62.943 | 0/5 | 0/5 |
| `in`, name order | 2,618.700 | 2,545.185 | 2,618.700 | 0/5 | 0/5 |
| `o`, name order | 6.649 | 4.738 | 6.649 | 0/5 | 0/5 |
| `ext:pdf`, name order | 4.295 | 4.026 | 4.295 | 0/5 | 0/5 |
| Generated absent filename | 17.196 | 3.929 | 17.196 | 0/5 | 0/5 |
| Direct content search (bounded) | 6,513.346 | 5,182.775 | 6,513.346 | 5/5 | 0/5 |
| Indexed-candidate content search (bounded) | 10,006.810 | 10,008.347 | 10,009.844 | 5/5 | 0/5 |

## Current limits

Whole-drive first indexing on this Windows system remains slow. Cached startup also rebuilds in-process filename buffers, and broad two-character searches can take seconds or fall back to an inspected subset. Focused project or document roots are the practical starting point; allow a long-running server when choosing whole-volume roots. Startup reconciliation is traversal-based and does not resume from a durable OS journal.

The initial Mac attempt exposed repeated full-index work while retaining inaccessible subtrees. That path was changed to indexed parent-location lookups and protected by a regression test before the reported Mac run was repeated. No speed-up ratio against the interrupted attempt is claimed.
