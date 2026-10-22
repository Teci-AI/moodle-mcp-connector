"""moodle-mcp-connector — an MCP server that exposes a Moodle site as typed AI tools."""

from moodle_mcp_connector.server import SERVER_VERSION as __version__
from moodle_mcp_connector.server import main, mcp

__all__ = ["main", "mcp", "__version__"]
