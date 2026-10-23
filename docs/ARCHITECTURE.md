# Architecture

Pathgrove is a headless Python MCP server over stdin/stdout. `IndexConfig` defines explicit roots and cache/query limits; `SearchBackend` adapts synchronous index and content operations to bounded worker threads.

## Filename storage and search

SQLite normalizes parent directories into `locations`; compact file rows store names and metadata. Generated columns/views provide folded names and full paths. WAL permits independent readers, and a process lock coordinates complete scan generations.

Casefolded UTF-8 names are packed into blocks of 4,096 IDs. Native `bytes.find` supplies candidate IDs for exact SQL filters and ordering. Blocks carry transactional revisions so writes from another process invalidate stale buffers. A small revision-keyed LRU retains only bounded candidate lists.

Broad single-letter name queries use an adaptive plan: collect at most 4,097 candidates, then traverse the filename B-tree in order if a term is broad. General searches reserve a fraction of the query budget for a fallback over at most 50,000 rows. Fallback pages explicitly label subset coverage and pagination.

## Scanning and freshness

The first discovered entry is committed immediately; subsequent batches publish every 256 entries or about 100 ms. Available records remain searchable while indexing proceeds. Native filesystem notifications update affected paths. Startup traversal reconciles offline changes; it does not resume from a durable OS journal.

Missing or unreadable subtrees retain cached entries and mark coverage incomplete. Links/reparse points are recorded but not followed. Experimental read-only NTFS enumeration cannot replace the portable traversal as the authoritative scanner.

Retention and deletion share an indexed subtree predicate over normalized parent locations. An inaccessible path does not trigger a repeated scan of every file's computed full path.

## Content search

`content:` predicates filter indexed metadata first, then stream candidate file contents in global metadata sort order. Offsets count content matches. `pathgrove_search_content` walks directory entries directly and filters extensions before opening files, independently of metadata discovery. Its results use traversal order rather than global sorting.

The text reader uses incremental decoding and overlapping chunk windows to match phrases and Unicode casefold across boundaries. Whole-word matches retain left/right context. Reads are bounded by time, file size, total bytes and candidate count. Skips and exhausted budgets produce explicit partial coverage. Text is not stored in SQLite.

## Platforms

Ordinary operation uses Python directory traversal, SQLite and filesystem notifications on Windows, Linux and macOS. Python 3.10+ and SQLite 3.35+ are required. Creation dates are unknown on POSIX platforms without birth times. Intel macOS retains a platform-specific `cryptography<49` dependency constraint; supported package versions remain an ongoing maintenance requirement.
