from pluggy import HookspecMarker

hookspec = HookspecMarker("datasette")


@hookspec
def register_mcp_tools(datasette, mcp):
    """Synchronously register tools on this Datasette instance's MCPServer."""
