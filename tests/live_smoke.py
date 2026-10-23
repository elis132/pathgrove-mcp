"""Exercise actual stdio MCP against isolated local fixtures; no Pathgrove needed."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pathgrove-mcp-smoke-") as temp:
        root = Path(temp) / "files"
        root.mkdir()
        (root / "needle-invoice.txt").write_text("independent index\n", encoding="utf-8")
        (root / "photo.jpg").write_bytes(b"fixture")
        deep = root.joinpath(*[f"level{i}" for i in range(10)])
        deep.mkdir(parents=True)
        (deep / "unrelated.txt").write_text("a hidden content-only phrase", encoding="utf-8")
        env = dict(os.environ)
        env["PATHGROVE_ROOTS"] = json.dumps([str(root)])
        env["PATHGROVE_DB"] = str(Path(temp) / "index.sqlite3")
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "pathgrove_mcp"], env=env
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {tool.name for tool in tools}
            assert {
                "pathgrove_search",
                "pathgrove_count_stats",
                "pathgrove_index_status",
            } <= names
            tool = next(tool for tool in tools if tool.name == "pathgrove_search")
            args = {"query": "needle-invoice", "max_results": 10}
            if "params" in tool.inputSchema.get("properties", {}):
                args = {"params": args}
            for _ in range(100):
                result = await session.call_tool("pathgrove_search", args)
                output = "\n".join(getattr(block, "text", "") for block in result.content)
                if "needle-invoice.txt" in output:
                    assert not result.isError
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError(f"Fixture not found through MCP: {output}")
            status = await session.call_tool("pathgrove_index_status", {})
            assert not status.isError
            for _ in range(100):
                parsed = json.loads("\n".join(getattr(b, "text", "") for b in status.content))
                if parsed["state"] == "ready":
                    break
                await asyncio.sleep(0.05)
                status = await session.call_tool("pathgrove_index_status", {})
            assert parsed["state"] == "ready"
            begin = time.perf_counter()
            content = await session.call_tool(
                "pathgrove_search",
                {
                    "params": {
                        "query": 'ext:txt content:"hidden content-only phrase"',
                        "sort": "name",
                    }
                },
            )
            content_ms = (time.perf_counter() - begin) * 1000
            content_text = "\n".join(getattr(b, "text", "") for b in content.content)
            assert not content.isError and "unrelated.txt" in content_text
            assert "Partial search" not in content_text
            begin = time.perf_counter()
            direct = await session.call_tool(
                "pathgrove_search_content",
                {"params": {"text": "hidden content-only phrase", "extensions": ["txt"]}},
            )
            direct_ms = (time.perf_counter() - begin) * 1000
            direct_text = "\n".join(getattr(b, "text", "") for b in direct.content)
            assert not direct.isError and "unrelated.txt" in direct_text
            assert "Partial search" not in direct_text
            details = await session.call_tool(
                "pathgrove_file_details",
                {"params": {"paths": [str(root / "needle-invoice.txt")], "preview_lines": 1}},
            )
            details_text = "\n".join(getattr(b, "text", "") for b in details.content)
            assert not details.isError
            assert json.loads(details_text)["preview"] == "independent index"
            outside = Path(temp) / "outside.txt"
            outside.write_text("outside-root-secret", encoding="utf-8")
            rejected = await session.call_tool(
                "pathgrove_file_details",
                {"params": {"paths": [str(outside)], "preview_lines": 1}},
            )
            rejected_text = "\n".join(getattr(b, "text", "") for b in rejected.content)
            assert "configured root" in json.loads(rejected_text)["error"]
            assert "outside-root-secret" not in rejected_text
            print(
                json.dumps(
                    {
                        "tools": sorted(names),
                        "search": output,
                        "content_search": content_text,
                        "content_roundtrip_ms": round(content_ms, 3),
                        "direct_content_search": direct_text,
                        "direct_content_roundtrip_ms": round(direct_ms, 3),
                        "status": [getattr(b, "text", "") for b in status.content],
                    },
                    indent=2,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
