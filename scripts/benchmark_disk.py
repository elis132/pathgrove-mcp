"""Measure real MCP searches over explicit disk roots; save aggregate data only."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import os
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def rss_bytes(pid: int) -> int:
    if os.name != "nt":
        return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)])) * 1024
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in ("peak", "rss", "pp", "p", "np", "n", "page", "peak_page")
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    memory = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not memory(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.rss
    finally:
        kernel.CloseHandle(handle)


def summary(text: str) -> dict:
    match = re.match(r"Found ([\d,]+) results", text)
    return {
        "returned": int(match[1].replace(",", "")) if match else 0,
        "partial": any(
            term in text.lower()
            for term in ("partial", "incomplete", "degraded", "stale", "index is building")
        ),
        "budget_reached": "budget reached" in text.lower(),
        "error": text.startswith("Error:"),
    }


async def call(client, tool, arguments):
    result = await client.call_tool(tool, arguments)
    if result.isError:
        raise RuntimeError("MCP request failed; no file paths are saved in this report")
    return "\n".join(getattr(block, "text", "") for block in result.content)


async def benchmark(roots, database, *, trials=5, wait_seconds=3600, query_timeout=10):
    if database.exists():
        raise ValueError("Use a new database path for a fresh-index measurement")
    roots = [str(Path(root).resolve(strict=True)) for root in roots]
    pid_path = Path(str(database) + "-benchmark-pid")
    environment = {
        **os.environ,
        "PATHGROVE_ROOTS": json.dumps(roots),
        "PATHGROVE_DB": str(database),
        "PATHGROVE_TIMEOUT": str(query_timeout),
    }
    wrapper = (
        "import os,sys,logging; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "from pathgrove_mcp.server import mcp; "
        "logging.getLogger().setLevel(logging.WARNING); mcp.run()"
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-c", wrapper, str(pid_path)], env=environment
    )
    report = {
        "kind": "actual MCP stdio; real filesystem traversal; no OS cache flush",
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "roots": roots,
        "trials": trials,
        "query_timeout_seconds": query_timeout,
        "sampled_peak_server_rss_bytes": 0,
        "queries": [],
    }
    started = time.perf_counter()
    with open(str(database) + "-benchmark-stderr", "w", encoding="utf-8") as log:
        async with (
            stdio_client(parameters, errlog=log) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            report["startup_ms"] = round((time.perf_counter() - started) * 1000, 3)
            pid = int(pid_path.read_text())
            next_progress = 0
            while True:
                status = json.loads(await call(client, "pathgrove_index_status", {}))
                elapsed = time.perf_counter() - started
                report["sampled_peak_server_rss_bytes"] = max(
                    report["sampled_peak_server_rss_bytes"], await asyncio.to_thread(rss_bytes, pid)
                )
                if status.get("indexed_entries") and "first_searchable_seconds" not in report:
                    report["first_searchable_seconds"] = round(elapsed, 3)
                if elapsed >= next_progress:
                    print(
                        json.dumps(
                            {
                                "elapsed_s": round(elapsed),
                                "entries": status.get("indexed_entries"),
                                "state": status.get("state"),
                            }
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    next_progress = elapsed + 30
                if status.get("state") in ("ready", "error") or elapsed >= wait_seconds:
                    break
                await asyncio.sleep(1)
            report["scan_seconds"] = round(elapsed, 3)
            report["scan_finished"] = status.get("state") == "ready"
            report["status"] = {
                key: status.get(key)
                for key in (
                    "state",
                    "indexed_entries",
                    "watching",
                    "scan_count",
                    "skipped_entries",
                    "partial",
                    "filename_memory_bytes",
                    "candidate_cache_bytes",
                )
            }
            report["status"]["has_error"] = bool(status.get("last_error"))
            needle = "pathgrove-absent-" + uuid.uuid4().hex
            cases = [
                ("pathgrove_search", {"query": query, "sort": sort, "max_results": 50})
                for query, sort in (
                    ("invoice", "name"),
                    ("invoice", "date-modified-desc"),
                    ("in", "name"),
                    ("o", "name"),
                    ("ext:pdf", "name"),
                    (needle, "name"),
                )
            ]
            cases += [
                (
                    "pathgrove_search_content",
                    {
                        "text": needle,
                        "extensions": ["txt", "md", "py", "ts", "json"],
                        "max_results": 50,
                    },
                ),
                (
                    "pathgrove_search",
                    {
                        "query": "ext:txt;md;py;ts;json content:" + needle,
                        "sort": "name",
                        "max_results": 50,
                    },
                ),
            ]
            for tool, params in cases:
                samples = []
                for _ in range(trials):
                    begin = time.perf_counter()
                    text = await call(client, tool, {"params": params})
                    samples.append(
                        {"ms": round((time.perf_counter() - begin) * 1000, 3), **summary(text)}
                    )
                    report["sampled_peak_server_rss_bytes"] = max(
                        report["sampled_peak_server_rss_bytes"],
                        await asyncio.to_thread(rss_bytes, pid),
                    )
                report["queries"].append(
                    {
                        "tool": tool,
                        "params": params,
                        "samples": samples,
                        "median_ms": round(
                            statistics.median(sample["ms"] for sample in samples), 3
                        ),
                        "first_ms": samples[0]["ms"],
                        "max_ms": max(sample["ms"] for sample in samples),
                    }
                )
    report["cache_bytes"] = sum(
        path.stat().st_size
        for path in database.parent.glob(database.name + "*")
        if path.name in (database.name, database.name + "-wal", database.name + "-shm")
    )
    started = time.perf_counter()
    with open(str(database) + "-benchmark-stderr", "a", encoding="utf-8") as log:
        async with (
            stdio_client(parameters, errlog=log) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            report["cached_startup_ms"] = round((time.perf_counter() - started) * 1000, 3)
            begin = time.perf_counter()
            text = await call(
                client,
                "pathgrove_search",
                {"params": {"query": "invoice", "sort": "name", "max_results": 50}},
            )
            report["cached_first_query"] = {
                "ms": round((time.perf_counter() - begin) * 1000, 3),
                **summary(text),
            }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--wait-seconds", type=float, default=3600)
    args = parser.parse_args()
    if args.trials < 1 or not math.isfinite(args.wait_seconds) or args.wait_seconds <= 0:
        parser.error("trials and wait-seconds must be positive")
    cache = Path(tempfile.mkdtemp(prefix="pathgrove-disk-benchmark-")) / "index.sqlite3"
    result = asyncio.run(
        benchmark(args.roots, cache, trials=args.trials, wait_seconds=args.wait_seconds)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
