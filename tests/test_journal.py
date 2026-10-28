"""NTFS change journal replay: record parsing, replay logic and real Windows restarts."""

import asyncio
import json
import os
import random
import shutil
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from pathgrove_mcp import journal
from pathgrove_mcp.index import LocalIndex


def _v2(file_id, parent_id, attributes, name="x", reason=0x100):
    encoded = name.encode("utf-16-le")
    body = struct.pack(
        "<IHHQQqqIIII", 0, 2, 0, file_id, parent_id, 1, 0, reason, 0, 0, attributes
    ) + struct.pack("<HH", len(encoded), 60)
    record = body + encoded
    record += b"\0" * (-len(record) % 8)
    return struct.pack("<I", len(record)) + record[4:]


def _v3(file_id, parent_id, attributes, reason=0x100):
    body = (
        struct.pack("<IHH", 0, 3, 0)
        + file_id.to_bytes(16, "little")
        + parent_id.to_bytes(16, "little")
        + struct.pack("<qqIIII", 1, 0, reason, 0, 0, attributes)
        + struct.pack("<HH", 0, 76)  # Unprivileged reads omit the filename.
    )
    return struct.pack("<I", len(body)) + body[4:]


def test_parses_v2_and_nameless_v3_records():
    data = _v2(11, 5, 0x20) + _v3(12, 11, 0x10, 0x200) + _v3(1 << 70, 12, 0x20)
    assert list(journal.parse_records(data)) == [
        (11, 5, 0x100, 0x20),
        (12, 11, 0x200, 0x10),
        (1 << 70, 12, 0x100, 0x20),
    ]


def test_changes_know_where_changed_folders_live_now():
    changes = journal.Changes()
    changes.add(_v3(21, 5, 0x20) + _v3(22, 5, 0x10) + _v3(23, 22, 0x20))
    changes.add(_v3(30, 5, 0x10) + _v3(30, 40, 0x10))  # Folder 30 moved from 5 to 40.
    changes.add(_v3(31, 5, 0x10) + _v3(31, 5, 0x10, reason=0x200))  # Folder 31 deleted.
    assert changes.directories == {5, 22, 30, 31, 40}
    assert changes.arrivals() == {5: {22}, 40: {30}}


@pytest.mark.parametrize(
    ("since", "message"),
    [
        (journal.Checkpoint("vol", 7, 500), "no longer reaches"),  # Before FirstUsn.
        (journal.Checkpoint("vol", 7, 3000), "no longer reaches"),  # After NextUsn.
        (journal.Checkpoint("vol", 8, 1500), "recreated"),
        (journal.Checkpoint("other", 7, 1500), "another volume"),
    ],
)
def test_checkpoint_outside_the_journal_refuses_replay(since, message):
    volume = object.__new__(journal.Volume)
    volume.name = "vol"
    volume._bounds = lambda: (7, 1000, 2000)  # Journal ID, FirstUsn, NextUsn.
    with pytest.raises(journal.JournalUnavailable, match=message):
        volume.changed_directories(since)


@pytest.mark.parametrize(
    "data",
    [
        b"\x01\x02\x03",  # Truncated header.
        struct.pack("<IHH", 8, 9, 0),  # Unknown version.
        struct.pack("<IHH", 4096, 3, 0) + b"\0" * 72,  # Length past the buffer.
        struct.pack("<IHH", 16, 3, 0) + b"\0" * 8,  # Shorter than a V3 record.
    ],
)
def test_malformed_records_refuse_replay(data):
    with pytest.raises(journal.JournalUnavailable):
        list(journal.parse_records(data))


class FakeVolume:
    """A journal that reports exactly the directory IDs a test names."""

    changed: set[int] = set()
    usn = 100

    def __init__(self, path):
        self.name = "fake-volume"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def position(self):
        return journal.Checkpoint(self.name, 7, FakeVolume.usn)

    def changed_directories(self, since, changes=None):
        if since.journal_id != 7:
            raise journal.JournalUnavailable("The change journal was recreated")
        FakeVolume.usn += 1
        changes = journal.Changes() if changes is None else changes
        changes.directories |= set(FakeVolume.changed)
        return changes, journal.Checkpoint(self.name, 7, FakeVolume.usn)


