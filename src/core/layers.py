"""src.core.layers — memory_layers state machine (READ side + init only).

Contract (Idea.md rev8 §9.2): core NEVER promotes/demotes on access (that was
v2 _update_layer_on_access write amplification on every recall). This module
reads layer state and initializes new nodes to 'working'. Promotion/demotion
runs on the brain tick (brain/engine.py, Phase 6). set_layer() exists for the
brain tick + bridge.promote — not for recall paths.
"""

import sqlite3
import time
from typing import Optional

LAYERS = ("working", "short_term", "long_term", "archive")
LAYER_ORDER = {"working": 1, "short_term": 2, "long_term": 3, "archive": 4}


def get_layer(conn: sqlite3.Connection, node_id: str) -> str:
    """Layer for a node; 'working' when no row exists (matches init default)."""
    row = conn.execute(
        "SELECT layer FROM memory_layers WHERE node_id = ?", (node_id,)
    ).fetchone()
    return row["layer"] if row else "working"


def init_layer(conn: sqlite3.Connection, node_id: str,
               now: Optional[int] = None) -> str:
    """New nodes start at 'working' (ports v2 _init_node_layer). Idempotent."""
    conn.execute(
        "INSERT OR IGNORE INTO memory_layers (node_id, layer, promoted_at, layer_order)"
        " VALUES (?, 'working', ?, 1)",
        (node_id, now if now is not None else int(time.time())),
    )
    return "working"


def set_layer(conn: sqlite3.Connection, node_id: str, layer: str,
              now: Optional[int] = None) -> str:
    """Brain-tick / bridge promotion setter. Rejects unknown layers."""
    if layer not in LAYER_ORDER:
        raise ValueError(f"Invalid layer: {layer}")
    conn.execute(
        "INSERT INTO memory_layers (node_id, layer, promoted_at, layer_order)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(node_id) DO UPDATE SET layer=excluded.layer,"
        " promoted_at=excluded.promoted_at, layer_order=excluded.layer_order",
        (node_id, layer, now if now is not None else int(time.time()),
         LAYER_ORDER[layer]),
    )
    return layer
