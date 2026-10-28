"""Terminal commands: read-only queries, clean stdout, exit codes and the MCP default."""

import os
import sys
import threading
import time

import pytest

import pathgrove_mcp
from pathgrove_mcp import cli
from pathgrove_mcp.index import LocalIndex, _process_lock


@pytest.fixture
def built(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PATHGROVE_ROOTS", raising=False)
    monkeypatch.delenv("PATHGROVE_DB", raising=False)
    root = tmp_path / "files"
    (root / "My Docs").mkdir(parents=True)
    (root / "My Docs" / "report.pdf").write_bytes(b"x" * 10)
    (root / "old.py").write_text("print('needle')", encoding="utf-8")
    (root / "big.log").write_bytes(b"x" * 2_000_000)
    for age, name in enumerate(("big.log", "My Docs/report.pdf", "old.py")):
        stamp = time.time() - 1000 * (age + 1)
        os.utime(root / name, (stamp, stamp))
    db = tmp_path / "cache" / "index.sqlite3"
    assert cli.main(["index", "--db", str(db), "--root", str(root)]) == 0
    capsys.readouterr()
    return root, db


def test_search_prints_only_paths_newest_first(built, capsys):
    root, db = built
    assert cli.main(["search", "--db", str(db), "file:"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        str(root / "big.log"),
        str(root / "My Docs" / "report.pdf"),
        str(root / "old.py"),
    ]
    assert err == ""


def test_filter_options_build_the_query(built, capsys):
    root, db = built
    cli.main(["search", "--db", str(db), "--size", ">1mb"])
    assert capsys.readouterr().out.splitlines() == [str(root / "big.log")]
    cli.main(["search", "--db", str(db), "--ext", ".py,pdf", "--sort", "name"])
    assert capsys.readouterr().out.splitlines() == [
        str(root / "old.py"),
        str(root / "My Docs" / "report.pdf"),
    ]
    cli.main(["search", "--db", str(db), "--path", str(root / "My Docs"), "--ext", "pdf"])
    assert capsys.readouterr().out.splitlines() == [str(root / "My Docs" / "report.pdf")]


def test_no_match_exits_1_with_note_on_stderr(built, capsys):
    _, db = built
    assert cli.main(["search", "--db", str(db), "nothing-here"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "no matches among 4 entries" in err


def test_null_and_json_output(built, capsys):
    root, db = built
    cli.main(["search", "--db", str(db), "--ext", "py", "--null"])
    assert capsys.readouterr().out == str(root / "old.py") + "\0"
    cli.main(["search", "--db", str(db), "--ext", "py", "--json"])
    assert '"name": "old.py"' in capsys.readouterr().out


def test_count_and_total_bytes(built, capsys):
    _, db = built
    assert cli.main(["count", "--db", str(db), "--ext", "py;log"]) == 0
    assert capsys.readouterr().out == "2\n"
    cli.main(["count", "--db", str(db), "--bytes"])
    assert capsys.readouterr().out == f"{2_000_000 + 10 + 15}\n"
    assert cli.main(["count", "--db", str(db), "content:needle"]) == 2
    assert "content:" in capsys.readouterr().err


def test_without_roots_searches_what_the_index_holds(built, capsys):
    root, db = built
    assert cli.main(["search", "--db", str(db), "--ext", "py"]) == 0
    assert capsys.readouterr().out.splitlines() == [str(root / "old.py")]


def test_search_never_starts_the_index(built, monkeypatch, capsys):
    _, db = built

    async def start(self):
        raise AssertionError("search must not scan or watch")

    monkeypatch.setattr(LocalIndex, "start", start)
    monkeypatch.setattr(LocalIndex, "catch_up", start)
    assert cli.main(["search", "--db", str(db), "old"]) == 0
    assert cli.main(["count", "--db", str(db)]) == 0


def test_search_does_not_wait_for_a_running_scan(built, capsys):
    root, db = built
    held, release = threading.Event(), threading.Event()

    def scan():
        with _process_lock(str(db) + ".scan.lock", threading.Event()):
            held.set()
            release.wait(30)

    scanner = threading.Thread(target=scan)
    scanner.start()
    try:
        assert held.wait(10)
        results = []
        searcher = threading.Thread(
            target=lambda: results.append(cli.main(["search", "--db", str(db), "old"]))
        )
        searcher.start()
        searcher.join(10)
        assert results == [0]
    finally:
        release.set()
        scanner.join()
    assert str(root / "old.py") in capsys.readouterr().out


def test_missing_index_is_an_error_and_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PATHGROVE_ROOTS", raising=False)
    db = tmp_path / "none" / "index.sqlite3"
    assert cli.main(["search", "--db", str(db), "--root", str(tmp_path), "x"]) == 2
    assert "No index at" in capsys.readouterr().err
    assert not db.parent.exists()


def test_pathgrove_without_arguments_still_runs_the_mcp_server(monkeypatch):
    from pathgrove_mcp import server

    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("run"))
    monkeypatch.setattr(sys, "argv", ["pathgrove"])
    pathgrove_mcp.main()
    assert calls == ["run"]


def test_pathgrove_command_routes_to_the_cli(built, monkeypatch, capsys):
    _, db = built
    monkeypatch.setattr(sys, "argv", ["pathgrove", "search", "--db", str(db), "nothing-here"])
    with pytest.raises(SystemExit) as exit_info:
        pathgrove_mcp.main()
    assert exit_info.value.code == 1