@pytest.fixture
def fake_journal(monkeypatch):
    FakeVolume.changed = set()
    monkeypatch.setattr(journal, "Volume", FakeVolume)
    return FakeVolume


def _ino(path):
    return os.stat(path, follow_symlinks=False).st_ino


async def test_replay_revisits_only_reported_directories(tmp_path, fake_journal):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "old.txt").write_text("old")
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        (root / "a" / "old.txt").unlink()
        (root / "a" / "reported.txt").write_text("new")
        (root / "b" / "unreported.txt").write_text("new")
        fake_journal.changed = {_ino(root / "a"), 1 << 40}  # Plus an ID outside the root.
        index.catch_up()
        assert index.count("reported.txt") == 1
        assert index.count("old.txt") == 0
        assert index.count("unreported.txt") == 0  # Proves no walk happened.
        status = index.status()
        assert status["last_reconcile"].startswith("change journal: 1 changed directories")
        assert status["scan_count"] == 1
    finally:
        await index.close()


async def test_replay_retries_unreadable_subtrees(tmp_path, fake_journal, monkeypatch):
    import pathgrove_mcp.index as module

    root = tmp_path / "root"
    private = root / "private"
    private.mkdir(parents=True)
    (private / "secret.txt").write_text("x")
    index = LocalIndex(tmp_path / "index.db", [root])
    real_scandir = module.os.scandir

    def denied(path):
        if str(path) == str(private):
            raise PermissionError("fixture denial")
        return real_scandir(path)

    try:
        monkeypatch.setattr(module.os, "scandir", denied)
        index.rescan()
        assert "unavailable" in index.status()["coverage_note"]
        assert index.count("secret.txt") == 0
        monkeypatch.setattr(module.os, "scandir", real_scandir)
        index.catch_up(periodic=True)  # Carried forward, not walked every minute.
        assert index.count("secret.txt") == 0
        assert "unavailable" in index.status()["coverage_note"]
        (private / "later.txt").write_text("x")  # Changes the retried folder's own time.
        index.catch_up()  # Startup: nothing reported, only the recorded retry is walked.
        assert index.count("secret.txt") == 1
        [row] = [row for row in index.search("private") if row["is_directory"]]
        expected = datetime.fromtimestamp(os.stat(private).st_mtime).isoformat()
        assert row["date_modified"] == expected
        assert index.status()["coverage_note"] == ""
        assert index.status()["scan_count"] == 1
    finally:
        await index.close()


async def test_client_that_waited_for_another_reconciliation_still_tracks(tmp_path, fake_journal):
    root = tmp_path / "root"
    root.mkdir()
    one = LocalIndex(tmp_path / "shared.db", [root])
    two = LocalIndex(tmp_path / "shared.db", [root])
    try:
        one.rescan()
        with two._writer:  # As if `one` finished while `two` waited for the scan lock.
            two._writer.execute("UPDATE roots SET scanned=?", (time.time() + 60,))
        two.catch_up()
        assert two.status()["scan_count"] == 0
        assert two.roots[0] in two._tracked
    finally:
        await one.close()
        await two.close()


async def test_folder_removed_after_listing_is_not_reported_unreadable(
    tmp_path, fake_journal, monkeypatch
):
    import pathgrove_mcp.index as module

    root = tmp_path / "root"
    root.mkdir()
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        (root / "new").mkdir()
        real_stat = module.os.stat

        def vanished(path, *args, **kwargs):
            if str(path) == str(root / "new"):
                os.rmdir(path)  # Listed, then removed before its stat.
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(module.os, "stat", vanished)
        fake_journal.changed = {_ino(root)}
        index.catch_up()
        assert index.status()["coverage_note"] == ""
        assert index.count("new") == 0
    finally:
        await index.close()


