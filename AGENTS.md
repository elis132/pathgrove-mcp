# Pathgrove developer guidance

Pathgrove is a local Python MCP server. Read the README and relevant implementation before changing behavior. Coordinate directly with other agents before editing shared files. Do not use a task board.

- `config.py`: explicit indexing roots, cache path and query limits.
- `storage.py`: normalized SQLite metadata, schema migration and query functions.
- `names.py`: packed UTF-8 name blocks, transactional revision invalidation and candidate caching.
- `index.py`: scans, watcher updates, process locks, queries and bounded fallback.
- `query.py`: structured query grammar and literal content-predicate planning.
- `content.py`: chunked text reads and direct filesystem traversal with explicit budgets.
- `backend.py`: asynchronous adapters and worker limits.
- `server.py`: seven MCP tools, lifecycle and diagnostics. Stdout is reserved for MCP; logs go to stderr.
- `ntfs.py`: experimental read-only metadata helpers, disabled by default.

Preserve root scoping, symlink/reparse exclusions and inaccessible-subtree retention. Publish initial scan batches promptly. Keep watcher updates scoped to changed paths and exclude cache/lock files. Coordinate full scan generations across processes sharing a cache.

Propagate query-specific partial/freshness metadata through every layer. Counts are exact for the current snapshot or return an error. A budget-limited search must identify incomplete coverage and its ordering/pagination limits. Content reads are not a filesystem snapshot.

Run targeted tests during implementation, then pytest, Ruff, the actual MCP stdio smoke and a package build for substantial changes. Use isolated fixtures. Separate synthetic query timings, startup/index time, content reads and process memory in reports. A renamed portable implementation does not constitute a fresh physical-Mac validation.

Use a local working branch. Publication, remote pushes and releases require the user's explicit instruction.
