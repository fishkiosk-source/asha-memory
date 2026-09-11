"""src.agents.store — SQLite open + schema + identity for memory/agents.db.

Single agents.db for ALL agents, sharded by agent_id column (locked §2.1).
Schema mirrors core (§3.2) + agent_id NOT NULL on nodes/edges (+denormalized
on edges so per-agent walks/deletes never scan) + agents identity registry +
agent_aliases (v2 free-form ID migration map) + mailbox tables (DDL owned by
mailbox.py, created here by ensure_schema).

THE INVARIANT: connect() below is the ONLY place that opens agents.db
(same PRAGMAs as core). No raw sqlite3.connect elsewhere.

Identity (locked §2.10/rev5): agents never invent IDs. agent_ensure() is the
only issuer (canonical agent_<slug>_<hex4>, idempotent on slug).
agent_remember/agent_recall/mailbox.send reject unknown IDs
(UNKNOWN_AGENT_HINT). Raw agent work is always AGENT_NOTE (v2 parity —
richer types only after promotion/review).
"""

import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..core import recall as core_recall
from ..core.layers import init_layer
from ..core.nodes import (
    DEFAULTS as CORE_DEFAULTS,
    build_index,
    bump_access,
    get_node as core_get_node,
    remember_many as core_remember_many,
)
from ..core import vectors as core_vectors
from .mailbox import MAILBOX_DDL

AGENTS_DB_NAME = "agents.db"

# agent_ensure validation (Idea.md rev8 §8.1)
PREFERRED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
UNKNOWN_AGENT_HINT = "unknown_agent, hint: call agent_ensure first"

ATTENTION_STATES = ("agent_private", "review_ready", "core_verified")
AGENT_WRITABLE_TYPES = {"TOPIC", "EVENT", "FACT", "PREFERENCE",
                        "AFFECT", "AGENT_NOTE", "CORE_REF"}
AGENT_DEFAULTS = {
    "agent_max_notes": 100,
    "agent_max_content_length": 800,
}

NODES_DDL = """CREATE TABLE IF NOT EXISTS nodes (
    node_id      TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    node_type    TEXT NOT NULL,
    label        TEXT NOT NULL,
    content      TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'AGENT',
    trust_level  REAL NOT NULL DEFAULT 0.5 CHECK (trust_level >= 0 AND trust_level <= 1),
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    importance   REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
    checksum     TEXT NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    CHECK (node_type IN ('PERSON','TOPIC','EVENT','FACT','PREFERENCE','BOUNDARY','AFFECT','AGENT_NOTE','CORE_REF','SKILL'))
)"""

EDGES_DDL = """CREATE TABLE IF NOT EXISTS edges (
    edge_id      TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    from_node    TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    to_node      TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    edge_type    TEXT NOT NULL,
    weight       REAL NOT NULL DEFAULT 1.0 CHECK (weight >= -1 AND weight <= 1),
    created_at   INTEGER NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    CHECK (edge_type IN ('RELATES_TO','CONTRADICTS','SUPPORTS','CAUSED_BY','PART_OF','TRUSTS','DISTRUSTS','REMEMBERS','HAS_PREFERENCE','HAS_BOUNDARY','HAS_AFFECT','HAS_SKILL','REFERS_TO','SUMMARIZES','PROMOTED_FROM')),
    UNIQUE(from_node, to_node, edge_type)
)"""

# Mirrors core auxiliaries (internal-content FTS per C19, compact vectors,
# ephemeral + df/meta for TF-IDF). memory_layers needed by the brain's
# agents-WORKING regulator (Phase 6).
AUX_DDL = [
    "CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(label, content, node_id UNINDEXED)",
    "CREATE TABLE IF NOT EXISTS node_index (word TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE, field TEXT NOT NULL DEFAULT 'content', weight REAL NOT NULL DEFAULT 1.0, PRIMARY KEY (word, node_id, field))",
    "CREATE TABLE IF NOT EXISTS access_log (log_id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE, accessed_at INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS node_vectors (node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE, vector TEXT NOT NULL, magnitude REAL NOT NULL DEFAULT 0.0)",
    "CREATE TABLE IF NOT EXISTS memory_layers (node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE, layer TEXT NOT NULL DEFAULT 'working', promoted_at INTEGER, layer_order INTEGER NOT NULL DEFAULT 1, CHECK (layer IN ('working','short_term','long_term','archive')))",
    "CREATE TABLE IF NOT EXISTS query_log (log_id INTEGER PRIMARY KEY AUTOINCREMENT, query_text TEXT NOT NULL, mode TEXT NOT NULL, result_count INTEGER NOT NULL DEFAULT 0, duration_ms REAL NOT NULL DEFAULT 0, cache_hit INTEGER NOT NULL DEFAULT 0, queried_at INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ephemeral_events (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, metadata TEXT NOT NULL DEFAULT '{}')",
    "CREATE TABLE IF NOT EXISTS vector_df (term TEXT PRIMARY KEY, df INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, job_hint TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, metadata TEXT NOT NULL DEFAULT '{}')",
    "CREATE TABLE IF NOT EXISTS agent_aliases (alias TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE)",
]