async def test_late_watcher_add_for_a_recorded_folder_keeps_its_rows(
    tmp_path, fake_journal, monkeypatch
):
    root = tmp_path / "root"
    (root / "d").mkdir(parents=True)
    (root / "d" / "x.txt").write_text("x")
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        deleted = []
        original = index._delete_subtree

        def recording(scope, path):
            deleted.append(path)
            original(scope, path)

        monkeypatch.setattr(index, "_delete_subtree", recording)
        index.apply_changes({(1, str(root / "d"))})  # Already indexed and recorded.
        assert deleted == []
        assert index.count("x.txt") == 1
    finally:
        await index.close()


async def test_startup_walks_fully_once_a_week(tmp_path, fake_journal):
    root = tmp_path / "root"
    root.mkdir()
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        index.catch_up()
        assert index.status()["scan_count"] == 1
        with index._writer:
            index._writer.execute("UPDATE journal_checkpoints SET walked=walked-8*86400")
        index.catch_up(periodic=True)  # Periodic replays never walk.
        assert index.status()["scan_count"] == 1
        index.catch_up()
        assert index.status()["scan_count"] == 2
        assert "weekly" in index.status()["last_reconcile"]
    finally:
        await index.close()


async def test_add_and_delete_event_for_an_unindexed_folder_indexes_it(tmp_path, fake_journal):
    root = tmp_path / "root"
    root.mkdir()
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        (root / "X").mkdir()
        (root / "X" / "inside.txt").write_text("x")
        (root / "other.txt").write_text("x")
        # mkdir X; rmdir X; mkdir X within one debounce window.
        index.apply_changes(
            {(1, str(root / "X")), (3, str(root / "X")), (1, str(root / "other.txt"))}
        )
        assert index.count("inside.txt") == 1 and index.count("other.txt") == 1
    finally:
        await index.close()


async def test_untracked_root_still_rewalks_on_a_folder_add_event(tmp_path, monkeypatch):
    def unavailable(path):
        raise journal.JournalUnavailable("no journal here")

    monkeypatch.setattr(journal, "Volume", unavailable)
    root = tmp_path / "root"
    (root / "d").mkdir(parents=True)
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        deleted = []
        original = index._delete_subtree

        def recording(scope, path):
            deleted.append(path)
            original(scope, path)

        monkeypatch.setattr(index, "_delete_subtree", recording)
        index.apply_changes({(1, str(root / "d"))})
        assert deleted == [str(root / "d")]
    finally:
        await index.close()


async def test_incremental_failure_is_retried_by_the_next_startup(
    tmp_path, fake_journal, monkeypatch
):
    import pathgrove_mcp.index as module

    root = tmp_path / "root"
    root.mkdir()
    index = LocalIndex(tmp_path / "index.db", [root])
    real_scandir = module.os.scandir
    try:
        index.rescan()
        private = root / "private"
        private.mkdir()
        (private / "secret.txt").write_text("x")

        def denied(path):
            if str(path) == str(private):
                raise PermissionError("fixture denial")
            return real_scandir(path)

        monkeypatch.setattr(module.os, "scandir", denied)
        index.apply_changes({(1, str(private))})
        retry = index._writer.execute("SELECT retry FROM journal_checkpoints").fetchone()[0]
        assert str(private) in json.loads(retry)
        monkeypatch.setattr(module.os, "scandir", real_scandir)
        index.catch_up()
        assert index.count("secret.txt") == 1
        assert index.status()["coverage_note"] == ""
    finally:
        await index.close()


