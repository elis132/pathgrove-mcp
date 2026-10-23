"""Measure synthetic metadata indexing growth; this is not a filesystem scan."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


def forecast(samples, total_entries, traversal_seconds):
    """Scenarios, not confidence bounds: extrapolate insertion separately from discovery."""
    last = samples[-1]
    ratio = total_entries / last["entries"]
    quadratic = 0
    if len(samples) > 1:
        previous = samples[-2]
        quadratic = max(
            0,
            (last["seconds"] / last["entries"] - previous["seconds"] / previous["entries"])
            / (last["entries"] - previous["entries"]),
        )
    linear = max(0, last["seconds"] / last["entries"] - quadratic * last["entries"])
    return {
        "target_entries": total_entries,
        "traversal_proxy_seconds": traversal_seconds,
        "linear_scenario_seconds": round(last["seconds"] * ratio + traversal_seconds, 3),
        "quadratic_scenario_seconds": round(last["seconds"] * ratio * ratio + traversal_seconds, 3),
        "fitted_scenario_seconds": round(
            linear * total_entries + quadratic * total_entries**2 + traversal_seconds, 3
        ),
        "warning": "Synthetic metadata extrapolation plus a filename-traversal proxy; not a measured whole-drive completion or a statistical confidence interval.",
    }


async def benchmark(targets, output, *, source=None):
    if source is not None:
        sys.path.insert(0, str(Path(source) / "src"))
    import pathgrove_mcp
    from pathgrove_mcp.index import LocalIndex

    runtime = Path(pathgrove_mcp.__file__).parent
    report = {
        "kind": "synthetic SQLite metadata; excludes real filesystem discovery",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "batch_size": 256,
        "files_per_directory": 64,
        "complete": False,
        "runtime_source_sha256_lf": {
            p.name: hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
            for p in runtime.glob("*.py")
        },
        "samples": [],
    }
    with tempfile.TemporaryDirectory(prefix="pathgrove-growth-") as temp:
        root = (Path(temp) / "files").resolve()
        root.mkdir()
        index = LocalIndex(Path(temp) / "index.sqlite3", [root])
        counters = {"count_calls": 0, "count_seconds": 0.0, "snapshot_seconds": 0.0}
        original_count, original_snapshot = index._refresh_count, index._names.snapshot

        def count():
            started = time.perf_counter()
            try:
                return original_count()
            finally:
                counters["count_calls"] += 1
                counters["count_seconds"] += time.perf_counter() - started

        def snapshot(connection):
            started = time.perf_counter()
            try:
                return original_snapshot(connection)
            finally:
                counters["snapshot_seconds"] += time.perf_counter() - started

        index._refresh_count, index._names.snapshot = count, snapshot
        data = SimpleNamespace(st_size=1024, st_mtime=1791800000, st_ctime=1791800000)
        started = time.perf_counter()
        inserted = 0
        try:
            for target in targets:
                while inserted < target:
                    end = min(inserted + 256, target)
                    index._upsert(
                        [
                            index._row(
                                str(
                                    root
                                    / f"folder-{number // 64}"
                                    / f"document-invoice-{number:08d}.txt"
                                ),
                                str(root),
                                "synthetic",
                                data,
                                False,
                            )
                            for number in range(inserted, end)
                        ]
                    )
                    inserted = end
                report["samples"].append(
                    {
                        "entries": inserted,
                        "seconds": round(time.perf_counter() - started, 6),
                        **{key: round(value, 6) for key, value in counters.items()},
                    }
                )
                output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(report["samples"][-1]), flush=True)
            index._refresh_count()
            assert index.status()["indexed_entries"] == inserted
            report["complete"] = True
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        finally:
            await index.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, help="Optional baseline checkout, loaded in a separate process"
    )
    parser.add_argument("--entries", type=int, nargs="+", default=[100000, 250000, 500000, 1000000])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.entries != sorted(set(args.entries)) or not args.entries or args.entries[0] < 1:
        parser.error("entries must be positive, distinct and increasing")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(benchmark(args.entries, args.output, source=args.source))
