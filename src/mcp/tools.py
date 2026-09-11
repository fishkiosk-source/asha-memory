"""src.mcp.tools — 23-tool surface + dispatch + inbox-injection wrapper.

Locked surface (Idea.md rev8 §8/C1). Handler map (dotted name -> cmd_*):
core (6):        remember, recall, relate, get_node, update_node, delete_node
agents (8):      agent_ensure, agent_list, agent_remember, agent_recall,
                 find_across_agents, promote_to_core, review_queue, set_attention
mailbox (3):     mailbox.send, mailbox.read, mailbox.ack
system (6):      profile, health, stats, export, vacuum, compact
+ deprecated (uncounted): register_skill -> remember(SKILL) alias, one minor.

Injection (C5, MCP-envelope-only): dispatch() derives the caller scope
(agent_id arg -> that agent's canonical inbox incl. alias resolution,
mailbox.send -> sender, mailbox.read/ack -> scope param, else core),
calls mailbox.check_inbox ONCE, and decorates the result with
"mailbox_notice" ONLY when mail is pending. Empty inbox -> the result object
is returned UNCHANGED (same object, no noise). Internal store/recall code
never touches the mailbox; only this dispatch wrapper does.

Transactions: dispatch() commits both DBs on success (persisting handler
writes + noted_at) and rolls back both on exception (single-threaded stdio
server; Phase 6 adds the inter-process lock for brain overlap).
"""

import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..agents import bridge, mailbox
from ..agents import store as agent_store
from ..agents.mailbox import check_inbox, format_injection
from ..core import edges as core_edges
from ..core import nodes as core_nodes
from ..core import recall as core_recall
from ..core.clock import InternalClock
from ..core.layers import get_layer
from ..core.migrate import check as core_check, migrate as core_migrate
from ..core.store import USER_VERSION_V3, connect as core_connect, core_db_path

TOOL_NAMES = [
    # core (6)
    "remember", "recall", "relate", "get_node", "update_node", "delete_node",
    # agents (8)
    "agent_ensure", "agent_list", "agent_remember", "agent_recall",
    "find_across_agents", "promote_to_core", "review_queue", "set_attention",
    # mailbox (3)
    "mailbox.send", "mailbox.read", "mailbox.ack",
    # system (6)
    "profile", "health", "stats", "export", "vacuum", "compact",
]
assert len(TOOL_NAMES) == 23, "locked v3 surface is 23 tools (C1)"

# register_skill survives only as thin remember(SKILL) alias for one minor (C1)
DEPRECATED_ALIASES = {"register_skill": "remember"}

SERVER_VERSION = "3.0.0"

RECALL_CONTENT_TRUNC = 200  # v2 parity for recall lists (get_node is full)


# ── Context ──

def open_context(memory_dir: Optional[str] = None) -> Dict[str, Any]:
    """Open both DBs + converge schema (first run creates memory/*.db)."""
    import threading
    mem = Path(memory_dir) if memory_dir else core_db_path().parent
    mem.mkdir(parents=True, exist_ok=True)
    core = core_connect(core_db_path(mem))
    core_migrate(core)
    agents = agent_store.connect(agent_store.agents_db_path(mem))
    agent_store.ensure_schema(agents)
    core.commit()
    agents.commit()
    return {"memory_dir": mem, "core": core, "agents": agents,
            "clock": InternalClock(enabled=True),
            "cache": core_recall.LRUCache(capacity=50),
            "config": {}, "_lock": threading.RLock()}


def close_context(ctx: Dict[str, Any]) -> None:
    ctx["core"].close()
    ctx["agents"].close()


# ── Injection ──

def _canonical_agent(agents_conn: sqlite3.Connection,
                     ref: str) -> Optional[str]:
    try:
        row = agent_store.resolve_agent(agents_conn, ref)
    except Exception:
        return None
    return f"agent:{row['agent_id']}" if row else None


def _scope_for(tool: str, args: Dict[str, Any],
               agents_conn: sqlite3.Connection) -> str:
    """Derive the caller's inbox scope (canonicalized)."""
    if tool == "mailbox.send":
        scope = args.get("from", "core")
    elif tool in ("mailbox.read", "mailbox.ack"):
        scope = args.get("scope", "core")
    elif args.get("agent_id"):
        scope = f"agent:{args['agent_id']}"
    else:
        return "core"
    if scope.startswith("agent:"):
        return _canonical_agent(agents_conn, scope[6:]) or scope
    return scope


def with_inbox_notice(scope: str, result: Any,
                      agents_conn: sqlite3.Connection) -> Any:
    """Envelope decoration. Empty inbox -> result returned UNCHANGED."""
    msgs = check_inbox(agents_conn, scope)
    if not msgs:
        return result
    notice = format_injection(msgs, scope)
    if isinstance(result, dict):
        out = dict(result)
        out["mailbox_notice"] = notice
        return out
    return {"result": result, "mailbox_notice": notice}


