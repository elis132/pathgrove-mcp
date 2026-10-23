"""Indexing cost, cache contention and request-wide deadline regressions."""

import threading
import time
from types import SimpleNamespace

import pytest

from pathgrove_mcp.index import LocalIndex
from pathgrove_mcp.query import QueryError


async def test_count_refresh_work_does_not_grow_with_file_count(tmp_path):
    index = LocalIndex(tmp_path / "cache.db", [tmp_path])
    data = SimpleNamespace(st_size=1, st_mtime=1791800000, st_ctime=1791800000)
    try:
        index._upsert(
            [
                index._row(str(tmp_path / f"entry-{number}"), str(tmp_path), "x", data, False)
                for number in range(10000)
            ]
        )
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return steps > 1000

        index._writer.set_progress_handler(progress, 1)
        try:
            index._refresh_count()
        finally:
            index._writer.set_progress_handler(None, 0)
        assert index.status()["indexed_entries"] == 10000
    finally:
        await index.close()


async def test_root_counts_survive_migration_upserts_rollback_and_shared_writers(tmp_path):
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
    db = tmp_path / "cache.db"
    data = SimpleNamespace(st_size=1, st_mtime=1791800000, st_ctime=1791800000)
    first = LocalIndex(db, roots)
    row = first._row(str(roots[0] / "invoice.txt"), str(roots[0]), "x", data, False)
    first._upsert([row, row])
    # Recreate the previous schema without counters, with existing committed rows.
    with first._writer:
        for trigger in ("root_counts_ai", "root_counts_ad", "root_counts_au"):
            first._writer.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        first._writer.execute("DROP TABLE IF EXISTS root_counts")
        first._writer.execute("PRAGMA user_version=2")
    await first.close()
    one, two = LocalIndex(db, [roots[0]]), LocalIndex(db, [roots[1]])
    try:
        one._refresh_count()
        assert one.status()["indexed_entries"] == 1
        assert (
            one._writer.execute(
                "SELECT entries FROM root_counts WHERE root=?", (str(roots[0]),)
            ).fetchone()[0]
            == 1
        )
        assert two.status()["indexed_entries"] == 0
        two._upsert([two._row(str(roots[1] / "other.txt"), str(roots[1]), "x", data, False)])
        with pytest.raises(RuntimeError), one._writer:
            one._writer.execute("DELETE FROM files")
            raise RuntimeError("rollback")
        one._refresh_count()
        two._refresh_count()
        assert one.status()["indexed_entries"] == two.status()["indexed_entries"] == 1
        with one._writer:
            one._writer.execute(
                "UPDATE files SET location_id=(SELECT id FROM locations WHERE root=?) WHERE location_id=(SELECT id FROM locations WHERE root=?)",
                (str(roots[1]), str(roots[0])),
            )
        one._refresh_count()
        two._refresh_count()
        assert one.status()["indexed_entries"] == 0
        assert two.status()["indexed_entries"] == 2
        two._delete_subtree(str(roots[1]), str(roots[1]))
        two._refresh_count()
        assert two.status()["indexed_entries"] == 0
    finally:
        await one.close()
        await two.close()


async def test_busy_filename_cache_does_not_block_status_or_sql_search(tmp_path):
    index = LocalIndex(tmp_path / "cache.db", [tmp_path])
    data = SimpleNamespace(st_size=1, st_mtime=1791800000, st_ctime=1791800000)
    index._upsert([index._row(str(tmp_path / "invoice.txt"), str(tmp_path), "x", data, False)])
    held, release = threading.Event(), threading.Event()

    def hold_cache():
        with index._names._lock:
            held.set()
            release.wait(2)

    holder = threading.Thread(target=hold_cache)
    holder.start()
    assert held.wait(2)
    try:
        started = time.monotonic()
        status = index.status()
        for query in ("invoice", "i"):
            assert index.search(query, timeout=0.5)[0]["name"] == "invoice.txt"
        assert index.count("invoice", timeout=0.5) == 1
        assert index.sum_size("invoice", timeout=0.5) == 1
        assert time.monotonic() - started < 1
        assert status["filename_memory_bytes"] is None
        assert not status["memory_metrics_available"]
    finally:
        release.set()
        holder.join()
        await index.close()


async def test_fallback_does_not_get_a_fresh_deadline(tmp_path, monkeypatch):
    index = LocalIndex(tmp_path / "cache.db", [tmp_path])

    def consume_budget(*args, **kwargs):
        time.sleep(0.06)
        raise QueryError("Query exceeded its time limit")

    monkeypatch.setattr(index, "_execute", consume_budget)
    original = index._connect
    connections = []

    def track(timeout):
        connections.append(timeout)
        return original(timeout)

    monkeypatch.setattr(index, "_connect", track)
    try:
        page = index.search("invoice", timeout=0.03)
        assert page.partial and "budget" in page.note.lower()
        assert connections == []
    finally:
        await index.close()
