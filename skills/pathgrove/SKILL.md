---
name: pathgrove
description: "Find local files and search their contents with Pathgrove MCP on Windows, Linux and macOS. Use for file locations, literal text inside files, scoped inventories, metadata and index health."
---

# Pathgrove local search

Use the `pathgrove_*` MCP tools to search local files. Check `pathgrove_index_status` for configured roots and freshness. Scope is explicit: a home-directory index does not cover an entire computer. When a requested drive or directory is outside those roots, report the limitation and configure the local server for that scope when authorized.

If the tools are not connected, a local checkout can be started through an MCP stdio client with `pathgrove` or `python -m pathgrove_mcp`. Inspect its published tool schema. Model-based tools take a `params` object in raw MCP calls; the status tool takes no parameters. Keep a process running between queries to receive live updates and avoid repeated startup reconciliation.

## Choose the right tool

| Request | Tool |
|---|---|
| Find a filename or path | `pathgrove_search` |
| Find text inside files, including files not yet indexed | `pathgrove_search_content` |
| Combine indexed metadata filters, global sorting and content predicates | `pathgrove_search` with `content:` |
| Search document/code/media categories | `pathgrove_search_by_type` |
| Find explicitly recent changes | `pathgrove_find_recent` |
| Inspect supplied paths or read a short preview | `pathgrove_file_details` |
| Count or size an indexed inventory | `pathgrove_count_stats` |
| Inspect scope, readiness and watcher failures | `pathgrove_index_status` |

## Names and metadata

Ordinary terms search filenames, not contents. `in` matches adjacent letters anywhere in `invoice`, `printing` or `mintfile`. Use `match_path` or `path:` for full paths.

```text
invoice !draft
"annual report"
ext:py;ts
path:"/absolute/path/to/project" ext:pdf
parent:"/absolute/path/to/project"
folder: assets
size:>10mb
regex:^test_.*\.py$
```

Quote paths containing spaces. Spaces mean AND; OR binds before AND: `invoice | photo ext:jpg` means `(invoice OR photo) AND ext:jpg`. `<...>` groups expressions. Case/whole-word/path/regex flags affect filename matching. Apply date filters when the user's request calls for dates.

## Text inside files

For direct disk search, call `pathgrove_search_content` with literal `text`, an `extensions` list (default `["txt"]`) and optionally a directory `path` inside the configured roots:

```json
{
  "params": {
    "text": "example-token-1234",
    "extensions": ["txt", "md"],
    "max_results": 10
  }
}
```

This tool works before filename indexing completes. Its default `recursive=true` searches subdirectories; `recursive=false` searches only the selected directory's files. Results use traversal order, without date ordering or pagination. Reaching the result limit stops the scan and warns that additional matches may exist. Choose extensions that match the request and disclose the scope used.

For indexed candidates, use `pathgrove_search`:

```text
ext:txt content:example-token-1234
path:"/absolute/path/to/project" ext:py content:"exact phrase"
ext:txt case:content:Example ww:content:word
```

Filters select metadata candidates before disk reads. Content terms are literal, case-insensitive by default; `case:`/`nocase:` and `ww:`/`noww:` modify matching. Multiple content predicates combine with AND. OR, groups and negated content predicates are rejected. Content regex/wildcards are not interpreted; `match_regex=true` treats the entire query as a filename regex. Count/size tools do not accept content predicates.

UTF-8 and BOM-marked UTF-16/32 text are supported. PDF/Word extraction and OCR are not provided. Files above 16 MiB, detected binary/unsupported-encoding files, inaccessible/changed files and links/reparse points are skipped. A request reads at most 256 MiB across 50,000 candidate files and checks its configured time budget between reads.

## Partial results and moved files

Search can return committed metadata while the index is building; a missing result may mean the file has not been discovered. For fresh or moved files, prefer direct content search when contents are the search criterion. The name cache persists; contents are read from disk again on every content query.

A partial miss does not establish absence. A positive hit remains useful even when coverage is incomplete. Preserve notices for inaccessible paths, skipped files, stale snapshots and exhausted budgets. Fallback ordering/pagination apply only to the inspected subset; count/size results describe the current index snapshot.

When a direct content search reaches its budget, split it into smaller scopes. Search a parent directory nonrecursively and its child subtrees recursively, keeping the user's original scope. Do not silently restrict a whole-drive request to a likely folder or reuse a previous hit's path for a new broad-search benchmark.

## Report results

Return observed full paths and state any scope/coverage limitation that affects the answer. If timing is requested, measure the actual MCP call. For searches spanning multiple calls, report total search elapsed time separately from the successful scope's call time. Distinguish startup/index discovery from query time and small fixtures from whole-drive searches. Do not substitute a known-path verification or filename-index benchmark for content-search performance.