async def test_root_back_after_being_unavailable_clears_the_error(
    tmp_path, fake_journal, monkeypatch
):
    import pathgrove_mcp.index as module

    root = tmp_path / "root"
    root.mkdir()
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        real_isdir = module.os.path.isdir
        monkeypatch.setattr(
            module.os.path,
            "isdir",
            lambda path: False if str(path) == str(root) else real_isdir(path),
        )
        index.catch_up()
        assert index.status()["last_error"].startswith("Indexed root is unavailable")
        monkeypatch.setattr(module.os.path, "isdir", real_isdir)
        # Backdate the scan time: two scans can share one Windows clock tick.
        index._writer.execute("UPDATE roots SET scanned=scanned-1")
        scanned = index._writer.execute("SELECT scanned FROM roots").fetchone()[0]
        index.catch_up()
        assert index.status()["last_error"] == ""
        assert index.status()["coverage_note"] == ""
        assert index._writer.execute("SELECT scanned FROM roots").fetchone()[0] > scanned
    finally:
        await index.close()


async def test_journal_lost_during_a_walk_is_reported_and_untracks_the_root(
    tmp_path, fake_journal, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        monkeypatch.setattr(index, "_follow", lambda *args: None)
        index.rescan()
        assert "did not cover the whole walk" in index.status()["last_reconcile"]
        assert not index._tracked
        assert index._writer.execute("SELECT COUNT(*) FROM journal_checkpoints").fetchone()[0] == 0
    finally:
        await index.close()


async def test_running_server_catches_up_periodically(tmp_path, fake_journal, monkeypatch):
    import pathgrove_mcp.index as module

    monkeypatch.setattr(module, "CATCH_UP_SECONDS", 0.05)
    root = tmp_path / "root"
    root.mkdir()
    index = LocalIndex(tmp_path / "index.db", [root])
    calls, original = [], index.catch_up

    def counted(*, periodic=False):
        calls.append(periodic)
        if periodic and calls.count(True) == 1:
            raise RuntimeError("transient")
        original(periodic=periodic)

    monkeypatch.setattr(index, "catch_up", counted)
    try:
        await index.start()
        await index.wait_ready()
        for _ in range(100):
            if calls.count(True) >= 2 and not index.status()["last_error"]:
                break
            await asyncio.sleep(0.05)
        assert calls.count(True) >= 2
        assert index.status()["last_error"] == ""  # The failure was cleared by a later success.
    finally:
        await index.close()


async def test_unusable_journal_walks_at_startup_but_never_periodically(tmp_path, fake_journal):
    root = tmp_path / "root"
    root.mkdir()
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        with index._writer:
            index._writer.execute("UPDATE journal_checkpoints SET journal_id=8")
        (root / "offline.txt").write_text("x")
        index.catch_up(periodic=True)
        assert index.status()["scan_count"] == 1
        assert "the next start walks it" in index.status()["last_reconcile"]
        assert not index._tracked
        index.catch_up()
        assert index.status()["scan_count"] == 2
        assert "recreated" in index.status()["last_reconcile"]
        assert index.count("offline.txt") == 1
    finally:
        await index.close()


def _ntfs(path):
    try:
        with journal.Volume(str(path)) as volume:
            volume.position()
    except journal.JournalUnavailable as exc:
        return str(exc)
    return ""


def _snapshot(index):
    return {
        (
            row["full_path"],
            row["is_directory"],
            row["size"],
            row["date_modified"],
            row.get("date_created"),
        )
        for row in index.search("", max_results=100000)
    }


async def _replayed_equals_fresh(tmp_path, root, db):
    """Restart from db; the replay must use the journal and match a fresh full walk."""
    reopened = LocalIndex(db, [root])
    fresh = LocalIndex(tmp_path / f"fresh-{len(list(tmp_path.glob('fresh-*')))}.db", [root])
    try:
        reopened.catch_up()
        status = reopened.status()
        assert status["last_reconcile"].startswith("change journal"), status
        assert status["scan_count"] == 0
        fresh.rescan()
        replayed, expected = _snapshot(reopened), _snapshot(fresh)
        assert replayed == expected, (sorted(replayed - expected), sorted(expected - replayed))
        return [os.path.basename(entry[0]) for entry in replayed]
    finally:
        await reopened.close()
        await fresh.close()


def _junction(link, target):
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
    )


