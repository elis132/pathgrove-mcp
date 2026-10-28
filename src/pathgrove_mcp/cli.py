"""Terminal commands over the Pathgrove index: ``pathgrove search``, ``count`` and ``index``.

``search`` and ``count`` open the existing cache read-only: they never scan, watch or
wait for a running scan. ``index`` builds or refreshes the cache. Results go to stdout,
one path per line; notes and errors go to stderr. Exit status follows grep: 0 when
something matched, 1 when nothing did, 2 on errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from pathgrove_mcp.backend import SORT_MAP
from pathgrove_mcp.config import IndexConfig
from pathgrove_mcp.index import LocalIndex

_QUERY_HELP = (
    "Query in Pathgrove syntax, e.g. report *.pdf, ext:py;js, size:>1mb, dm:today, "
    "path:Projects, content:TODO. Quote it in PowerShell if it contains < > or |; "
    "the filter options below build these terms for you."
)


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", nargs="*", help=_QUERY_HELP)
    parser.add_argument("--ext", help="extensions, e.g. py,js")
    parser.add_argument("--size", help='file size, e.g. ">1mb" or 10kb..2mb')
    parser.add_argument("--modified", help="modified date, e.g. today, last7days or >2026-01-01")
    parser.add_argument("--path", help="only results whose full path contains this")


def _location(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db", help="index file (default: PATHGROVE_DB, else the MCP server's default)"
    )
    parser.add_argument(
        "--root",
        action="append",
        help="root folder; repeatable (default: PATHGROVE_ROOTS, else the indexed roots)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pathgrove",
        description="Search the Pathgrove file index. Without a command, pathgrove runs "
        "the MCP server on stdin/stdout.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="list matching paths")
    _filters(search)
    _location(search)
    search.add_argument(
        "--sort",
        choices=sorted(SORT_MAP),
        default="date-modified-desc",
        help="default: date-modified-desc",
    )
    search.add_argument("-n", "--limit", type=int, default=100, help="default: 100")
    output = search.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="one JSON object per line")
    output.add_argument("--null", action="store_true", help="end paths with NUL, for xargs -0")

    count = commands.add_parser("count", help="count matches")
    _filters(count)
    _location(count)
    count.add_argument(
        "--bytes", action="store_true", help="print the total size of matching files"
    )

    index = commands.add_parser(
        "index",
        help="build or refresh the index (waits while another process is scanning)",
    )
    _location(index)
    return parser


def _query(args: argparse.Namespace) -> str:
    terms = list(args.query)
    if args.ext:
        terms.append("ext:" + ";".join(e.strip().lstrip(".") for e in re.split("[,;]", args.ext)))
    if args.size:
        terms.append("size:" + args.size)
    if args.modified:
        terms.append("dm:" + args.modified)
    if args.path:
        if '"' in args.path:
            raise ValueError("--path cannot contain a double quote")
        path = os.path.abspath(args.path) if os.path.isdir(args.path) else args.path
        terms.append(f'path:"{path}"')
    return " ".join(terms)


def _recorded_roots(db: Path) -> list[str]:
    if not db.is_file():
        return []
    connection = sqlite3.connect(db)
    try:
        return [row[0] for row in connection.execute("SELECT root FROM roots ORDER BY root")]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _settings(args: argparse.Namespace) -> tuple[IndexConfig, Path, list]:
    config = IndexConfig.from_env()
    invalid = [error for error in config.errors if error.startswith("Invalid PATHGROVE_")]
    if invalid:
        raise ValueError("; ".join(invalid))
    db = Path(args.db).expanduser().resolve() if args.db else config.db_path
    if args.root:
        roots = [Path(root).expanduser().resolve() for root in args.root]
    elif "PATHGROVE_ROOTS" in os.environ:
        roots = config.roots
    else:
        # Without configuration, search what the cache holds rather than the home folder.
        roots = _recorded_roots(db) or ([Path.home()] if args.command == "index" else [])
    if not roots:
        raise FileNotFoundError(f"No index at {db}; run `pathgrove index` first")
    return config, db, roots


def _close(index: LocalIndex) -> None:
    asyncio.run(index.close())


def _search(args, config, db, roots) -> int:
    index = LocalIndex(db, roots, read_only=True)
    try:
        page = index.search(
            _query(args), timeout=config.timeout, max_results=args.limit, sort=args.sort
        )
        entries = index.status()["indexed_entries"]
    finally:
        _close(index)
    for row in page:
        if args.json:
            sys.stdout.write(json.dumps(row) + "\n")
        else:
            sys.stdout.write(row["full_path"] + ("\0" if args.null else "\n"))
    if page.partial and page.note:
        print(f"pathgrove: partial results: {page.note}", file=sys.stderr)
    if len(page) == args.limit:
        print(f"pathgrove: showing the first {args.limit}; raise --limit", file=sys.stderr)
    if not page:
        print(
            f"pathgrove: no matches among {entries:,} entries in {db}",
            file=sys.stderr,
        )
    return 0 if page else 1


def _count(args, config, db, roots) -> int:
    index = LocalIndex(db, roots, read_only=True)
    try:
        method = index.sum_size if args.bytes else index.count
        print(method(_query(args), timeout=config.timeout))
    finally:
        _close(index)
    return 0


def _index(args, config, db, roots) -> int:
    print(f"pathgrove: indexing {', '.join(map(str, roots))} into {db}", file=sys.stderr)
    index = LocalIndex(db, roots)
    try:
        index.catch_up()
        status = index.status()
    finally:
        _close(index)
    print(
        f"pathgrove: {status['indexed_entries']:,} entries. {status['last_reconcile']}",
        file=sys.stderr,
    )
    for note in (status["coverage_note"], status["last_error"]):
        if note:
            print(f"pathgrove: {note}", file=sys.stderr)
    return 2 if status["last_error"] else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Lone surrogates in Windows file names must not crash output.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _parser().parse_args(argv)
    try:
        config, db, roots = _settings(args)
        return {"search": _search, "count": _count, "index": _index}[args.command](
            args, config, db, roots
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"pathgrove: {exc}", file=sys.stderr)
        return 2
