"""src.agents.bridge — core <-> agents read/promote helpers.

ONLY module that opens BOTH memory/core.db and memory/agents.db in the same
operation. Callers pass both connections (transaction control stays explicit:
core commits first, then agents — at-least-once + idempotent converge, C7).

- list_review_queue(agents_conn, limit): SQL-side WHERE + ORDER + LIMIT (C18).
- search_all_agents(agents_conn, query, ...): TF-IDF SEMANTIC over agents.db
  via core recall (core never searches agents by accident; explicit or via
  include_agent_notes in Phase 5 MCP).
- get_agent_note(agents_conn, agent_id, node_id): isolation-checked fetch.
- promote(core_conn, agents_conn, agent_id, node_id, ...): MOVE agents.db ->
  core.db in 6 steps (Idea.md §6.2/C7). Only core/dashboard-as-core calls it.
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional

from ..core import recall as core_recall
from ..core import vectors as core_vectors
from ..core.edges import EDGE_TYPES, relate as core_relate
from ..core.layers import init_layer
from ..core.nodes import _now, _uuid, get_node as core_get_node, row_to_dict

PROMOTABLE_TYPES = {t for t in (
    "PERSON", "TOPIC", "EVENT", "FACT", "PREFERENCE",
    "BOUNDARY", "AFFECT", "CORE_REF", "SKILL")}


def list_review_queue(agents_conn: sqlite3.Connection,
                      limit: int = 50) -> List[Dict[str, Any]]:
    """Agent notes awaiting core review — filtered in SQL (C18 fix of v2's
    full-scan-then-Python-filter that ignored LIMIT)."""
    rows = agents_conn.execute(
        """SELECT * FROM nodes
           WHERE node_type = 'AGENT_NOTE'
             AND json_extract(metadata, '$.attention_state') = 'review_ready'
           ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()
    return [row_to_dict(r) for r in rows]


def review_stats(agents_conn: sqlite3.Connection) -> Dict[str, int]:
    """Counts for the dashboard Graduate tab (total/review-ready/graduable/private).

    Graduable == review_ready only (importance ignored per operator request 2026-09-07).
    Trust/importance remain for telemetry ranking but not for graduation gate.
    """
    total = agents_conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE node_type = 'AGENT_NOTE'").fetchone()[0]
    ready = agents_conn.execute(
        """SELECT COUNT(*) FROM nodes WHERE node_type = 'AGENT_NOTE'
           AND json_extract(metadata, '$.attention_state') = 'review_ready'""").fetchone()[0]
    graduable = ready  # ignore trust/importance, only explicit review_ready
    return {"total": total, "review_ready": ready,
            "graduable": graduable, "private": total - ready}


def search_all_agents(agents_conn: sqlite3.Connection, query: str,
                      min_confidence: float = 0.15,
                      bound: int = 20) -> List[Dict[str, Any]]:
    """Cross-agent TF-IDF search (v2 find_across_agents parity, agents.db edition).

    Core-verified notes are skipped (they already live in core recall);
    min_confidence filters _similarity; results carry _agent_id, sorted, cut.
    """
    res = core_recall.recall(agents_conn, query, mode="SEMANTIC",
                             bound=max(bound * 5, 50),
                             include_agent_notes=True,
                             config={"semantic_relevance_floor": 0.0})
    out = []
    for node in res["nodes"]:
        meta = node.get("metadata") or {}
        if meta.get("attention_state") == "core_verified":
            continue
        sim = meta.get("_similarity", 0.0)
        if sim >= min_confidence:
            node["_agent_id"] = node.get("agent_id")
            node["_similarity"] = sim
            out.append(node)
    out.sort(key=lambda x: -x.get("_similarity", 0.0))
    return out[:bound]


def get_agent_note(agents_conn: sqlite3.Connection, agent_id: str,
                   node_id: str) -> Optional[Dict[str, Any]]:
    """Single-note fetch, isolation-checked (None on agent mismatch)."""
    node = core_get_node(agents_conn, node_id)
    if not node or node.get("agent_id") != agent_id:
        return None
    return node


def _core_id_for(agents_node_id: str, core_conn: sqlite3.Connection) -> str:
    """Reuse the agents node_id when free in core.db, else mint node_<hex>."""
    if not core_conn.execute("SELECT 1 FROM nodes WHERE node_id = ?",
                             (agents_node_id,)).fetchone():
        return agents_node_id
    return _uuid()


