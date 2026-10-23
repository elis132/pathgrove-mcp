"""Compare cached MCP filename pages with independent, exhaustive os.scandir searches."""

from __future__ import annotations

import argparse
import asyncio
import bisect
import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pathgrove_mcp.storage import register_functions


def scan(roots, query, excluded, limit=50):
    """No Pathgrove query/index code: visit every accessible entry, then return top-k."""
    page = []
    visited = skipped = matches = 0
    stack = list(roots)
    while stack:
        try:
            with os.scandir(stack.pop()) as children:
                for child in children:
                    if any(
                        child.path == path
                        or child.path.startswith(path + "-")
                        or child.path.startswith(path + ".scan")
                        for path in excluded
                    ):
                        continue
                    try:
                        child.path.encode("utf-8")
                        directory = child.is_dir(follow_symlinks=False)
                        if directory and not (
                            os.name == "nt"
                            and child.stat(follow_symlinks=False).st_file_attributes & 0x400
                        ):
                            stack.append(child.path)
                        visited += 1
                        name = child.name.casefold()
                        hit = (
                            not directory and os.path.splitext(name)[1] == ".pdf"
                            if query == "ext:pdf"
                            else query in name
                        )
                        if hit:
                            matches += 1
                            item = (name, child.path.casefold(), child.path)
                            if len(page) < limit or item < page[-1]:
                                bisect.insort(page, item)
                                if len(page) > limit:
                                    page.pop()
                    except (OSError, UnicodeError, ValueError):
                        skipped += 1
        except OSError:
            skipped += 1
    return {
        "paths": [item[2] for item in page],
        "visited": visited,
        "skipped": skipped,
        "matches": matches,
    }


def parse_page(text):
    # A filename containing a newline cannot round-trip this human-readable format;
    # equality then fails and the report deliberately withholds its speed-up ratio.
    return [
        line.split("] ", 1)[1].rsplit("  (", 1)[0]
        for line in text.splitlines()
        if line.startswith(("  [FILE] ", "  [DIR] "))
    ]


def comparison(samples, scan_complete):
    eligible = scan_complete and all(
        row["same_ordered_page"] and not row["budget_reached"] and not row["error"]
        for row in samples
    )
    baseline = statistics.median(row["scan_ms"] for row in samples)
    indexed = statistics.median(row["mcp_ms"] for row in samples)
    return {
        "scan_median_ms": round(baseline, 3),
        "mcp_median_ms": round(indexed, 3),
        "ratio_eligible": eligible,
        "speedup": round(baseline / indexed, 2) if eligible and indexed else None,
    }


async def benchmark(database, roots, *, trials=5, timeout=10):
    roots = [str(Path(root).resolve(strict=True)) for root in roots]
    if any(
        root != other and Path(root).is_relative_to(other) for root in roots for other in roots
    ) or len(set(roots)) != len(roots):
        raise ValueError("Use distinct, non-overlapping roots")
    report = {
        "kind": "cached MCP stdio vs independent fresh os.scandir per query",
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "roots": roots,
        "trials": trials,
        "query_timeout_seconds": timeout,
        "snapshot_mode": "SQLite backup; reconciliation/watchers disabled in benchmark only",
        "os_cache_flushed": False,
        "queries": [],
    }
    with tempfile.TemporaryDirectory(prefix="pathgrove-compare-") as temp:
        snapshot = Path(temp) / "snapshot.sqlite3"
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            register_functions(source)
            with closing(sqlite3.connect(snapshot)) as destination:
                source.backup(destination)
                register_functions(destination)
                completed = dict(destination.execute("SELECT root,scanned FROM roots"))
                report["indexed_entries"] = destination.execute(
                    "SELECT COUNT(*) FROM files"
                ).fetchone()[0]
                indexed_roots = {
                    row[0] for row in destination.execute("SELECT DISTINCT root FROM locations")
                }
        if not indexed_roots.issubset(set(roots)):
            raise ValueError("Database contains roots outside the requested scope")
        report["initial_traversal_complete"] = all(completed.get(root) for root in roots)
        digest = hashlib.sha256()
        with snapshot.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        report["snapshot_sha256"] = digest.hexdigest()
        # Use the normal tools, formatting and MCP transport against a frozen backup.
        # Only lifecycle scanning is suppressed, so neither comparator changes the snapshot.
        wrapper = (
            "from pathgrove_mcp.index import LocalIndex; "
            "exec('async def no_scan(self): pass'); LocalIndex.start=no_scan; "
            "from pathgrove_mcp.server import mcp; mcp.run()"
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", wrapper],
            env={
                **os.environ,
                "PATHGROVE_ROOTS": json.dumps(roots),
                "PATHGROVE_DB": str(snapshot),
                "PATHGROVE_TIMEOUT": str(timeout),
            },
        )
        excluded = [str(database.resolve()), str(snapshot)]
        started = time.perf_counter()
        with open(Path(temp) / "stderr.log", "w", encoding="utf-8") as log:
            async with (
                stdio_client(parameters, errlog=log) as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                report["mcp_initialization_ms"] = round((time.perf_counter() - started) * 1000, 3)

                async def search(query):
                    begin = time.perf_counter()
                    result = await client.call_tool(
                        "pathgrove_search",
                        {"params": {"query": query, "sort": "name", "max_results": 50}},
                    )
                    elapsed = (time.perf_counter() - begin) * 1000
                    text = "\n".join(getattr(block, "text", "") for block in result.content)
                    return (
                        elapsed,
                        parse_page(text),
                        {
                            "budget_reached": "budget reached" in text.lower(),
                            "error": bool(result.isError) or text.startswith("Error:"),
                        },
                    )

                for query in (
                    "invoice",
                    "in",
                    "o",
                    "ext:pdf",
                    "pathgrove-absent-" + uuid.uuid4().hex,
                ):
                    first_ms, _, flags = await search(query)
                    row = {
                        "query": query,
                        "sort": "name",
                        "first_query_ms": round(first_ms, 3),
                        "first_query_flags": flags,
                        "samples": [],
                    }
                    for trial in range(trials):

                        async def baseline(query=query):
                            begin = time.perf_counter()
                            result = await asyncio.to_thread(scan, roots, query, excluded)
                            return (time.perf_counter() - begin) * 1000, result

                        if trial % 2:
                            mcp_ms, paths, flags = await search(query)
                            scan_ms, observed = await baseline()
                        else:
                            scan_ms, observed = await baseline()
                            mcp_ms, paths, flags = await search(query)
                        row["samples"].append(
                            {
                                "scan_ms": round(scan_ms, 3),
                                "mcp_ms": round(mcp_ms, 3),
                                "same_ordered_page": paths == observed["paths"],
                                "mcp_returned": len(paths),
                                "scan_returned": len(observed["paths"]),
                                "scan_matches": observed["matches"],
                                "visited": observed["visited"],
                                "skipped": observed["skipped"],
                                **flags,
                            }
                        )
                        print(
                            json.dumps({"query": query, "trial": trial + 1, **row["samples"][-1]}),
                            file=sys.stderr,
                            flush=True,
                        )
                    row.update(comparison(row["samples"], report["initial_traversal_complete"]))
                    report["queries"].append(row)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, required=True, help="Existing private disk cache; never published"
    )
    parser.add_argument("--roots", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if args.trials < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("trials and timeout must be positive")
    report = asyncio.run(
        benchmark(args.database, args.roots, trials=args.trials, timeout=args.timeout)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
