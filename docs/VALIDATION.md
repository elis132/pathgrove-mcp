# Validation

These checks exercise the Pathgrove package after its pre-release rename, including the whole-volume indexing fix. Earlier package-name results are superseded by these runs.

## Local results, 2026-10-23

| Platform | Runtime | Full test suite |
|---|---|---|
| Windows 11 Pro | Python 3.12.11, SQLite 3.49.1 | 189 passed, 5 skipped |
| Ubuntu 24.04 / WSL2 | Python 3.12.3, SQLite 3.45.1 | 191 passed, 3 skipped |
| Physical Apple M1 Mac mini, macOS 26.2 | Python 3.14.3, SQLite 3.51.3 | 192 passed, 2 skipped |

Ruff check/format, the actual MCP stdio smoke and wheel/source-package builds passed on all three platforms. The Mac used a locally built and installed wheel over SSH. Linux used an isolated copy on the WSL Linux filesystem, rather than Windows-mounted test fixtures.

## Coverage

### Comparative benchmark follow-up

The comparison benchmark adds six tests. Windows Python 3.12 passed the full suite with **195 passed, 5 skipped**, plus Ruff check/format, the actual MCP stdio smoke and wheel/source-package builds. [CI for the benchmark implementation](https://github.com/elis132/pathgrove-mcp/actions/runs/40003265339) passed all eight platform/Python combinations and lint; [the package workflow](https://github.com/elis132/pathgrove-mcp/actions/runs/40003265239) also passed.

The new tests compare real MCP pages with an independent filesystem scanner, including more than 50 hits and directory links. They verify that mismatched ordering, errors, query-budget exhaustion and unfinished initial indexing suppress speed-up claims, and that public reports omit matching file paths and contents. Whole-volume comparison results and limitations are recorded in [COMPARISON.md](COMPARISON.md).

### Server coverage

The suite covers query grammar, Unicode substring matching, stable pagination, packed filename candidates, cache migration, shared-cache invalidation, incremental indexing, watcher updates, unavailable paths, content encodings/chunk boundaries, read budgets and direct content search before metadata readiness.

Regression checks include:

- File details reject out-of-root paths and links within the configured scope, while accepting aliases above that scope, such as macOS's `/var` to `/private/var` mapping.
- Overlapping roots do not duplicate direct content matches.
- Budget-limited filename searches sort their inspected subset before pagination.
- Inaccessible incremental subtrees produce explicit partial coverage, cleared after successful reconciliation.
- Retaining cached entries under inaccessible subtrees uses bounded SQLite work instead of repeatedly traversing unrelated metadata; neighboring paths are still removed correctly.
- The whole-volume benchmark exercises actual MCP stdio and excludes matching file paths and file contents from its aggregate report.

`tests/live_smoke.py` lists all seven tools, searches a phrase through ten nested directories, previews an in-scope text file and rejects an out-of-root preview through the actual protocol.

Platform-specific tests are skipped where their prerequisites do not apply. The Mac additionally skips creation of an undecodable raw-byte filename because its filesystem rejects that name; the synthetic surrogate-name coverage test still runs.

## Installation and evidence limits

PyPI downloads timed out on the Mac. Matching wheels were downloaded on Windows, verified against lockfile hashes and transferred over SSH, then installed into a separate virtual environment. A direct online installation on the Mac remains unverified. The test directory, cache and environment are separate from existing projects.

Fixture timings are not whole-drive performance claims. Real-volume measurements, hardware, methods and coverage restrictions are documented in [PERFORMANCE.md](PERFORMANCE.md).

A fresh physical Intel Mac run has not been performed. The GitHub workflow defines Windows, Linux, Apple silicon macOS and Intel macOS jobs with Python 3.10 and 3.14. Check the [live CI runs](https://github.com/elis132/pathgrove-mcp/actions/workflows/ci.yml) separately from the local results above.
