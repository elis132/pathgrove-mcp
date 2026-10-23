"""Content predicates read scoped files, independently of filename matches."""

import os

import pytest

from pathgrove_mcp.index import LocalIndex
from pathgrove_mcp.query import QueryError


async def test_direct_search_works_without_starting_metadata_index(valid_config):
    from pathgrove_mcp.backend import SearchBackend

    backend = SearchBackend(valid_config)
    page = await backend.search_content("notes", ["txt"])
    assert backend.index is None
    assert [r.name for r in page] == ["notes.txt"]
    assert not page.partial
    with pytest.raises(ValueError, match="configured root"):
        await backend.search_content("notes", ["txt"], path=str(valid_config.roots[0].parent))


async def test_direct_search_recursive_filters_and_match_limit(content_index):
    import threading

    from pathgrove_mcp.content import search_roots

    _, root = content_index
    page = search_roots([root], "SECRET needle", ["txt"], 50, 10, threading.Event())
    assert {r["name"] for r in page} == {"a.txt", "b.txt", "unrelated.txt"}
    assert not page.partial
    limited = search_roots([root], "SECRET needle", ["txt"], 1, 10, threading.Event())
    assert len(limited) == 1 and limited.partial and "limit" in limited.note
    shallow = search_roots(
        [root], "SECRET needle", ["txt"], 50, 10, threading.Event(), recursive=False
    )
    assert {r["name"] for r in shallow} == {"a.txt", "b.txt"}
    assert not shallow.partial


