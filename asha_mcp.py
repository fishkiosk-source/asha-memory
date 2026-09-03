"""
ASHA MCP Server — stdio JSON-RPC 2.0 Model Context Protocol interface.

Exposes ASHA_MEMORY_SYSTEM v2 as MCP tools + resources over stdio transport.
Skills from the registry are exposed as callable MCP tools.

Usage:
    python asha_mcp.py --base-path ./data                  # default skills
    python asha_mcp.py --base-path ./data --skills ./ASHA_SKILLS_REGISTRY.txt
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python asha_mcp.py
"""

import sys, json, os, re, math, uuid, time, threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from asha_memory_v2 import AshaMemory, _tokenize

# ── MCP Protocol Constants ────────────────────────────────────────────────

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-03-26"

SERVER_INFO = {
    "name": "asha-memory",
    "version": "2.0.0",
}

# ── Tool Definition Builder ────────────────────────────────────────────────

def _str_schema(description: str) -> dict:
    return {"type": "string", "description": description}

def _int_schema(description: str) -> dict:
    return {"type": "integer", "description": description}

def _float_schema(description: str) -> dict:
    return {"type": "number", "description": description}

def _bool_schema(description: str) -> dict:
    return {"type": "boolean", "description": description}


def _tool_def(name: str, description: str, properties: dict,
              required: List[str] = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


# ── Tool Implementations ───────────────────────────────────────────────────

class ToolRegistry:
    """Registry of MCP tools backed by an AshaMemory instance."""

    def __init__(self, mem: AshaMemory):
        self.mem = mem

    # ── Clock context ──

    def _clock(self) -> Optional[dict]:
        """Today's date/time snapshot (None when the internal clock is disabled)."""
        clock = getattr(self.mem, "clock", None)
        return clock.now() if clock and clock.enabled else None

    # ── Definitions ──

    def definitions(self) -> List[dict]:
        skills = self._skill_tools()
        core = self._core_tools()
        agent = self._agent_tools()
        query = self._query_tools()
        system = self._system_tools()
        return skills + core + agent + query + system

    def _skill_tools(self) -> List[dict]:
        return [
            _tool_def(
                "register_skill",
                "Register a new capability skill.",
                {
                    "name": _str_schema("Skill identifier, e.g. EXECUTE_CODE"),
                    "description": _str_schema("What the skill does"),
                    "level": _str_schema("Hierarchy: CORE_ONLY, ASSIGNABLE, AGENT_AUTO, AGENT_ONLY"),
                    "tags": _str_schema("Comma-separated keywords"),
                },
                required=["name", "description"],
            ),
            _tool_def(
                "find_skills",
                "Search registered skills by keyword.",
                {
                    "query": _str_schema("Free-text search across name, content, tags"),
                    "level": _str_schema("Optional filter: CORE_ONLY|ASSIGNABLE|AGENT_AUTO|AGENT_ONLY"),
                },
                required=["query"],
            ),
            _tool_def(
                "assign_skill",
                "Assign a skill to an agent.",
                {
                    "agent_id": _str_schema("Agent identifier"),
                    "skill_name": _str_schema("Skill identifier from registry"),
                },
                required=["agent_id", "skill_name"],
            ),
            _tool_def(
                "agent_skills",
                "List skills assigned to an agent.",
                {
                    "agent_id": _str_schema("Agent identifier"),
                },
                required=["agent_id"],
            ),
        ]

    def _core_tools(self) -> List[dict]:
        return [
            _tool_def(
                "remember",
                "Store a memory node in core.",
                {
                    "content": _str_schema("The memory content text"),
                    "node_type": _str_schema("PERSON|FACT|PREFERENCE|EVENT|TOPIC|AFFECT|BOUNDARY|SKILL|AGENT_NOTE|CORE_REF"),
                    "label": _str_schema("Short label (optional)"),
                    "source": _str_schema("Origin: USER|CORE|AGENT|<agent_id>"),
                    "trust": _float_schema("Confidence 0.0–1.0"),
                    "importance": _float_schema("Importance 0.0–1.0"),
                },
                required=["content", "node_type"],
            ),
            _tool_def(
                "recall",
                "Retrieve memories from core. Response includes top-level clock (today's date/time) and per-node age (added / last checked). Optional node_type filters results post-retrieval (PERSON|FACT|PREFERENCE|EVENT|TOPIC|AFFECT|BOUNDARY|SKILL|AGENT_NOTE|CORE_REF).",
                {
                    "query": _str_schema("Search text, node label, or node_id"),
                    "mode": _str_schema("RELATED|WHO_IS|WHAT_ABOUT|SEMANTIC|PATH|CLUSTER|TIMELINE|RECENT|PRUNE"),
                    "limit": _int_schema("Max results (alias for bound, default 10)"),
                    "bound": _int_schema("Max results (default 10)"),
                    "include_agent_notes": _bool_schema("Include raw agent notes (default false)"),
                    "node_type": _str_schema("Optional post-filter: PERSON|FACT|PREFERENCE|EVENT|TOPIC|AFFECT|BOUNDARY|SKILL|AGENT_NOTE|CORE_REF"),
                },
                required=["query"],
            ),
            _tool_def(
                "relate",
                "Create a directed edge between two nodes.",
                {
                    "from_id": _str_schema("Source node_id"),
                    "to_id": _str_schema("Target node_id"),
                    "edge_type": _str_schema("RELATES_TO|CONTRADICTS|SUPPORTS|CAUSED_BY|PART_OF|TRUSTS|DISTRUSTS|REMEMBERS|HAS_PREFERENCE|HAS_BOUNDARY|HAS_AFFECT|HAS_SKILL|REFERS_TO|SUMMARIZES"),
                    "weight": _float_schema("Edge strength 0.0–1.0"),
                },
                required=["from_id", "to_id", "edge_type"],
            ),
            _tool_def(
                "get_node",
                "Get a single memory node by ID. Response includes clock and node metadata with _clock (age) summary.",
                {
                    "node_id": _str_schema("Node identifier"),
                },
                required=["node_id"],
            ),
        ]

    def _agent_tools(self) -> List[dict]:
        return [
            _tool_def(
                "spawn_agent",
                "Prepare an agent memory scope in the shared graph (no new database by default).",
                {
                    "agent_id": _str_schema("Unique agent identifier"),
                },
                required=["agent_id"],
            ),
            _tool_def(
                "agent_remember",
                "Store an agent note in the shared graph. Raw notes are always stored as AGENT_NOTE (outside normal core recall); richer types are granted only via promotion.",
                {
                    "agent_id": _str_schema("Agent identifier"),
                    "content": _str_schema("Note content"),
                    "label": _str_schema("Short label (optional)"),
                    "attention_state": _str_schema("agent_private (default) or review_ready"),
                },
                required=["agent_id", "content"],
            ),
            _tool_def(
                "find_across_agents",
                "Search raw agent notes in the shared graph. Response includes clock and per-note age.",
                {
                    "query": _str_schema("Search topic"),
                    "min_confidence": _float_schema("Minimum similarity 0.0–1.0"),
                    "bound": _int_schema("Max results per agent"),
                },
                required=["query"],
            ),
            _tool_def(
                "promote_to_core",
                "Verify an agent note in place, preserving its graph links and node ID.",
                {
                    "agent_id": _str_schema("Agent identifier"),
                    "agent_node_id": _str_schema("Node_id in agent's shard"),
                    "new_type": _str_schema("Target core node type (optional)"),
                },
                required=["agent_id", "agent_node_id"],
            ),
            _tool_def(
                "agent_review_queue",
                "List findings agents marked ready for core review.",
                {"bound": _int_schema("Maximum findings to return")},
            ),
            _tool_def(
                "agent_set_attention",
                "Mark an agent note agent_private or review_ready.",
                {
                    "agent_id": _str_schema("Agent identifier"),
                    "agent_node_id": _str_schema("Shared agent-note identifier"),
                    "attention_state": _str_schema("agent_private or review_ready"),
                },
                required=["agent_id", "agent_node_id", "attention_state"],
            ),
        ]

    def _query_tools(self) -> List[dict]:
        return [
            _tool_def(
                "query_dsl",
                "Run a structured query DSL string. Response includes clock and per-node age.",
                {
                    "query": _str_schema('FIND PERSON "SAM" -> PREFERENCE | FIND SEMANTIC "topic" | FIND PATH "A" -> "B"'),
                },
                required=["query"],
            ),
        ]

    def _system_tools(self) -> List[dict]:
        return [
            _tool_def(
                "profile",
                "Return system performance profile.",
                {},
            ),
            _tool_def(
                "health",
                "Run system integrity check.",
                {},
            ),
            _tool_def(
                "stats",
                "Return memory statistics.",
                {},
            ),
            _tool_def(
                "rebuild_vector_index",
                "Rebuild all TF-IDF vectors from scratch.",
                {},
            ),
            _tool_def(
                "export_json",
                "Export all memory as JSON to a file.",
                {
                    "path": _str_schema("Output file path"),
                },
                required=["path"],
            ),
            _tool_def(
                "get_bloat_metrics",
                "Return DB bloat signals: freelist %, ephemeral log counts, CONTRADICTS, needs_vacuum. Use to decide self-heal.",
                {},
            ),
            _tool_def(
                "compact_ephemeral_logs",
                "Compact ephemeral telemetry logs (FEED_SNAPSHOT, RUNTIME_SAMPLE, etc.): keeps last N per label (default 3) and 7-day TTL, removes stale edges. Prevents DB bloat.",
                {
                    "keep_last": _int_schema("Keep last N per ephemeral label (default 3)"),
                    "max_age_days": _int_schema("Max age in days before TTL expiry (default 7)"),
                },
            ),
            _tool_def(
                "vacuum",
                "Run SQLite VACUUM to reclaim freelist space after deletes/compactions. Returns before/after MB.",
                {},
            ),
        ]

    # ── Dispatch ──

    def call(self, name: str, args: dict) -> Any:
        handler = getattr(self, f"cmd_{name}", None)
        if not handler:
            raise ValueError(f"Unknown tool: {name}")
        try:
            result = handler(**args)
            return result
        except TypeError as e:
            # Check for unexpected keyword arguments
            raise ValueError(f"Invalid arguments for {name}: {e}")

    # ── Core handlers ──

    def cmd_remember(self, content: str, node_type: str, label: str = None,
                     source: str = None, trust: float = None, importance: float = None) -> dict:
        kwargs = dict(content=content, node_type=node_type)
        if label is not None:
            kwargs["label"] = label
        if source is not None:
            kwargs["source"] = source
        if trust is not None:
            kwargs["trust"] = trust
        if importance is not None:
            kwargs["importance"] = importance
        node_id = self.mem.remember(**kwargs)
        return {"node_id": node_id}

    def cmd_recall(self, query: str, mode: str = "RELATED", bound: int = 10,
                   include_agent_notes: bool = False, limit: int = None,
                   node_type: str = None) -> dict:
        if limit is not None:
            bound = limit
        result = self.mem.recall(query, mode=mode.upper(), bound=bound,
                                 include_agent_notes=include_agent_notes)
        nodes = result.nodes
        if node_type:
            nodes = [n for n in nodes if n.node_type == node_type]
        return {
            "mode": result.mode,
            "total_found": result.total_found,
            "clock": self._clock(),
            "nodes": [
                {
                    "node_id": n.node_id,
                    "node_type": n.node_type,
                    "label": n.label,
                    "content": n.content[:200] if n.content else None,
                    "trust_level": n.trust_level,
                    "importance": n.importance,
                    "similarity": n.metadata.get("_similarity"),
                    "age": n.metadata.get("_clock"),
                }
                for n in nodes
            ],
        }

    def cmd_relate(self, from_id: str, to_id: str, edge_type: str,
                   weight: float = 1.0) -> dict:
        self.mem.relate(from_id, to_id, edge_type, weight=weight)
        return {"status": "ok"}

    def cmd_get_node(self, node_id: str) -> dict:
        node = self.mem.get_node(node_id)
        if not node:
            return {"error": "not found"}
        return {
            "node_id": node.node_id,
            "node_type": node.node_type,
            "label": node.label,
            "content": node.content,
            "trust_level": node.trust_level,
            "importance": node.importance,
            "clock": self._clock(),
            "metadata": node.metadata,
        }

    # ── Skill handlers ──

    def cmd_register_skill(self, name: str, description: str,
                           level: str = "ASSIGNABLE", tags: str = "") -> dict:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        self.mem.register_skill(name, description, level.upper(), tag_list)
        return {"skill": name, "level": level, "status": "registered"}

    def cmd_find_skills(self, query: str, level: str = None) -> dict:
        results = self.mem.find_skills(query=query, level=level.upper() if level else None)
        return {
            "total_found": len(results),
            "skills": [
                {"name": s["name"], "level": s["metadata"].get("skill_level", ""),
                 "description": s["description"][:150]}
                for s in results
            ],
        }

    def cmd_assign_skill(self, agent_id: str, skill_name: str) -> dict:
        self.mem.assign_skill(agent_id, skill_name)
        return {"agent_id": agent_id, "skill": skill_name, "status": "assigned"}

    def cmd_agent_skills(self, agent_id: str) -> dict:
        skills = self.mem.agent_skills(agent_id)
        return {"agent_id": agent_id, "skills": skills}

    # ── Agent handlers ──

    def cmd_spawn_agent(self, agent_id: str) -> dict:
        self.mem.spawn_agent_memory(agent_id)
        return {"agent_id": agent_id, "status": "spawned"}

    def cmd_agent_remember(self, agent_id: str, content: str, label: str = None,
                           attention_state: str = "agent_private", **kwargs) -> dict:
        # Raw agent work is always stored as AGENT_NOTE in core_shared mode;
        # richer types are only granted via explicit promotion/review.
        node_id = self.mem.agent_remember(agent_id, content, label=label,
                                          attention_state=attention_state)
        return {"agent_id": agent_id, "node_id": node_id}

    def cmd_find_across_agents(self, query: str, min_confidence: float = 0.15,
                                bound: int = 10) -> dict:
        results = self.mem.find_across_agents(query, min_confidence, bound)
        for r in results:
            r["age"] = r.get("metadata", {}).get("_clock")
        return {"clock": self._clock(), "total_found": len(results), "results": results}

    def cmd_promote_to_core(self, agent_id: str, agent_node_id: str,
                            new_type: str = None) -> dict:
        core_id = self.mem.promote_to_core(agent_id, agent_node_id, new_type=new_type)
        if core_id:
            return {"core_node_id": core_id, "status": "promoted"}
        return {"error": "agent node not found"}

    def cmd_agent_review_queue(self, bound: int = 20, limit: int = None) -> dict:
        if limit is not None:
            bound = limit
        notes = self.mem.agent_review_queue(bound)
        return {"total_found": len(notes), "notes": notes}

    def cmd_agent_set_attention(self, agent_id: str, agent_node_id: str,
                                attention_state: str) -> dict:
        ok = self.mem.agent_set_attention(agent_id, agent_node_id, attention_state)
        return {"status": "updated" if ok else "agent node not found"}

    # ── Query handlers ──

    def cmd_query_dsl(self, query: str) -> dict:
        result = self.mem.query(query)
        return {
            "mode": result.mode,
            "total_found": result.total_found,
            "clock": self._clock(),
            "nodes": [
                {
                    "node_id": n.node_id,
                    "node_type": n.node_type,
                    "label": n.label,
                    "content": n.content[:200] if n.content else None,
                    "age": n.metadata.get("_clock"),
                }
                for n in result.nodes
            ],
        }

    # ── System handlers ──

    def cmd_profile(self) -> dict:
        return self.mem.profile()

    def cmd_health(self) -> dict:
        return {"checks": self.mem.health()}

    def cmd_stats(self, limit: int = None) -> dict:
        return self.mem.stats()

    def cmd_rebuild_vector_index(self) -> dict:
        self.mem.rebuild_vector_index()
        return {"status": "rebuilt"}

    def cmd_export_json(self, path: str) -> dict:
        self.mem.export_json(path)
        return {"path": path, "status": "exported"}

    def _brain(self):
        """Lazy BrainEngine bound to this mem's DB, with robust import."""
        import sys
        brain_dir = Path(__file__).parent / "brain"
        if str(brain_dir) not in sys.path:
            sys.path.insert(0, str(brain_dir))
        if str(brain_dir.parent) not in sys.path:
            sys.path.insert(0, str(brain_dir.parent))
        from brain_engine import BrainEngine
        return BrainEngine(db_path=str(self.mem.core_db_path), brain_dir=str(brain_dir))

    def cmd_get_bloat_metrics(self) -> dict:
        # Prefer AshaMemory.get_bloat_info if available, else BrainEngine
        if hasattr(self.mem, "get_bloat_info"):
            try:
                return self.mem.get_bloat_info()
            except Exception:
                pass
        try:
            be = self._brain()
            return be.get_bloat_metrics()
        except Exception as e:
            return {"error": str(e)}

    def cmd_compact_ephemeral_logs(self, keep_last: int = None, max_age_days: int = None) -> dict:
        try:
            be = self._brain()
            kwargs = {}
            if keep_last is not None:
                kwargs["keep_last"] = int(keep_last)
            if max_age_days is not None:
                kwargs["max_age_days"] = int(max_age_days)
            res = be.compact_ephemeral_logs(**kwargs, auto_snapshot=False)
            # auto vacuum + rebuild if compaction removed anything
            if res.get("status") == "success" and res.get("removed_total", 0) > 0:
                if getattr(be, "config", {}).get("vacuum_after_prune", True):
                    res["vacuum"] = be.vacuum_db()
                if getattr(be, "config", {}).get("auto_rebuild_vectors", True):
                    res["vector_rebuild"] = be.rebuild_vectors()
            # refresh health/bloat after
            res["bloat_after"] = be.get_bloat_metrics()
            res["health_after"] = be.get_health_metrics()
            return res
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def cmd_vacuum(self) -> dict:
        if hasattr(self.mem, "vacuum"):
            try:
                return self.mem.vacuum()
            except Exception:
                pass
        try:
            be = self._brain()
            return be.vacuum_db()
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ── Resource Definitions ────────────────────────────────────────────────────

def resource_definitions(mem: AshaMemory) -> List[dict]:
    """Return static resource definitions for the memory system."""
    return [
        {
            "uri": "asha://memory/stats",
            "name": "Memory Statistics",
            "description": "Aggregate memory statistics",
            "mimeType": "application/json",
        },
        {
            "uri": "asha://memory/health",
            "name": "System Health",
            "description": "Health check results",
            "mimeType": "application/json",
        },
        {
            "uri": "asha://memory/profile",
            "name": "System Profile",
            "description": "Performance profile data",
            "mimeType": "application/json",
        },
        {
            "uri": "asha://memory/bloat",
            "name": "Bloat Metrics",
            "description": "Ephemeral log counts, freelist %, CONTRADICTS, needs_vacuum",
            "mimeType": "application/json",
        },
        {
            "uri": "asha://skills",
            "name": "Skill Registry",
            "description": "All registered skills",
            "mimeType": "application/json",
        },
    ]


def read_resource(mem: AshaMemory, uri: str) -> Optional[dict]:
    """Read a resource by URI. Returns content dict or None."""
    if uri == "asha://memory/stats":
        return {"uri": uri, "data": mem.stats()}
    if uri == "asha://memory/health":
        return {"uri": uri, "data": {"checks": mem.health()}}
    if uri == "asha://memory/profile":
        return {"uri": uri, "data": mem.profile()}
    if uri == "asha://memory/bloat":
        try:
            if hasattr(mem, "get_bloat_info"):
                return {"uri": uri, "data": mem.get_bloat_info()}
            import sys
            brain_dir = Path(__file__).parent / "brain"
            if str(brain_dir) not in sys.path:
                sys.path.insert(0, str(brain_dir))
            from brain_engine import BrainEngine
            be = BrainEngine(db_path=str(mem.core_db_path), brain_dir=str(brain_dir))
            return {"uri": uri, "data": be.get_bloat_metrics()}
        except Exception as e:
            return {"uri": uri, "data": {"error": str(e)}}
    if uri == "asha://skills":
        skills = mem.find_skills("")
        return {"uri": uri, "data": {"total": len(skills), "skills": skills}}
    return None


# ── JSON-RPC / MCP Protocol Engine ──────────────────────────────────────────

class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data


# Standard JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-specific error codes
TOOL_NOT_FOUND = -32001
TOOL_EXECUTION_ERROR = -32002
RESOURCE_NOT_FOUND = -32003


class MCPServer:
    """JSON-RPC 2.0 MCP server over stdio transport."""

    def __init__(self, mem: AshaMemory, skills_path: str = None):
        self.mem = mem
        self.tools = ToolRegistry(mem)
        self._initialized = False

        # Auto-load skills if path provided
        if skills_path:
            p = Path(skills_path)
            if p.exists():
                count = mem.load_skill_registry(str(p))
                sys.stderr.write(f"MCP: loaded {count} skills from {p.name}\n")
                sys.stderr.flush()

    # ── Dispatch ──

    def handle_line(self, line: str):
        """Process a single JSON-RPC message. Writes response to stdout."""
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
        # ── Lifecycle ──
        if method == "initialize":
            return self._initialize(params)
        if method == "notifications/initialized":
            self._initialized = True
            return None  # notification
        if method == "notifications/cancelled":
            return None

        # ── Tools ──
        if method == "tools/list":
            return {"tools": self.tools.definitions()}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            if not name:
                raise MCPError(INVALID_PARAMS, "Tool name required")
            try:
                result = self.tools.call(name, args)
                return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
            except ValueError as e:
                raise MCPError(TOOL_EXECUTION_ERROR, str(e))

        # ── Resources ──
        if method == "resources/list":
            return {"resources": resource_definitions(self.mem)}
        if method == "resources/read":
            uri = params.get("uri")
            if not uri:
                raise MCPError(INVALID_PARAMS, "URI required")
            content = read_resource(self.mem, uri)
            if content is None:
                raise MCPError(RESOURCE_NOT_FOUND, f"Resource not found: {uri}")
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(content["data"], indent=2),
                    }
                ]
            }

        # ── Ping ──
        if method == "ping":
            return {}

        raise MCPError(METHOD_NOT_FOUND, f"Unknown method: {method}")

    def _initialize(self, params: dict) -> dict:
        client_info = params.get("clientInfo", {})
        client_name = client_info.get("name", "unknown")
        client_version = client_info.get("version", "0.0")
        sys.stderr.write(f"MCP: client={client_name}/{client_version}\n")
        sys.stderr.flush()
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
                "resources": {},
            },
            "serverInfo": SERVER_INFO,
        }

    # ── Wire protocol ──

    def _send_result(self, msg_id: Any, result: Any):
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": msg_id,
            "result": result if result is not None else {},
        }
        sys.stdout.write(json.dumps(response, default=str) + "\n")
        sys.stdout.flush()

    def _send_error(self, msg_id: Any, code: int, message: str, data: Any = None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": msg_id,
            "error": error,
        }
        sys.stdout.write(json.dumps(response, default=str) + "\n")
        sys.stdout.flush()


# ── CLI Entry Point ─────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="ASHA Memory MCP Server (stdio)")
    parser.add_argument("--base-path", default="./asha_mcp_data",
                        help="Memory data directory")
    parser.add_argument("--skills", default=None,
                        help="Path to ASHA_SKILLS_REGISTRY.txt")
    args = parser.parse_args()

    # Initialize memory system
    mem = AshaMemory(base_path=args.base_path)
    server = MCPServer(mem, skills_path=args.skills)

    sys.stderr.write(f"MCP: server ready, base_path={os.path.abspath(args.base_path)}\n")
    sys.stderr.flush()

    # Read JSON-RPC messages from stdin (one per line)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        server.handle_line(line)


if __name__ == "__main__":
    main()