AUX_TRIGGERS = [
    "CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN INSERT INTO node_fts(label, content, node_id) VALUES (new.label, new.content, new.node_id); END",
    "CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN DELETE FROM node_fts WHERE node_id = old.node_id; END",
    # C20: scoped to label/content so access bumps never reindex FTS (see core schema.json)
    "CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE OF label, content ON nodes BEGIN DELETE FROM node_fts WHERE node_id = old.node_id; INSERT INTO node_fts(label, content, node_id) VALUES (new.label, new.content, new.node_id); END",
]

AUX_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_agents_agent_id ON nodes(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_agents_agent_updated ON nodes(agent_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_agents_edges_agent ON edges(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type)",
    "CREATE INDEX IF NOT EXISTS idx_node_index_word ON node_index(word)",
    "CREATE INDEX IF NOT EXISTS idx_access_log_node_time ON access_log(node_id, accessed_at)",
    "CREATE INDEX IF NOT EXISTS idx_node_vectors_mag ON node_vectors(magnitude)",
    "CREATE INDEX IF NOT EXISTS idx_memory_layers_layer ON memory_layers(layer)",
    "CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_node)",
    "CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_node)",
]

REQUIRED_TABLES = ["nodes", "edges", "node_fts", "node_index", "access_log",
                   "schema_meta", "node_vectors", "memory_layers", "query_log",
                   "ephemeral_events", "vector_df", "vector_meta",
                   "agents", "agent_aliases", "mailbox", "mailbox_receipts"]


def agents_db_path(base: Optional[Union[Path, str]] = None) -> Path:
    from ..core.store import memory_dir
    return memory_dir(base) / AGENTS_DB_NAME


def connect(db_path: Union[Path, str],
            cache_size: int = -64000,
            timeout_s: float = 30.0) -> sqlite3.Connection:
    """Open agents.db with the v3 PRAGMA invariant. Caller owns close()."""
    from ..core.store import BUSY_TIMEOUT_MS
    conn = sqlite3.connect(str(db_path), timeout=timeout_s, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA cache_size={int(cache_size)}")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Idempotent full sync of agents.db (tables + triggers + indexes + seeds)."""
    conn.execute(NODES_DDL)
    conn.execute(EDGES_DDL)
    for stmt in AUX_DDL + MAILBOX_DDL:
        conn.execute(stmt)
    for stmt in AUX_TRIGGERS:
        conn.execute(stmt)
    for stmt in AUX_INDEXES:
        conn.execute(stmt)
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '3.0')")
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lexicon_version', '3')")
    return {"ok": True}


def check(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Read-only schema completeness (mirrors core check() shape)."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    want = ["idx_agents_agent_id", "idx_agents_agent_updated", "idx_agents_edges_agent",
            "idx_nodes_type", "idx_node_index_word", "idx_access_log_node_time",
            "idx_node_vectors_mag", "idx_memory_layers_layer"]
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    missing_idx = [i for i in want if i not in have]
    ok = not missing and not missing_idx
    return {"ok": ok, "missing_tables": missing, "missing_indexes": missing_idx}


def _now() -> int:
    return int(time.time())


def _slugify(job_hint: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (job_hint or "").lower()).strip("-")
    return slug or "agent"


def agent_ensure(conn: sqlite3.Connection, job_hint: str,
                 preferred_id: Optional[str] = None) -> Dict[str, Any]:
    """Issue (or return) the canonical agent ID for a job. Idempotent on slug + preferred_id.

    Returns {"agent_id": ..., "created": bool}.
    - Idempotent on slug: same job_hint (slugified) returns same ID.
    - Idempotent on preferred_id: if preferred_id already exists (agents or alias), returns that ID (created=False) instead of error.
    - New IDs are deterministic (hash of slug) so re-creation after purge yields same ID.
    Preferred_id still validated ^[a-z0-9][a-z0-9_-]{1,40}$ when creating new.
    """
    import hashlib
    slug = _slugify(job_hint)
    row = conn.execute("SELECT agent_id FROM agents WHERE slug = ?", (slug,)).fetchone()
    if row:
        return {"agent_id": row["agent_id"], "created": False}
    # preferred_id idempotency: if already taken, return existing
    if preferred_id is not None:
        exists = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (preferred_id,)).fetchone()
        if exists:
            return {"agent_id": exists["agent_id"], "created": False}
        alias_row = conn.execute("SELECT agent_id FROM agent_aliases WHERE alias = ?", (preferred_id,)).fetchone()
        if alias_row:
            return {"agent_id": alias_row["agent_id"], "created": False}
        if not PREFERRED_ID_RE.match(preferred_id):
            # deterministic canonical for hint
            hint = f"agent_{slug}_{hashlib.md5(slug.encode()).hexdigest()[:4]}"
            raise ValueError(f"preferred_id rejected ({preferred_id!r}); canonical hint: {hint}")
        agent_id = preferred_id
        conn.execute("INSERT INTO agents (agent_id, slug, job_hint, created_at, metadata)"
                     " VALUES (?, ?, ?, ?, '{}')",
                     (agent_id, slug, job_hint or "", _now()))
        return {"agent_id": agent_id, "created": True}
    # deterministic canonical from slug hash
    base_hex = hashlib.md5(slug.encode()).hexdigest()[:4]
    canonical = f"agent_{slug}_{base_hex}"
    agent_id = canonical
    counter = 0
    while conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone() or conn.execute("SELECT 1 FROM agent_aliases WHERE alias = ?", (agent_id,)).fetchone():
        counter += 1
        alt_hex = hashlib.md5(f"{slug}:{counter}".encode()).hexdigest()[:4]
        agent_id = f"agent_{slug}_{alt_hex}"
        if counter > 20:
            agent_id = f"agent_{slug}_{uuid.uuid4().hex[:4]}"
            break
    conn.execute("INSERT INTO agents (agent_id, slug, job_hint, created_at, metadata)"
                 " VALUES (?, ?, ?, ?, '{}')",
                 (agent_id, slug, job_hint or "", _now()))
    return {"agent_id": agent_id, "created": True}


