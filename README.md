# Pathgrove

<!-- mcp-name: io.github.elis132/pathgrove-mcp -->

**Local file and text search for AI agents.**

Pathgrove gives an AI agent a search engine for your files through the Model Context Protocol (MCP). It runs on Windows, Linux and macOS, combining a persistent filename index with direct text search and filesystem notifications.

- Find files by name, path, extension, size or date.
- Search literal text without waiting for the filename index to finish.
- Search committed index batches while the first scan continues.
- Keep searches inside directories or drives you explicitly choose.
- Run locally over stdio, without a hosted search service or API key.

Version **0.1.0** is available from source. It has not been published to PyPI.

## Quick start

Requires **Python 3.10+**, **SQLite 3.35+ with JSON support**, Git and [uv](https://docs.astral.sh/uv/getting-started/installation/). Recent Python installations normally include a suitable SQLite build.

```sh
git clone https://github.com/elis132/pathgrove-mcp.git
cd pathgrove-mcp
uv sync --frozen
```

Add the checkout to your MCP client. Clients using a `mcpServers` JSON configuration can use this template on Windows, Linux or macOS:

```json
{
  "mcpServers": {
    "pathgrove": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "<ABSOLUTE_CHECKOUT_PATH>",
        "--frozen",
        "pathgrove"
      ],
      "env": {
        "PATHGROVE_ROOTS": "[\"<ABSOLUTE_SEARCH_ROOT>\"]"
      }
    }
  }
}
```

Replace both placeholders before starting the client. `<ABSOLUTE_CHECKOUT_PATH>` is the directory you cloned; `<ABSOLUTE_SEARCH_ROOT>` is an existing directory you want to search. Use your own absolute paths, for example `C:/path/to/documents` on Windows or `/absolute/path/to/documents` on Linux/macOS. Forward slashes also work on Windows and avoid nested backslash escaping. `PATHGROVE_ROOTS` is a JSON-encoded array, so you can add more roots inside that array. If the client cannot find `uv`, use its absolute executable path.

If you choose to index entire Windows drives, an example environment value is `"PATHGROVE_ROOTS": "[\"C:/\", \"D:/\"]"`; select the drive letters that actually exist on your computer. Without `PATHGROVE_ROOTS`, only the current user's home directory is indexed.

You can also start the server with `uv run --frozen pathgrove`, the installed `pathgrove` command or `python -m pathgrove_mcp`. It waits for an MCP client on stdin/stdout; it is not an interactive terminal prompt. Logs go to stderr.

## Tools

| Tool | Use it to |
|---|---|
| `pathgrove_search` | Search names and paths; filter, sort, paginate or add indexed-candidate `content:` predicates |
| `pathgrove_search_content` | Search literal text directly on disk, including files not yet indexed |
| `pathgrove_search_by_type` | Find documents, code, images, audio, video and other file categories |
| `pathgrove_find_recent` | Find files modified in a specified period |
| `pathgrove_file_details` | Inspect metadata, directory samples and short text previews |
| `pathgrove_count_stats` | Count matches and sum sizes in the current metadata snapshot |
| `pathgrove_index_status` | Inspect roots, indexing progress, coverage and watcher health |

The `pathgrove://status` resource exposes the same health information. Raw MCP calls to tools with an input model use a `params` object; the status tool takes `{}`. Inspect the published tool schema for all parameters.

## Find files

| Query | Meaning |
|---|---|
| `invoice !draft` | Names containing `invoice` but not `draft` |
| `"annual report"` | A literal phrase in the name |
| `ext:pdf;txt` | PDF or text files |
| `path:"/absolute/path/to/project" ext:py` | Python files whose full path contains the supplied text |
| `parent:"/absolute/path/to/project"` | Entries immediately inside that directory |
| `folder:assets` | Directories with `assets` in their name |
| `size:>10mb` | Files larger than 10 MiB |
| `dm:last7days` | Files modified in the last seven days |
| `regex:^test_.*\.py$` | Names matching a regular expression |

Ordinary terms search filenames, not file contents. Matching is case-insensitive by default and supports Unicode casefolding. Short terms retain substring matching: `in` matches `invoice`, `printing` and `mintfile`.

Spaces mean AND. **OR binds before AND:** `invoice | photo ext:jpg` means `(invoice OR photo) AND ext:jpg`. Use `<...>` for groups. Quoted phrases, negation, wildcards and case/whole-word/path/regex flags are supported. Unknown query functions return an error.

Search results default to newest modified first. Use `sort: "name"` for alphabetical results. Sorting and offsets are global when the query completes; a budget-limited fallback explicitly limits them to the inspected subset.

## Search inside files

For a direct disk search, call `pathgrove_search_content`:

```json
{
  "params": {
    "text": "example-token-1234",
    "extensions": ["txt", "md"],
    "max_results": 10
  }
}
```

This works before filename indexing completes. An optional directory `path` narrows the search within configured roots. `recursive` defaults to `true`; set it to `false` to inspect only that directory's files. Extensions default to `["txt"]`. Results use filesystem traversal order, without global sorting or pagination.

To combine indexed metadata, sorting and content matching, use `pathgrove_search`:

```text
ext:txt content:example-token-1234
path:"D:\Projects" ext:py content:"exact phrase"
ext:txt case:content:Example ww:content:word
```

Metadata filters select candidates before content reads. Offsets count content matches. Content predicates support flat AND combinations, literal text, case matching and whole-word matching. OR, grouping and negated content predicates are rejected. Count/size tools reject `content:`. Content is not interpreted as a wildcard or regular expression. The global `match_regex` flag applies to the entire filename query.

Both content-search paths read current disk contents in 64 KiB chunks:

| Limit | Value |
|---|---|
| Encodings | UTF-8 and BOM-marked UTF-16/UTF-32 |
| Maximum individual file | 16 MiB |
| Total bytes read per request | 256 MiB |
| Maximum candidate files | 50,000 |
| Default query budget | 10 seconds |

PDF/Word extraction, archive extraction and OCR are not included. Detected binary/unsupported-encoding files, inaccessible or changed files, links/reparse points and oversized files are skipped. The reader checks deadlines between reads; an individual OS read can exceed the user-space deadline. Content reads are not a filesystem snapshot.

**A partial miss does not prove absence.** Returned hits remain useful, but skipped files and exhausted budgets mean coverage is incomplete. Reaching the direct tool's result limit also reports that more matches may exist. Split large content searches into explicit smaller scopes instead of treating one bounded call as an exhaustive disk search.

## Performance

Measurements use real volumes and actual MCP stdio calls. The comparison checks identical ordered result pages before calculating speed-ups; indexing, startup and individual request timings are reported separately.

### Windows indexing improvements and estimated completion

Transactional counters, nonblocking filename-cache fallback and a shared query deadline improve indexing and responsiveness. A one-million-entry synthetic indexing run took **114 seconds versus 151 seconds**, about **24% less time**. On the real 4.79-million-entry metadata cache, count refresh fell from **734 ms to 0.054 ms**; this ratio applies to the counter operation, not the entire scan.

**Windows full-drive planning guess: approximately 75 minutes for the measured C: + D: scope of 7.94 million entries.** This is a calibrated simulation, not a completed full-drive run of the optimized version. An optimistic synthetic extrapolation gives about 19 minutes and a quadratic scenario about 124 minutes. See [measurements, assumptions, raw data and reproduction](docs/WINDOWS-ESTIMATE.md).

The full-volume tables below describe the earlier baseline, before these indexing/deadline changes. Their source identities are preserved in the reports.

### Indexed filename search versus a fresh scan

On the physical **Mac mini M1**, searching the entire APFS data volume with a completed, warmed index gave these five-trial medians. Both methods returned the same alphabetically ordered first 50 paths in every trial.

| Query | Independent `os.scandir` traversal per search | Pathgrove MCP | Measured speed-up |
|---|---|---|---|
| `invoice` | 77.060 s | 64.424 ms | 1,196.13× |
| `in` (over one million matching names) | 77.650 s | 2,394.593 ms | 32.43× |

This compares repeated filename discovery with **reusing an existing index**, not two Pathgrove versions or competing indexed search tools. MCP transport is included; index construction, startup and initial buffer loading are excluded from these ratios. The initial index cost 17.16 minutes in the earlier whole-volume run: approximately 14 searches like `invoice` would amortize that cost under these measured conditions. OS caches were not flushed. Inaccessible paths remain outside the available coverage; page equality does not establish that all matches are identical.

See [the comparison report](docs/COMPARISON.md) for every tested query, individual samples, reproduction commands and the Windows limitation. **No whole-volume Windows speed-up is claimed:** its initial index was unfinished.

### Initial indexing, startup and request latency

Whole-volume measurements and reproduction commands are recorded in [the benchmark report](docs/PERFORMANCE.md). Filename-query latency, initial indexing, cached startup, content reads and server memory are measured separately over actual MCP stdio calls.

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


These measurements use real files, not generated SQLite rows. The separate `scripts/benchmark_index.py` utility measures synthetic query workloads and excludes filesystem crawling.

## Scope, freshness and privacy

The cache stores filenames, paths and metadata; it does not persist file contents. Pathgrove does not send searches to a hosted service. Your MCP client decides what results are sent to its model. Choose roots whose names and readable text you intend to expose to that client.

Overlapping roots are searched once. File details and previews require paths inside configured roots. Links/reparse points within that scope are rejected; directory links are recorded by the filename index but are not traversed.

Keep the MCP process running to receive filesystem notifications. On restart, cached metadata is searchable while a new traversal reconciles offline changes. Missing/unreadable subtrees retain cached records and produce coverage warnings. `state: "ready"` means the startup traversal ended; inspect `partial`, `coverage_note`, `watching` and `last_error` before assuming complete or fresh coverage.

A whole-volume scope does not bypass Windows permissions or macOS privacy controls. No administrator access is needed for ordinary readable folders. Experimental native Windows metadata helpers are disabled by default. Creation-date sorting requires platform birth-time support; POSIX metadata-change time is not treated as creation time.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PATHGROVE_ROOTS` | Current user's home | JSON array of directory or drive roots |
| `PATHGROVE_DB` | Application cache directory / `pathgrove/index.sqlite3` | Persistent SQLite metadata cache |
| `PATHGROVE_TIMEOUT` | `10` | Query budget in seconds |
| `PATHGROVE_MAX_RESULTS` | `1000` | Backend result cap; MCP search requests also have a 500-result cap |

The cache base is `%LOCALAPPDATA%` on Windows and `$XDG_CACHE_HOME` or `~/.cache` on Linux/macOS. Reuse a compatible cache with the same roots through `PATHGROVE_DB`. Stop writers before manually moving cache files.

## Agent skill and plugin

The bundled [Pathgrove skill](skills/pathgrove/SKILL.md) explains tool selection, query syntax, scope checks and partial results. Python packages include the skill as well.

The `.claude-plugin` manifests run the local checkout through `uv` and include the skill. They do not require a PyPI release. See the [Claude plugin reference](https://code.claude.com/docs/en/plugins-reference) for client configuration.

## Development

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev ruff check src tests scripts
uv run --frozen --extra dev ruff format --check src tests scripts
uv run --frozen --extra dev python tests/live_smoke.py
uv build
```

See [architecture](docs/ARCHITECTURE.md), [validation evidence](docs/VALIDATION.md) and [the changelog](CHANGELOG.md). Tests use isolated fixtures. CI covers Windows, Linux and macOS with Python 3.10 and 3.14; the package workflow builds archives and runs the MCP smoke against the installed wheel.

Linux tests, lint and package builds run on Ubicloud using `ubicloud-standard-2`. Windows and macOS tests use their native GitHub-hosted runners. To run the Linux jobs in your own fork, connect that repository to the Ubicloud Managed Runners app as described in [Ubicloud's setup guide](https://www.ubicloud.com/docs/github-actions-integration/quickstart).

## License

[MIT](LICENSE). Copyright notices are retained in the license file.
