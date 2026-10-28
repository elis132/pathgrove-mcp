# Changelog

## 0.1.0 — Unreleased

- Exact transactional per-root counters and atomic migration of existing metadata caches.
- Nonblocking cache-contention fallback, status metrics and shared query deadlines.
- Worker concurrency remains bounded after caller timeouts.
- Synthetic Windows growth measurements and explicitly labeled completion estimates.
- Portable MCP configuration templates with user-supplied absolute paths.

- Persistent SQLite filename and metadata index with incremental first-run batches.
- Packed Unicode filename search, metadata filters, stable sorting and pagination.
- Live filesystem updates and startup reconciliation.
- Windows NTFS restarts replay the change journal without administrator rights and revisit only changed folders; `last_reconcile` reports the method.
- OneDrive Files On-Demand and other cloud placeholder folders are indexed without downloading files; symlinks, junctions and mount points are still not followed.
- First indexing and full traversals about 3 times faster, with about 20 times fewer disk writes.
- Literal file-content predicates and direct recursive text search before index readiness.
- Explicit partial-coverage, skipped-file and freshness diagnostics.
- Seven read-only MCP tools and a status resource.
- Local plugin packaging and a bundled agent skill.
- Python implementation for Windows, Linux and Apple silicon/Intel macOS.
- Scoped file previews, duplicate-free overlapping roots and corrected fallback pagination.
- Indexed retention of inaccessible subtrees during full-volume reconciliation.
- Reproducible whole-volume MCP benchmarks with aggregate-only reports.
- Pathgrove package, CLI, MCP tools and configuration naming.

This version has not been published to PyPI.