def agent_list(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """All registered agents (dashboard dropdowns + broadcast fan-out)."""
    return [dict(r) for r in conn.execute(
        "SELECT agent_id, slug, job_hint, created_at FROM agents ORDER BY created_at")]


def resolve_agent(conn: sqlite3.Connection, ref: str) -> Optional[Dict[str, Any]]:
    """Canonical agent row by ID or migrated alias (None when unknown)."""
    row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (ref,)).fetchone()
    if row:
        return dict(row)
    alias = conn.execute("SELECT agent_id FROM agent_aliases WHERE alias = ?",
                         (ref,)).fetchone()
    if alias:
        row = conn.execute("SELECT * FROM agents WHERE agent_id = ?",
                           (alias["agent_id"],)).fetchone()
        return dict(row) if row else None
    return None


def require_agent(conn: sqlite3.Connection, agent_id: str) -> Dict[str, Any]:
    """Resolve or raise unknown_agent (all agent tools gate here)."""
    row = resolve_agent(conn, agent_id)
    if not row:
        raise ValueError(f"{UNKNOWN_AGENT_HINT}: {agent_id!r}")
    return row


def add_alias(conn: sqlite3.Connection, alias: str, agent_id: str) -> None:
    """Map a v2 free-form ID to a canonical agent (migration compat)."""
    require_agent(conn, agent_id)
    conn.execute("INSERT OR REPLACE INTO agent_aliases (alias, agent_id) VALUES (?, ?)",
                 (alias, agent_id))


def _agent_metadata(agent_id: str, metadata: Optional[Dict] = None,
                    attention_state: str = "agent_private") -> Dict:
    """Port of v2 _agent_metadata (states locked; agent_id embedded)."""
    if attention_state not in ("agent_private", "review_ready", "core_verified"):
        raise ValueError("Invalid agent attention_state")
    result = dict(metadata or {})
    result.update({"agent_id": agent_id, "agent_scoped": True,
                   "attention_state": attention_state})
    return result


