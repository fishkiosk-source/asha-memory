"""src.core.migrate — idempotent user_version chain for core.db.

DDL lives ONLY in schema.json (this module executes it, never inline SQL).
Chain (mirrors the v2 v1->v2 philosophy, fixes its gaps: v2 skipped
indexes/triggers/FTS/lexicon-seed on migrate and diverged on layer default):
  v1 — base tables (nodes/edges/fts/index/access/meta) + FTS triggers + P1-5 indexes
  v2 — node_vectors/memory_layers/query_log + backfill rows missing them
  v3 — ephemeral_events/vector_df/vector_meta + five new indexes + seeds
       (version '3.0', lexicon_version) + FTS/index/vector/layer backfill +
       P0-1 orphan purge
Fresh DBs run 1->2->3; v1/v2 DBs converge (v2 never set PRAGMA user_version,
so a v2 DB enters at 0 with tables present — all DDL is IF NOT EXISTS and
backfills are missing-only, except the one-time vector-format rebuild in v3).

health() calls check() (Phase 6 wires it). CLI: migrate.py [--check] [--memory DIR].
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .layers import init_layer
from .nodes import build_index
from .store import USER_VERSION_V3, connect, core_db_path
from .vectors import get_ndocs, rebuild_all

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.json"

BASE_TABLES = ["nodes", "edges", "node_fts", "node_index", "access_log", "schema_meta"]
BASE_INDEXES = ["idx_nodes_label_type", "idx_nodes_updated_access", "idx_nodes_source",
                "idx_edges_from", "idx_edges_to"]
V2_TABLES = ["node_vectors", "memory_layers", "query_log"]
V2_INDEXES = ["idx_memory_layers_layer"]  # table lands in v2, so does its index
V3_TABLES = ["ephemeral_events", "vector_df", "vector_meta"]

REQUIRED_TABLES = BASE_TABLES + V2_TABLES + V3_TABLES


def load_schema() -> Dict[str, Any]:
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _find(schema: Dict[str, Any], section: str, name: str) -> str:
    if section in ("triggers", "indexes"):
        for stmt in schema[section]:
            if re.search(r"\b" + re.escape(name) + r"\b", stmt):
                return stmt
        raise KeyError(f"{section} entry {name} not in schema.json")
    for key, stmt in schema[section].items():
        if key == name:
            return stmt
    raise KeyError(f"{section}.{name} not in schema.json")


def get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in
            conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def _apply_v1(conn: sqlite3.Connection, schema: Dict[str, Any]) -> None:
    for name in BASE_TABLES:
        conn.execute(_find(schema, "tables", name))
    for stmt in schema["triggers"]:
        conn.execute(stmt)
    for name in BASE_INDEXES:
        conn.execute(_find(schema, "indexes", name))


def _apply_v2(conn: sqlite3.Connection, schema: Dict[str, Any]) -> None:
    for name in V2_TABLES:
        conn.execute(_find(schema, "tables", name))
    for name in V2_INDEXES:
        conn.execute(_find(schema, "indexes", name))
    # Backfill rows predating v2 tables (missing-only, cheap on converged DBs)
    for row in conn.execute(
            "SELECT node_id FROM nodes WHERE node_id NOT IN"
            " (SELECT node_id FROM memory_layers)").fetchall():
        init_layer(conn, row["node_id"])


def _apply_v3(conn: sqlite3.Connection, schema: Dict[str, Any]) -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    for name in V3_TABLES:
        conn.execute(_find(schema, "tables", name))
    for name in ("idx_nodes_type", "idx_node_index_word", "idx_access_log_node_time",
                 "idx_node_vectors_mag", "idx_query_log_time_mode",
                 "idx_ephemeral_label_time"):
        conn.execute(_find(schema, "indexes", name))

    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '3.0')")
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lexicon_version', ?)",
        (str(schema["lexicon_version"]),))

    report["fts_backfilled"] = _backfill_fts(conn)
    report["index_backfilled"] = _backfill_index(conn)
    # One-time vector-format migration (v2 JSON dicts -> compact term:weight).
    # Runs only here (version step), never per-remember.
    if conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]:
        stats = rebuild_all(conn)
        report["vectors_rebuilt"] = stats
    else:
        report["vectors_rebuilt"] = {"nodes": 0, "terms": 0}
    for row in conn.execute(
            "SELECT node_id FROM nodes WHERE node_id NOT IN"
            " (SELECT node_id FROM memory_layers)").fetchall():
        init_layer(conn, row["node_id"])
    report["orphans_purged"] = _purge_orphans(conn)
    return report


def _backfill_fts(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT n.node_id, n.label, n.content FROM nodes n"
        " WHERE n.node_id NOT IN (SELECT node_id FROM node_fts)").fetchall()
    for r in rows:
        conn.execute("INSERT INTO node_fts (label, content, node_id) VALUES (?, ?, ?)",
                     (r["label"] or "", r["content"] or "", r["node_id"]))
    return len(rows)


def _backfill_index(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT n.node_id, n.label, n.content FROM nodes n"
        " LEFT JOIN node_index i ON i.node_id = n.node_id"
        " WHERE i.node_id IS NULL GROUP BY n.node_id").fetchall()
    for r in rows:
        build_index(conn, r["node_id"], r["label"] or "", r["content"] or "")
    return len(rows)


def _purge_orphans(conn: sqlite3.Connection) -> Dict[str, int]:
    """P0-1: pre-FK-era orphan edges/rows (v2 ran this inline; v3 owns it here)."""
    purged = {}
    stmts = {
        "edges": "DELETE FROM edges WHERE from_node NOT IN (SELECT node_id FROM nodes)"
                 " OR to_node NOT IN (SELECT node_id FROM nodes)",
        "node_vectors": "DELETE FROM node_vectors WHERE node_id NOT IN (SELECT node_id FROM nodes)",
        "memory_layers": "DELETE FROM memory_layers WHERE node_id NOT IN (SELECT node_id FROM nodes)",
        "access_log": "DELETE FROM access_log WHERE node_id NOT IN (SELECT node_id FROM nodes)",
        "node_index": "DELETE FROM node_index WHERE node_id NOT IN (SELECT node_id FROM nodes)",
    }
    for table, stmt in stmts.items():
        cur = conn.execute(stmt)
        purged[table] = cur.rowcount if cur.rowcount >= 0 else 0
    return purged


def _apply_v4(conn: sqlite3.Connection, schema: Dict[str, Any]) -> Dict[str, Any]:
    """C20: scope nodes_au to UPDATE OF label, content (drop + recreate all
    three triggers from schema.json so existing v3 DBs converge)."""
    for name in ("nodes_ai", "nodes_ad", "nodes_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for stmt in schema["triggers"]:
        conn.execute(stmt)
    return {"triggers_recreated": 3}


MIGRATIONS = [
    {"version": 1, "description": "base tables + FTS triggers + P1-5 indexes",
     "apply": _apply_v1},
    {"version": 2, "description": "vectors/layers/query_log + layer backfill",
     "apply": _apply_v2},
    {"version": 3, "description": "ephemeral/df-meta + new indexes + seeds + backfills + orphan purge",
     "apply": _apply_v3},
    {"version": 4, "description": "C20: UPDATE OF label,content trigger scope",
     "apply": _apply_v4},
]


def migrate(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Apply pending version steps in order. Returns {'from','to','ran','report'}."""
    schema = load_schema()
    start = get_user_version(conn)
    ran: List[int] = []
    report: Dict[str, Any] = {}
    for step in MIGRATIONS:
        if step["version"] > start:
            out = step["apply"](conn, schema)
            if isinstance(out, dict):
                report[f"v{step['version']}"] = out
            _set_user_version(conn, step["version"])
            ran.append(step["version"])
    return {"from": start, "to": get_user_version(conn), "ran": ran, "report": report}