windows_ntfs = pytest.mark.skipif(os.name != "nt", reason="NTFS change journal")


@windows_ntfs
async def test_restart_replays_real_ntfs_journal_instead_of_walking(tmp_path):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    folders = ("a/sub", "b/inner", "c/deep", "d", "e", "g", "moved", "h/same", "k/same")
    for directory in (*folders, "p/q", "Case/inner", "j"):
        (root / directory).mkdir(parents=True)
    (outside / "target").mkdir(parents=True)
    (outside / "target" / "t.txt").write_text("x")
    for name in ("keep.txt", "gone.txt", "grow.txt", "f"):
        (root / name).write_text("x")
    for name in ("a/sub/s.txt", "b/inner/deep.txt", "c/deep/c.txt", "d/old.txt", "e/x.txt"):
        (root / name).write_text("x")
    for name in ("g/old.txt", "h/same/from-h.txt", "k/same/from-k.txt", "Case/inner/i.txt"):
        (root / name).write_text("x")
    (root / "j" / "old.txt").write_text("x")
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    await index.close()

    (root / "gone.txt").unlink()
    (root / "grow.txt").write_text("much longer content")
    (root / "a" / "new.txt").write_text("x")
    (root / "b").rename(root / "b2")
    shutil.rmtree(root / "c")
    shutil.rmtree(root / "d")
    (root / "d").mkdir()
    (root / "d" / "fresh.txt").write_text("x")
    shutil.rmtree(root / "e")
    (root / "e").write_text("now a file")
    (root / "f").unlink()
    (root / "f").mkdir()
    (root / "f" / "y.txt").write_text("x")
    shutil.rmtree(root / "g")
    (root / "g").mkdir()  # Empty replacement: only its own journal record names it.
    (outside / "m").mkdir()
    (outside / "m" / "z.txt").write_text("x")
    (outside / "m").rename(root / "moved" / "m")
    (root / "a" / "sub").rename(outside / "sub")
    # Swap folders whose subfolders share names: only directory IDs tell them apart.
    (root / "h").rename(root / "h-old")
    (root / "k").rename(root / "h")
    # Only q changes: its own new mtime is not journaled, and p has no records.
    (root / "p" / "q" / "new.txt").write_text("x")
    (root / "Case").rename(root / "CASE")
    shutil.rmtree(root / "j")
    _junction(root / "j", outside / "target")  # Recorded, never followed.

    names = await _replayed_equals_fresh(tmp_path, root, db)
    assert "old.txt" not in names
    assert names.count("deep.txt") == 1
    assert "t.txt" not in names


@windows_ntfs
async def test_watcher_rename_then_offline_change_is_replayed(tmp_path):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root = tmp_path / "root"
    (root / "h").mkdir(parents=True)
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    (root / "h").rename(root / "h2")
    index.apply_changes({(3, str(root / "h")), (1, str(root / "h2"))})
    await index.close()
    (root / "h2" / "later.txt").write_text("x")
    await _replayed_equals_fresh(tmp_path, root, db)


@windows_ntfs
async def test_interrupted_live_walk_is_finished_by_the_next_replay(tmp_path, monkeypatch):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root = tmp_path / "root"
    root.mkdir()
    # Built before the checkpoint: after it, the journal only records the move in.
    moved = tmp_path / "outside" / "D"
    (moved / "S2").mkdir(parents=True)
    (moved / "S2" / "x.txt").write_text("x")
    (moved / "S1").mkdir()
    for number in range(1100):  # More than one 1000-row batch.
        (moved / "S1" / f"f{number}.txt").write_text("x")
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    moved.rename(root / "D")
    original = index._walk

    def killed(path, scope, generation, failed, directories=None):
        for count, row in enumerate(original(path, scope, generation, failed, directories)):
            if count == 1050:
                raise InterruptedError("process killed mid-walk")
            yield row

    monkeypatch.setattr(index, "_walk", killed)
    with pytest.raises(InterruptedError):
        index.apply_changes({(1, str(root / "D"))})
    await index.close()
    assert "f1099.txt" in await _replayed_equals_fresh(tmp_path, root, db)