def _enforce_cap(conn: sqlite3.Connection, agent_id: str,
                 config: Optional[Dict] = None) -> None:
    """agent_max_notes cap (v2 P0-6 parity): drop oldest agent_private first,
    never review_ready; fallback to oldest regardless. Best-effort."""
    cfg = {**AGENT_DEFAULTS, **(config or {})}
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM nodes WHERE agent_id = ?",
                           (agent_id,)).fetchone()[0]
        if cnt < int(cfg["agent_max_notes"]):
            return
        oldest = conn.execute(
            """SELECT node_id FROM nodes WHERE agent_id = ?
               AND json_extract(metadata,'$.attention_state') = 'agent_private'
               ORDER BY updated_at ASC LIMIT 1""", (agent_id,)).fetchone()
        if oldest is None:
            oldest = conn.execute("SELECT node_id FROM nodes WHERE agent_id = ?"
                                  " ORDER BY updated_at ASC LIMIT 1",
                                  (agent_id,)).fetchone()
        if oldest:
            row = conn.execute("SELECT label, content FROM nodes WHERE node_id = ?",
                               (oldest["node_id"],)).fetchone()
            if row:
                core_vectors.update_on_delete(conn, oldest["node_id"],
                                              row["label"] or "", row["content"] or "")
            conn.execute("DELETE FROM nodes WHERE node_id = ?", (oldest["node_id"],))
    except sqlite3.Error:
        pass


def agent_remember(conn: sqlite3.Connection, agent_id: str, content: str,
                   label: Optional[str] = None,
                   metadata: Optional[Dict] = None,
                   attention_state: str = "agent_private",
                   config: Optional[Dict] = None) -> str:
    """Write one agent note. Always AGENT_NOTE (v2 parity — richer types only
    after promotion). Rejects unknown agent_id. Returns node_id."""
    agent = require_agent(conn, agent_id)
    cfg = {**CORE_DEFAULTS, **AGENT_DEFAULTS, **(config or {})}
    max_len = int(cfg["agent_max_content_length"])
    if len(content) > max_len:
        content = content[:max_len - 3] + "..."
    _enforce_cap(conn, agent_id, cfg)
    meta = _agent_metadata(agent["agent_id"], metadata, attention_state)
    nid = core_remember_many(conn, [{
        "content": content, "node_type": "AGENT_NOTE",
        "label": label, "source": f"AGENT_{agent['agent_id']}",
        "trust": 0.5, "importance": 0.5, "metadata": meta,
    }], config=cfg, extra_cols={"agent_id": agent["agent_id"]})[0]
    # if created directly as review_ready, emit reminder mail
    if attention_state == "review_ready":
        try:
            from . import mailbox as _mailbox
            date_s = __import__("datetime").datetime.fromtimestamp(_now()).strftime("%Y-%m-%d %H:%M")
            body = (f"REVIEW_READY: NODE {nid} (\"{label or content[:30]}\") from "
                    f"agent:{agent['agent_id']} ready for review at {date_s} "
                    f"-- use review_queue / promote_to_core to graduate.")
            _mailbox.send(conn, f"agent:{agent['agent_id']}", "core", body,
                          metadata={"kind": "review_ready", "node_id": nid,
                                   "agent_id": agent["agent_id"], "label": label or content[:30]})
        except Exception:
            pass
    return nid


def agent_recall(conn: sqlite3.Connection, agent_id: str, query: str,
                 mode: str = "RELATED", bound: int = 30, offset: int = 0,
                 config: Optional[Dict] = None, clock=None) -> Dict[str, Any]:
    """Scoped recall over ONE agent's notes (isolation via agent_id predicate).

    include_agent_notes=True (own notes visible regardless of attention_state).
    """
    agent = require_agent(conn, agent_id)
    return core_recall.recall(conn, query, mode=mode, bound=bound, offset=offset,
                              include_agent_notes=True, agent_id=agent["agent_id"],
                              config=config, clock=clock)


def agent_get_node(conn: sqlite3.Connection, agent_id: str,
                   node_id: str) -> Optional[Dict[str, Any]]:
    """Isolation-checked fetch (None on agent mismatch — fail closed)."""
    agent = require_agent(conn, agent_id)
    node = core_get_node(conn, node_id)
    if not node or node.get("agent_id") != agent["agent_id"]:
        return None
    return node


def agent_delete_node(conn: sqlite3.Connection, agent_id: str,
                      node_id: str) -> bool:
    """Isolation-checked delete (False on agent mismatch)."""
    from ..core.nodes import delete_node as core_delete
    agent = require_agent(conn, agent_id)
    node = core_get_node(conn, node_id)
    if not node or node.get("agent_id") != agent["agent_id"]:
        return False
    return core_delete(conn, node_id)