def dispatch(tool: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    """Route tool call -> LogicEngine heal + handler + injection wrapper. Commits on success."""
    # cross-process WAL retry covers handler + commit (both can hit 'database is locked')
    import time as _t, sqlite3 as _sq
    _healed: Dict[str, str] = {}
    try:
        from ..logic.engine import LogicEngine as _LogicEngine  # neutral import, not in src/core
        _engine = _LogicEngine(ctx["agents"], ctx["core"])
        # infer auto_create: writes True, reads False
        from ..logic.engine import WRITE_TOOLS as _WRITE
        _auto = tool in _WRITE
        # mailbox.send healing needs auto_create True for agent from/to
        if tool == "mailbox.send":
            _auto = True
        args, _healed = _engine.heal(tool, args, auto_create=_auto)
    except Exception:
        # heal must never break dispatch; fallback to raw args
        _healed = {}
    # NOTE: register_skill is NOT remapped here — cmd_register_skill IS the
    # thin alias (creates a SKILL node via remember internally). DEPRECATED_ALIASES
    # only documents the conceptual mapping for the Phase-6 removal.
    handler = _HANDLERS.get(tool)
    if handler is None:
        raise ValueError(f"Unknown tool: {tool}")
    # retry loop covers handler + inbox + commit, with per-attempt RLock
    for _attempt in range(6):
        _lock = ctx.get("_lock")
        _acquired = False
        if _lock is not None:
            _lock.acquire()
            _acquired = True
        try:
            try:
                result = handler(args, ctx)
                # attach healed map for AI learning (non-breaking)
                if _healed and isinstance(result, dict) and "healed" not in result:
                    result = dict(result)
                    result["healed"] = _healed
            except (TypeError, KeyError) as e:
                raise ValueError(f"Invalid arguments for {tool}: {e}")
            except (TypeError, KeyError) as e:
                raise ValueError(f"Invalid arguments for {tool}: {e}")
            except Exception:
                ctx["core"].rollback()
                ctx["agents"].rollback()
                raise
            scope = _scope_for(tool, args, ctx["agents"])
            try:
                result = with_inbox_notice(scope, result, ctx["agents"])
            except Exception:
                # Inbox decoration must never break a tool call (mailbox table may be
                # mid-migration); return the undecorated result.
                pass
            # cross-process WAL retry on commit (dashboard/direct/MCP fighting)
            ctx["core"].commit()
            ctx["agents"].commit()
            break
        except _sq.OperationalError as _e:
            if "locked" in str(_e).lower() or "busy" in str(_e).lower():
                if _attempt==5:
                    raise
                ctx["core"].rollback()
                ctx["agents"].rollback()
                # release lock before sleeping so other threads can progress
                if _acquired:
                    _lock.release()
                    _acquired = False
                _t.sleep([0.05,0.12,0.25,0.5,0.9,1.5][_attempt])
                continue
            raise
        finally:
            if _acquired:
                try:
                    _lock.release()
                except: pass
    return result


# ── Shaping helpers ──

def _as_dict(value: Any, field: str) -> Dict[str, Any]:
    """Accept a dict or JSON-string object (MCP clients vary)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {field}: must be an object or JSON string")
    if not isinstance(parsed, dict):
        raise ValueError(f"Invalid {field}: must be an object or JSON string")
    return parsed


def _as_id_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        raise ValueError("Invalid msg_ids: must be a list or JSON string")
    if not isinstance(parsed, list):
        raise ValueError("Invalid msg_ids: must be a list or JSON string")
    return [str(v) for v in parsed]


def _bound(args: Dict[str, Any], default: int) -> int:
    """v2-exact limit/bound aliasing (explicit limit wins, even 0 is kept)."""
    bound = args.get("bound", default)
    if args.get("limit") is not None:
        bound = args["limit"]
    try:
        bound = int(bound)
    except (ValueError, TypeError):
        bound = int(default)
    return max(1, min(bound, 200))

def _offset(args: Dict[str, Any], default: int = 0) -> int:
    try:
        return int(args.get("offset", default))
    except (ValueError, TypeError):
        return int(default)


def _clock_now(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    clock = ctx.get("clock")
    return clock.now() if clock and clock.enabled else None


def _collect_label_versions(conn, nodes, agent_id=None):
    """Map node_id -> (v, total) for duplicate labels (global rank by created_at).

    Global rank: oldest with same label = v1, newest = vN. Uses created_at ASC
    across the whole DB (scoped by agent_id for agents.db). Only labels with
    total >1 are annotated - brain janitor handles real dedup.
    """
    from collections import defaultdict
    label_to_nids = defaultdict(list)
    for n in nodes:
        lbl = n.get("label")
        if not lbl:
            continue
        label_to_nids[lbl].append(n.get("node_id"))
    out = {}
    for label, nids_in_result in label_to_nids.items():
        try:
            if agent_id is not None:
                rows = conn.execute(
                    "SELECT node_id FROM nodes WHERE label = ? AND agent_id = ? ORDER BY created_at ASC",
                    (label, agent_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT node_id FROM nodes WHERE label = ? ORDER BY created_at ASC",
                    (label,)).fetchall()
        except Exception:
            continue
        if not rows or len(rows) <= 1:
            continue
        order = [r["node_id"] for r in rows]
        total = len(order)
        pos = {nid: i + 1 for i, nid in enumerate(order)}
        for nid in nids_in_result:
            if nid in pos:
                out[nid] = (pos[nid], total)
    return out


def _annotate_nodes_with_versions(conn, nodes, agent_id=None):
    """Inject _label_version/_label_display into node metadata for duplicates."""
    try:
        vm = _collect_label_versions(conn, nodes, agent_id=agent_id)
    except Exception:
        return
    if not vm:
        return
    for n in nodes:
        nid = n.get("node_id")
        if nid not in vm:
            continue
        v, total = vm[nid]
        meta = n.get("metadata")
        if not isinstance(meta, dict):
            try:
                import json as _json
                meta = _json.loads(meta) if isinstance(meta, str) else {}
            except Exception:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            n["metadata"] = meta
        # e.g. v1/10, v2/10 - plus short hash (last 4 of node_id)
        meta["_label_version"] = f"v{v}/{total}"
        meta["_label_display"] = f"{n.get('label')} v{v}/{total} ({nid[-4:]})" if nid else f"{n.get('label')} v{v}/{total}"
        meta["_node_short"] = nid[-4:] if nid else None


def _shape_node(n: Dict[str, Any], trunc: Optional[int] = RECALL_CONTENT_TRUNC) -> Dict[str, Any]:
    meta = n.get("metadata") or {}
    content = n.get("content")
    return {
        "node_id": n.get("node_id"),
        "node_type": n.get("node_type"),
        "label": n.get("label"),
        "label_display": meta.get("_label_display"),
        "label_version": meta.get("_label_version"),
        "node_id_short": meta.get("_node_short") or (n.get("node_id")[-4:] if n.get("node_id") else None),
        "content": (content[:trunc] if content and trunc else content),
        "trust_level": n.get("trust_level"),
        "importance": n.get("importance"),
        "similarity": meta.get("_similarity"),
        "age": meta.get("_clock"),
    }


# ── Core handlers ──

def cmd_remember(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    # Unified: if agent resolved -> agent_remember path (orchestration compression)
    ag = args.get("agent") or args.get("agent_id")
    if ag:
        # heal already canonicalized agent -> agent_id
        try:
            from ..logic.engine import LogicEngine as _LE
            le = _LE(ctx["agents"], ctx["core"])
            aid = le.resolve_agent(str(ag), auto_create=True)
            if aid:
                nid = agent_store.agent_remember(
                    ctx["agents"], aid, args["content"], label=args.get("label"),
                    metadata=_as_dict(args.get("metadata"), "metadata"),
                    attention_state=args.get("attention_state", "agent_private"))
                return {"node_id": nid, "agent_id": aid}
        except Exception:
            pass  # fallback to core
    kwargs = {"content": args["content"], "node_type": args["node_type"]}
    for k in ("label", "source", "trust", "importance"):
        if args.get(k) is not None:
            kwargs[k] = args[k]
    # persist healed_fields if engine recorded heals (guardrail #2)
    if args.get("metadata") and isinstance(args.get("metadata"), dict):
        kwargs["metadata"] = args["metadata"]
    nid = core_nodes.remember_many(ctx["core"], [kwargs])[0]
    return {"node_id": nid}


def cmd_recall(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    # Unified: if agent resolved -> scoped agent_recall (guardrail #3: no implicit cross-agent merge)
    ag = args.get("agent") or args.get("agent_id")
    if ag:
        try:
            from ..logic.engine import LogicEngine as _LE
            le = _LE(ctx["agents"], ctx["core"])
            aid = le.resolve_agent(str(ag), auto_create=False)
            if aid:
                bound = _bound(args, 10)
                res = agent_store.agent_recall(
                    ctx["agents"], aid, args["query"],
                    mode=args.get("mode", "RELATED").upper(), bound=bound,
                    offset=_offset(args, 0), clock=ctx.get("clock"))
                nodes = res["nodes"]
                if args.get("node_type"):
                    nodes = [n for n in nodes if n.get("node_type") == args["node_type"]]
                try:
                    _annotate_nodes_with_versions(ctx["agents"], nodes, agent_id=aid)
                except Exception:
                    pass
                out = {"agent_id": aid, "mode": res["mode"], "total_found": res["total_found"],
                       "bound_applied": res["bound_applied"], "clock": _clock_now(ctx),
                       "nodes": [_shape_node(n) for n in nodes]}
                return out
        except Exception:
            pass
    bound = _bound(args, 10)
    # explicit include_agent_notes only when caller sets True (guardrail #3)
    inc = bool(args.get("include_agent_notes", False))
    res = core_recall.recall(
        ctx["core"], args["query"], mode=args.get("mode", "RELATED").upper(),
        bound=bound, offset=_offset(args, 0),
        include_agent_notes=inc,
        config=ctx.get("config"), clock=ctx.get("clock"), cache=ctx.get("cache"))
    nodes = res["nodes"]
    if args.get("node_type"):
        nodes = [n for n in nodes if n.get("node_type") == args["node_type"]]
    # guardrail #3: explicit cross-agent merge only when include_agent_notes True and no agent scoped
    if inc:
        try:
            from ..agents import bridge as _bridge
            x = _bridge.search_all_agents(ctx["agents"], args["query"], bound=bound)
            # merge without dup, tag _agent_id, cap to bound
            seen = {n.get("node_id") for n in nodes}
            for r in x:
                if r["node_id"] not in seen:
                    # shape agent result like core node
                    nodes.append(r)
                    seen.add(r["node_id"])
            nodes = nodes[:bound]
        except Exception:
            pass
    # annotate duplicate labels globally (v1 oldest ... vN newest) for core scope
    try:
        _annotate_nodes_with_versions(ctx["core"], [n for n in nodes if not n.get("_agent_id")])
        # agent-merged nodes (if any) get per-agent version from agents.db
        agent_merged = [n for n in nodes if n.get("_agent_id")]
        if agent_merged:
            # group by agent for version lookup
            from collections import defaultdict as _dd
            grp = _dd(list)
            for n in agent_merged:
                grp[n.get("_agent_id")].append(n)
            for aid2, lst in grp.items():
                try:
                    _annotate_nodes_with_versions(ctx["agents"], lst, agent_id=aid2)
                except Exception:
                    pass
    except Exception:
        pass
    return {"mode": res["mode"], "total_found": len(nodes) if inc else res["total_found"],
            "bound_applied": len(nodes) > bound if inc else res["bound_applied"], "clock": _clock_now(ctx),
            "nodes": [_shape_node(n) for n in nodes]}


def cmd_relate(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    # Unified relate with exact vs fuzzy (guardrail #1)
    ag = args.get("agent") or args.get("agent_id")
    fuzzy = bool(args.get("fuzzy", False))
    from_raw = args["from_id"]
    to_raw = args["to_id"]
    # resolve agent if provided
    aid: Optional[str] = None
    if ag:
        try:
            from ..logic.engine import LogicEngine as _LE
            le = _LE(ctx["agents"], ctx["core"])
            aid = le.resolve_agent(str(ag), auto_create=False)
        except Exception:
            aid = None
    if aid:
        # agent-scoped: resolve refs exactly unless fuzzy
        try:
            from ..logic.engine import LogicEngine as _LE2
            le2 = _LE2(ctx["agents"], ctx["core"])
            if fuzzy:
                f = le2.resolve_ref_fuzzy(ctx["agents"], from_raw, aid) or from_raw
                t = le2.resolve_ref_fuzzy(ctx["agents"], to_raw, aid) or to_raw
            else:
                f = le2.resolve_ref_exact(ctx["agents"], from_raw, aid) or from_raw
                t = le2.resolve_ref_exact(ctx["agents"], to_raw, aid) or to_raw
            eid = agent_store.agent_relate(ctx["agents"], aid, f, t,
                                            args["edge_type"], args.get("weight", 1.0))
            return {"status": "ok", "edge_id": eid, "agent_id": aid}
        except Exception as e:
            raise ValueError(str(e))
    # core path: resolve with exact/fuzzy
    try:
        from ..logic.engine import LogicEngine as _LE3
        le3 = _LE3(ctx["agents"], ctx["core"])
        if fuzzy:
            f = le3.resolve_ref_fuzzy(ctx["core"], from_raw) or from_raw
            t = le3.resolve_ref_fuzzy(ctx["core"], to_raw) or to_raw
        else:
            f = le3.resolve_ref_exact(ctx["core"], from_raw) or from_raw
            t = le3.resolve_ref_exact(ctx["core"], to_raw) or to_raw
        eid = core_edges.relate(ctx["core"], f, t,
                                args["edge_type"], args.get("weight", 1.0))
        return {"status": "ok", "edge_id": eid}
    except Exception as e:
        raise ValueError(str(e))


def cmd_get_node(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    # unified get: if agent -> try agents.db with exact ref, else core
    ag = args.get("agent") or args.get("agent_id")
    if ag:
        try:
            from ..logic.engine import LogicEngine as _LE
            le = _LE(ctx["agents"], ctx["core"])
            aid = le.resolve_agent(str(ag), auto_create=False)
            if aid:
                nid = le.resolve_ref_exact(ctx["agents"], args["node_id"], aid) or args["node_id"]
                node = agent_store.agent_get_node(ctx["agents"], aid, nid)
                if node:
                    # bump access via core path (agent table)
                    ctx["agents"].execute("UPDATE nodes SET access_count = access_count + 1, updated_at = ? WHERE node_id = ?", (int(time.time()), nid))
                    ctx["agents"].execute("INSERT INTO access_log (node_id, accessed_at) VALUES (?, ?)", (nid, int(time.time())))
                    try:
                        _annotate_nodes_with_versions(ctx["agents"], [node], agent_id=aid)
                    except Exception:
                        pass
                    out = _shape_node(node, trunc=None)
                    out["clock"] = _clock_now(ctx)
                    out["metadata"] = node.get("metadata") or {}
                    out["layer"] = get_layer(ctx["agents"], nid)
                    return out
        except Exception:
            pass
    node = core_nodes.get_node(ctx["core"], args["node_id"])
    if not node:
        # try exact label fallback
        try:
            from ..logic.engine import LogicEngine as _LE2
            le2 = _LE2(ctx["agents"], ctx["core"])
            nid = le2.resolve_ref_exact(ctx["core"], args["node_id"])
            if nid and nid != args["node_id"]:
                node = core_nodes.get_node(ctx["core"], nid)
        except Exception:
            pass
    if not node:
        return {"error": "not found"}
    core_nodes.bump_access(ctx["core"], node["node_id"])
    clock = ctx.get("clock")
    if clock and clock.enabled:
        last = clock.last_accessed_before(ctx["core"], node["node_id"], int(time.time()))
        node["metadata"]["_clock"] = clock.summarize_node(node, last_accessed=last)
    try:
        _annotate_nodes_with_versions(ctx["core"], [node])
    except Exception:
        pass
    out = _shape_node(node, trunc=None)
    out["clock"] = _clock_now(ctx)
    out["metadata"] = node.get("metadata") or {}
    out["layer"] = get_layer(ctx["core"], node["node_id"])
    return out


def cmd_update_node(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    fields = {k: args[k] for k in ("label", "content", "trust_level",
                                   "importance", "source") if k in args}
    if "metadata" in args:
        fields["metadata"] = _as_dict(args["metadata"], "metadata")
    ag = args.get("agent") or args.get("agent_id")
    if ag:
        try:
            from ..logic.engine import LogicEngine as _LE
            le = _LE(ctx["agents"], ctx["core"])
            aid = le.resolve_agent(str(ag), auto_create=False)
            if aid:
                nid = le.resolve_ref_exact(ctx["agents"], args["node_id"], aid) or args["node_id"]
                node = core_nodes.update_node(ctx["agents"], nid, **fields) if False else None
                # use agents direct update (isolated check)
                existing = agent_store.agent_get_node(ctx["agents"], aid, nid)
                if existing:
                    node = core_nodes.update_node(ctx["agents"], nid, **fields)
                    if not node:
                        return {"error": "not found"}
                    return _shape_node(node, trunc=None)
                else:
                    return {"error": "not found"}
        except Exception:
            pass
    node = core_nodes.update_node(ctx["core"], args["node_id"], **fields)
    if not node:
        return {"error": "not found"}
    return _shape_node(node, trunc=None)


def cmd_delete_node(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    ag = args.get("agent") or args.get("agent_id")
    if ag:
        try:
            from ..logic.engine import LogicEngine as _LE
            le = _LE(ctx["agents"], ctx["core"])
            aid = le.resolve_agent(str(ag), auto_create=False)
            if aid:
                nid = le.resolve_ref_exact(ctx["agents"], args["node_id"], aid) or args["node_id"]
                ok = agent_store.agent_delete_node(ctx["agents"], aid, nid)
                return {"status": "deleted" if ok else "not found"}
        except Exception:
            pass
    ok = core_nodes.delete_node(ctx["core"], args["node_id"])
    return {"status": "deleted" if ok else "not found"}


# ── Agent handlers ──

def cmd_agent_ensure(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return agent_store.agent_ensure(ctx["agents"], args["job_hint"],
                                    args.get("preferred_id"))


def cmd_agent_list(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {"agents": agent_store.agent_list(ctx["agents"])}


def cmd_agent_remember(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    nid = agent_store.agent_remember(
        ctx["agents"], args["agent_id"], args["content"], label=args.get("label"),
        metadata=_as_dict(args.get("metadata"), "metadata"),
        attention_state=args.get("attention_state", "agent_private"))
    return {"agent_id": args["agent_id"], "node_id": nid}


def cmd_agent_recall(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    bound = _bound(args, 10)
    res = agent_store.agent_recall(
        ctx["agents"], args["agent_id"], args["query"],
        mode=args.get("mode", "RELATED").upper(), bound=bound,
        offset=_offset(args, 0), clock=ctx.get("clock"))
    nodes = res["nodes"]
    try:
        _annotate_nodes_with_versions(ctx["agents"], nodes, agent_id=args["agent_id"])
    except Exception:
        pass
    return {"agent_id": args["agent_id"], "mode": res["mode"],
            "total_found": res["total_found"], "bound_applied": res["bound_applied"],
            "clock": _clock_now(ctx), "nodes": [_shape_node(n) for n in nodes]}


def cmd_find_across_agents(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    results = bridge.search_all_agents(ctx["agents"], args["query"],
                                       args.get("min_confidence", 0.15),
                                       args.get("bound", 10))
    clock = ctx.get("clock")
    if clock and clock.enabled:
        now = int(time.time())
        for r in results:
            last = clock.last_accessed_before(ctx["agents"], r["node_id"], now)
            (r.get("metadata") or {})["_clock"] = clock.summarize_node(r, last_accessed=last)
    # annotate duplicate labels per-agent (v1 oldest globally per agent)
    try:
        from collections import defaultdict as _dd2
        grp2 = _dd2(list)
        for r in results:
            grp2[r.get("_agent_id") or r.get("agent_id")].append(r)
        for aid2, lst in grp2.items():
            if aid2:
                _annotate_nodes_with_versions(ctx["agents"], lst, agent_id=aid2)
    except Exception:
        pass
    return {"clock": _clock_now(ctx), "total_found": len(results), "results": results}


def cmd_promote_to_core(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        cid = bridge.promote(ctx["core"], ctx["agents"], args["agent_id"],
                             args["agent_node_id"], args.get("new_type") or "FACT",
                             args.get("new_label"))
    except ValueError as e:
        if "does not exist" in str(e):
            return {"error": "agent node not found"}
        raise
    return {"core_node_id": cid, "status": "promoted"}


def cmd_review_queue(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    bound = _bound(args, 20)
    notes = bridge.list_review_queue(ctx["agents"], bound)
    return {"total_found": len(notes), "notes": notes}


def cmd_set_attention(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    ok = agent_store.agent_set_attention(ctx["agents"], args["agent_id"],
                                         args["agent_node_id"],
                                         args["attention_state"])
    return {"status": "updated" if ok else "agent node not found"}


# ── Mailbox handlers ──

def cmd_mailbox_send(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return mailbox.send(ctx["agents"], args["from"], args["to"], args["body"])


def cmd_mailbox_read(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        limit = int(args.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(args.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0
    msgs = mailbox.read(ctx["agents"], args.get("scope", "core"),
                        args.get("state", "pending"),
                        limit, offset)
    return {"scope": args.get("scope", "core"), "messages": msgs}


def cmd_mailbox_ack(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return mailbox.ack(ctx["agents"], args.get("scope", "core"),
                       _as_id_list(args.get("msg_ids")))


# ── System handlers ──

def _db_size_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / 1_048_576, 3)
    except OSError:
        return 0.0


def cmd_profile(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    core_nodes_n = ctx["core"].execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    agents_nodes_n = ctx["agents"].execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    qlog = ctx["core"].execute("SELECT COUNT(*) FROM query_log").fetchone()[0]
    cache = ctx.get("cache")
    return {
        "core_nodes": core_nodes_n,
        "agents_nodes": agents_nodes_n,
        "core_query_log": qlog,
        "cache_hits": getattr(cache, "hits", 0),
        "cache_misses": getattr(cache, "misses", 0),
        "server_version": SERVER_VERSION,
    }


def cmd_health(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    from ..core.migrate import check as core_check
    core_rep = core_check(ctx["core"])
    agents_rep = agent_store.check(ctx["agents"])
    ok = bool(core_rep.get("ok") and agents_rep.get("ok"))
    return {"ok": ok, "core": core_rep, "agents": agents_rep}


def cmd_stats(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    which = args.get("db", "all")
    out: Dict[str, Any] = {}
    mem = ctx["memory_dir"]
    if which in ("all", "core"):
        out["core"] = _stats_one(ctx["core"], mem / "core.db")
    if which in ("all", "agents"):
        out["agents"] = _stats_one(ctx["agents"], mem / "agents.db")
    return out


def _stats_one(conn: sqlite3.Connection, path: Path) -> Dict[str, Any]:
    by_type = {r["node_type"]: r["c"] for r in conn.execute(
        "SELECT node_type, COUNT(*) AS c FROM nodes GROUP BY node_type")}
    return {
        "nodes": sum(by_type.values()),
        "nodes_by_type": by_type,
        "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "db_mb": _db_size_mb(path),
    }


def cmd_export(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    dest = Path(args["path"])
    if not dest.parent.exists():
        raise ValueError(f"export(): parent dir does not exist: {dest.parent}")
    mem = ctx["memory_dir"]
    manifest: Dict[str, Any] = {
        "v3_version": SERVER_VERSION,
        "user_version": USER_VERSION_V3,
        "created_at": int(time.time()),
        "files": {},
        "counts": {
            "core_nodes": ctx["core"].execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "agents_nodes": ctx["agents"].execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        },
    }
    with tempfile.TemporaryDirectory(prefix="asha_export_") as td:
        staged = []
        for name, conn in (("core.db", ctx["core"]), ("agents.db", ctx["agents"])):
            # Sanctioned exception to the connect() invariant: ephemeral export
            # staging target (temp dir, deleted after tar), never a store open.
            target = sqlite3.connect(str(Path(td) / name))
            try:
                conn.backup(target)
            finally:
                target.close()
            staged.append((name, Path(td) / name))
        cfg = mem / "config.json"
        if cfg.exists():
            staged.append(("config.json", cfg))
        with tarfile.open(dest, "w:gz") as tar:
            for arcname, src in staged:
                if arcname != "config.json":
                    with open(src, "rb") as fh:
                        manifest["files"][arcname] = hashlib.sha256(fh.read()).hexdigest()
                tar.add(str(src), arcname=arcname)
        manifest["files"]["config.json"] = "external" if (mem / "config.json").exists() else "absent"
    with open(str(dest) + ".manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return {"path": str(dest), "manifest": manifest, "status": "exported"}


def cmd_vacuum(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    mem = ctx["memory_dir"]
    out: Dict[str, Any] = {}
    for name, conn in (("core", ctx["core"]), ("agents", ctx["agents"])):
        path = mem / f"{name}.db"
        before = _db_size_mb(path)
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
        out[name] = {"before_mb": before, "after_mb": _db_size_mb(path)}
    return out


def cmd_compact(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    keep_last = int(args.get("keep_last", 3))
    ttl = int(args.get("max_age_days", 7)) * 86400
    which = args.get("db", "all")
    now = int(time.time())
    out: Dict[str, Any] = {}
    targets = []
    if which in ("all", "core"):
        targets.append(("core", ctx["core"]))
    if which in ("all", "agents"):
        targets.append(("agents", ctx["agents"]))
    for name, conn in targets:
        # TTL ALWAYS applies (C13 fix of the v2 no-op); keep_last caps survivors
        cur = conn.execute("DELETE FROM ephemeral_events WHERE created_at < ?",
                           (now - ttl,))
        removed_ttl = cur.rowcount if cur.rowcount >= 0 else 0
        removed_cap = 0
        labels = [r[0] for r in conn.execute(
            "SELECT DISTINCT label FROM ephemeral_events")]
        for lab in labels:
            keep = [r[0] for r in conn.execute(
                "SELECT id FROM ephemeral_events WHERE label = ?"
                " ORDER BY created_at DESC LIMIT ?", (lab, keep_last))]
            if keep:
                ph = ",".join("?" * len(keep))
                cur = conn.execute(
                    f"DELETE FROM ephemeral_events WHERE label = ? AND id NOT IN ({ph})",
                    (lab, *keep))
                removed_cap += cur.rowcount if cur.rowcount >= 0 else 0
        out[name] = {"removed_ttl": removed_ttl, "removed_cap": removed_cap,
                     "labels": labels}
    return out


def cmd_register_skill(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Deprecated thin alias -> remember(SKILL). Removed next minor (C1)."""
    tags = args.get("tags", "")
    tag_list = [t.strip() for t in str(tags).split(",") if t.strip()]
    nid = core_nodes.remember_many(ctx["core"], [{
        "content": args["description"], "node_type": "SKILL", "label": args["name"],
        "metadata": {"skill_level": args.get("level", "ASSIGNABLE"),
                     "tags": tag_list, "deprecated_alias": "register_skill"},
    }])[0]
    return {"skill": args["name"], "node_id": nid, "status": "registered",
            "deprecated": "use remember(node_type=SKILL)"}


_HANDLERS: Dict[str, Callable] = {
    "remember": cmd_remember,
    "recall": cmd_recall,
    "relate": cmd_relate,
    "get_node": cmd_get_node,
    "update_node": cmd_update_node,
    "delete_node": cmd_delete_node,
    "agent_ensure": cmd_agent_ensure,
    "agent_list": cmd_agent_list,
    "agent_remember": cmd_agent_remember,
    "agent_recall": cmd_agent_recall,
    "find_across_agents": cmd_find_across_agents,
    "promote_to_core": cmd_promote_to_core,
    "review_queue": cmd_review_queue,
    "set_attention": cmd_set_attention,
    "mailbox.send": cmd_mailbox_send,
    "mailbox.read": cmd_mailbox_read,
    "mailbox.ack": cmd_mailbox_ack,
    "profile": cmd_profile,
    "health": cmd_health,
    "stats": cmd_stats,
    "export": cmd_export,
    "vacuum": cmd_vacuum,
    "compact": cmd_compact,
    "register_skill": cmd_register_skill,
}


# ── Tool definitions (tools/list) ──

def _str(description: str, enum: Optional[List[str]] = None) -> dict:
    d = {"type": "string", "description": description}
    if enum:
        d["enum"] = enum
    return d


def _int(description: str) -> dict:
    return {"type": "integer", "description": description}


def _float(description: str) -> dict:
    return {"type": "number", "description": description}


def _bool(description: str) -> dict:
    return {"type": "boolean", "description": description}


def _tool_def(name: str, description: str, properties: dict,
              required: Optional[List[str]] = None) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required or []}}


