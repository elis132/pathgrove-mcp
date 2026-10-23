"""The public benchmark report must not expose searched file paths or text."""

import importlib.util
import json
from pathlib import Path


async def test_disk_benchmark_uses_real_stdio_and_saves_only_aggregates(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_disk.py"
    spec = importlib.util.spec_from_file_location("benchmark_disk", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "scope"
    root.mkdir()
    (root / "invoice-private-filename.txt").write_text("private-content-token", encoding="utf-8")
    report = await module.benchmark(
        [root], tmp_path / "index.db", trials=1, wait_seconds=10, query_timeout=1
    )
    assert report["scan_finished"] and report["status"]["indexed_entries"] == 1
    assert report["queries"][0]["samples"][0]["returned"] == 1
    assert all(not sample["error"] for query in report["queries"] for sample in query["samples"])
    assert report["sampled_peak_server_rss_bytes"] > 0
    assert "private-filename" not in json.dumps(report)
    assert "private-content-token" not in json.dumps(report)