def agent_relate(conn: sqlite3.Connection, agent_id: str, from_id: str,
                 to_id: str, edge_type: str = "RELATES_TO",
                 weight: float = 1.0,
                 metadata: Optional[Dict] = None) -> str:
    """Isolation-checked relate; stamps edges.agent_id (denormalized)."""
    from ..core.edges import EDGE_TYPES, relate as core_relate
    agent = require_agent(conn, agent_id)
    for nid in (from_id, to_id):
        node = core_get_node(conn, nid)
        if not node or node.get("agent_id") != agent["agent_id"]:
            raise ValueError(f"agent_relate(): node {nid!r} not owned by {agent['agent_id']!r}")
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"Invalid edge_type: {edge_type}")
    return core_relate(conn, from_id, to_id, edge_type, weight, metadata,
                       agent_id=agent["agent_id"])


def agent_set_attention(conn: sqlite3.Connection, agent_id: str, node_id: str,
                         attention_state: str) -> bool:
    """Move a note between agent_private and review_ready (v2 parity).

    core_verified is promotable-only (bridge.promote) — rejected here.
    On transition to review_ready a REVIEW_READY mail to core is emitted
    (kind=review_ready) so the MCP inbox injection surfaces it distinctly.
    On demotion the corresponding pending reminder is acked.
    """
    if attention_state not in ("agent_private", "review_ready"):
        raise ValueError("attention_state must be 'agent_private' or 'review_ready'")
    agent = require_agent(conn, agent_id)
    node = core_get_node(conn, node_id)
    if (not node or node.get("agent_id") != agent["agent_id"]
            or node.get("node_type") != "AGENT_NOTE"):
        return False
    prev_state = (node.get("metadata") or {}).get("attention_state", "agent_private")
    meta = dict(node.get("metadata") or {})
    meta["attention_state"] = attention_state
    meta["attention_updated_at"] = _now()
    import json
    conn.execute("UPDATE nodes SET metadata = ?, updated_at = ? WHERE node_id = ?",
                 (json.dumps(meta), _now(), node_id))
    # ── mail side-effect (same transaction, no extra commit) ──
    try:
        if attention_state == "review_ready" and prev_state != "review_ready":
            # lazy import to avoid circular init
            from . import mailbox as _mailbox
            label = node.get("label") or node.get("content", "")[:30]
            date_s = __import__("datetime").datetime.fromtimestamp(
                node.get("updated_at") or _now()).strftime("%Y-%m-%d %H:%M")
            body = (f"REVIEW_READY: NODE {node_id} (\"{label}\") from "
                    f"agent:{agent['agent_id']} ready for review at {date_s} "
                    f"-- use review_queue / promote_to_core to graduate.")
            _mailbox.send(conn, f"agent:{agent['agent_id']}", "core", body,
                          metadata={"kind": "review_ready", "node_id": node_id,
                                   "agent_id": agent["agent_id"], "label": label})
        elif attention_state == "agent_private" and prev_state == "review_ready":
            # auto-ack stale reminder for this node (best-effort, no error)
            try:
                from . import mailbox as _mailbox2
                # find pending review mail for this node and ack for core
                rows = conn.execute(
                    """SELECT m.msg_id FROM mailbox m
                       JOIN mailbox_receipts r ON r.msg_id=m.msg_id
                       WHERE r.scope='core' AND r.acked_at IS NULL
                         AND json_extract(m.metadata,'$.kind')='review_ready'
                         AND json_extract(m.metadata,'$.node_id')=?""",
                    (node_id,)).fetchall()
                for r in rows:
                    _mailbox2.ack(conn, "core", [r["msg_id"]])
            except Exception:
                pass
    except Exception:
        # mail failure must not roll back the attention change
        pass
    return True


def agent_digest(conn: sqlite3.Connection, agent_id: str,
                 limit: int = 20) -> List[Dict[str, Any]]:
    """Recent notes for one agent (v2 agent_digest parity, SQL-scoped)."""
    agent = require_agent(conn, agent_id)
    rows = conn.execute("SELECT * FROM nodes WHERE agent_id = ?"
                        " ORDER BY updated_at DESC LIMIT ?",
                        (agent["agent_id"], limit)).fetchall()
    from ..core.nodes import row_to_dict
    return [row_to_dict(r) for r in rows]
