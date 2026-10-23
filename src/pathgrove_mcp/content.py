"""Bounded on-demand text reads for content: predicates; no persisted content copy."""

from __future__ import annotations

import codecs
import os
import stat
import time
from pathlib import Path

import regex

from pathgrove_mcp.names import SearchPage
from pathgrove_mcp.query import ContentTerm

CHUNK_BYTES = 64 * 1024
MAX_FILE_BYTES = 16 * 1024**2
MAX_READ_BYTES = 256 * 1024**2
MAX_FILES = 50_000


def search_roots(
    roots,
    text,
    extensions,
    max_results,
    timeout,
    stop,
    *,
    match_case=False,
    match_whole_word=False,
    recursive=True,
):
    """Walk directory entries directly, opening only the requested file extensions.

    This path works before the metadata index has discovered the files. Results
    are in traversal order; it does not promise global sorting or pagination.
    """
    scanner = ContentScanner(
        (ContentTerm(text, match_case, match_whole_word),),
        [str(root) for root in roots],
        time.monotonic() + timeout,
        stop,
    )
    suffixes = {extension.casefold().lstrip(".") for extension in extensions}
    stack = [str(root) for root in reversed(roots)]
    results = []
    notes = []
    unavailable = 0
    try:
        while stack:
            scanner.check_budget()
            directory = stack.pop()
            try:
                with os.scandir(directory) as children:
                    for child in children:
                        scanner.check_budget()
                        try:
                            if child.is_dir(follow_symlinks=False):
                                if (
                                    recursive
                                    and not getattr(
                                        child.stat(follow_symlinks=False), "st_file_attributes", 0
                                    )
                                    & 0x400
                                ):
                                    stack.append(child.path)
                            elif Path(child.name).suffix.lstrip(
                                "."
                            ).casefold() in suffixes and scanner.matches(child.path):
                                results.append({"full_path": child.path, "name": child.name})
                                if len(results) >= max_results:
                                    notes.append(
                                        "Stopped at the requested match limit; more matches may exist."
                                    )
                                    return SearchPage(results, partial=True, note=" ".join(notes))
                        except OSError:
                            unavailable += 1
            except OSError:
                unavailable += 1
    except ContentBudgetExceeded as exc:
        notes.append(f"Content search budget reached: {exc}.")
    if scanner.skipped:
        notes.append(
            f"Skipped {scanner.skipped:,} binary, unsupported, inaccessible, linked, changed or oversized files."
        )
    if unavailable:
        notes.append(f"Unable to inspect {unavailable:,} filesystem entries/directories.")
    if notes:
        notes.append("Coverage is incomplete; a miss does not prove absence.")
    return SearchPage(results, partial=bool(notes), note=" ".join(notes))


class ContentBudgetExceeded(Exception):
    pass


class _Skipped(Exception):
    pass


class ContentScanner:
    def __init__(self, terms: tuple[ContentTerm, ...], roots: list[str], deadline: float, stop):
        self.roots = [Path(root) for root in roots]
        self.deadline = deadline
        self.stop = stop
        self.files = 0
        self.bytes_read = 0
        self.skipped = 0
        self.patterns = []
        self.overlap = {True: 2, False: 2}
        for term in terms:
            text = term.text if term.match_case else term.text.casefold()
            pattern = regex.escape(text)
            if term.match_whole_word:
                pattern = rf"(?<!\w){pattern}(?!\w)"
            self.patterns.append((regex.compile(pattern), term))
            self.overlap[term.match_case] = max(self.overlap[term.match_case], len(text) + 2)

    def check_budget(self):
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise ContentBudgetExceeded("time budget reached")

    def matches(self, path: str) -> bool:
        self.check_budget()
        if self.files >= MAX_FILES:
            raise ContentBudgetExceeded(f"file budget reached ({MAX_FILES:,} files)")
        self.files += 1
        try:
            source = Path(path)
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise _Skipped("symlink, reparse point or special file")
            resolved = source.resolve(strict=True)
            if not any(resolved.is_relative_to(root) for root in self.roots):
                raise _Skipped("outside configured roots")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(source, flags), "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                    raise _Skipped("special or oversized file")
                matched = self._read(handle)
                after = os.fstat(handle.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise _Skipped("file changed during reading")
                return matched
        except (OSError, UnicodeError, _Skipped):
            self.skipped += 1
            return False

    def _read(self, handle) -> bool:
        file_bytes = 0
        decoder = None
        encoding = "utf-8"
        tails = {True: "", False: ""}
        trimmed = {True: False, False: False}
        found = [False] * len(self.patterns)
        while True:
            self.check_budget()
            remaining = MAX_READ_BYTES - self.bytes_read
            if remaining <= 0:
                raise ContentBudgetExceeded("read budget reached (256 MiB)")
            raw = handle.read(min(max(CHUNK_BYTES, 4), remaining + 1))
            self.bytes_read += len(raw)
            file_bytes += len(raw)
            if self.bytes_read > MAX_READ_BYTES:
                raise ContentBudgetExceeded("read budget reached (256 MiB)")
            if file_bytes > MAX_FILE_BYTES:
                raise _Skipped("file grew beyond the per-file limit")
            if decoder is None:
                if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
                    encoding = "utf-32"
                elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
                    encoding = "utf-16"
                elif raw.startswith(codecs.BOM_UTF8):
                    encoding = "utf-8-sig"
                decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            if encoding.startswith("utf-8") and b"\0" in raw:
                raise _Skipped("binary content")
            eof = not raw
            text = decoder.decode(raw, final=eof)
            windows = {True: tails[True] + text, False: tails[False] + text.casefold()}
            for i, (pattern, term) in enumerate(self.patterns):
                if found[i]:
                    continue
                window = windows[term.match_case]
                remaining_time = self.deadline - time.monotonic()
                if remaining_time <= 0:
                    raise ContentBudgetExceeded("time budget reached")
                try:
                    # A trimmed left edge has unknown preceding word context.
                    # Ignore that edge instead of inventing a word boundary.
                    start = int(term.match_whole_word and trimmed[term.match_case])
                    match = pattern.search(window, pos=start, timeout=remaining_time)
                except TimeoutError as exc:
                    raise ContentBudgetExceeded("time budget reached") from exc
                # A whole word at a chunk's right edge may continue in the next
                # chunk. Keep its left context and decide when that byte arrives.
                if match and (not term.match_whole_word or eof or match.end() < len(window)):
                    found[i] = True
            if all(found):
                return True
            if eof:
                return False
            for case, window in windows.items():
                trimmed[case] = trimmed[case] or len(window) > self.overlap[case]
                tails[case] = window[-self.overlap[case] :]
