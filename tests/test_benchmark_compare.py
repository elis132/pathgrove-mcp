"""Require identical ordered pages before publishing a comparative speed-up."""

import importlib.util
import json
from pathlib import Path

import pytest

from pathgrove_mcp.index import LocalIndex


def load_benchmark():
    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_compare.py"
    spec = importlib.util.spec_from_file_location("benchmark_compare", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failure", ["page", "budget", "error", "unfinished"])
def test_comparison_withholds_invalid_speedups(failure):
    benchmark = load_benchmark()
    sample = {
        "scan_ms": 100,
        "mcp_ms": 2,
        "same_ordered_page": True,
        "budget_reached": False,
        "error": False,
    }
    assert benchmark.comparison([sample], True)["speedup"] == 50
    sample.update(
        {
            "same_ordered_page": failure != "page",
            "budget_reached": failure == "budget",
            "error": failure == "error",
        }
    )
    assert benchmark.comparison([sample], failure != "unfinished")["speedup"] is None


async def test_real_stdio_comparison_checks_order_and_keeps_paths_private(tmp_path):
    benchmark = load_benchmark()
    root = tmp_path / "scope"
    root.mkdir()
    for name in ("invoice-private.pdf", "invoice-AAA.txt", "PRINTING.txt", "o.txt"):
        (root / name).write_text("secret-token", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "INVOICE-private.pdf").write_text("secret-token", encoding="utf-8")
    for number in range(60):
        (root / f"invoice-{number:03d}.txt").write_text("secret-token", encoding="utf-8")
    database = tmp_path / "index.sqlite3"
    index = LocalIndex(database, [root])
    index.rescan()
    await index.close()
    original = database.read_bytes()
    report = await benchmark.benchmark(database, [root], trials=2)
    assert database.read_bytes() == original
    assert report["initial_traversal_complete"]
    assert all(row["ratio_eligible"] for row in report["queries"])
    assert all(sample["visited"] == 66 for row in report["queries"] for sample in row["samples"])
    assert report["queries"][0]["samples"][0]["scan_matches"] == 63
    assert report["queries"][0]["samples"][0]["mcp_returned"] == 50
    assert "private.pdf" not in json.dumps(report)
    assert "secret-token" not in json.dumps(report)


def test_scan_records_but_does_not_follow_directory_links(tmp_path):
    benchmark = load_benchmark()
    root = tmp_path / "scope"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "invoice.txt").write_text("private", encoding="utf-8")
    try:
        (root / "invoice-link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation is unavailable")
    observed = benchmark.scan([str(root)], "invoice", [])
    assert observed["visited"] == 1
    assert observed["paths"] == [str(root / "invoice-link")]
