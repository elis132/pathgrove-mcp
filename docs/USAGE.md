# Usage reference

For installation and a quick overview, see the [README](../README.md).

## Requirements

Python 3.10+, SQLite 3.35+ with JSON support, Git and uv are required. Filename search does not require FTS5. The server uses MCP over stdin/stdout; logs go to stderr.

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
path:"/absolute/path/to/project" ext:py content:"exact phrase"
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

## Scope, freshness and privacy

The cache stores filenames, paths and metadata; it does not persist file contents. Pathgrove does not send searches to a hosted service. Your MCP client decides what results are sent to its model. Choose roots whose names and readable text you intend to expose to that client.

Overlapping roots are searched once. File details and previews require paths inside configured roots. Links/reparse points within that scope are rejected; directory links are recorded by the filename index but are not traversed.

Keep the MCP process running to receive filesystem notifications. On restart, cached metadata is searchable while reconciliation catches up with offline changes. On Windows NTFS this reads the change journal and revisits only changed folders, without administrator rights; a restart within the journal's history typically takes under a second. The journal keeps a fixed amount of history (32 MiB by default), which on a busy system drive can cover well under an hour. When it no longer reaches back to the last checkpoint, and at least once a week, a full traversal runs instead. While the server runs, it keeps its checkpoint current every 60 seconds. On other platforms every start is a full traversal. `last_reconcile` says which happened and how long it took. Missing/unreadable subtrees retain cached records and produce coverage warnings. `state: "ready"` means startup reconciliation ended; inspect `partial`, `coverage_note`, `watching` and `last_error` before assuming complete or fresh coverage.

A whole-volume scope does not bypass Windows permissions or macOS privacy controls. No administrator access is needed for ordinary readable folders. Experimental native Windows metadata helpers are disabled by default. Creation-date sorting requires platform birth-time support; POSIX metadata-change time is not treated as creation time.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PATHGROVE_ROOTS` | Current user's home | JSON array of directory or drive roots |
| `PATHGROVE_DB` | Application cache directory / `pathgrove/index.sqlite3` | Persistent SQLite metadata cache |
| `PATHGROVE_TIMEOUT` | `10` | Query budget in seconds |
| `PATHGROVE_MAX_RESULTS` | `1000` | Backend result cap; MCP search requests also have a 500-result cap |

The cache base is `%LOCALAPPDATA%` on Windows and `$XDG_CACHE_HOME` or `~/.cache` on Linux/macOS. Reuse a compatible cache with the same roots through `PATHGROVE_DB`. Stop writers before manually moving cache files.

## Terminal commands

`pathgrove` without arguments runs the MCP server. With a command it searches the same index from a terminal:

```sh
pathgrove index                                  # build or refresh the index
pathgrove search invoice --ext pdf --modified last7days
pathgrove search --size ">100mb" --sort size-desc -n 20
pathgrove search "content:TODO" --ext py --path D:\Projects
pathgrove count --ext log --bytes                # total size of matching files
```

- `search` and `count` read the existing cache only. They never scan or watch, and they answer while the MCP server is scanning. Results are as fresh as the last scan: keep the MCP server running, or run `pathgrove index` first.
- `index` replays the NTFS change journal where it can and walks the rest. It waits while another process holds the scan, for example the MCP server during its first build.
- The index file is `--db`, else `PATHGROVE_DB`, else the default above. Roots are `--root` (repeatable), else `PATHGROVE_ROOTS`, else the roots the index already holds. An MCP client's `env` block does not reach your shell: set the same variables in your user environment, or pass `--db`.
- `--ext`, `--size`, `--modified` and `--path` build query terms, so PowerShell never sees an unquoted `>`. Quote a query yourself when it contains `<`, `>` or `|`.
- Paths go to stdout, one per line (`--null` for `xargs -0`, `--json` for one object per line); notes go to stderr. Exit status is 0 for matches, 1 for none and 2 for errors. `--sort` defaults to `date-modified-desc`, like the MCP tool.
- Put `pathgrove` on your `PATH` with `uv tool install --editable <checkout>`.

## Agent skill and plugin

The bundled [Pathgrove skill](../skills/pathgrove/SKILL.md) explains tool selection, query syntax, scope checks and partial results. Python packages include the skill as well.

The `.claude-plugin` manifests run the local checkout through `uv` and include the skill. They do not require a PyPI release. See the [Claude plugin reference](https://code.claude.com/docs/en/plugins-reference) for client configuration.

## Development

Dependabot uses the `uv` ecosystem to maintain the project's lockfile. Major MCP and cryptography upgrades are reviewed manually; minor and patch updates remain enabled. The `mcp<2` bound and Intel-macOS `cryptography<49` bound are intentional. MCP 2 changes client field names used by the smoke/benchmark helpers; cryptography 49 removed Intel-macOS support. A migration must update the lockfile and test the newly selected versions, including actual MCP stdio and both Mac architectures. A green run with the old frozen lockfile does not validate a new permitted major version.

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev ruff check src tests scripts
uv run --frozen --extra dev ruff format --check src tests scripts
uv run --frozen --extra dev python tests/live_smoke.py
uv build
```

See [architecture](ARCHITECTURE.md), [validation evidence](VALIDATION.md) and [the changelog](../CHANGELOG.md). Tests use isolated fixtures. CI covers Windows, Linux and macOS with Python 3.10 and 3.14; the package workflow builds archives and runs the MCP smoke against the installed wheel.

Linux tests, lint and package builds run on Ubicloud using `ubicloud-standard-2`. Windows and macOS tests use their native GitHub-hosted runners. To run the Linux jobs in your own fork, connect that repository to the Ubicloud Managed Runners app as described in [Ubicloud's setup guide](https://www.ubicloud.com/docs/github-actions-integration/quickstart).
