"""src.mcp.server — MCP JSON-RPC 2.0 stdio server (thin).

Ports v2 asha_mcp.py protocol engine verbatim (same methods, codes, shapes)
minus skills-registry bloat: no --skills flag, no skill tool inflation, no
asha://skills resource, no bloat resource (brain Phase 6 owns bloat; health
covers integrity). Backed by tools.open_context (both DBs converged on start).

Run from the v3 root:  python -m src.mcp.server --memory-path ./memory
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow direct file launch: python3 /path/to/src/mcp/server.py (hermes)
# v2 asha_mcp.py was standalone; v3 needs v3 root on sys.path for `src`.
_v3root = Path(__file__).resolve().parents[2]
if str(_v3root) not in sys.path:
    sys.path.insert(0, str(_v3root))
try:
    from . import tools
except ImportError:
    from src.mcp import tools  # fallback when __package__ is None

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-03-26"

SERVER_INFO = {"name": "asha-memory", "version": tools.SERVER_VERSION}


class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOL_NOT_FOUND = -32001
TOOL_EXECUTION_ERROR = -32002
RESOURCE_NOT_FOUND = -32003


def resource_definitions() -> List[dict]:
    return [
        {"uri": "asha://memory/stats", "name": "Memory Statistics",
         "description": "Aggregate memory statistics", "mimeType": "application/json"},
        {"uri": "asha://memory/health", "name": "System Health",
         "description": "Health check results", "mimeType": "application/json"},
        {"uri": "asha://memory/profile", "name": "System Profile",
         "description": "Performance profile data", "mimeType": "application/json"},
    ]


def read_resource(ctx: Dict[str, Any], uri: str) -> Optional[dict]:
    if uri == "asha://memory/stats":
        return {"uri": uri, "data": tools.cmd_stats({}, ctx)}
    if uri == "asha://memory/health":
        return {"uri": uri, "data": tools.cmd_health({}, ctx)}
    if uri == "asha://memory/profile":
        return {"uri": uri, "data": tools.cmd_profile({}, ctx)}
    return None


class MCPServer:
    """JSON-RPC 2.0 MCP server over stdio transport (v2 protocol parity)."""

    def __init__(self, ctx: Dict[str, Any]):
        self.ctx = ctx
        self._initialized = False

    def handle_line(self, line: str):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            self._send_error(None, PARSE_ERROR, f"Invalid JSON: {e}")
            return
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params", {})
        if not method:
            self._send_error(msg_id, INVALID_REQUEST, "Method required")
            return
        try:
            result = self._dispatch(method, params)
            if msg_id is not None:
                self._send_result(msg_id, result)
        except MCPError as e:
            self._send_error(msg_id, e.code, e.message, e.data)
        except Exception as e:
            self._send_error(msg_id, INTERNAL_ERROR, str(e))

    def _dispatch(self, method: str, params: dict) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "notifications/initialized":
            self._initialized = True
            return None
        if method == "notifications/cancelled":
            return None
        if method == "tools/list":
            return {"tools": tools.definitions()}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            if not name:
                raise MCPError(INVALID_PARAMS, "Tool name required")
            try:
                result = tools.dispatch(name, args, self.ctx)
                return {"content": [{"type": "text",
                                     "text": json.dumps(result, indent=2)}]}
            except ValueError as e:
                raise MCPError(TOOL_EXECUTION_ERROR, str(e))
        if method == "resources/list":
            return {"resources": resource_definitions()}
        if method == "resources/read":
            uri = params.get("uri")
            if not uri:
                raise MCPError(INVALID_PARAMS, "URI required")
            content = read_resource(self.ctx, uri)
            if content is None:
                raise MCPError(RESOURCE_NOT_FOUND, f"Resource not found: {uri}")
            return {"contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps(content["data"], indent=2)}]}
        if method == "ping":
            return {}
        raise MCPError(METHOD_NOT_FOUND, f"Unknown method: {method}")

    def _initialize(self, params: dict) -> dict:
        client_info = params.get("clientInfo", {})
        sys.stderr.write(f"MCP: client={client_info.get('name', 'unknown')}/"
                         f"{client_info.get('version', '0.0')}\n")
        sys.stderr.flush()
        return {"protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": SERVER_INFO}

    def _send_result(self, msg_id: Any, result: Any):
        sys.stdout.write(json.dumps(
            {"jsonrpc": JSONRPC_VERSION, "id": msg_id,
             "result": result if result is not None else {}}, default=str) + "\n")
        sys.stdout.flush()

    def _send_error(self, msg_id: Any, code: int, message: str, data: Any = None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        sys.stdout.write(json.dumps(
            {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "error": error},
            default=str) + "\n")
        sys.stdout.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ASHA Memory v3 MCP Server (stdio)")
    ap.add_argument("--memory-path", default=None,
                    help="memory/ data dir (default <v3root>/memory or $ASHA_MEMORYV3_DATA)")
    args = ap.parse_args(argv)
    env_mem = os.environ.get("ASHA_MEMORYV3_DATA") or os.environ.get("ASHA_MEMORY_V3_DATA")
    if env_mem:
        # hermes sets ASHA_MEMORYV3_DATA=/.../v3/ (dir) -> use /.../v3/memory
        p = Path(env_mem)
        env_mem = str(p / "memory") if p.is_dir() and not (p / "core.db").exists() and (p.name != "memory") else str(p)
    mem = args.memory_path or env_mem or str(Path(__file__).resolve().parents[2] / "memory")
    ctx = tools.open_context(mem)
    server = MCPServer(ctx)
    sys.stderr.write(f"MCP: server ready (v{tools.SERVER_VERSION}),"
                     f" memory={os.path.abspath(mem)}\n")
    sys.stderr.flush()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            server.handle_line(line)
    finally:
        tools.close_context(ctx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


