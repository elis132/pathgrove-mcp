"""
Pathgrove - Independent local file search for agents.

Lightning-fast file search for AI agents.
"""

__version__ = "0.1.0"


def main() -> None:
    """Entry point for the ``pathgrove`` command."""
    from pathgrove_mcp.server import mcp

    mcp.run()
