"""tools/migrate_v2_to_v3.py — one-shot v2 -> v3 splitter.

Reads <v2>/asha_memory/core.db READ-ONLY (never writes into v2) and builds:
  memory/core.db   — core knowledge (incl. core_verified ex-agent notes)
  memory/agents.db — single agents.db: agent notes sharded by agent_id,
                     telemetry moved to ephemeral_events, agents registry +
                     agent_aliases (v2 free-form IDs stay resolvable)
  memory/config.json — informational core/agents namespaces only (never read)
  brain/config.json  — the single live config (intervals/thresholds/token)

Row rules (Idea.md rev8 §9.3/C13, §13.4):
- agent row = (AGENT_NOTE and attention != core_verified) or
  (agent_scoped and attention != core_verified)  [v2 brain is_agent_note]
- core_verified rows ALWAYS stay core (human-verified wins over telemetry rule)
- telemetry row = label IN allowlist OR _looks_like_json_log(content)
  -> ephemeral_events in the row's own DB (count + sample in report)
- edges copy only when BOTH endpoints land in the same target DB
  (cross-scope edges dropped + counted — no phantom cross-DB links);
  agents.db edges stamped with the from-owner's agent_id (NOT NULL)
- ID collisions impossible (split preserves v2-unique IDs per target)
- per-agent caps BYPASSED (preserve data; over-cap agents reported, health flags)
- vectors rebuilt once per DB after load (v2 JSON format not carried over);
  FTS/node_index rebuilt via triggers + explicit build_index

Usage:
  python tools/migrate_v2_to_v3.py <v2_asha_memory_dir> --v3-memory <dir>
      [--v3-brain <dir>] [--v2-brain-config <path>] [--dry-run] [--force]
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents import store as agent_store
from src.core import nodes as core_nodes
from src.core import vectors as core_vectors
from src.core.lexicon import _looks_like_json_log
from src.core.migrate import migrate as core_migrate
from src.core.store import connect as core_connect

VERIFIED = "core_verified"


def _meta(row) -> Dict[str, Any]:
    try:
        return json.loads(row["metadata"] or "{}")
    except (ValueError, TypeError, KeyError):
        return {}


def _is_agent_row(node_type: str, meta: Dict[str, Any]) -> bool:
    att = meta.get("attention_state")
    return ((node_type == "AGENT_NOTE" and att != VERIFIED)
            or (bool(meta.get("agent_scoped")) and att != VERIFIED))


def _is_telemetry(label: str, content: str, allowlist) -> bool:
    return label in allowlist or _looks_like_json_log(content or "")


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def migrate(v2_data_dir: str, v3_memory_dir: Optional[str] = None,
            v3_brain_dir: Optional[str] = None,
            v2_brain_config: Optional[str] = None,
            dry_run: bool = False, force: bool = False) -> Dict[str, Any]:
    v2dir = Path(v2_data_dir)
    v2db = v2dir / "core.db"
    if not v2db.exists():
        return {"status": "error", "message": f"v2 core.db not found: {v2db}"}
    mem = Path(v3_memory_dir) if v3_memory_dir else Path("memory")
    brain_dir = Path(v3_brain_dir) if v3_brain_dir else Path("brain")

    report: Dict[str, Any] = {"status": "success", "dry_run": dry_run,
                              "v2_db": str(v2db), "at": int(time.time())}

    src = sqlite3.connect(f"file:{v2db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return {"status": "error", "message": "v2 integrity_check failed — aborting"}
        v2_cfg = _read_json(v2dir / "config.json")
        bpath = Path(v2_brain_config) if v2_brain_config else v2dir.parent / "brain" / "brain_config.json"
        v2_brain = _read_json(bpath)
        allowlist = set(v2_cfg.get("ephemeral_labels")
                        or v2_brain.get("ephemeral_labels", []))

        nodes = src.execute("SELECT * FROM nodes").fetchall()
        edges = src.execute("SELECT * FROM edges").fetchall()
        layers = {r["node_id"]: dict(r) for r in
                  src.execute("SELECT * FROM memory_layers")}
        access = src.execute("SELECT node_id, accessed_at FROM access_log").fetchall()
        queries = src.execute("SELECT query_text, mode, result_count, duration_ms,"
                              " cache_hit, queried_at FROM query_log"
                              " ORDER BY log_id DESC LIMIT 5000").fetchall()
        report["v2"] = {"nodes": len(nodes), "edges": len(edges),
                        "access_log": len(access),
                        "allowlist": sorted(allowlist)}

        # ── classify ──
        core_rows, agent_rows = [], []
        eph_core, eph_agents = [], []
        verified_telemetry = []
        agent_ids: Dict[str, List] = {}
        for r in nodes:
            meta = _meta(r)
            att = meta.get("attention_state")
            if att == VERIFIED:
                if _is_telemetry(r["label"] or "", r["content"] or "", allowlist):
                    verified_telemetry.append(r["node_id"])
                core_rows.append(r)
            elif _is_telemetry(r["label"] or "", r["content"] or "", allowlist):
                (eph_agents if _is_agent_row(r["node_type"], meta) else eph_core).append(r)
            elif _is_agent_row(r["node_type"], meta):
                agent_rows.append(r)
                aid = str(meta.get("agent_id") or "legacy-unknown")
                agent_ids.setdefault(aid, []).append(r["node_id"])
            else:
                core_rows.append(r)
        report["moved"] = {"core_nodes": len(core_rows), "agents_notes": len(agent_rows),
                           "ephemeral_core": len(eph_core), "ephemeral_agents": len(eph_agents),
                           "verified_telemetry_kept_core": verified_telemetry,
                           "ephemeral_sample": [r["label"] for r in (eph_core + eph_agents)[:10]]}
        total = len(core_rows) + len(agent_rows) + len(eph_core) + len(eph_agents)
        report["reconcile_nodes"] = {"v2": len(nodes), "accounted": total,
                                     "ok": total == len(nodes)}

        # ── edge plan ──
        core_ids = {r["node_id"] for r in core_rows}
        agent_ids_set = {r["node_id"] for r in agent_rows}
        eph_ids = {r["node_id"] for r in eph_core + eph_agents}
        copy_core, copy_agents, dropped = [], [], []
        owner = {}
        for r in agent_rows:
            owner[r["node_id"]] = str(_meta(r).get("agent_id") or "legacy-unknown")
        for e in edges:
            a, b = e["from_node"], e["to_node"]
            if a in eph_ids or b in eph_ids:
                dropped.append({"edge_id": e["edge_id"], "reason": "ephemeral-endpoint",
                                "type": e["edge_type"]})
            elif a in core_ids and b in core_ids:
                copy_core.append(e)
            elif a in agent_ids_set and b in agent_ids_set:
                copy_agents.append(e)
            elif a in core_ids or b in core_ids or a in agent_ids_set or b in agent_ids_set:
                dropped.append({"edge_id": e["edge_id"], "reason": "cross-scope",
                                "type": e["edge_type"]})
            else:
                dropped.append({"edge_id": e["edge_id"], "reason": "missing-endpoint",
                                "type": e["edge_type"]})
        by_reason: Dict[str, int] = {}
        for d in dropped:
            by_reason[d["reason"]] = by_reason.get(d["reason"], 0) + 1
        report["edges"] = {"v2": len(edges), "copied_core": len(copy_core),
                           "copied_agents": len(copy_agents),
                           "dropped": len(dropped), "dropped_by_reason": by_reason,
                           "dropped_sample": dropped[:10],
                           "reconcile_ok": len(copy_core) + len(copy_agents)
                           + len(dropped) == len(edges)}

        # ── agent registry plan (sorted for determinism) ──
        registry, merges = [], []
        seen_slugs: Dict[str, str] = {}
        for original in sorted(agent_ids):
            slug = agent_store._slugify(original)
            if slug in seen_slugs:
                merges.append({"from": original, "into_agent": seen_slugs[slug],
                               "reason": "slug-collision (same worker, inconsistent naming)"})
                registry.append({"original": original, "agent_id": seen_slugs[slug],
                                 "merged": True})
            else:
                registry.append({"original": original, "slug": slug, "merged": False})
                seen_slugs[slug] = original  # placeholder; resolved to canonical below
        report["agents"] = {"distinct_v2_ids": len(agent_ids),
                            "per_agent_counts": {k: len(v) for k, v in agent_ids.items()},
                            "over_cap_100": [k for k, v in agent_ids.items() if len(v) > 100],
                            "slug_merges": merges}
        if dry_run:
            report["registry_plan"] = registry
            report["config"] = _plan_config(v2_cfg, v2_brain)
            return report

        # ── build targets ──
        mem.mkdir(parents=True, exist_ok=True)
        if force:
            # rebuild-from-scratch: drop existing targets (v2 source untouched)
            for stale in ("core.db", "core.db-wal", "core.db-shm",
                          "agents.db", "agents.db-wal", "agents.db-shm"):
                try:
                    (mem / stale).unlink()
                except OSError:
                    pass
        core = core_connect(str(mem / "core.db"))
        core_migrate(core)
        agents = agent_store.connect(str(mem / "agents.db"))
        agent_store.ensure_schema(agents)
        for label, conn, table in (("core", core, "nodes"), ("agents", agents, "nodes")):
            if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] and not force:
                core.close()
                agents.close()
                return {"status": "error",
                        "message": f"target {label}.db not empty (use --force)"}

        # agents registry first (need canonical IDs for node rows).
        # Sorted order guarantees a slug-collision keeper (alphabetically first)
        # is created before its mergers resolve to it.
        canon: Dict[str, str] = {}
        keeper_of_slug: Dict[str, str] = {}
        for entry in registry:
            original = entry["original"]
            if entry.get("merged"):
                keeper = keeper_of_slug[agent_store._slugify(original)]
                canon[original] = canon[keeper]
                agent_store.add_alias(agents, original, canon[original])
                continue
            res = agent_store.agent_ensure(
                agents, original,
                preferred_id=original if agent_store.PREFERRED_ID_RE.match(original) else None)
            canon[original] = res["agent_id"]
            keeper_of_slug[entry["slug"]] = original
            if res["agent_id"] != original:
                agent_store.add_alias(agents, original, res["agent_id"])
        if "legacy-unknown" in agent_ids and "legacy-unknown" not in canon:
            canon["legacy-unknown"] = agent_store.agent_ensure(
                agents, "Legacy unknown notes")["agent_id"]
        report["agents"]["canonical"] = canon
        report["agents"]["created"] = len({v for v in canon.values()})

        # nodes (bulk) — agent rows carry their canonical agent_id
        core.executemany(
            "INSERT INTO nodes (node_id, node_type, label, content, source, trust_level,"
            " created_at, updated_at, access_count, importance, checksum, metadata)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["node_id"], r["node_type"], r["label"], r["content"], r["source"],
              r["trust_level"], r["created_at"], r["updated_at"], r["access_count"],
              r["importance"], r["checksum"], r["metadata"]) for r in core_rows])
        agents.executemany(
            "INSERT INTO nodes (node_id, agent_id, node_type, label, content, source,"
            " trust_level, created_at, updated_at, access_count, importance, checksum, metadata)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["node_id"], canon[str(_meta(r).get("agent_id") or "legacy-unknown")],
              r["node_type"], r["label"], r["content"], r["source"], r["trust_level"],
              r["created_at"], r["updated_at"], r["access_count"], r["importance"],
              r["checksum"], r["metadata"]) for r in agent_rows])
        # layers as stored (v2's real state, not re-init)
        for r in core_rows:
            if r["node_id"] in layers:
                L = layers[r["node_id"]]
                core.execute("INSERT OR REPLACE INTO memory_layers (node_id, layer,"
                             " promoted_at, layer_order) VALUES (?,?,?,?)",
                             (r["node_id"], L["layer"], L["promoted_at"], L["layer_order"]))
        for r in agent_rows:
            if r["node_id"] in layers:
                L = layers[r["node_id"]]
                agents.execute("INSERT OR REPLACE INTO memory_layers (node_id, layer,"
                               " promoted_at, layer_order) VALUES (?,?,?,?)",
                               (r["node_id"], L["layer"], L["promoted_at"], L["layer_order"]))
        # edges (same-target only; agents stamped with from-owner)
        core.executemany(
            "INSERT INTO edges (edge_id, from_node, to_node, edge_type, weight,"
            " created_at, metadata) VALUES (?,?,?,?,?,?,?)",
            [(e["edge_id"], e["from_node"], e["to_node"], e["edge_type"], e["weight"],
              e["created_at"], e["metadata"]) for e in copy_core])
        agents.executemany(
            "INSERT INTO edges (edge_id, agent_id, from_node, to_node, edge_type, weight,"
            " created_at, metadata) VALUES (?,?,?,?,?,?,?,?)",
            [(e["edge_id"], canon[str(_meta(
                next(r for r in agent_rows if r["node_id"] == e["from_node"])).get("agent_id")
                or "legacy-unknown")], e["from_node"], e["to_node"], e["edge_type"],
              e["weight"], e["created_at"], e["metadata"]) for e in copy_agents])
        # logs (query_log already capped at 5000 by SELECT)
        core.executemany("INSERT INTO access_log (node_id, accessed_at) VALUES (?,?)",
                         [(r["node_id"], r["accessed_at"]) for r in access
                          if r["node_id"] in core_ids])
        agents.executemany("INSERT INTO access_log (node_id, accessed_at) VALUES (?,?)",
                           [(r["node_id"], r["accessed_at"]) for r in access
                            if r["node_id"] in agent_ids_set])
        core.executemany("INSERT INTO query_log (query_text, mode, result_count,"
                         " duration_ms, cache_hit, queried_at) VALUES (?,?,?,?,?,?)",
                         [tuple(r) for r in queries])
        # telemetry -> ephemeral_events in the row's own DB
        for r in eph_core:
            core.execute("INSERT INTO ephemeral_events (label, body, created_at, metadata)"
                         " VALUES (?,?,?,?)",
                         (r["label"], r["content"] or "", r["created_at"], r["metadata"]))
        for r in eph_agents:
            agents.execute("INSERT INTO ephemeral_events (label, body, created_at, metadata)"
                           " VALUES (?,?,?,?)",
                           (r["label"], r["content"] or "", r["created_at"], r["metadata"]))
        # node_index for moved rows (triggers covered FTS on INSERT)
        for r in core_rows:
            core_nodes.build_index(core, r["node_id"], r["label"] or "", r["content"] or "")
        for r in agent_rows:
            core_nodes.build_index(agents, r["node_id"], r["label"] or "", r["content"] or "")
        core.commit()
        agents.commit()

        # vectors rebuilt once (v2 JSON format not carried over)
        report["vectors"] = {
            "core": core_vectors.rebuild_all(core),
            "agents": core_vectors.rebuild_all(agents)}
        core.commit()
        agents.commit()

        # per-DB health verify
        from brain.engine import BrainEngine
        eng = BrainEngine(core_path=str(mem / "core.db"),
                          agents_path=str(mem / "agents.db"),
                          brain_dir=str(brain_dir))
        health = eng.health("both")
        report["health"] = {"core_ok": health["core"]["check"].get("ok"),
                            "agents_ok": health["agents"]["check"].get("ok"),
                            "combined": health.get("combined")}
        report["health"]["ok"] = bool(report["health"]["core_ok"]
                                      and report["health"]["agents_ok"])
        core.close()
        agents.close()

        # configs
        report["config"] = _write_config(v2_cfg, v2_brain, mem, brain_dir)
        return report
    finally:
        src.close()


def _plan_config(v2_cfg: Dict[str, Any], v2_brain: Dict[str, Any]) -> Dict[str, Any]:
    """Diff v2 keys against v3 DEFAULTS (every default change listed)."""
    from brain.engine import DEFAULTS
    changes = []
    for key, val in v2_brain.items():
        if key in ("last_db_path",):
            changes.append({"key": key, "v2": val, "v3": "(dropped: relative paths now)"})
        elif key in DEFAULTS and DEFAULTS[key] != val:
            changes.append({"key": key, "v2": val, "v3_default": DEFAULTS[key],
                            "migrated": True})
    core_keys = set(v2_cfg) - {"schema_version"}
    changes.append({"core_config_keys_carried": sorted(core_keys)})
    if v2_brain.get("dashboard_token"):
        changes.append({"token_warning": "dashboard_token preserved; restrict file perms"
                        " (chmod 600 on POSIX; Windows: limit ACLs)"})
    return {"changes": changes}


def _write_config(v2_cfg: Dict[str, Any], v2_brain: Dict[str, Any],
                  mem: Path, brain_dir: Path) -> Dict[str, Any]:
    plan = _plan_config(v2_cfg, v2_brain)
    agent_keys = ("agent_max_notes", "agent_max_content_length")
    # memory/config.json keeps core/agents namespaces as informational data only.
    # The brain owns ALL live config in brain/config.json (single config —
    # operator directive 2026-09-04 night, supersedes C17: no shared section).
    mem_cfg = {
        "core": {k: v for k, v in v2_cfg.items() if k not in agent_keys},
        "agents": {k: v2_cfg[k] for k in agent_keys if k in v2_cfg},
        "_note": ("informational only — live config is brain/config.json; "
                  "this file is never read at runtime"),
        "_migrated_from": "asha_memory v2",
        "_migrated_at": int(time.time()),
    }
    (mem / "config.json").write_text(json.dumps(mem_cfg, indent=2), encoding="utf-8")

    from brain.engine import DEFAULTS
    brain_dir.mkdir(parents=True, exist_ok=True)
    bpath = brain_dir / "config.json"
    current = _read_json(bpath)
    merged = {**DEFAULTS, **current}
    for key, val in v2_brain.items():
        if key == "last_db_path" or key not in DEFAULTS:
            continue
        if key == "ephemeral_labels":
            merged[key] = sorted(set(val))  # brain-owned; no memory mirror
        else:
            merged[key] = val
    token_warn = None
    if v2_brain.get("dashboard_token"):
        merged["dashboard_token"] = v2_brain["dashboard_token"]
        token_warn = ("dashboard_token preserved from v2; restrict perms "
                      "(POSIX: chmod 600 applied where possible)")
        try:
            import os as _os
            _os.chmod(bpath, 0o600)
        except (OSError, AttributeError):
            token_warn += " [chmod unavailable on this platform]"
    bpath.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    try:
        import os as _os
        if not v2_brain.get("dashboard_token"):
            _os.chmod(bpath, 0o600)
    except (OSError, AttributeError):
        pass
    plan["written"] = {"memory_config": str(mem / "config.json"),
                       "brain_config": str(bpath)}
    if token_warn:
        plan["token_warning"] = token_warn
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="one-shot v2 -> v3 splitter (v2 read-only)")
    ap.add_argument("v2_data_dir", help="v2 asha_memory/ data dir (read-only)")
    ap.add_argument("--v3-memory", default=None)
    ap.add_argument("--v3-brain", default=None)
    ap.add_argument("--v2-brain-config", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="migrate into non-empty v3 DBs (default: refuse)")
    args = ap.parse_args(argv)
    report = migrate(args.v2_data_dir, args.v3_memory, args.v3_brain,
                     args.v2_brain_config, args.dry_run, args.force)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
