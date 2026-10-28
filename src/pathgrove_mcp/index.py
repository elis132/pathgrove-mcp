"""Independent, persistent SQLite filesystem index.

Startup reconciles each configured root once. On Windows NTFS it replays the
change journal from the last checkpoint and revisits only the directories it
names; without a usable checkpoint it walks the root. Ordinary watcher events
update only the affected path/subtree. WAL readers use separate connections;
generation scans are serialized across processes with an OS file lock, not a
process-local flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from stat import S_ISDIR

import regex as regex_lib

from pathgrove_mcp import content, journal
from pathgrove_mcp.names import CompactNames, SearchPage
from pathgrove_mcp.query import QueryError, QueryOptions, compile_query, split_content_query
from pathgrove_mcp.storage import UPSERT, initialize, register_functions


def _canonical(path: str | Path) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def _has_surrogates(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _long_name(path: str) -> str:
    """Expand 8.3 short names (LONGFO~1) in the existing part of a Windows path."""
    if "~" not in path:
        return path
    import ctypes

    head, tail = path, []
    while not os.path.lexists(head):
        head, name = os.path.split(head)
        if not name:
            return path
        tail.append(name)
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetLongPathNameW(head, buffer, len(buffer)):
        return path
    return os.path.join(buffer.value, *reversed(tail))


def _event_path(path: str) -> str:
    """Ask the OS for the current on-disk spelling without opening file contents.

    Windows notifications can carry 8.3 short names (LONGFO~1) and, after a
    case-only rename, the old case; a path through a link or junction keeps its
    notification spelling, with 8.3 names expanded. On macOS:

    On case-insensitive APFS, stat(old_name) still succeeds after a case-only
    rename. F_GETPATH distinguishes the current spelling and preserves symlinks
    with O_SYMLINK. Constants are from the macOS sys/fcntl.h SDK header; older
    Python versions do not export both open flags.
    """
    if sys.platform == "win32":
        real, long = os.path.realpath(path), _long_name(path)
        return _canonical(real) if _key(real) == _key(long) else long
    if sys.platform != "darwin":
        return path
    import fcntl

    try:
        descriptor = os.open(
            path, getattr(os, "O_EVTONLY", 0x8000) | getattr(os, "O_SYMLINK", 0x200000)
        )
        try:
            actual = fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024)
            return _canonical(os.fsdecode(actual.split(b"\0", 1)[0]))
        finally:
            os.close(descriptor)
    except OSError:
        return path  # Deleted/inaccessible paths retain their notification spelling.


def _key(path: str) -> str:
    return path.casefold() if os.name == "nt" else path


def _creation_time(stat, *, windows: bool | None = None) -> float:
    if hasattr(stat, "st_birthtime"):
        return stat.st_birthtime
    if windows if windows is not None else os.name == "nt":
        return stat.st_ctime  # Windows compatibility before Python exposed st_birthtime.
    return 0.0  # POSIX ctime is metadata change, never claim that it is birth time.


def _inside(path: str, root: str) -> bool:
    candidate, parent = _key(path), _key(root)
    return candidate == parent or candidate.startswith(parent.rstrip(os.sep) + os.sep)


def _sqlite_id(file_id: int) -> int:
    # SQLite integers are signed 64-bit; NTFS file IDs use the whole unsigned range.
    return file_id - (1 << 64) if file_id >= 1 << 63 else file_id


def _descendants_where(root: str, path: str):
    prefix = _key(path).rstrip(os.sep) + os.sep
    return (
        """location_id IN (SELECT id FROM locations WHERE root=? AND
            (parent_key=? OR (parent_key>=? AND parent_key<?)))""",
        (root, _key(path), prefix, prefix + chr(0x10FFFF)),
    )


def _under(path: str, keys: set[str]) -> bool:
    """Whether path or one of its ancestors is in keys (path keys)."""
    while True:
        if _key(path) in keys:
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


_ID_MASK = (1 << 64) - 1
_GONE = (FileNotFoundError, NotADirectoryError)


def _vanished(path: str) -> bool:
    """Whether path is gone, also under names Win32 normalizes (trailing dot or space)."""
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        path = ("\\\\?\\UNC\\" + path[2:]) if path.startswith("\\\\") else "\\\\?\\" + path
    return not os.path.lexists(path)


def _real(path: str) -> bool:
    """Whether path reaches its folder without passing through a link or junction."""
    return _key(os.path.realpath(path)) == _key(_canonical(path))


def _link(data) -> bool:
    """A symlink, junction or mount point (name-surrogate reparse point): never followed.

    Other reparse points, such as OneDrive Files On-Demand folders, are walked like any
    folder: listing reads only names and attributes, which never downloads a cloud file.
    os.stat reports tag 0 for the non-link reparse points it opens through.
    """
    return bool(
        getattr(data, "st_file_attributes", 0) & 0x400
        and getattr(data, "st_reparse_tag", 0) & 0x20000000
    )


def _plain_directory(path: str) -> bool:
    """A real directory, not a link or junction (those are recorded, never followed)."""
    try:
        data = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return S_ISDIR(data.st_mode) and not _link(data)


def _subtree_where(root: str, path: str):
    prefix = _key(path).rstrip(os.sep) + os.sep
    parent, name = os.path.split(path)
    return (
        """(location_id IN (SELECT id FROM locations WHERE root=? AND parent_key=?) AND name_key=?)
        OR location_id IN (SELECT id FROM locations WHERE root=? AND
            (parent_key=? OR (parent_key>=? AND parent_key<?)))""",
        (root, _key(parent), _key(name), root, _key(path), prefix, prefix + chr(0x10FFFF)),
    )


@contextlib.contextmanager
def _memory_temp_store(connection: sqlite3.Connection):
    """Per-row triggers otherwise write statement journals to temporary files (about 18
    small writes per row). Kept to bounded insert batches: large sweeps through the
    entries view materialize every matching row and must be able to spill to disk."""
    connection.execute("PRAGMA temp_store=MEMORY")
    try:
        yield
    finally:
        connection.execute("PRAGMA temp_store=DEFAULT")


@contextlib.contextmanager
def _process_lock(path: str, stop: threading.Event):
    """Serialize scan generations across all MCP processes sharing this cache."""
    handle = open(path, "a+b")  # noqa: SIM115 - finally releases OS lock before closing handle
    acquired = False
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not stop.is_set():
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError):
                stop.wait(0.05)
        if not acquired:
            raise InterruptedError("Index closed while waiting for scan lock")
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


CATCH_UP_SECONDS = 60
FOLLOW_SECONDS = 30  # How often a full walk reads the journal it will resume from.
# Changes made while another system mounted the volume, or a restore from backup,
# leave no journal records: walk each root fully at least this often.
FULL_TRAVERSAL_SECONDS = 7 * 24 * 3600


class LocalIndex:
    def __init__(self, db_path: str | Path, roots: list[str | Path]):
        if sqlite3.sqlite_version_info < (3, 35, 0):
            raise RuntimeError("SQLite >= 3.35 is required")
        self.db_path = _canonical(Path(db_path).resolve())
        # Watchers report physical paths (e.g. /private/var rather than /var on
        # macOS). Resolve roots/cache only; recorded child symlinks stay unfollowed.
        unique = sorted({_canonical(Path(r).resolve()) for r in roots}, key=len)
        self.roots: list[str] = []
        for root in unique:
            if not any(_inside(root, existing) for existing in self.roots):
                self.roots.append(root)
        if not self.roots:
            raise ValueError("At least one indexed root is required")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._watch_ready = threading.Event()
        self._changes: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._state = "not_started"
        self._last_error = ""
        self._coverage_incomplete = False
        self._coverage_note = ""
        self._coverage: dict[str, str] = {}  # root -> why its coverage is incomplete
        # Roots whose directory IDs are recorded, so the NTFS journal can be replayed.
        self._tracked: set[str] = set()
        self._last_reconcile: dict[str, str] = {}  # root -> how it was last reconciled
        self._watching = False
        self._scan_count = 0
        self._event_count = 0
        self._skipped = 0
        self._backend = "packed filename memory index + compact SQLite metadata"
        self._names = CompactNames(self.roots)
        self._query_context = threading.local()
        self._parent_ids = {}
        self._indexed_entries = 0
        self._last_count_refresh = 0.0
        self._writer = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._writer.execute("PRAGMA journal_mode=WAL")
        self._writer.execute("PRAGMA synchronous=NORMAL")
        self._writer.execute("PRAGMA busy_timeout=30000")
        # Five indexes per file row thrash the default 2 MiB cache during a walk.
        self._writer.execute("PRAGMA cache_size=-65536")
        try:
            with _process_lock(self.db_path + ".scan.lock", self._stop):
                initialize(self._writer)
        except BaseException:
            self._writer.close()
            raise
        self._writer.commit()
        self._refresh_count()

    async def start(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("Index is closed")
        if self._worker is not None:
            return
        self._state = "building"
        self._watcher = threading.Thread(
            target=self._watch_loop, name="filesystem-watch", daemon=True
        )
        self._watcher.start()
        self._worker = threading.Thread(
            target=self._work_loop, name="filesystem-index", daemon=True
        )
        self._worker.start()

    async def wait_ready(self, timeout: float = 60) -> None:
        if not await asyncio.to_thread(self._ready.wait, timeout):
            raise TimeoutError("Initial filesystem indexing is still in progress")
        if self._state == "error":
            raise RuntimeError(self._last_error)

    async def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        for worker in (self._watcher, self._worker):
            if worker is not None:
                await asyncio.to_thread(worker.join)
        with self._write_lock:
            self._writer.close()
        self._state = "closed"
        self._watching = False

    def _excluded(self, path: str) -> bool:
        # Never index cache writes and feed them back into the watcher.
        candidate, cache = _key(_canonical(path)), _key(self.db_path)
        return (
            candidate == cache
            or candidate.startswith(cache + "-")
            or candidate.startswith(cache + ".scan")
        )

    def _watch_loop(self) -> None:
        try:
            from watchfiles import watch

            existing = [root for root in self.roots if os.path.isdir(root)]
            if not existing:
                return
            self._watching = True
            for changes in watch(
                *existing,
                watch_filter=lambda change, path: not self._excluded(path),
                stop_event=self._stop,
                debounce=100,
                step=25,
                rust_timeout=100,
                yield_on_timeout=True,
            ):
                self._watch_ready.set()
                if changes:
                    self._changes.put(changes)
        except Exception as exc:
            if not self._stop.is_set():
                self._last_error = "Filesystem watching unavailable: " + str(exc)
        finally:
            self._watching = False
            self._watch_ready.set()

    def _work_loop(self) -> None:
        try:
            self._watch_ready.wait(2)
            self.catch_up()
            if not self._stop.is_set():
                self._state = "ready"
            self._ready.set()
            caught_up = time.monotonic()
            while not self._stop.is_set():
                try:
                    changes = self._changes.get(timeout=0.1)
                except queue.Empty:
                    changes = None
                if changes:
                    try:
                        self.apply_changes(changes)
                    except Exception as exc:
                        self._last_error = "Incremental update failed: " + str(exc)
                if self._tracked and time.monotonic() - caught_up >= CATCH_UP_SECONDS:
                    # Keeps the checkpoint inside the journal and repairs missed events.
                    try:
                        self.catch_up(periodic=True)
                        if self._last_error.startswith("Change journal catch-up failed"):
                            self._last_error = ""
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        self._last_error = "Change journal catch-up failed: " + str(exc)
                    caught_up = time.monotonic()
        except InterruptedError:
            self._ready.set()
        except Exception as exc:
            self._state = "error"
            self._last_error = str(exc)
            self._ready.set()

    def rescan(self) -> None:
        """Explicit full reconciliation; never called per ordinary watcher event."""
        self._reconcile(replay=False)

    def catch_up(self, *, periodic: bool = False) -> None:
        """Reconcile from the NTFS change journal wherever it proves what changed.

        Startup walks roots without a usable checkpoint. Periodic calls only replay,
        so a journal that stops working never causes repeated full walks.
        """
        self._reconcile(replay=True, periodic=periodic)

    def _reconcile(self, *, replay: bool, periodic: bool = False) -> None:
        requested_at = time.time()
        with _process_lock(self.db_path + ".scan.lock", self._stop):
            scanned = False
            for root in self.roots:
                if self._stop.is_set():
                    raise InterruptedError("Index stopped")
                if periodic and root not in self._tracked:
                    continue
                if not os.path.isdir(root):
                    self._last_error = (
                        f"Indexed root is unavailable: {root} (retaining cached entries)"
                    )
                    self._coverage[root] = (
                        "A configured root is unavailable; cached entries were retained."
                    )
                    continue
                with self._write_lock:
                    prior = self._writer.execute(
                        "SELECT scanned FROM roots WHERE root=?", (root,)
                    ).fetchone()
                    checkpointed = self._writer.execute(
                        "SELECT 1 FROM journal_checkpoints WHERE root=?", (root,)
                    ).fetchone()
                if prior and prior[0] is not None and prior[0] > requested_at:
                    # Another client reconciled this root while we waited. Still track
                    # its journal, for periodic catch-ups and directory IDs.
                    if checkpointed:
                        self._journal_position(root)
                    continue
                reason = "explicit rescan"
                failed = None
                if replay:
                    try:
                        failed = self._replay(root, periodic=periodic)
                    except journal.JournalUnavailable as exc:
                        reason = exc.strerror or str(exc)
                if failed is None and periodic:
                    self._tracked.discard(root)
                    self._last_reconcile[root] = (
                        f"change journal unavailable for {root} ({reason}); the next start walks it"
                    )
                    continue
                if failed is None:
                    if not scanned:
                        self._scan_count += 1
                        scanned = True
                    failed = self._full_scan(root, reason)
                self._coverage[root] = (
                    "Some paths were unavailable; cached entries under them were retained."
                    if failed
                    else ""
                )
            self._publish_coverage()
            self._refresh_count()

    def _publish_coverage(self) -> None:
        notes = [note for note in self._coverage.values() if note]
        self._coverage_incomplete = bool(notes)
        self._coverage_note = " ".join(dict.fromkeys(notes))
        if not notes and self._last_error.startswith("Indexed root is unavailable:"):
            self._last_error = ""

    def _full_scan(self, root: str, reason: str) -> list[str]:
        started = time.monotonic()
        checkpoint, unavailable = self._journal_position(root)  # Before reading the disk.
        generation = uuid.uuid4().hex
        with self._write_lock, self._writer:
            self._writer.execute(
                "INSERT INTO roots(root,generation) VALUES(?,?) ON CONFLICT(root) DO UPDATE SET generation=excluded.generation",
                (root, generation),
            )
            # Until this walk completes, no checkpoint may vouch for the index.
            self._writer.execute("DELETE FROM journal_checkpoints WHERE root=?", (root,))
            self._writer.execute("DELETE FROM directory_ids WHERE root=?", (root,))
        directories = [] if checkpoint else None
        # A long walk can outlast the journal's history; reading it as the walk goes
        # keeps the checkpoint reachable, and replays what changed during the walk.
        changes = journal.Changes()
        followed_at = time.monotonic()
        batch = []
        published_at = time.monotonic()
        failed_subtrees: list[str] = []
        for row in self._walk(root, root, generation, failed_subtrees, directories):
            batch.append(row)
            # First rows publish at once and early batches stay small, so searches see
            # results quickly. Each commit rewrites every index page it touched, so later
            # commits grow further apart: a long walk writes its index a few times, not
            # hundreds.
            elapsed = time.monotonic() - started
            limit, interval = (256, 0.1) if elapsed < 2 else (20000, min(5.0, elapsed / 4))
            if (
                not self._indexed_entries
                or len(batch) >= limit
                or time.monotonic() - published_at >= interval
            ):
                self._upsert(batch)
                self._record_directories(root, directories)
                batch.clear()
                published_at = time.monotonic()
                if checkpoint and time.monotonic() - followed_at >= FOLLOW_SECONDS:
                    checkpoint = self._follow(root, checkpoint, changes)
                    followed_at = time.monotonic()
        self._upsert(batch)
        self._record_directories(root, directories)
        # Inaccessible subtrees retain their previous snapshot rather than
        # masquerading as deletions. Retry reconciliation at next startup.
        with self._write_lock, self._writer:
            for subtree in failed_subtrees:
                where, parameters = _subtree_where(root, subtree)
                self._writer.execute(
                    f"UPDATE files SET generation=? WHERE {where}",
                    (generation, *parameters),
                )
            self._writer.execute(
                "DELETE FROM entries WHERE root=? AND generation<>?", (root, generation)
            )
            self._writer.execute("UPDATE roots SET scanned=? WHERE root=?", (time.time(), root))
        if checkpoint:
            checkpoint = self._follow(root, checkpoint, changes)
        if directories is not None and not checkpoint:
            unavailable = "the change journal did not cover the whole walk"
            self._tracked.discard(root)
        if checkpoint:
            # Folders that changed while they were being walked are revisited like a replay.
            self._apply_journal(root, changes, [], failed_subtrees)
            with self._write_lock, self._writer:
                self._save_checkpoint(root, checkpoint, failed_subtrees, walked=time.time())
        note = reason if checkpoint else unavailable
        self._last_reconcile[root] = (
            f"full traversal of {root} in {time.monotonic() - started:.1f} s"
            + (f" ({note})" if note else "")
        )
        return failed_subtrees

    def _journal_position(self, root: str) -> tuple[journal.Checkpoint | None, str]:
        """Where the change journal stands now, or None and why the root cannot be tracked."""
        try:
            with journal.Volume(root) as volume:
                checkpoint = volume.position()
        except journal.JournalUnavailable as exc:
            self._tracked.discard(root)
            # Other platforms simply have no NTFS journal; say why only on Windows.
            return None, (exc.strerror or str(exc)) if os.name == "nt" else ""
        self._tracked.add(root)
        return checkpoint, ""

    def _follow(self, root: str, checkpoint, changes: journal.Changes):
        """Read records since `checkpoint` into `changes`; the new position, or None."""
        try:
            with journal.Volume(root) as volume:
                return volume.changed_directories(checkpoint, changes)[1]
        except journal.JournalUnavailable:
            return None

    def _save_checkpoint(self, root: str, checkpoint, retry, walked: float | None = None) -> None:
        # Callers hold the write transaction that made the index match this position.
        self._writer.execute(
            "INSERT INTO journal_checkpoints(root,volume,journal_id,usn,retry,walked) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(root) DO UPDATE SET volume=excluded.volume,"
            "journal_id=excluded.journal_id,usn=excluded.usn,retry=excluded.retry,"
            "walked=COALESCE(excluded.walked,walked)",
            (
                root,
                checkpoint.volume,
                _sqlite_id(checkpoint.journal_id),
                checkpoint.usn,
                json.dumps(sorted(set(retry))),
                walked,
            ),
        )

    def _retry_later(self, root: str, paths: list[str]) -> None:
        """Have the next journal replay walk paths an incremental update could not read."""
        with self._write_lock, self._writer:
            row = self._writer.execute(
                "SELECT retry FROM journal_checkpoints WHERE root=?", (root,)
            ).fetchone()
            if row:
                self._writer.execute(
                    "UPDATE journal_checkpoints SET retry=? WHERE root=?",
                    (json.dumps(sorted({*json.loads(row[0]), *paths})), root),
                )

    def _replay(self, root: str, *, periodic: bool = False) -> list[str]:
        """Revisit only the directories the change journal names since the checkpoint.

        Raises JournalUnavailable when the journal cannot prove what changed: it was
        recreated or wrapped past the checkpoint, the root moved or was replaced, or
        a week passed since the last full traversal (changes made while another
        system mounted the volume, or after it was restored from a backup, are not
        journaled). Returns the paths that could not be read. Startup replays retry
        them; periodic replays carry them forward instead of walking them every minute.
        """
        started = time.monotonic()
        with self._write_lock:
            stored = self._writer.execute(
                "SELECT volume,journal_id,usn,retry,walked FROM journal_checkpoints WHERE root=?",
                (root,),
            ).fetchone()
        if stored is None:
            raise journal.JournalUnavailable("no change journal checkpoint yet")
        if not periodic and time.time() - (stored[4] or 0) > FULL_TRAVERSAL_SECONDS:
            raise journal.JournalUnavailable("weekly check for changes no journal recorded")
        since = journal.Checkpoint(stored[0], stored[1] & _ID_MASK, stored[2])
        with journal.Volume(root) as volume:
            changes, checkpoint = volume.changed_directories(since)
        self._tracked.add(root)
        try:
            root_id = os.stat(root, follow_symlinks=False).st_ino
        except OSError as exc:
            raise journal.JournalUnavailable(f"cannot identify the indexed root: {exc}") from exc
        if not self._mapped(root, root, root_id):
            raise journal.JournalUnavailable("the indexed root was replaced")
        retry = json.loads(stored[3])
        failed: list[str] = list(retry) if periodic else []
        revisited = self._apply_journal(root, changes, [] if periodic else retry, failed)
        with self._write_lock, self._writer:
            self._save_checkpoint(root, checkpoint, failed)
            self._writer.execute("UPDATE roots SET scanned=? WHERE root=?", (time.time(), root))
        self._last_reconcile[root] = (
            f"change journal: {revisited} changed directories under {root} "
            f"revisited in {time.monotonic() - started:.2f} s"
        )
        return failed

    def _apply_journal(self, root: str, changes, retry: list[str], failed: list[str]) -> int:
        """Bring the directories that `changes` names, and `retry` paths, up to date.

        Returns how many recorded directories the journal named.
        """
        with self._write_lock:
            row = self._writer.execute(
                "SELECT generation FROM roots WHERE root=?", (root,)
            ).fetchone()
        generation = row[0] if row and row[0] else "live"
        # path key -> (path, IDs of folders that arrived there, mode)
        targets = self._directory_paths(root, changes)
        revisited = len(targets)
        for path in retry:
            if _plain_directory(path) and _real(path):
                targets[_key(path)] = (path, None, "walk")
            else:
                # A file, or gone: its directory lists or drops it.
                parent = os.path.dirname(path)
                targets[_key(parent)] = (parent, None, "list")  # None: check every subfolder.
        walked: set[str] = set()
        pending: list[tuple] = []
        for key, (path, arrived, mode) in sorted(targets.items(), key=lambda item: len(item[0])):
            if self._stop.is_set():
                raise InterruptedError("Index stopped")
            if not _inside(path, root) or _under(path, walked):
                continue  # Parents sort first, so walked subtrees are already current.
            if mode == "walk":
                try:
                    info = os.stat(path, follow_symlinks=False)
                except OSError:
                    info = None  # The walk below records why.
                if info is not None:
                    with self._write_lock, self._writer:
                        self._refresh_folder_times(root, path, info)
                self._walk_into(root, path, failed)
                walked.add(key)
            else:
                self._reconcile_directory(root, path, generation, failed, walked, pending, arrived)
        self._upsert(pending)
        return revisited

    def _directory_paths(self, root: str, changes) -> dict[str, tuple]:
        """Recorded paths of the directories `changes` names, keyed by path key.

        IDs outside this root (most of a busy volume) simply have no row here.
        """
        arrivals = changes.arrivals()
        values = [_sqlite_id(file_id) for file_id in changes.directories if file_id < 1 << 64]
        targets: dict[str, tuple] = {}
        with self._write_lock:
            for start in range(0, len(values), 500):
                chunk = values[start : start + 500]
                for file_id, path in self._writer.execute(
                    "SELECT file_id,path FROM directory_ids WHERE root=? AND file_id IN ("
                    + ",".join("?" for _ in chunk)
                    + ")",
                    (root, *chunk),
                ):
                    target = targets.setdefault(_key(path), (path, set(), "list"))
                    target[1].update(arrivals.get(file_id & _ID_MASK, ()))
        return targets

    def _recorded_path(self, root: str, file_id: int) -> str | None:
        if file_id >= 1 << 64:
            return None
        with self._write_lock:
            row = self._writer.execute(
                "SELECT path FROM directory_ids WHERE root=? AND file_id=?",
                (root, _sqlite_id(file_id)),
            ).fetchone()
        return row[0] if row else None

    def _mapped(self, root: str, path: str, file_id: int) -> bool:
        """Whether this directory ID was last recorded at this very path."""
        recorded = self._recorded_path(root, file_id)
        return recorded is not None and _key(recorded) == _key(path)

    def _arrivals_in_place(self, root: str, path: str, arrived: set[int]) -> bool:
        """Whether every folder that arrived in `path` is already recorded where it is."""
        for file_id in arrived:
            recorded = self._recorded_path(root, file_id)
            if recorded is None or _key(os.path.dirname(recorded)) != _key(path):
                return False
            try:
                if os.stat(recorded, follow_symlinks=False).st_ino != file_id:
                    return False
            except OSError:
                return False
        return True

    def _record_directories(self, root: str, directories: list | None) -> None:
        if directories:
            with self._write_lock, self._writer:
                self._insert_directory_ids(root, directories)

    def _insert_directory_ids(self, root: str, directories: list) -> None:
        # Callers hold the write transaction.
        self._writer.executemany(
            "INSERT INTO directory_ids(root,file_id,path) VALUES(?,?,?) "
            "ON CONFLICT(root,file_id) DO UPDATE SET path=excluded.path",
            [(root, _sqlite_id(file_id), path) for path, file_id in directories],
        )
        directories.clear()

    def _forget_directory_ids(self, root: str, path: str) -> None:
        # Callers hold the write transaction. Without INDEXED BY, SQLite scans every
        # row of the root and evaluates path_key for each.
        prefix = _key(path).rstrip(os.sep) + os.sep
        self._writer.execute(
            "DELETE FROM directory_ids INDEXED BY directory_ids_path WHERE root=? AND path_key=?",
            (root, _key(path)),
        )
        self._writer.execute(
            "DELETE FROM directory_ids INDEXED BY directory_ids_path "
            "WHERE root=? AND path_key>=? AND path_key<?",
            (root, prefix, prefix + chr(0x10FFFF)),
        )

    def _replace_directory_ids(self, root: str, path: str, directories: list | None) -> None:
        """Record a completed walk's directory IDs, dropping vanished or replaced ones.

        Callers hold the write transaction. Recording only after the walk keeps an
        interrupted walk's folders unrecognized, so the next replay walks them again.
        """
        if directories is not None:
            self._forget_directory_ids(root, path)
            self._insert_directory_ids(root, directories)

    def _refresh_folder_times(self, root: str, path: str, info) -> None:
        # Callers hold the write transaction. NTFS journals a child's change, but not
        # the folder's own new modification time.
        parent, name = os.path.split(path)
        created = _creation_time(info)
        self._writer.execute(
            "UPDATE files SET modified=?,created=? WHERE name_key=? AND location_id="
            "(SELECT id FROM locations WHERE root=? AND parent_key=?) "
            "AND (modified IS NOT ? OR created IS NOT ?)",
            (info.st_mtime, created, _key(name), root, _key(parent), info.st_mtime, created),
        )

    def _reconcile_directory(
        self, root, path, generation, failed, walked, pending, arrived=None
    ) -> None:
        """Make one directory's own times and direct children match the disk.

        A path that leads through a junction or link (even one to the same folder) is
        skipped: the journal records of its parent cover it. New subfolders are walked, and so are subfolders whose ID is
        not recorded at their path (renamed, moved-in or replaced ones, even when the
        watcher already added their name). Only folders in `arrived` can be such
        folders; with None, every subfolder is checked.
        """
        is_root = _key(path) == _key(root)
        try:
            info = os.stat(path, follow_symlinks=False)
            plain = S_ISDIR(info.st_mode) and (is_root or not _link(info))
            if plain and not is_root and not _real(path):
                plain = False  # Reached through a junction or link.
            if plain:
                with os.scandir(path) as listing:
                    children = list(listing)
        except (FileNotFoundError, NotADirectoryError):
            return  # Removed or replaced: its parent's own journal record covers it.
        except OSError:
            self._skipped += 1
            failed.append(path)
            return
        if not plain:
            return  # A file or a link now (never followed).
        check_all = arrived is None or not self._arrivals_in_place(root, path, arrived)
        with self._write_lock, self._writer:
            if not is_root:
                self._refresh_folder_times(root, path, info)
            known = {
                _key(name): (name, *metadata)
                for name, *metadata in self._writer.execute(
                    "SELECT f.name,f.is_directory,f.size,f.modified,f.created FROM files f "
                    "JOIN locations p ON p.id=f.location_id WHERE p.root=? AND p.parent_key=?",
                    (root, _key(path)),
                )
            }
        for child in children:
            if self._excluded(child.path):
                continue
            # Unreadable children keep their cached row instead of being deleted below.
            prior = known.pop(_key(child.name), None)
            try:
                data = child.stat(follow_symlinks=False)
                directory = child.is_dir(follow_symlinks=False)
                descend = directory and not _link(data)
                renamed = prior is not None and prior[0] != child.name  # Case-only rename.
                demoted = prior is not None and prior[1] and not descend  # Now file or link.
                row = unseen = None
                if descend:
                    # A folder's times come from the folder itself (see _walk); its own
                    # journal records bring it here whenever they change.
                    unseen = prior is None or not prior[1] or renamed
                    if unseen or check_all:
                        data = os.stat(child.path, follow_symlinks=False)
                        unseen = unseen or not self._mapped(root, child.path, data.st_ino)
                    if unseen:
                        row = self._row(child.path, root, generation, data, True)
                else:
                    metadata = (int(directory), 0 if directory else data.st_size)
                    metadata += (data.st_mtime, _creation_time(data))
                    if prior is None or renamed or demoted or tuple(prior[1:]) != metadata:
                        row = self._row(child.path, root, generation, data, directory)
            except _GONE:
                if _vanished(child.path):
                    if prior is not None:
                        self._delete_subtree(root, child.path)  # Gone since the listing.
                    continue
                # A folder name Win32 cannot open (trailing dot or space): keep it and
                # its rows, and report it, as a full walk does.
                self._skipped += 1
                failed.append(child.path)
                if prior is None:
                    pending.append(
                        self._row(
                            child.path, root, generation, child.stat(follow_symlinks=False), True
                        )
                    )
                continue
            except UnicodeEncodeError:
                self._skipped += 1
                failed.append(path)
                continue
            except (OSError, ValueError):
                self._skipped += 1
                failed.append(path if _has_surrogates(child.path) else child.path)
                continue
            if demoted:
                self._delete_subtree(root, child.path)
            if row is not None:
                pending.append(row)
            if unseen:
                self._walk_into(root, child.path, failed)
                walked.add(_key(child.path))
        for name, *_metadata in known.values():
            self._delete_subtree(root, os.path.join(path, name))
        if len(pending) >= 256:
            self._upsert(pending)
            pending.clear()

    def _walk_into(self, root: str, path: str, failed: list[str]) -> None:
        """Re-index one subtree and drop descendants that no longer exist."""
        generation = uuid.uuid4().hex
        directories = [] if root in self._tracked else None
        unreadable: list[str] = []
        batch = []
        for row in self._walk(path, root, generation, unreadable, directories):
            batch.append(row)
            if len(batch) >= 1000:
                self._upsert(batch)
                batch.clear()
        self._upsert(batch)
        with self._write_lock, self._writer:
            for subtree in unreadable:
                where, parameters = _subtree_where(root, subtree)
                self._writer.execute(
                    f"UPDATE files SET generation=? WHERE {where}", (generation, *parameters)
                )
            where, parameters = _descendants_where(root, path)
            self._writer.execute(
                f"DELETE FROM files WHERE generation<>? AND {where}", (generation, *parameters)
            )
            self._replace_directory_ids(root, path, directories)
        failed.extend(unreadable)

    def _walk(self, path: str, root: str, generation: str, failed: list[str], directories=None):
        stack: list[tuple[str, object]] = [(path, None)]
        while stack:
            if self._stop.is_set():
                raise InterruptedError("Index stopped")
            parent, listed = stack.pop()
            try:
                if directories is not None:
                    data = os.stat(parent, follow_symlinks=False)
                    if listed is not None and (not S_ISDIR(data.st_mode) or _link(data)):
                        # Became a file or a link after it was listed: never followed.
                        yield self._row(parent, root, generation, data, S_ISDIR(data.st_mode))
                        continue
                    # Journal records name directories only by file ID.
                    directories.append((parent, data.st_ino))
                    # NTFS refreshes a subfolder's times in its parent's listing lazily.
                    times = (data.st_mtime, _creation_time(data))
                    if listed is not None and times != (listed.st_mtime, _creation_time(listed)):
                        yield self._row(parent, root, generation, data, True)
                with os.scandir(parent) as children:
                    for child in children:
                        if self._stop.is_set():
                            raise InterruptedError("Index stopped")
                        if self._excluded(child.path):
                            continue
                        try:
                            data = child.stat(follow_symlinks=False)
                            directory = child.is_dir(follow_symlinks=False)
                            yield self._row(child.path, root, generation, data, directory)
                            # Links, junctions and mount points are recorded, never followed.
                            if directory and not _link(data):
                                stack.append((child.path, data))
                        except UnicodeEncodeError:
                            self._skipped += 1
                            # The parent remains SQLite-safe and its previous
                            # snapshot is retained instead of deleting entries.
                            failed.append(parent)
                        except (OSError, ValueError):
                            self._skipped += 1
                            failed.append(parent if _has_surrogates(child.path) else child.path)
            except OSError as exc:
                if listed is not None and isinstance(exc, _GONE) and _vanished(parent):
                    continue  # Removed since its parent was listed: nothing to keep.
                self._skipped += 1
                failed.append(parent)

    @staticmethod
    def _row(path: str, root: str, generation: str, stat, directory: bool) -> tuple:
        path = _canonical(path)
        path.encode("utf-8")  # SQLite TEXT cannot bind surrogate-escaped POSIX names.
        name, parent = os.path.basename(path), os.path.dirname(path)
        extension = "" if directory else os.path.splitext(name)[1].lstrip(".")
        return (
            root,
            _key(path),
            path,
            path.casefold(),
            name,
            name.casefold(),
            parent,
            parent.casefold(),
            extension,
            extension.casefold(),
            int(directory),
            0 if directory else stat.st_size,
            stat.st_mtime,
            _creation_time(stat),
            generation,
        )

    def _upsert(self, rows: list[tuple]) -> None:
        if rows:
            with self._write_lock, _memory_temp_store(self._writer), self._writer:
                compact = []
                for row in rows:
                    key = (row[0], row[6])
                    location_id = self._parent_ids.get(key)
                    if location_id is None:
                        self._writer.execute(
                            "INSERT INTO locations(root,parent) VALUES(?,?) "
                            "ON CONFLICT(root,parent_key) DO UPDATE SET parent=excluded.parent",
                            key,
                        )
                        location_id = self._writer.execute(
                            "SELECT id FROM locations WHERE root=? AND parent_key=?",
                            (row[0], _key(row[6])),
                        ).fetchone()[0]
                        self._parent_ids[key] = location_id
                    compact.append((location_id, row[4], *row[10:]))
                self._writer.executemany(UPSERT, compact)
            # Publish the same committed batch to packed memory before moving
            # on. First queries do not have to load a million cold filenames.
            if not self._stop.is_set():
                connection = self._connect(30)
                try:
                    connection.execute("BEGIN")
                    self._names.snapshot(connection)
                finally:
                    connection.close()
            if not self._indexed_entries or time.monotonic() - self._last_count_refresh > 0.1:
                self._refresh_count()

    def _refresh_count(self):
        with self._write_lock:
            self._indexed_entries = self._writer.execute(
                "SELECT COALESCE(SUM(entries),0) FROM root_counts WHERE root IN ("
                + ",".join("?" for _ in self.roots)
                + ")",
                self.roots,
            ).fetchone()[0]
            self._last_count_refresh = time.monotonic()

    def apply_changes(self, changes) -> None:
        """Apply watchfiles (Change,path) batches; rename is delete+create."""
        with _process_lock(self.db_path + ".scan.lock", self._stop):
            self._apply_changes_unlocked(changes)
            self._refresh_count()

    def _apply_changes_unlocked(self, changes) -> None:
        events: dict[str, set[int]] = {}
        for kind, path in changes:
            original = _canonical(path)
            path = _event_path(original)
            if not self._excluded(path):
                # FSEvents reports both spellings as added on a case-only rename.
                if original != path:
                    for root in self.roots:
                        if _inside(original, root):
                            self._delete_subtree(root, original)
                events.setdefault(path, set()).add(int(kind))
        paths = sorted(events, key=len)
        # Coalesce directory events so a subtree is traversed only once per burst.
        handled: list[str] = []
        for path in paths:
            if any(_inside(path, parent) for parent in handled):
                continue
            roots = [root for root in self.roots if _inside(path, root)]
            if not roots:
                continue
            self._event_count += 1
            for root in roots:
                with self._write_lock:
                    row = self._writer.execute(
                        "SELECT generation FROM roots WHERE root=?", (root,)
                    ).fetchone()
                generation = row[0] if row else "live"
                try:
                    stat = os.stat(path, follow_symlinks=False)
                except FileNotFoundError:
                    self._delete_subtree(root, path)
                    handled.append(path)
                    continue
                except OSError as exc:
                    self._last_error = f"Cannot refresh {path}: {exc}"
                    continue
                directory = os.path.isdir(path) and not os.path.islink(path)
                with self._write_lock:
                    prior = self._writer.execute(
                        "SELECT is_directory FROM entries WHERE root=? AND path_key=?",
                        (root, _key(path)),
                    ).fetchone()
                indexed = prior is not None
                replaced = 3 in events[path] or (indexed and bool(prior[0]) != directory)
                # An add at an already indexed directory can represent an atomic
                # replacement/rename, even when the OS coalesces its delete event.
                if indexed and directory and 1 in events[path]:
                    replaced = True
                if (
                    replaced
                    and directory
                    and indexed
                    and bool(prior[0])
                    and root in self._tracked
                    and self._recorded_path(root, stat.st_ino) == path
                ):
                    # Same folder as recorded (a late or coalesced event): its contents
                    # are current, so deleting and re-walking them would only risk them.
                    replaced = False
                if replaced:
                    self._delete_subtree(root, path)
                if path != root:
                    self._upsert([self._row(path, root, generation, stat, directory)])
                # Existing directories receive metadata updates only: a parent
                # mtime notification must never trigger a root-wide rescan.
                if (
                    directory
                    and (not indexed or replaced)
                    and (path != root or 1 in events[path] or 3 in events[path])
                    and not _link(stat)
                ):
                    batch = []
                    failed_subtrees: list[str] = []
                    directories = [] if root in self._tracked else None
                    for descendant in self._walk(
                        path, root, generation, failed_subtrees, directories
                    ):
                        batch.append(descendant)
                        if len(batch) >= 1000:
                            self._upsert(batch)
                            batch.clear()
                    self._upsert(batch)
                    with self._write_lock, self._writer:
                        self._replace_directory_ids(root, path, directories)
                    if failed_subtrees:
                        self._coverage[root] = (
                            "Some paths were unavailable during incremental indexing; "
                            "coverage is incomplete until reconciliation succeeds."
                        )
                        self._publish_coverage()
                        self._retry_later(root, failed_subtrees)
                    handled.append(path)

    def _delete_subtree(self, root: str, path: str) -> None:
        where, parameters = _subtree_where(root, path)
        with self._write_lock, self._writer:
            self._writer.execute(f"DELETE FROM files WHERE {where}", parameters)
            self._forget_directory_ids(root, path)

    def _connect(self, timeout: float):
        if self._stop.is_set():
            raise RuntimeError("Index is closed")
        deadline = min(
            time.monotonic() + max(0.001, timeout),
            getattr(self._query_context, "deadline", float("inf")),
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError("Query exceeded its time limit")
        connection = sqlite3.connect(self.db_path, timeout=min(remaining, 30))
        connection.row_factory = sqlite3.Row
        register_functions(connection)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA case_sensitive_like=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        patterns = {}

        def safe_regex(pattern: str, value: str, flags: int) -> int:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Query time limit exceeded")
            key = (pattern, flags)
            compiled = patterns.get(key)
            if compiled is None:
                compiled = patterns[key] = regex_lib.compile(pattern, flags)
            return int(compiled.search(value, timeout=min(0.02, remaining)) is not None)

        connection.create_function("safe_regex", 3, safe_regex)
        connection.create_function(
            "filename_candidates", 1, lambda term: self._names.candidates(connection, term)
        )
        return connection

    def _where(self, query: str, *, include_candidates: bool = False, **flags):
        options = QueryOptions(
            **{
                key: flags[key]
                for key in (
                    "match_case",
                    "match_path",
                    "match_whole_word",
                    "regex",
                    "use_candidates",
                )
                if key in flags
            }
        )
        compiled = compile_query(query, options)
        root_clause = "e.root IN (" + ",".join("?" for _ in self.roots) + ")"
        result = (f"{root_clause} AND ({compiled.sql})", tuple(self.roots) + compiled.parameters)
        return result + (compiled.uses_candidates,) if include_candidates else result

    def _execute(
        self,
        sql: str,
        parameters: tuple,
        timeout: float = 5.0,
        *,
        ordered_sql=None,
        ordered_parameters=None,
        candidate_term=None,
    ):
        connection = self._connect(timeout)
        try:
            connection.execute("BEGIN")
            if ordered_sql is not None:
                candidates = self._names.limited_candidates(connection, candidate_term)
                if candidates is None:
                    sql = ordered_sql
                    parameters = ordered_parameters
                else:
                    connection.create_function("filename_candidates", 1, lambda term: candidates)
            return connection.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc) or "user-defined function" in str(exc):
                raise QueryError(
                    "Query exceeded its time limit (including bounded regex evaluation)"
                ) from exc
            raise
        finally:
            connection.close()

    def _search_statement(
        self, query: str, sort: str, descending: bool, max_results: int, offset: int, **flags
    ):
        # Common one-letter searches must not materialize millions of row IDs.
        # Walk the filename B-tree in order and stop at the requested page instead.
        literal = query.strip()
        if len(literal) == 3 and literal[0] == literal[-1] == '"':
            literal = literal[1:-1]
        single_letter = (
            len(literal) == 1
            and literal not in "*?"
            and not flags.get("regex")
            and not flags.get("match_path")
            and sort in ("name", "name-asc", "name-desc")
        )
        if single_letter and flags.pop("ordered_short_query", False):
            flags = {**flags, "use_candidates": False}
        where, parameters, uses_candidates = self._where(query, include_candidates=True, **flags)
        if sort.endswith("-desc"):
            descending, sort = True, sort[:-5]
        elif sort.endswith("-asc"):
            sort = sort[:-4]
        columns = {
            "name": "name_fold",
            "path": "full_path_fold",
            "size": "size",
            "date-modified": "modified",
            "date-created": "created",
            "extension": "extension_fold",
        }
        if sort not in columns:
            raise QueryError(f"Unsupported sort {sort!r}")
        if (
            sort == "date-created"
            and os.name != "nt"
            and not hasattr(os.stat_result, "st_birthtime")
        ):
            raise QueryError(
                "date-created sorting is unavailable: this platform does not expose file birth time"
            )
        direction = "DESC" if descending else "ASC"
        table = "candidate_entries" if uses_candidates else "entries"
        if single_letter and not uses_candidates:
            table = "name_ordered_entries"
        sql = f"""WITH selected AS MATERIALIZED (
            SELECT e.id,e.{columns[sort]} AS sort_key,e.full_path_fold AS path_key
            FROM {table} e WHERE {where}
            ORDER BY e.{columns[sort]} {direction},e.full_path_fold {direction},e.id {direction}
            LIMIT ? OFFSET ?)
            SELECT e.* FROM selected JOIN entries e ON e.id=selected.id
            ORDER BY selected.sort_key {direction},selected.path_key {direction},selected.id {direction}"""
        return sql, parameters + (max_results, offset)

    @contextlib.contextmanager
    def _request(self, timeout, deadline=None):
        previous = getattr(self._query_context, "deadline", float("inf"))
        self._query_context.deadline = min(
            previous, time.monotonic() + timeout, deadline if deadline is not None else float("inf")
        )
        owned = self._names._lock.acquire(blocking=False)
        try:
            yield owned
        finally:
            if owned:
                self._names._lock.release()
            self._query_context.deadline = previous

    def search(self, query="", *, timeout=5.0, deadline=None, **kwargs):
        with self._request(timeout, deadline) as available:
            kwargs["use_candidates"] = available and kwargs.get("use_candidates", True)
            return self._search(query, timeout=timeout, **kwargs)

    def _search(
        self,
        query: str = "",
        *,
        offset: int = 0,
        max_results: int = 100,
        sort: str = "name",
        descending: bool = False,
        timeout: float = 5.0,
        **flags,
    ) -> list[dict]:
        if offset < 0 or max_results < 0 or max_results > 100_000:
            raise QueryError("offset must be nonnegative and max_results between 0 and 100000")
        plan = split_content_query(
            query,
            QueryOptions(
                **{
                    key: flags[key]
                    for key in (
                        "match_case",
                        "match_path",
                        "match_whole_word",
                        "regex",
                        "use_candidates",
                    )
                    if key in flags
                }
            ),
        )
        if plan is not None:
            return self._search_content(
                plan, sort, descending, max_results, offset, timeout, **flags
            )
        sql, parameters = self._search_statement(
            query, sort, descending, max_results, offset, **flags
        )
        if not max_results:
            return SearchPage(
                partial=self._coverage_incomplete,
                note=self._coverage_note if self._coverage_incomplete else "",
            )
        partial = self._coverage_incomplete
        budget_partial = False
        ordered_sql, ordered_parameters = self._search_statement(
            query, sort, descending, max_results, offset, ordered_short_query=True, **flags
        )
        adaptive = (
            flags.get("use_candidates", True) and "FROM name_ordered_entries e" in ordered_sql
        )
        try:
            rows = self._execute(
                sql,
                parameters,
                min(timeout * 0.65, 0.25) if adaptive else timeout * 0.65,
                **(
                    {
                        "ordered_sql": ordered_sql,
                        "ordered_parameters": ordered_parameters,
                        "candidate_term": parameters[len(self.roots)],
                    }
                    if adaptive
                    else {}
                ),
            )
        except QueryError as exc:
            if "time limit" not in str(exc):
                raise
            # Reserve part of the request budget for useful bounded results.
            # This page is explicitly marked: ordering/pagination cover only the
            # inspected subset, never pretend it is the global sorted result.
            partial = True
            budget_partial = True
            rows = self._bounded_search(
                query, sort, descending, max_results, offset, timeout * 0.35, **flags
            )
        notes = []
        if self._coverage_incomplete and self._coverage_note:
            notes.append(self._coverage_note)
        if budget_partial:
            notes.append(
                "Search budget reached; results cover a bounded subset (at most 50000 index rows). "
                "Ordering and pagination apply only within that subset. Narrow the query for complete results."
            )
        return SearchPage(self._format_rows(rows), partial=partial, note=" ".join(notes))

    @staticmethod
    def _format_rows(rows):
        results = [
            dict(
                name=row["name"],
                path=row["parent"],
                full_path=row["full_path"],
                size=row["size"],
                date_modified=datetime.fromtimestamp(row["modified"]).isoformat(),
                is_directory=bool(row["is_directory"]),
                extension=row["extension"],
            )
            for row in rows
        ]
        for result, row in zip(results, rows, strict=False):
            if row["created"]:
                result["date_created"] = datetime.fromtimestamp(row["created"]).isoformat()
        return results

    def _search_content(self, plan, sort, descending, max_results, offset, timeout, **flags):
        # Select metadata before reading files, irrespective of predicate order.
        # Stream candidates in global sort order; pagination counts content hits.
        sql, parameters = self._search_statement(
            plan.metadata, sort, descending, content.MAX_FILES + 1, 0, **flags
        )
        notes = [self._coverage_note] if self._coverage_incomplete else []
        rows = []
        matches = 0
        scanner = content.ContentScanner(
            plan.terms,
            self.roots,
            min(time.monotonic() + timeout, getattr(self._query_context, "deadline", float("inf"))),
            self._stop,
        )
        if max_results:
            connection = None
            try:
                connection = self._connect(timeout)
                connection.execute("BEGIN")
                for row in connection.execute(sql, parameters):
                    if scanner.matches(row["full_path"]):
                        matches += 1
                        if matches > offset:
                            rows.append(row)
                        if len(rows) >= max_results:
                            break
            except content.ContentBudgetExceeded as exc:
                notes.append(f"Content search budget reached: {exc}.")
            except (sqlite3.OperationalError, QueryError) as exc:
                if not any(
                    term in str(exc)
                    for term in ("interrupted", "user-defined function", "time limit")
                ):
                    raise
                notes.append("Content search budget reached while selecting candidate files.")
            finally:
                if connection is not None:
                    connection.close()
        if scanner.skipped:
            notes.append(
                f"Skipped {scanner.skipped:,} files: binary/unsupported encoding, inaccessible, "
                "symlink/reparse point, outside roots, changed during reading, or larger than 16 MiB."
            )
        partial = self._coverage_incomplete or bool(notes)
        if partial:
            notes.append(
                "Content results cover only the inspected readable files; "
                "ordering and pagination apply to that subset. Narrow the query for complete results."
            )
        return SearchPage(self._format_rows(rows), partial=partial, note=" ".join(notes))

    def _bounded_search(self, query, sort, descending, max_results, offset, timeout, **flags):
        if time.monotonic() >= getattr(self._query_context, "deadline", float("inf")):
            return []
        where, parameters = self._where(query, **{**flags, "use_candidates": False})
        connection = self._connect(timeout)
        rows = []
        try:
            connection.execute("BEGIN")
            # No ORDER BY before LIMIT: a global sort would reproduce the stall.
            cursor = connection.execute(
                f"SELECT e.* FROM entries e WHERE e.id IN (SELECT id FROM files ORDER BY id LIMIT 50000) "
                f"AND {where}",
                parameters,
            )
            for row in cursor:
                rows.append(row)
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc) and "user-defined function" not in str(exc):
                raise
        finally:
            connection.close()
        if sort.endswith("-desc"):
            descending, sort = True, sort[:-5]
        elif sort.endswith("-asc"):
            sort = sort[:-4]
        column = {
            "name": "name_fold",
            "path": "full_path_fold",
            "size": "size",
            "date-modified": "modified",
            "date-created": "created",
            "extension": "extension_fold",
        }[sort]
        rows.sort(
            key=lambda row: (row[column], row["full_path_fold"], row["id"]), reverse=descending
        )
        return rows[offset : offset + max_results]

    def count(self, query: str = "", *, timeout: float = 5.0, deadline=None, **flags) -> int:
        with self._request(timeout, deadline) as available:
            flags["use_candidates"] = available and flags.get("use_candidates", True)
            where, parameters, uses_candidates = self._where(
                query, include_candidates=True, **flags
            )
            table = "candidate_entries" if uses_candidates else "entries"
            return self._execute(
                f"SELECT COUNT(*) FROM {table} e WHERE {where}", parameters, timeout
            )[0][0]

    def sum_size(self, query: str = "", *, timeout: float = 5.0, deadline=None, **flags) -> int:
        with self._request(timeout, deadline) as available:
            flags["use_candidates"] = available and flags.get("use_candidates", True)
            where, parameters, uses_candidates = self._where(
                query, include_candidates=True, **flags
            )
            table = "candidate_entries" if uses_candidates else "entries"
            return self._execute(
                f"SELECT COALESCE(SUM(e.size),0) FROM {table} e WHERE e.is_directory=0 AND {where}",
                parameters,
                timeout,
            )[0][0]

    def explain(
        self,
        query: str = "",
        *,
        sort: str = "name",
        descending: bool = False,
        max_results: int = 50,
        offset: int = 0,
        **flags,
    ) -> list[str]:
        sql, parameters = self._search_statement(
            query, sort, descending, max_results, offset, **flags
        )
        plan = [row[3] for row in self._execute("EXPLAIN QUERY PLAN " + sql, parameters)]
        ordered_sql, ordered_parameters = self._search_statement(
            query, sort, descending, max_results, offset, ordered_short_query=True, **flags
        )
        if "FROM name_ordered_entries e" in ordered_sql:
            plan += ["ADAPTIVE: over 4096 matches uses filename order (250ms primary budget)"]
            plan += [
                row[3]
                for row in self._execute("EXPLAIN QUERY PLAN " + ordered_sql, ordered_parameters)
            ]
        return plan

    def status(self) -> dict:
        metrics = self._names.metrics()
        return dict(
            state=self._state,
            indexed_entries=self._indexed_entries,
            roots=self.roots,
            db_path=self.db_path,
            backend=self._backend,
            last_error=self._last_error,
            watching=self._watching,
            scan_count=self._scan_count,
            event_count=self._event_count,
            skipped_entries=self._skipped,
            filename_memory_bytes=metrics[0] if metrics is not None else None,
            candidate_cache_bytes=metrics[1] if metrics is not None else None,
            memory_metrics_available=metrics is not None,
            partial=self._state != "ready" or self._coverage_incomplete,
            coverage_note=self._coverage_note,
            last_reconcile="; ".join(self._last_reconcile.values()),
        )