@windows_ntfs
async def test_interrupted_replay_walk_is_redone_by_the_next_replay(tmp_path, monkeypatch):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root = tmp_path / "root"
    (root / "E" / "S1").mkdir(parents=True)
    (root / "E" / "S1" / "old.txt").write_text("x")
    replacement = tmp_path / "outside" / "E"
    (replacement / "S1").mkdir(parents=True)
    for number in range(1100):  # More than one 1000-row batch.
        (replacement / "S1" / f"f{number}.txt").write_text("x")
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    await index.close()
    # Same name, different folder: only its directory ID shows it must be walked.
    (root / "E").rename(tmp_path / "outside" / "E-old")
    replacement.rename(root / "E")
    killed_index = LocalIndex(db, [root])
    original = killed_index._walk

    def killed(path, scope, generation, failed, directories=None):
        for count, row in enumerate(original(path, scope, generation, failed, directories)):
            if count == 1050:
                raise InterruptedError("process killed mid-walk")
            yield row

    monkeypatch.setattr(killed_index, "_walk", killed)
    with pytest.raises(InterruptedError):
        killed_index.catch_up()
    await killed_index.close()
    names = await _replayed_equals_fresh(tmp_path, root, db)
    assert "f1099.txt" in names and "old.txt" not in names


@windows_ntfs
async def test_junction_replacing_an_ancestor_is_not_followed(tmp_path):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x")
    (root / "A" / "B" / "C").mkdir(parents=True)
    (outside / "store" / "pkg" / "lib").mkdir(parents=True)
    (outside / "store" / "pkg" / "index.js").write_text("x")
    (outside / "store" / "pkg" / "lib" / "a.js").write_text("x")
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    await index.close()
    shutil.rmtree(root / "node_modules")
    _junction(root / "node_modules", outside / "store")
    (root / "A").rename(root / "A2")
    _junction(root / "A", root / "A2")
    (root / "A2" / "B" / "added.txt").write_text("x")
    names = await _replayed_equals_fresh(tmp_path, root, db)
    assert "a.js" not in names
    (root / "A2" / "B" / "C" / "later.txt").write_text("x")  # Recorded IDs must still be right.
    assert "later.txt" in await _replayed_equals_fresh(tmp_path, root, db)


@windows_ntfs
async def test_changes_during_a_full_walk_are_replayed_at_its_end(tmp_path, monkeypatch):
    import pathgrove_mcp.index as module

    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    monkeypatch.setattr(module, "FOLLOW_SECONDS", 0)
    root = tmp_path / "root"
    for name in ("early", "late"):
        (root / name).mkdir(parents=True)
        for number in range(300):
            (root / name / f"{name}{number}.txt").write_text("x")
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    original = index._walk

    def busy(path, scope, generation, failed, directories=None):
        for count, row in enumerate(original(path, scope, generation, failed, directories)):
            if count == 400:  # One folder is done: change both behind and ahead of the walk.
                for name in ("early", "late"):
                    (root / name / "during.txt").write_text("x")
                    (root / name / f"{name}0.txt").unlink()
            yield row

    monkeypatch.setattr(index, "_walk", busy)
    index.rescan()
    fresh = LocalIndex(tmp_path / "fresh.db", [root])
    try:
        fresh.rescan()
        assert _snapshot(index) == _snapshot(fresh)
        assert index.status()["last_reconcile"].startswith("full traversal")
    finally:
        await index.close()
        await fresh.close()