def promote(core_conn: sqlite3.Connection, agents_conn: sqlite3.Connection,
            agent_id: str, node_id: str, new_type: str = "FACT",
            new_label: Optional[str] = None) -> str:
    """Move a note agents.db -> core.db (6 steps, Idea.md §6.2/C7).

    1. Verify source exists + belongs to agent_id (fail closed).
    2. Idempotency: core row with this promoted_from provenance? -> skip to 4.
    3. INSERT into core (reused or minted ID; attention core_verified;
       promoted_from/provenance metadata; trust = max(orig, 0.8); layers row;
       incremental vector) + re-create edges whose BOTH endpoints now live in
       core (chains of promotions stay linked; cross-DB edges are dropped —
       node stays clean, provenance is promoted_from only).
    4. DELETE source (+ cascade; df decremented first). No tombstone (locked).
    5/6. Commit order: core first, then agents (at-least-once; retry converges
       via step 2). Returns core_node_id. Only core/dashboard-as-core calls this.
    """
    if new_type not in PROMOTABLE_TYPES:
        raise ValueError(f"Invalid promoted node_type: {new_type}")
    src = agents_conn.execute("SELECT * FROM nodes WHERE node_id = ?",
                              (node_id,)).fetchone()
    if not src:
        raise ValueError(f"promote(): node {node_id!r} does not exist")
    if src["agent_id"] != agent_id:
        raise ValueError(f"promote(): node {node_id!r} not owned by {agent_id!r}")
    src_meta = json.loads(src["metadata"] or "{}")

    # Step 2: idempotent converge — already promoted?
    hit = core_conn.execute(
        """SELECT node_id FROM nodes
           WHERE json_extract(metadata, '$.promoted_from.node_id') = ?
             AND json_extract(metadata, '$.promoted_from.agent_id') = ?""",
        (node_id, agent_id)).fetchone()
    if hit:
        _delete_source(agents_conn, node_id, src["label"] or "", src["content"] or "")
        agents_conn.commit()
        return hit["node_id"]

    # Step 3: insert into core
    core_id = _core_id_for(node_id, core_conn)
    now = _now()
    meta = {
        "attention_state": "core_verified",
        "promoted_from": {"agent_id": agent_id, "node_id": node_id},
        "promoted_at": now,
        "promoted_from_agent": agent_id,
        "original_agents_node_id": node_id,
        "original_node_type": src["node_type"],
        "original_trust": src["trust_level"],
    }
    trust = max(float(src["trust_level"]), 0.8)
    core_conn.execute(
        """INSERT INTO nodes (node_id, node_type, label, content, source, trust_level,
                              created_at, updated_at, access_count, importance,
                              checksum, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (core_id, new_type, new_label or src["label"], src["content"],
         f"AGENT_{agent_id}", trust, src["created_at"], now, 0,
         src["importance"], src["checksum"], json.dumps(meta)))
    from ..core.nodes import build_index
    build_index(core_conn, core_id, new_label or src["label"], src["content"] or "")
    init_layer(core_conn, core_id, now)
    core_vectors.update_on_insert(core_conn, core_id,
                                  new_label or src["label"], src["content"] or "")

    # Re-create edges whose other endpoint already lives in core.db
    # (cross-DB edges are dropped — node stays clean with only provenance
    #  promoted_from / promoted_from_agent; no summarized noise)
    incident = agents_conn.execute(
        "SELECT * FROM edges WHERE from_node = ? OR to_node = ?",
        (node_id, node_id)).fetchall()
    for e in incident:
        other = e["to_node"] if e["from_node"] == node_id else e["from_node"]
        if core_conn.execute("SELECT 1 FROM nodes WHERE node_id = ?",
                             (other,)).fetchone():
            etype = e["edge_type"] if e["edge_type"] in EDGE_TYPES else "RELATES_TO"
            direction_out = e["from_node"] == node_id
            try:
                core_relate(core_conn,
                            core_id if direction_out else other,
                            other if direction_out else core_id,
                            etype, e["weight"], {"promoted_with": core_id})
            except (ValueError, sqlite3.IntegrityError):
                pass
    core_conn.commit()

    # Step 4: clean-remove the source (df first, then cascade delete)
    _delete_source(agents_conn, node_id, src["label"] or "", src["content"] or "")
    # ack any pending REVIEW_READY reminder for this node (best-effort)
    try:
        rows = agents_conn.execute(
            """SELECT m.msg_id FROM mailbox m
               JOIN mailbox_receipts r ON r.msg_id=m.msg_id
               WHERE r.scope='core' AND r.acked_at IS NULL
                 AND json_extract(m.metadata,'$.kind')='review_ready'
                 AND json_extract(m.metadata,'$.node_id')=?""",
            (node_id,)).fetchall()
        now = _now()
        for r in rows:
            agents_conn.execute(
                "UPDATE mailbox_receipts SET acked_at=COALESCE(acked_at,?), read_at=COALESCE(read_at,?) WHERE msg_id=? AND scope='core'",
                (now, now, r["msg_id"]))
            agents_conn.execute("UPDATE mailbox SET acked_at=COALESCE(acked_at,?) WHERE msg_id=?", (now, r["msg_id"]))
        # if we held an aggregated mail (count>1), it is now stale — let _sync fix it next inbox check;
        # proactively remove stale aggregated so next sync recreates with correct count
        agg = agents_conn.execute(
            """SELECT m.msg_id, m.metadata FROM mailbox m
               JOIN mailbox_receipts r ON r.msg_id=m.msg_id
               WHERE r.scope='core' AND r.acked_at IS NULL
                 AND json_extract(m.metadata,'$.kind')='review_ready'
                 AND json_extract(m.metadata,'$.count') IS NOT NULL""").fetchall()
        if agg:
            # mark stale — next _sync will recreate, but we ack now to avoid ghost
            for r in agg:
                try:
                    meta = json.loads(r["metadata"] or "{}")
                    if node_id in (meta.get("node_ids") or []):
                        agents_conn.execute("UPDATE mailbox_receipts SET acked_at=?, read_at=? WHERE msg_id=? AND scope='core'", (now, now, r["msg_id"]))
                        agents_conn.execute("UPDATE mailbox SET acked_at=? WHERE msg_id=?", (now, r["msg_id"]))
                except Exception:
                    pass
    except Exception:
        pass
    agents_conn.commit()
    return core_id


def _delete_source(agents_conn: sqlite3.Connection, node_id: str,
                   label: str, content: str) -> None:
    core_vectors.update_on_delete(agents_conn, node_id, label, content)
    agents_conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))


def graduate_many(core_conn: sqlite3.Connection, agents_conn: sqlite3.Connection,
                  agent_id: str, node_ids: List[str],
                  new_type: str = "FACT") -> Dict[str, str]:
    """Bulk Graduate-Selected (dashboard): per-note promote, {agents_id: core_id}."""
    return {nid: promote(core_conn, agents_conn, agent_id, nid, new_type)
            for nid in node_ids}
