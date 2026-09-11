"""src.logic.engine — Logic Engine: smart agent resolver + healing.

Spec: logicengine.md rev2 + 3 guardrails (Asha).
- resolve_agent(ref, *, auto_create: bool) 6-step pipeline, explicit flag.
- resolve_ref_exact vs fuzzy (guardrail #1): exact only branches 1-2 of src/core/nodes.py:280.
- healed_fields ring buffer cap 5 (guardrail #2).
- find_across_agents never implicit (guardrail #3).

Pure Python, stdlib only. No new sqlite3.connect; reuses connections passed in.
"""

import hashlib
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..agents import store as agent_store
from ..core import nodes as core_nodes

# ── constants ──
HEALED_FIELDS_CAP = 5  # guardrail #2
VALID_NODE_TYPES = core_nodes.NODE_TYPES
VALID_EDGE_TYPES = {"RELATES_TO", "CONTRADICTS", "SUPPORTS", "CAUSED_BY", "PART_OF",
                    "TRUSTS", "DISTRUSTS", "REMEMBERS", "HAS_PREFERENCE", "HAS_BOUNDARY",
                    "HAS_AFFECT", "HAS_SKILL", "REFERS_TO", "SUMMARIZES", "PROMOTED_FROM"}
VALID_MODES = {"RELATED", "WHO_IS", "WHAT_ABOUT", "SEMANTIC", "PATH", "CLUSTER",
               "TIMELINE", "RECENT", "PRUNE"}
WRITE_TOOLS = {"remember", "agent_remember", "relate", "agent_relate",
               "mailbox.send", "mail_send", "promote", "promote_to_core",
               "set_attention", "update", "update_node", "delete", "delete_node",
               "register_skill"}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "agent"


def _clamp01(v: float) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return 0.5