def definitions() -> List[dict]:
    """23 canonical tools + deprecated register_skill (v2 tools/list parity)."""
    node_types = ["PERSON", "TOPIC", "EVENT", "FACT", "PREFERENCE", "BOUNDARY",
                  "AFFECT", "AGENT_NOTE", "CORE_REF", "SKILL"]
    edge_types = sorted(core_edges.EDGE_TYPES)
    modes = ["RELATED", "WHO_IS", "WHAT_ABOUT", "SEMANTIC", "PATH", "CLUSTER",
             "TIMELINE", "RECENT", "PRUNE"]
    return [
        _tool_def("remember", "Store a memory node. Unified: if agent provided -> agent note (auto-ensure), else core. Heals missing node_type/trust.",
                  {"content": _str("The memory content text"),
                   "node_type": _str("Node type (optional, heals to FACT/AGENT_NOTE)", node_types),
                   "label": _str("Short label (optional)"),
                   "source": _str("Origin: USER|CORE or agent source label"),
                   "trust": _float("Confidence 0.0-1.0 (default 0.5)"),
                   "importance": _float("Importance 0.0-1.0 (default 0.5)"),
                   "agent": _str("Agent hint/id: AshaWeb | web scout | agent_ashaweb_4fee (optional, auto-resolves + auto-creates)"),
                   "agent_id": _str("Alias for agent (legacy)"),
                   "metadata": _str("JSON object (healed_fields auto-tracked)"),
                   "attention_state": _str("agent_private|review_ready (agent only)", ["agent_private", "review_ready"])},
                  ["content"]),
        _tool_def("recall", "Retrieve memories. DSL strings (FIND ...) auto-detected. Unified: agent scopes to one agent.",
                  {"query": _str("Search text, node label, node_id, or FIND ... DSL"),
                   "mode": _str("Recall mode", modes),
                   "bound": _int("Max results (default 10)"),
                   "limit": _int("Alias for bound"),
                   "offset": _int("Pagination offset"),
                   "include_agent_notes": _bool("Explicit cross-agent merge when agent=None (guardrail #3)"),
                   "node_type": _str("Optional post-filter", node_types),
                   "agent": _str("Agent hint/id to scope recall (optional)"),
                   "agent_id": _str("Alias for agent")},
                  ["query"]),
        _tool_def("relate", "Create a directed edge. Unified: agent scopes edge; fuzzy=False exact only (guardrail #1).",
                  {"from_id": _str("Source node_id or exact label"),
                   "to_id": _str("Target node_id or exact label"),
                   "edge_type": _str("Edge type", edge_types),
                   "weight": _float("Edge strength -1.0-1.0 (clamped)"),
                   "agent": _str("Agent hint/id to scope edge (optional)"),
                   "agent_id": _str("Alias for agent"),
                   "fuzzy": _bool("Allow LIKE substring fallback when exact miss (default false)")},
                  ["from_id", "to_id", "edge_type"]),
        _tool_def("get_node", "Get a single node by ID or exact label. Unified: agent scopes lookup.",
                  {"node_id": _str("Node identifier or exact label"),
                   "agent": _str("Agent hint/id (optional)"),
                   "agent_id": _str("Alias for agent")}, ["node_id"]),
        _tool_def("update_node", "Partially update a node. Unified: agent scopes lookup.",
                  {"node_id": _str("Node identifier or exact label"),
                   "label": _str("New label"), "content": _str("New content"),
                   "trust_level": _float("0.0-1.0"), "importance": _float("0.0-1.0"),
                   "source": _str("New source"), "metadata": _str("JSON merge patch"),
                   "agent": _str("Agent hint/id (optional)"),
                   "agent_id": _str("Alias for agent")},
                  ["node_id"]),
        _tool_def("delete_node", "Delete a node (cascades). Unified: agent scopes lookup.",
                  {"node_id": _str("Node identifier or exact label"),
                   "agent": _str("Agent hint/id (optional)"),
                   "agent_id": _str("Alias for agent")}, ["node_id"]),
        _tool_def("agent_ensure", "Issue (or return) the canonical agent ID for a job.",
                  {"job_hint": _str("Job description, e.g. 'nightly scout'"),
                   "preferred_id": _str("Optional requested ID (must be free + valid)")},
                  ["job_hint"]),
        _tool_def("agent_list", "List registered agents.", {}),
        _tool_def("agent_remember", "Store an agent note (always AGENT_NOTE).",
                  {"agent_id": _str("Canonical agent ID (agent_ensure first)"),
                   "content": _str("Note content"), "label": _str("Short label"),
                   "attention_state": _str("agent_private|review_ready",
                                           ["agent_private", "review_ready"]),
                   "metadata": _str("JSON object")}, ["agent_id", "content"]),
        _tool_def("agent_recall", "Recall ONE agent's notes (isolated).",
                  {"agent_id": _str("Canonical agent ID"), "query": _str("Search text"),
                   "mode": _str("Recall mode", modes), "bound": _int("Max results"),
                   "limit": _int("Alias for bound"), "offset": _int("Offset")},
                  ["agent_id", "query"]),
        _tool_def("find_across_agents", "TF-IDF search over all agents' notes (core use).",
                  {"query": _str("Search topic"),
                   "min_confidence": _float("Minimum similarity"),
                   "bound": _int("Max results")}, ["query"]),
        _tool_def("promote_to_core", "Move an agent note to core (core use only).",
                  {"agent_id": _str("Agent ID"), "agent_node_id": _str("Agent node ID"),
                   "new_type": _str("Target core type", node_types),
                   "new_label": _str("Optional new label")},
                  ["agent_id", "agent_node_id"]),
        _tool_def("review_queue", "List agent notes marked review_ready.",
                  {"bound": _int("Max findings"), "limit": _int("Alias for bound")}),
        _tool_def("set_attention", "Mark an agent note agent_private|review_ready.",
                  {"agent_id": _str("Agent ID"), "agent_node_id": _str("Node ID"),
                   "attention_state": _str("agent_private|review_ready",
                                           ["agent_private", "review_ready"])},
                  ["agent_id", "agent_node_id", "attention_state"]),
        _tool_def("mailbox.send", "Send a message (core<->agent, agent->agent, core->broadcast, user<->...).",
                  {"from": _str("Sender scope: core|agent:<id>|user"),
                   "to": _str("Recipient: core|agent:<id>|broadcast|user"),
                   "body": _str("Message text")}, ["from", "to", "body"]),
        _tool_def("mailbox.read", "Read a scope's messages (marks read).",
                  {"scope": _str("core|agent:<id>|user|all"),
                   "state": _str("pending|noted|all",
                                 ["pending", "noted", "all"]),
                   "limit": _int("Max messages"), "offset": _int("Offset")}),
        _tool_def("mailbox.ack", "Ack scope messages (default all unacked).",
                  {"scope": _str("core|agent:<id>|user"),
                   "msg_ids": _str("JSON list of msg_ids (default all)")}),
        _tool_def("profile", "Performance profile (counts, cache).", {}),
        _tool_def("health", "Integrity check for both DBs.", {}),
        _tool_def("stats", "Statistics per DB.",
                  {"db": _str("all|core|agents", ["all", "core", "agents"])}),
        _tool_def("export", "Export both DBs + manifest to tar.gz.",
                  {"path": _str("Destination .tar.gz path")}, ["path"]),
        _tool_def("vacuum", "VACUUM both DBs (before/after MB).", {}),
        _tool_def("compact", "Ephemeral TTL sweep (TTL always applies).",
                  {"keep_last": _int("Keep last N per label"),
                   "max_age_days": _int("TTL days"),
                   "db": _str("all|core|agents", ["all", "core", "agents"])}),
        _tool_def("register_skill", "DEPRECATED alias -> remember(node_type=SKILL).",
                  {"name": _str("Skill identifier"), "description": _str("What it does"),
                   "level": _str("Skill level"), "tags": _str("Comma-separated keywords")},
                  ["name", "description"]),
    ]
