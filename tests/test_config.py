import json
from pathlib import Path

import pytest

from pathgrove_mcp.config import IndexConfig


def test_default_is_only_user_home(monkeypatch):
    for name in (
        "PATHGROVE_ROOTS",
        "PATHGROVE_DB",
        "PATHGROVE_TIMEOUT",
        "PATHGROVE_MAX_RESULTS",
    ):
        monkeypatch.delenv(name, raising=False)
    config = IndexConfig.from_env()
    assert config.is_valid
    assert config.roots == [Path.home().resolve()]
    assert config.db_path.name == "index.sqlite3"


def test_explicit_json_scopes_and_database(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("PATHGROVE_ROOTS", json.dumps([str(first), str(second), str(first)]))
    monkeypatch.setenv("PATHGROVE_DB", str(tmp_path / "custom.db"))
    monkeypatch.setenv("PATHGROVE_TIMEOUT", "2.5")
    monkeypatch.setenv("PATHGROVE_MAX_RESULTS", "23")
    config = IndexConfig.from_env()
    assert config.is_valid and config.roots == [first.resolve(), second.resolve()]
    assert config.db_path == tmp_path / "custom.db"
    assert config.timeout == 2.5 and config.max_results_cap == 23


@pytest.mark.parametrize("raw", ["oops", '"D:/"', "[]", "[null]", '[""]', "[5]"])
def test_invalid_roots_fail_without_fallback_scan(raw, monkeypatch):
    monkeypatch.setenv("PATHGROVE_ROOTS", raw)
    config = IndexConfig.from_env()
    assert not config.is_valid and "PATHGROVE_ROOTS" in config.errors[0]


def test_missing_root_is_error(tmp_path):
    config = IndexConfig(roots=[tmp_path / "missing"], db_path=tmp_path / "db")
    assert not config.is_valid and "not a directory" in config.errors[0]


@pytest.mark.parametrize(
    "variable,value",
    [
        ("PATHGROVE_TIMEOUT", "nan"),
        ("PATHGROVE_TIMEOUT", "0"),
        ("PATHGROVE_TIMEOUT", "broken"),
        ("PATHGROVE_MAX_RESULTS", "-1"),
        ("PATHGROVE_MAX_RESULTS", "bad"),
    ],
)
def test_invalid_limits_rejected(variable, value, monkeypatch):
    monkeypatch.setenv(variable, value)
    assert not IndexConfig.from_env().is_valid


def test_obsolete_external_environment_is_ignored(monkeypatch):
    monkeypatch.setenv("UNRELATED_SEARCH_PATH", "nonexistent.exe")
    monkeypatch.setenv("UNRELATED_SEARCH_INSTANCE", "not-an-instance")
    assert IndexConfig.from_env().is_valid