class LogicEngine:
    """Orchestration layer: resolver + healing + routing.

    Usage:
        engine = LogicEngine(agents_conn, core_conn)
        agent_id = engine.resolve_agent("AshaWeb", auto_create=True)
        healed, args = engine.heal("remember", raw_args, auto_create=True)
    """

    def __init__(self, agents_conn: sqlite3.Connection,
                 core_conn: Optional[sqlite3.Connection] = None):
        self.agents = agents_conn
        self.core = core_conn

    # ── resolver ──

    def resolve_agent(self, ref: Optional[str], *, auto_create: bool) -> Optional[str]:
        """6-step pipeline. Returns canonical agent_id or None (core scope).

        auto_create: explicit flag — True on write path, False on read path.
        """
        if not ref or not str(ref).strip():
            return None
        ref = str(ref).strip()
        # Step 2: exact canonical (agent_id or alias)
        row = agent_store.resolve_agent(self.agents, ref)
        if row:
            return row["agent_id"]
        # Handle "agent:<id>" wrapper
        if ref.startswith("agent:"):
            inner = ref[6:]
            row = agent_store.resolve_agent(self.agents, inner)
            if row:
                return row["agent_id"]
        slug = _slugify(ref)
        # Step 3: slug hit
        hit = self.agents.execute(
            "SELECT agent_id FROM agents WHERE slug = ?", (slug,)).fetchone()
        if hit:
            return hit["agent_id"]
        # Step 4: source/substring heuristic (read-only, never writes)
        # token split slug, search agents.slug/job_hint and nodes.source
        tokens = [t for t in slug.split("-") if t]
        # try agents slug/job_hint LIKE
        for tok in tokens:
            if len(tok) < 3:
                continue
            r = self.agents.execute(
                "SELECT agent_id FROM agents WHERE slug LIKE ? OR job_hint LIKE ? ORDER BY created_at DESC LIMIT 1",
                (f"%{tok}%", f"%{tok}%")).fetchone()
            if r:
                return r["agent_id"]
            # also scan nodes.source (agents.db source column may contain AshaWeb)
            try:
                r2 = self.agents.execute(
                    "SELECT agent_id FROM nodes WHERE source LIKE ? ORDER BY updated_at DESC LIMIT 1",
                    (f"%{tok}%",)).fetchone()
                if r2 and r2["agent_id"]:
                    # verify agent still exists
                    if agent_store.resolve_agent(self.agents, r2["agent_id"]):
                        return r2["agent_id"]
            except sqlite3.OperationalError:
                pass
            # scan metadata agent_hint
            try:
                r3 = self.agents.execute(
                    "SELECT agent_id FROM nodes WHERE json_extract(metadata,'$.agent_hint') LIKE ? ORDER BY updated_at DESC LIMIT 1",
                    (f"%{tok}%",)).fetchone()
                if r3 and r3["agent_id"]:
                    if agent_store.resolve_agent(self.agents, r3["agent_id"]):
                        return r3["agent_id"]
            except sqlite3.OperationalError:
                pass
        # Step 5: auto-create (only when flag True)
        if auto_create:
            try:
                res = agent_store.agent_ensure(self.agents, ref)
                return res["agent_id"]
            except ValueError:
                # Step 6: validate — if preferred_id invalid, ensure with slug
                hint = f"agent_{slug}_{hashlib.md5(slug.encode()).hexdigest()[:4]}"
                try:
                    res = agent_store.agent_ensure(self.agents, ref, preferred_id=hint)
                    return res["agent_id"]
                except Exception:
                    # final fallback: deterministic from slug
                    res = agent_store.agent_ensure(self.agents, slug)
                    return res["agent_id"]
        return None

    def resolve_db(self, agent_ref: Optional[str], *, auto_create: bool) -> str:
        """Helper for dashboard: returns 'agents' if resolved, else 'core'."""
        aid = self.resolve_agent(agent_ref, auto_create=auto_create) if agent_ref else None
        return "agents" if aid else "core"

    # ── exact ref resolver (guardrail #1) ──

    def resolve_ref_exact(self, conn: sqlite3.Connection, ref: str,
                          agent_id: Optional[str] = None) -> Optional[str]:
        """Branches 1-2 only: node_id exact then label exact. Never LIKE."""
        pred, params = (" AND agent_id = ?", (agent_id,)) if agent_id is not None else ("", ())
        row = conn.execute(
            "SELECT node_id FROM nodes WHERE node_id = ?" + pred,
            (ref, *params)).fetchone()
        if row:
            return row["node_id"]
        row = conn.execute(
            "SELECT node_id FROM nodes WHERE label = ?" + pred + " LIMIT 1",
            (ref, *params)).fetchone()
        if row:
            return row["node_id"]
        return None

    def resolve_ref_fuzzy(self, conn: sqlite3.Connection, ref: str,
                          agent_id: Optional[str] = None) -> Optional[str]:
        """Branch 3: LIKE fallback (only when fuzzy=True)."""
        # try exact first
        nid = self.resolve_ref_exact(conn, ref, agent_id)
        if nid:
            return nid
        pred, params = (" AND agent_id = ?", (agent_id,)) if agent_id is not None else ("", ())
        row = conn.execute(
            "SELECT node_id FROM nodes WHERE label LIKE ?" + pred + " LIMIT 1",
            (f"%{ref}%", *params)).fetchone()
        return row["node_id"] if row else None

    # ── healing helpers ──

    @staticmethod
    def _record_heal(node_meta: Dict[str, Any], entry: str) -> None:
        """Append to healed_fields ring buffer cap 5 (guardrail #2)."""
        lst = node_meta.get("healed_fields")
        if not isinstance(lst, list):
            lst = []
        lst.append(entry)
        # truncate oldest
        if len(lst) > HEALED_FIELDS_CAP:
            lst = lst[-HEALED_FIELDS_CAP:]
        node_meta["healed_fields"] = lst

    @staticmethod
    def _healed_response(healed: Dict[str, str]) -> Dict[str, str]:
        return healed

    # ── heal per tool ──

    def heal(self, tool: str, args: Dict[str, Any],
             *, auto_create: Optional[bool] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Normalize args, return (healed_args, healed_map).

        auto_create: if None, inferred from WRITE_TOOLS (writes True, reads False).
        Caller should pass explicit flag when tool semantics differ.
        """
        if auto_create is None:
            auto_create = tool in WRITE_TOOLS
        healed: Dict[str, str] = {}
        out = dict(args) if args else {}

        # generic: resolve agent alias fields -> canonical agent_id
        # unified tools use "agent", legacy use "agent_id" / "job_hint"
        agent_raw = out.get("agent", out.get("agent_id", out.get("job_hint")))
        if agent_raw is not None and tool not in ("agent_ensure", "agent_list", "find_across_agents"):
            # Don't heal agent_list/find_across_agents agent param
            aid = self.resolve_agent(str(agent_raw), auto_create=auto_create)
            if aid and str(agent_raw) != aid:
                healed["agent"] = f"{agent_raw!r} -> {aid}"
                out["agent"] = aid
                out["agent_id"] = aid  # normalize both keys

        # tool-specific heals
        if tool in ("remember", "agent_remember", "register_skill"):
            # content required — no heal if missing (let handler throw)
            if "node_type" in out and out["node_type"] not in VALID_NODE_TYPES:
                orig = out["node_type"]
                healed["node_type"] = f"{orig!r} -> FACT"
                out["node_type"] = "FACT"
            elif "node_type" not in out:
                # default
                default = "AGENT_NOTE" if out.get("agent") or out.get("agent_id") else "FACT"
                healed["node_type"] = f"(missing) -> {default}"
                out["node_type"] = default
            # trust/importance coerce + clamp
            for k in ("trust", "trust_level", "importance"):
                if k in out and out[k] is not None:
                    try:
                        v = float(out[k])
                        clamped = _clamp01(v)
                        if clamped != v:
                            healed[k] = f"{v} -> {clamped}"
                            out[k] = clamped
                    except Exception:
                        healed[k] = f"{out[k]!r} -> 0.5"
                        out[k] = 0.5
            # defaults if absent
            if "trust" not in out and "trust_level" not in out:
                out["trust"] = 0.5
            if "importance" not in out:
                out["importance"] = 0.5
            # label fallback handled by store, but strip markdown
            if "label" in out and out["label"]:
                lab = str(out["label"]).strip()
                if lab != out["label"]:
                    healed["label"] = f"trimmed"
                    out["label"] = lab
            # source preserved verbatim + agent_hint for circularity (§8)
            if out.get("source") and out.get("agent"):
                # ensure metadata has agent_hint
                meta = out.get("metadata")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                if "agent_hint" not in meta:
                    meta["agent_hint"] = out["source"]
                    out["metadata"] = meta
            # persist healed_fields ring buffer cap 5 (guardrail #2) into metadata for writes
            if healed:
                meta = out.get("metadata")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                for k, v in list(healed.items()):
                    self._record_heal(meta, f"{k}: {v}")
                out["metadata"] = meta

        elif tool in ("recall", "agent_recall"):
            if "mode" in out and out["mode"]:
                m = str(out["mode"]).upper().strip()
                if m not in VALID_MODES:
                    healed["mode"] = f"{out['mode']!r} -> RELATED"
                    out["mode"] = "RELATED"
                elif m != out["mode"]:
                    out["mode"] = m
            # bound/limit coerce
            for k in ("bound", "limit"):
                if k in out and out[k] is not None:
                    try:
                        v = int(str(out[k]).strip())
                        clamped = max(1, min(200, v))
                        if clamped != v:
                            healed[k] = f"{v} -> {clamped}"
                        out[k] = clamped
                    except Exception:
                        healed[k] = f"{out[k]!r} -> 10"
                        out[k] = 10

        elif tool in ("relate", "agent_relate"):
            # edge_type heal
            if "edge_type" in out and out["edge_type"] not in VALID_EDGE_TYPES:
                healed["edge_type"] = f"{out['edge_type']!r} -> RELATES_TO"
                out["edge_type"] = "RELATES_TO"
            if "weight" in out and out["weight"] is not None:
                try:
                    w = float(out["weight"])
                    cl = max(-1.0, min(1.0, w))
                    if cl != w:
                        healed["weight"] = f"{w} -> {cl}"
                        out["weight"] = cl
                except Exception:
                    healed["weight"] = f"{out['weight']!r} -> 1.0"
                    out["weight"] = 1.0
            # ensure fuzzy defaults False
            if "fuzzy" not in out:
                out["fuzzy"] = False
            else:
                # coerce bool
                if isinstance(out["fuzzy"], str):
                    out["fuzzy"] = out["fuzzy"].lower() in ("1", "true", "yes")

        elif tool in ("mailbox.send", "mail_send"):
            # heal from/to scopes: "AshaWeb" -> "agent:agent_ashaweb_4fee"
            for k in ("from", "to"):
                if k in out and out[k]:
                    raw = str(out[k]).strip()
                    if raw not in ("core", "user", "broadcast") and not raw.startswith("agent:"):
                        # try resolve as agent
                        aid = self.resolve_agent(raw, auto_create=False)
                        if aid:
                            healed[k] = f"{raw!r} -> agent:{aid}"
                            out[k] = f"agent:{aid}"
                    elif raw.startswith("agent:"):
                        inner = raw[6:]
                        aid = self.resolve_agent(inner, auto_create=False)
                        if aid and aid != inner:
                            healed[k] = f"{raw} -> agent:{aid}"
                            out[k] = f"agent:{aid}"
            # agent->broadcast heal: rewrite to core with marker
            if out.get("from", "").startswith("agent:") and out.get("to") == "broadcast":
                healed["to"] = "broadcast -> agent:core (was_broadcast_attempt)"
                out["to"] = "core"
                meta = out.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                meta["was_broadcast_attempt"] = True
                out["metadata"] = meta

        # metadata csv heal (key=value csv)
        if "metadata" in out and isinstance(out["metadata"], str):
            s = out["metadata"].strip()
            if s and not (s.startswith("{") or s.startswith("[")):
                # try key=value csv
                if "=" in s:
                    d: Dict[str, Any] = {}
                    for part in s.split(","):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            d[k.strip()] = v.strip()
                    if d:
                        healed["metadata"] = f"csv -> {d}"
                        out["metadata"] = d

        return out, healed

    def attach_healed_to_response(self, result: Any, healed: Dict[str, str]) -> Any:
        """Attach healed map to response for AI learning (non-breaking)."""
        if not healed:
            return result
        if isinstance(result, dict):
            r = dict(result)
            r["healed"] = healed
            return r
        return {"result": result, "healed": healed}
