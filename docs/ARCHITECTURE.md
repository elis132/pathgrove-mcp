# Architecture

Pathgrove is a headless Python MCP server over stdin/stdout. `IndexConfig` defines explicit roots and cache/query limits; `SearchBackend` adapts synchronous index and content operations to bounded worker threads.

## Filename storage and search

SQLite normalizes parent directories into `locations`; compact file rows store names and metadata. Generated columns/views provide folded names and full paths. WAL permits independent readers, and a process lock coordinates complete scan generations.

Schema 3 maintains `root_counts` in the same transaction as file inserts, deletes and location moves. A one-time atomic backfill migrates existing caches. Progress refresh sums the configured roots' counters; filtered aggregate tools still execute exact snapshot queries.

Casefolded UTF-8 names are packed into blocks of 4,096 IDs. Native `bytes.find` supplies candidate IDs for exact SQL filters and ordering. Blocks carry transactional revisions so writes from another process invalidate stale buffers. A small revision-keyed LRU retains only bounded candidate lists.

Broad single-letter name queries use an adaptive plan: collect at most 4,097 candidates, then traverse the filename B-tree in order if a term is broad. General searches reserve a fraction of the query budget for a fallback over at most 50,000 rows. Fallback pages explicitly label subset coverage and pagination.

Indexed queries and aggregates bypass packed-name candidates when the cache lock is busy. Optional memory diagnostics return `null` with `memory_metrics_available: false` in that case. Queueing and SQL phases share one absolute deadline. A caller timing out does not free its worker slot until the underlying thread finishes. Deadlines remain cooperative for filesystem reads.

## Scanning and freshness

The first discovered entry is committed immediately; for the first two seconds batches publish every 256 entries or about 100 ms, then commits grow further apart (at most 20,000 entries or 5 seconds), because each commit rewrites every index page it touched. Available records remain searchable while indexing proceeds. Native filesystem notifications update affected paths. On Windows NTFS, startup resumes from the volume's change journal: after a walk, the index stores the journal position and every indexed directory's file ID. A restart reads the records written since, maps their directory IDs to paths and revisits only those directories. New, moved-in, renamed or replaced folders are recognized by an ID not recorded at that path and are walked. `FSCTL_READ_UNPRIVILEGED_USN_JOURNAL` needs no administrator rights; it omits filenames, which is why the index keeps directory IDs. Directory IDs are recorded only after a walk completes, so a walk interrupted by a crash is redone by the next replay. Unreadable subtrees are retried at each start. While running, the same replay repeats every 60 seconds, keeping the checkpoint inside the journal and repairing missed notifications; it carries retries forward instead of re-walking them. A recreated or wrapped journal, another volume, a replaced root or any read error falls back to a full traversal at the next start; a periodic replay never walks. NTFS refreshes a subfolder's times in its parent's listing lazily and does not journal a folder's own new modification time, so folder times are read from the folder itself, both when walking and when replaying. Other platforms and filesystems reconcile with a full traversal at startup. A full traversal also reads the journal as it goes and finally replays what changed during the walk, so even a walk that outlasts the journal's history usually ends with a usable checkpoint.

Journal limits: a change to a hard-linked file names only the directory of the link that recorded it, so a size or time seen through another link's directory updates when that directory changes again. Changes made while another operating system mounted the volume (for example Linux with ntfs-3g) or a restore from backup leave no records; each root therefore gets a full traversal at least once a week. The journal's size is a volume setting. An administrator can enlarge it so that restarts after longer breaks stay fast, for example `fsutil usn createjournal m=0x20000000 a=0x4000000 C:` (512 MiB); Pathgrove never changes it. Names that differ only in case inside a case-sensitive folder, or only by full case folding (`ß`/`ss`), share one index key, as they did before the journal.

Missing or unreadable subtrees retain cached entries and mark coverage incomplete. Symlinks, junctions and mount points are recorded but not followed. Other reparse points, such as OneDrive Files On-Demand folders, are walked: listing reads only names and attributes, so it never downloads cloud files. Experimental read-only NTFS enumeration cannot replace the portable traversal as the authoritative scanner.

Retention and deletion share an indexed subtree predicate over normalized parent locations. An inaccessible path does not trigger a repeated scan of every file's computed full path.

## Content search

`content:` predicates filter indexed metadata first, then stream candidate file contents in global metadata sort order. Offsets count content matches. `pathgrove_search_content` walks directory entries directly and filters extensions before opening files, independently of metadata discovery. Its results use traversal order rather than global sorting.

The text reader uses incremental decoding and overlapping chunk windows to match phrases and Unicode casefold across boundaries. Whole-word matches retain left/right context. Reads are bounded by time, file size, total bytes and candidate count. Skips and exhausted budgets produce explicit partial coverage. Text is not stored in SQLite.

## Platforms

Ordinary operation uses Python directory traversal, SQLite and filesystem notifications on Windows, Linux and macOS. Python 3.10+ and SQLite 3.35+ are required. Creation dates are unknown on POSIX platforms without birth times. Intel macOS retains a platform-specific `cryptography<49` dependency constraint; supported package versions remain an ongoing maintenance requirement.
