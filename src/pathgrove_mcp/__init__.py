"""
Pathgrove - Independent local file search for agents.

Lightning-fast file search for AI agents.
"""

__version__ = "0.1.0"


def main() -> None:
    """Entry point for the ``pathgrove`` command.

    MCP clients start ``pathgrove`` without arguments, so anything that is not a
    terminal command still runs the server exactly as before.
    """
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("search", "count", "index", "-h", "--help"):
        from pathgrove_mcp.cli import main as cli

        sys.exit(cli(sys.argv[1:]))
    from pathgrove_mcp.server import mcp

    mcp.run()