@windows_ntfs
async def test_short_name_notifications_are_recorded_under_long_names(tmp_path):
    import ctypes

    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root = tmp_path / "root"
    (root / "Long Folder Name" / "Inner Long Name").mkdir(parents=True)
    buffer = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetShortPathNameW(
        str(root / "Long Folder Name" / "Inner Long Name"), buffer, len(buffer)
    )
    if "~" not in buffer.value:
        pytest.skip("8.3 names are disabled on this volume")
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    (root / "Long Folder Name" / "Inner Long Name" / "New Dir").mkdir()
    (root / "Long Folder Name" / "Inner Long Name" / "New Dir" / "f.txt").write_text("x")
    index.apply_changes({(1, buffer.value + "\\New Dir")})
    paths = [row[0] for row in index._writer.execute("SELECT path FROM directory_ids")]
    assert not any("~" in path for path in paths), paths
    await index.close()
    (root / "Long Folder Name" / "Inner Long Name" / "New Dir" / "later.txt").write_text("x")
    assert "later.txt" in await _replayed_equals_fresh(tmp_path, root, db)


@windows_ntfs
async def test_case_only_folder_rename_while_running_respells_descendants(tmp_path):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root = tmp_path / "root"
    (root / "Case" / "sub").mkdir(parents=True)
    (root / "Case" / "sub" / "f.txt").write_text("x")
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    (root / "Case").rename(root / "CASE")
    index.apply_changes({(3, str(root / "Case")), (1, str(root / "CASE"))})
    assert [row["full_path"] for row in index.search("f.txt")] == [
        str(root / "CASE" / "sub" / "f.txt")
    ]
    await index.close()
    (root / "CASE" / "sub" / "later.txt").write_text("x")
    await _replayed_equals_fresh(tmp_path, root, db)


@windows_ntfs
async def test_folder_win32_cannot_open_by_name_is_kept_and_reported(tmp_path):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root = tmp_path / "root"
    root.mkdir()
    trailing = "\\\\?\\" + str(root / "trail ")
    os.mkdir(trailing)
    with open(trailing + "\\inside.txt", "w") as handle:
        handle.write("x")
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        index.rescan()
        assert "unavailable" in index.status()["coverage_note"]
        assert index.status()["skipped_entries"] >= 1
    finally:
        await index.close()
        os.remove(trailing + "\\inside.txt")
        os.rmdir(trailing)


@windows_ntfs
async def test_replay_keeps_and_reports_a_folder_win32_cannot_open(tmp_path):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    await index.close()
    trailing = "\\\\?\\" + str(root / "trail ")
    os.mkdir(trailing)
    reopened = LocalIndex(db, [root])
    try:
        reopened.catch_up()
        assert reopened.status()["last_reconcile"].startswith("change journal")
        assert "unavailable" in reopened.status()["coverage_note"]
        assert reopened.count("trail") == 1
    finally:
        await reopened.close()
        os.rmdir(trailing)


@windows_ntfs
def test_windows_notification_paths_get_their_on_disk_spelling(tmp_path):
    import ctypes

    from pathgrove_mcp.index import _event_path

    (tmp_path / "Long Folder Name").mkdir()
    (tmp_path / "Case").mkdir()
    (tmp_path / "Case").rename(tmp_path / "CASE")
    assert _event_path(str(tmp_path / "Case")) == str(tmp_path / "CASE")
    assert _event_path(str(tmp_path / "CASE" / "gone.txt")) == str(tmp_path / "CASE" / "gone.txt")
    buffer = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetShortPathNameW(str(tmp_path / "Long Folder Name"), buffer, 32768)
    if "~" in buffer.value:  # 8.3 names can be disabled per volume.
        assert _event_path(buffer.value) == str(tmp_path / "Long Folder Name")
        assert _event_path(buffer.value + "\\deleted") == str(
            tmp_path / "Long Folder Name" / "deleted"
        )


async def test_folder_rows_are_searchable_before_the_walk_enters_them(tmp_path):
    root = tmp_path / "root"
    for name in ("A", "B"):
        (root / name / "deep").mkdir(parents=True)
        (root / name / "deep" / f"{name}.txt").write_text("x")
    index = LocalIndex(tmp_path / "index.db", [root])
    try:
        order = [os.path.basename(row[2]) for row in index._walk(str(root), str(root), "g", [])]
        # Both top folders come from the root's listing, before either is entered.
        assert max(order.index("A"), order.index("B")) < min(
            order.index("A.txt"), order.index("B.txt")
        )
    finally:
        await index.close()