def _index_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in
            conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _trigger_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in
            conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def _columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def check(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Schema completeness report for health(). Missing-only, read-only."""
    schema = load_schema()
    tables = _tables(conn)
    missing_tables = [t for t in REQUIRED_TABLES if t not in tables]
    want_indexes = [s.split("INDEX IF NOT EXISTS ")[1].split(" ON ")[0]
                    for s in schema["indexes"]]
    have_indexes = _index_names(conn)
    missing_indexes = [i for i in want_indexes if i not in have_indexes]
    missing_triggers = [t for t in ("nodes_ai", "nodes_ad", "nodes_au")
                        if t not in _trigger_names(conn)]
    orphans = {}
    if not missing_tables:
        orphans["edges"] = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE from_node NOT IN (SELECT node_id FROM nodes)"
            " OR to_node NOT IN (SELECT node_id FROM nodes)").fetchone()[0]
        orphans["layers_missing"] = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_id NOT IN"
            " (SELECT node_id FROM memory_layers)").fetchone()[0]
        orphans["fts_missing"] = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_id NOT IN"
            " (SELECT node_id FROM node_fts)").fetchone()[0]
    meta = {r["key"]: r["value"] for r in
            conn.execute("SELECT key, value FROM schema_meta")} if "schema_meta" in tables else {}
    version = get_user_version(conn)
    ok = (not missing_tables and not missing_indexes and not missing_triggers
          and all(v == 0 for v in orphans.values())
          and version == USER_VERSION_V3
          and meta.get("lexicon_version") == str(schema["lexicon_version"]))
    return {"ok": ok, "user_version": version, "target": USER_VERSION_V3,
            "missing_tables": missing_tables, "missing_indexes": missing_indexes,
            "missing_triggers": missing_triggers, "orphans": orphans,
            "schema_meta": meta, "ndocs": get_ndocs(conn) if "vector_meta" in tables else 0}


def main() -> None:
    ap = argparse.ArgumentParser(description="v3 core.db migration chain")
    ap.add_argument("--check", action="store_true", help="verify schema, no writes")
    ap.add_argument("--memory", default=None, help="memory/ dir override")
    args = ap.parse_args()
    conn = connect(core_db_path(args.memory))
    try:
        if args.check:
            print(check(conn))
        else:
            print(migrate(conn))
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
