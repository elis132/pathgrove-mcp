# Pathgrove

<!-- mcp-name: io.github.elis132/pathgrove-mcp -->

**Give your AI assistant a fast way to find your local files.**

Pathgrove lets an MCP-compatible assistant find files without knowing their location first. It keeps a reusable filename index, so repeated searches do not have to walk every folder again. Search text inside files, inspect matches and get the paths you need through the same connection.

Ask your assistant:

> Find invoice PDFs modified in the last seven days.
>
> Find Python files containing `API_BASE_URL`.
>
> Show me a preview of `annual-report.txt`.

Runs locally on **Windows, Linux and macOS**. You choose the search folders. Pathgrove needs no hosted search service or API key; your MCP client controls what is sent to its model.

## The payoff

On a physical **Mac mini M1 with about four million indexed entries**, repeated filename searches returned these results:

| Search | Fresh filesystem scan | Pathgrove MCP | Speed-up |
|---|---|---|---|
| `invoice` | 77.06 seconds | **64 milliseconds** | **1,196×** |
| Broad substring `in` | 77.65 seconds | **2.39 seconds** | **32×** |

Five trials per query, with identical ordered first-50 result pages checked each time. The comparison is against a fresh Python `os.scandir` traversal, using a **warmed filename index**. Indexing and startup are excluded; it is not a comparison against Everything or Spotlight. These measurements describe the recorded baseline, before the later indexing optimizations. [Method and raw data →](https://github.com/elis132/pathgrove-mcp/blob/main/docs/COMPARISON.md)

The first index costs time: that Mac traversal took **17.2 minutes**, and inaccessible paths still limited coverage. Start with your project or document folders. Optimized Windows full-drive completion is **estimated at about 75 minutes**, not measured to completion. [Windows measurements and estimate →](https://github.com/elis132/pathgrove-mcp/blob/main/docs/WINDOWS-ESTIMATE.md)

## Get started

Requires **Python 3.10+** and [uv](https://docs.astral.sh/uv/getting-started/installation/). See the [SQLite requirements](https://github.com/elis132/pathgrove-mcp/blob/main/docs/USAGE.md#requirements).

```sh
uv tool install pathgrove-mcp
```

This puts the `pathgrove` command on your `PATH`; `pipx install pathgrove-mcp` works too. Add this to an MCP client that uses `mcpServers` configuration:

```json
{
  "mcpServers": {
    "pathgrove": {
      "command": "pathgrove",
      "env": {
        "PATHGROVE_ROOTS": "[\"<ABSOLUTE_SEARCH_ROOT>\"]"
      }
    }
  }
}
```

Replace `<ABSOLUTE_SEARCH_ROOT>` with an existing folder you want to search. Forward slashes work on Windows too, avoiding nested backslash escaping. You can put multiple roots in the JSON-encoded array. If the client cannot find `pathgrove`, use its absolute path (`uv tool dir --bin` shows the folder), or use `"command": "uvx"` with `"args": ["--from", "pathgrove-mcp", "pathgrove"]` without installing.

To run from a checkout instead, clone the repository, run `uv sync --frozen` and use `"command": "uv"` with `"args": ["run", "--directory", "<ABSOLUTE_CHECKOUT_PATH>", "--frozen", "pathgrove"]`.

Restart your MCP client and ask it to find a file. Initial indexing runs in the background; committed batches become searchable before it finishes. Keep the server running for filesystem updates. On Windows (NTFS), later restarts read the drive's change journal and revisit only changed folders instead of walking everything again. No administrator rights or other search app are needed.

## What it can do

| Task | Tool |
|---|---|
| Find files by name, path, extension, size or date | `pathgrove_search` |
| Search literal text, even before the index finishes | `pathgrove_search_content` |
| Find file categories or recent changes | `pathgrove_search_by_type`, `pathgrove_find_recent` |
| Preview a file or inspect its metadata | `pathgrove_file_details` |
| Count indexed matches or check index health | `pathgrove_count_stats`, `pathgrove_index_status` |
| Search from a terminal or script | `pathgrove search`, `pathgrove count`, `pathgrove index` ([usage](https://github.com/elis132/pathgrove-mcp/blob/main/docs/USAGE.md#terminal-commands)) |

Examples of filename queries:

```text
invoice ext:pdf dm:last7days
"annual report" !draft
ext:py content:"API_BASE_URL"
```

## Know the limits

- Search stays inside your configured roots and respects normal filesystem permissions. Links are not followed.
- The index stores names, paths and metadata. Text is read from disk per query; PDF/Word extraction and OCR are not included.
- Building indexes, inaccessible files and query budgets can produce partial results. **A partial miss does not prove absence.** The tools report these limits explicitly.

## Read more

- [Usage, query syntax, configuration and development](https://github.com/elis132/pathgrove-mcp/blob/main/docs/USAGE.md)
- [Measured comparison and raw data](https://github.com/elis132/pathgrove-mcp/blob/main/docs/COMPARISON.md) · [Whole-volume results](https://github.com/elis132/pathgrove-mcp/blob/main/docs/PERFORMANCE.md) · [Windows estimate](https://github.com/elis132/pathgrove-mcp/blob/main/docs/WINDOWS-ESTIMATE.md)
- [Architecture](https://github.com/elis132/pathgrove-mcp/blob/main/docs/ARCHITECTURE.md) · [Validation](https://github.com/elis132/pathgrove-mcp/blob/main/docs/VALIDATION.md) · [Agent skill](https://github.com/elis132/pathgrove-mcp/blob/main/skills/pathgrove/SKILL.md) · [Changelog](https://github.com/elis132/pathgrove-mcp/blob/main/CHANGELOG.md)

CI tests Python 3.10 and 3.14 across Linux, Windows and ARM/Intel macOS. Linux tests, lint and package builds use **Ubicloud** (`ubicloud-standard-2`); Windows and macOS use native GitHub-hosted runners. Forks need the [Ubicloud app connected](https://www.ubicloud.com/docs/github-actions-integration/quickstart) to run those Linux jobs.

[MIT license](https://github.com/elis132/pathgrove-mcp/blob/main/LICENSE).
