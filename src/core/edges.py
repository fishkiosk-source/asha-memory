"""src.core.edges — relate + batched neighbor fetch + edge delete.

Ports v2 relate:1740 (existence-checked, missing-node ValueError) with deltas:
- EDGE_TYPES = v2 set + PROMOTED_FROM (bridge provenance, §6.2; the CHECK in
  schema.json matches this set — the one intentional v2 deviation)
- weight clamped to -1..1 at the boundary (C16; v2 let CHECK raise)
- neighbors() is a SINGLE batched query both directions returning
  (neighbor_id, weight, edge_type, direction) — the primitive Phase 3 PATH
  (heapq, no 1-query-per-node) and CLUSTER (deque) build on
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .nodes import _edge_uuid, _now

EDGE_TYPES = {
    "RELATES_TO", "CONTRADICTS", "SUPPORTS", "CAUSED_BY",
    "PART_OF", "TRUSTS", "DISTRUSTS", "REMEMBERS", "HAS_PREFERENCE",
    "HAS_BOUNDARY", "HAS_AFFECT", "HAS_SKILL", "REFERS_TO", "SUMMARIZES",
    # v3 addition: bridge.promote provenance when the source had edges (§6.2)
    "PROMOTED_FROM",
}


def _clamp_weight(weight: float) -> float:
    return max(-1.0, min(1.0, float(weight)))


def relate(conn: sqlite3.Connection, from_id: str, to_id: str,
           edge_type: str = "RELATES_TO", weight: float = 1.0,
           metadata: Optional[Dict] = None,
           agent_id: Optional[str] = None) -> str:
    """Create edge (v2 parity + clamp). Returns edge_id.

    agent_id stamps edges.agent_id in ONE statement (agents.db NOT NULL);
    core.db has no such column — pass it only for agents.db.
    """
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"Invalid edge_type: {edge_type}")
    from_ok = conn.execute("SELECT 1 FROM nodes WHERE node_id = ?",
                           (from_id,)).fetchone()
    to_ok = conn.execute("SELECT 1 FROM nodes WHERE node_id = ?",
                         (to_id,)).fetchone()
    if not from_ok or not to_ok:
        missing = from_id if not from_ok else to_id
        raise ValueError(f"relate(): cannot create edge '{edge_type}'"
                         f" — node '{missing}' does not exist")
    edge_id = _edge_uuid()
    if agent_id is None:
        conn.execute(
            """INSERT OR REPLACE INTO edges
               (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (edge_id, from_id, to_id, edge_type, _clamp_weight(weight),
             _now(), json.dumps(metadata or {})))
    else:
        conn.execute(
            """INSERT OR REPLACE INTO edges
               (edge_id, agent_id, from_node, to_node, edge_type, weight, created_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (edge_id, agent_id, from_id, to_id, edge_type, _clamp_weight(weight),
             _now(), json.dumps(metadata or {})))
    return edge_id


def neighbors(conn: sqlite3.Connection, node_id: str,
              agent_id: Optional[str] = None
              ) -> List[Tuple[str, float, str, str]]:
    """One query, both directions: [(neighbor_id, weight, edge_type, direction)].

    agent_id filters edges.agent_id (column exists ONLY in agents.db — pass it
    solely for agents.db walks in PATH/CLUSTER; core callers leave it None).
    """
    pred, params = (" AND agent_id = ?", (agent_id,)) if agent_id is not None else ("", ())
    rows = conn.execute(
        """SELECT CASE WHEN from_node = ? THEN to_node ELSE from_node END AS nid,
                  weight, edge_type,
                  CASE WHEN from_node = ? THEN 'out' ELSE 'in' END AS direction
           FROM edges WHERE (from_node = ? OR to_node = ?)""" + pred,
        (node_id, node_id, node_id, node_id, *params)).fetchall()
    return [(r["nid"], float(r["weight"]), r["edge_type"], r["direction"])
            for r in rows]


def edges_between(conn: sqlite3.Connection, a: str, b: str) -> List[Dict[str, Any]]:
    """All edges touching both nodes (bridge.promote edge re-creation, Phase 4)."""
    rows = conn.execute(
        """SELECT * FROM edges WHERE (from_node = ? AND to_node = ?)
           OR (from_node = ? AND to_node = ?)""", (a, b, b, a)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except ValueError:
            d["metadata"] = {}
        out.append(d)
    return out


def delete_edge(conn: sqlite3.Connection, edge_id: str) -> bool:
    cur = conn.execute("DELETE FROM edges WHERE edge_id = ?", (edge_id,))
    return cur.rowcount > 0