@pytest.fixture
async def content_index(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    nested = root.joinpath(*[f"level{i}" for i in range(10)])
    nested.mkdir(parents=True)
    (nested / "unrelated.txt").write_text("before SECRET needle after", encoding="utf-8")
    (root / "a.txt").write_text("secret needlework", encoding="utf-8")
    (root / "b.txt").write_text("secret needle", encoding="utf-16")
    (root / "c.md").write_text("secret needle", encoding="utf-8")
    (root / "secret needle.txt").write_text("nothing here", encoding="utf-8")
    index = LocalIndex(tmp_path / "cache.db", [root])
    await index.start()
    await index.wait_ready(10)
    try:
        yield index, root
    finally:
        await index.close()


async def test_content_filters_extensions_and_reaches_deep_subdirectories(content_index):
    index, root = content_index
    page = index.search('ext:txt content:"secret needle"')
    assert {row["name"] for row in page} == {"a.txt", "b.txt", "unrelated.txt"}
    assert not page.partial
    deep = index.search(f'path:"{root / "level0"}" ext:txt content:needle')
    assert [row["name"] for row in deep] == ["unrelated.txt"]


async def test_content_case_words_and_literal_syntax(content_index):
    index, _ = content_index
    assert [r["name"] for r in index.search("ext:txt case:content:SECRET")] == ["unrelated.txt"]
    assert {r["name"] for r in index.search("ext:txt ww:content:needle")} == {
        "b.txt",
        "unrelated.txt",
    }
    assert {r["name"] for r in index.search("ext:txt content:secret content:needle")} == {
        "a.txt",
        "b.txt",
        "unrelated.txt",
    }
    assert not index.search('ext:txt content:"secret.*needle"')
    # The quoted function-looking token remains a filename literal.
    assert not index.search('"content:needle"')


async def test_pagination_applies_to_content_matches_not_candidate_files(content_index):
    index, _ = content_index
    all_rows = index.search('ext:txt content:"secret needle"', sort="name-desc")
    for offset in range(len(all_rows)):
        page = index.search(
            'ext:txt content:"secret needle"', sort="name-desc", offset=offset, max_results=1
        )
        assert page == all_rows[offset : offset + 1]
        assert not page.partial


async def test_plain_filename_queries_do_not_search_content(content_index):
    index, _ = content_index
    assert [r["name"] for r in index.search('ext:txt "secret needle"')] == ["secret needle.txt"]


@pytest.mark.parametrize(
    "query",
    [
        "ext:txt content:",
        "ext:txt !content:secret",
        "ext:txt content:secret | content:needle",
        "<ext:txt> content:needle",
    ],
)
async def test_unsupported_content_boolean_forms_are_explicit(content_index, query):
    index, _ = content_index
    with pytest.raises(QueryError):
        index.search(query)


async def test_binary_and_unreadable_files_are_reported_as_incomplete(content_index, monkeypatch):
    import pathgrove_mcp.content as content

    index, root = content_index
    binary = root / "binary.txt"
    binary.write_bytes(b"\x00\x01secret needle")
    index.apply_changes({(1, str(binary))})
    page = index.search("ext:txt content:needle")
    assert "binary.txt" not in {r["name"] for r in page}
    assert page.partial and "skipped" in page.note.lower()

    monkeypatch.setattr(content, "MAX_FILE_BYTES", 2)
    oversized = index.search("ext:txt content:needle")
    assert not oversized and oversized.partial
    assert "skipped" in oversized.note.lower()


async def test_content_file_budget_never_presents_a_global_miss(content_index, monkeypatch):
    import pathgrove_mcp.content as content

    index, _ = content_index
    monkeypatch.setattr(content, "MAX_FILES", 1)
    page = index.search("ext:txt content:absent")
    assert not page and page.partial
    assert "budget" in page.note.lower()


async def test_streaming_crosses_unicode_and_chunk_boundaries(content_index, monkeypatch):
    import pathgrove_mcp.content as content

    index, root = content_index
    path = root / "boundary.txt"
    path.write_text("x " + "å" * 50 + " Straße needle ", encoding="utf-8")
    index.apply_changes({(1, str(path))})
    monkeypatch.setattr(content, "CHUNK_BYTES", 7)
    page = index.search('ext:txt ww:content:"STRASSE needle"')
    assert [r["name"] for r in page] == ["boundary.txt"]
    assert not page.partial


async def test_streaming_does_not_invent_word_boundaries(content_index, monkeypatch):
    import pathgrove_mcp.content as content

    index, root = content_index
    path = root / "boundary.txt"
    path.write_text("xneedle xyzneedlework", encoding="utf-8")
    index.apply_changes({(1, str(path))})
    for size in range(4, 20):
        monkeypatch.setattr(content, "CHUNK_BYTES", size)
        assert not index.search("name:boundary.txt ww:content:needle")


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
async def test_bom_text_encodings(content_index, encoding):
    index, root = content_index
    path = root / "encoded.txt"
    path.write_text("héj världen", encoding=encoding)
    index.apply_changes({(1, str(path))})
    page = index.search('ext:txt content:"HÉJ VÄRLDEN"')
    assert [r["name"] for r in page] == ["encoded.txt"]
    assert not page.partial


async def test_unreadable_and_unsupported_encoding_are_not_silent_misses(
    content_index, monkeypatch
):
    import pathgrove_mcp.content as content

    index, root = content_index
    path = root / "encoded.txt"
    path.write_bytes(b"\xffnot a supported encoding")
    index.apply_changes({(1, str(path))})
    page = index.search("name:encoded.txt content:absent")
    assert not page and page.partial
    original = content.os.open

    def denied(path, *args, **kwargs):
        if str(path).endswith("a.txt"):
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(content.os, "open", denied)
    page = index.search("name:a.txt content:needle")
    assert not page and page.partial


async def test_metadata_filter_runs_before_content_reads(content_index, monkeypatch):
    import pathgrove_mcp.content as content

    index, _ = content_index
    paths = []
    original = content.ContentScanner.matches

    def track(self, path):
        paths.append(path)
        return original(self, path)

    monkeypatch.setattr(content.ContentScanner, "matches", track)
    # content comes first in the query but only .md candidates may be opened.
    index.search("content:needle ext:md")
    assert len(paths) == 1 and paths[0].endswith("c.md")
    paths.clear()
    index.search("ext:txt")
    assert not paths


async def test_read_and_time_budgets_are_labeled(content_index, monkeypatch):
    import pathgrove_mcp.content as content

    index, _ = content_index
    page = index.search("content:needle ext:txt", timeout=0.000001)
    assert page.partial and "budget" in page.note.lower()
    monkeypatch.setattr(content, "MAX_READ_BYTES", 7)
    page = index.search("content:absent ext:txt")
    assert not page and page.partial and "read budget" in page.note.lower()


async def test_content_aggregates_remain_exact_or_error(content_index):
    index, _ = content_index
    for operation in (index.count, index.sum_size):
        with pytest.raises(QueryError, match="not metadata count/size"):
            operation("ext:txt content:needle")


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks may need Windows privileges")
async def test_content_does_not_follow_symlinks_outside_roots(content_index, tmp_path):
    index, root = content_index
    outside = tmp_path / "outside.txt"
    outside.write_text("secret needle", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(outside)
    index.apply_changes({(1, str(link))})
    page = index.search("ext:txt content:needle")
    assert "link.txt" not in {r["name"] for r in page}
    assert page.partial