def _tree(root):
    """Plain directories and other entries, without following links or junctions."""
    directories, entries, stack = [], [], [root]
    while stack:
        with os.scandir(stack.pop()) as listing:
            for entry in listing:
                attributes = entry.stat(follow_symlinks=False).st_file_attributes
                if entry.is_dir(follow_symlinks=False) and not attributes & 0x400:
                    directories.append(Path(entry.path))
                    stack.append(entry.path)
                else:
                    entries.append(Path(entry.path))
    return directories, entries


def _mutate(rng, root, outside, steps, prefix):
    for step in range(steps):
        directories, entries = _tree(root)
        name = f"{prefix}{step}"
        operation = rng.choice(
            [
                "file", "file", "dir", "append", "unlink", "rmtree", "move", "move",
                "case", "dir-to-file", "file-to-dir", "out", "in", "swap", "junction",
            ]
        )  # fmt: skip
        places = [root, *directories]
        if operation == "file":
            (rng.choice(places) / f"{name}.txt").write_text("x" * rng.randint(0, 40))
        elif operation == "dir":
            (rng.choice(places) / name).mkdir()
        elif operation == "append" and entries:
            target = rng.choice(entries)
            if target.is_file():
                with open(target, "a") as handle:
                    handle.write("more")
        elif operation == "unlink" and entries:
            target = rng.choice(entries)
            target.rmdir() if target.is_dir() else target.unlink()  # Junctions are dirs.
        elif operation == "rmtree" and directories:
            shutil.rmtree(rng.choice(directories))
        elif operation == "move" and (directories or entries):
            source = rng.choice(directories + entries)
            destination = rng.choice(places)
            if destination != source and source not in destination.parents:
                source.rename(destination / name)
        elif operation == "case" and (directories or entries):
            source = rng.choice(directories + entries)
            source.rename(source.with_name(source.name.swapcase()))
        elif operation == "dir-to-file" and directories:
            target = rng.choice(directories)
            shutil.rmtree(target)
            target.write_text("was a folder")
        elif operation == "file-to-dir" and entries:
            target = rng.choice(entries)
            if target.is_file():
                target.unlink()
                target.mkdir()
                (target / "inner.txt").write_text("x")
        elif operation == "out" and directories:
            rng.choice(directories).rename(outside / f"{name}-out")
        elif operation == "in":
            (outside / name / "sub").mkdir(parents=True)
            (outside / name / "sub" / "f.txt").write_text("x")
            (outside / name).rename(rng.choice(places) / name)
        elif operation == "swap" and len(directories) >= 2:
            first, second = rng.sample(directories, 2)
            if first not in second.parents and second not in first.parents:
                parked = first.with_name(first.name + "-parked")
                first.rename(parked)
                second.rename(first)
                parked.rename(second)
        elif operation == "junction":
            _junction(rng.choice(places) / name, outside / "target")


@windows_ntfs
@pytest.mark.parametrize("seed", range(4))
async def test_random_offline_changes_replay_like_a_full_walk(tmp_path, seed):
    if reason := _ntfs(tmp_path):
        pytest.skip(reason)
    rng = random.Random(seed)
    root, outside = tmp_path / "root", tmp_path / "outside"
    (outside / "target").mkdir(parents=True)
    (outside / "target" / "t.txt").write_text("x")
    root.mkdir()
    _mutate(rng, root, outside, 30, "a")  # Initial shape.
    db = tmp_path / "index.db"
    index = LocalIndex(db, [root])
    index.rescan()
    await index.close()
    for restart in range(3):  # Each replay also advances the checkpoint.
        _mutate(rng, root, outside, 25, f"r{restart}-")
        await _replayed_equals_fresh(tmp_path, root, db)
