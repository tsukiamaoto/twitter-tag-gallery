"""Stdio MCP transport. Tool definitions and execution are shared with Settings."""
import json
import os
import sys

from tool_registry import CATALOG, sdk_functions


def protocol_tools():
    from integration_settings import allowed, record_session
    if not os.environ.get("TWITTER_GALLERY_MCP_DIAGNOSTIC"):
        record_session("tools/list")
    return [tool.protocol() for tool in CATALOG.values() if allowed(tool.name)]


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
        modern = True
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        modern = False
    import anyio
    from mcp.types import CallToolResult, TextContent, Tool

    class ManagedMCP(FastMCP):
        async def list_tools(self):
            return [Tool.model_validate(tool) for tool in protocol_tools()]

        async def call_tool(self, name, arguments, *args, **kwargs):
            from tool_runtime import dispatch, failed_result
            try:
                result = await anyio.to_thread.run_sync(lambda: dispatch(name, arguments or {}))
                failed = failed_result(result)
                content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
            except Exception as error:
                if not modern:
                    from mcp.server.fastmcp.exceptions import ToolError
                    raise ToolError(str(error)) from error
                return CallToolResult(content=[TextContent(type="text", text=str(error))], isError=True)
            if modern:
                return CallToolResult(content=content, isError=failed)
            if failed:
                from mcp.server.fastmcp.exceptions import ToolError
                raise ToolError(content[0].text)
            return content

    server = ManagedMCP("twitter-tag-gallery")
    for tool, function in sdk_functions():
        server.add_tool(function, name=tool.name, description=tool.description)
    return server


def run_stdio_loop(input_stream=None, output_stream=None):
    """Fallback uses the exact same schemas, validation, policy, locks and history."""
    from tool_runtime import dispatch, failed_result
    source, target = input_stream or sys.stdin, output_stream or sys.stdout
    for line in source:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                continue
            if method == "initialize":
                version = request.get("params", {}).get("protocolVersion", "2025-06-18")
                if version not in ("2024-11-05", "2025-03-26", "2025-06-18"):
                    version = "2025-06-18"
                result = {"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "twitter-tag-gallery", "version": "2.1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": protocol_tools()}
            elif method == "tools/call":
                params = request.get("params", {})
                try:
                    value = dispatch(params.get("name"), params.get("arguments", {}))
                    failed = failed_result(value)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}], "isError": failed}
                except Exception as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
                target.write(json.dumps(response, ensure_ascii=False) + "\n")
                target.flush()
                continue
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ValueError, TypeError, AttributeError) as error:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": str(error)}}
        target.write(json.dumps(response, ensure_ascii=False) + "\n")
        target.flush()


if __name__ == "__main__":
    if not os.environ.get("TWITTER_GALLERY_MCP_DIAGNOSTIC"):
        from database import init_db
        init_db()
    try:
        server = build_server()
    except ImportError:
        run_stdio_loop()
    else:
        server.run()
