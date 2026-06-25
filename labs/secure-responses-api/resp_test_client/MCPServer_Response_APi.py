#from mcp.server.fastmcp import FastMCP

from fastmcp import FastMCP
# Initialize the MCP server with a friendly name
mcp = FastMCP("DemoServer")

# Define a tool to add two numbers
@mcp.tool()
def add(a: int, b: int) -> int:
    """Adds two integers and returns the sum."""
    return a + b

# Define a tool to greet a user
@mcp.tool()
def greet(name: str) -> str:
    """Greets the given name with a friendly message."""
    return f"Hello, {name}! Welcome to the MCP server."

# Run the MCP server locally
if __name__ == '__main__':
    # Use HTTP transport so remote clients (e.g. APIM) can fetch the server
    # metadata and tool list via the well-known MCP endpoints.
    mcp.run(transport="http", host="0.0.0.0", port=8000)
