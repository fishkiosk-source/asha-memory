"""brain.engine — dual-DB maintenance engine (BrainEngine(core_path, agents_path)).

Rework of v2 brain/brain_engine.py. NEVER imported by src/core.
Every mutating job runs per-DB and returns {"core": {...}, "agents": {...}}
(single-scope jobs report {"status": "skipped", ...} on the other side).
Conventions (Idea.md rev8):
- No cross-DB merges/links: dedup/discover scope = whole core.db / per-agent_id.
- detect_contradictions core + per-agent (never cross-agent); regulate_agent_working_memory agents-only;
  graduate_agent_notes manual-only via bridge (never in DEFAULT runs).
- Compact operates on the ephemeral_events TABLE (C13, TTL always applies);
  the old nodes-as-telemetry path is gone (migration moves it, Phase 8).
- Snapshots: manual always; auto only when the toggle is ON and coalesced
  (<=1 per DB per run/window, snapshot_cooldown_s, serialized under the
  inter-process memory/.lock). Pre-rollback backups use the backup API,
  never copy2 on a live WAL DB. commit_manager_db is DELETED (C4) — row
  endpoints replace it in Phase 7.
- Connections come from src stores (PRAGMA invariant). Sanctioned raw-connect
  exceptions: snapshot backup staging + VACUUM isolated conn (commented).
- Silent truncates banned: capped scans report truncated:true + counts.
- Bare-except-close banned: every path commits or rolls back explicitly.
"""

import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.agents import bridge as agent_bridge
from src.agents import store as agent_store
from src.core import edges as core_edges
from src.core import nodes as core_nodes
from src.core import vectors as core_vectors
from src.core.layers import LAYER_ORDER
from src.core.lexicon import (
    DEFAULT_EPHEMERAL_LABELS,
    NEGATIVE_WORDS,
    POSITIVE_WORDS,
    STOPWORDS,
    _extract_keywords,
    _looks_like_json_log,
    _tokenize,
)
from src.core.migrate import check as core_check
from src.core.store import connect as core_connect

DEFAULTS = {
    "cron_enabled": False,
    "interval_minutes": 60,
    "auto_snapshot_before_jobs": False,
    "snapshot_cooldown_s": 300,
    "dedup_similarity_threshold": 0.85,
    "short_term_promote_after": 3,
    "dedup_scan_cap": 2000,
    "discover_scan_cap": 2000,
    "discover_link_floor": 0.50,
    "discover_link_ceil": 0.85,
    "prune_importance_floor": 0.05,
    "prune_threshold": 0.05,
    "auto_rebuild_vectors": True,
    "max_unused_days": 4,
    "ephemeral_labels": sorted(DEFAULT_EPHEMERAL_LABELS),
    "ephemeral_ignored": [],
    "ephemeral_keep_last": 3,
    "ephemeral_max_age_days": 7,
    "ephemeral_max_age_hours": 168,
    "vacuum_after_prune": True,
    "vacuum_freelist_threshold_pct": 15,
    "vacuum_freelist_min_pages": 50,
    "contradiction_auto_resolve": False,
    "contradiction_low_trust": 0.3,
    "contradiction_high_trust": 0.8,
    "contradiction_scan_cap": 2000,
    "contradiction_min_overlap_ratio": 0.25,
    "keep_last_snapshots": 10,
    "keep_last_logs": 30,
    "sqlite_cache_size": -64000,
    "agent_working_regulator_enabled": True,
    "agent_working_high_water": 12,
    "agent_working_demote_batch": 5,
    "agent_working_max_age_hours": 48,
    "agent_working_weight_access": 1.5,
    "agent_working_weight_importance": 4.0,
    "agent_working_weight_age": 0.15,
    "core_helper_enabled": False,
    "core_helper_min_age_hours": 72,
    "core_helper_imp_threshold": 0.6,
    "core_helper_trust_low": 0.5,
    "core_helper_trust_high": 0.8,
    "core_helper_access_threshold": 4,
    "prune_empty_agents": False,
    "prune_empty_agents_min_age_hours": 24,
    "dashboard_token": "",
}

MEMORY_DECAY = {"working": 1.0, "short_term": 0.97,
                "long_term": 0.995, "archive": 1.0}
PROTECTED_TYPES = ("PERSON", "SKILL", "BOUNDARY", "FACT", "CORE_REF")
ATTENTION_PRIORITY = {"core_verified": 3, "review_ready": 2, "agent_private": 1}

CORE_ONLY_JOBS = set()  # historical: contradictions went per-DB+per-agent
AGENTS_ONLY_JOBS = {"agent_working"}  # (operator call 2026-09-05; scheduler mirrors it)


def _now() -> int:
    return int(time.time())


def _edge_uuid() -> str:
    import uuid
    return "edge_" + uuid.uuid4().hex[:16]


class BrainEngine:
    """Dual-DB maintenance backend (caller owns connection lifecycle per job)."""

    def __init__(self, core_path=None, agents_path=None, brain_dir=None,
                 config: Optional[Dict[str, Any]] = None):
        self.brain_dir = Path(brain_dir) if brain_dir else Path(__file__).resolve().parent
        self.snapshots_dir = self.brain_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.brain_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.brain_dir / "job_history.json"
        self.config_path = self.brain_dir / "config.json"
        # Single config: brain/config.json is the ONLY config file.
        # (Operator directive 2026-09-04 night, supersedes C17: no overlay from
        # memory/config.json, no write-through, no shared section.)
        self.core_path = Path(core_path) if core_path else self._default_core_path()
        self.agents_path = Path(agents_path) if agents_path else self._default_agents_path()
        self.config = self._load_config(config)
        # reentrancy depth for locked() (same thread may nest: run -> snapshot)
        import threading as _th
        self._lock_state = _th.local()
        self._save_config()

    def _default_core_path(self) -> Path:
        from src.core.store import core_db_path
        try:
            return core_db_path()
        except Exception:
            return Path("memory/core.db")

    def _default_agents_path(self) -> Path:
        try:
            return agent_store.agents_db_path()
        except Exception:
            return Path("memory/agents.db")

    @property
    def lock_path(self) -> Path:
        for p in (self.core_path.parent, self.agents_path.parent):
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p / ".lock"
            except OSError:
                continue
        return Path(".lock")

    # ── Config ──

    def _load_config(self, override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Load brain/config.json over DEFAULTS. Single file — nothing else read."""
        merged = dict(DEFAULTS)
        if self.config_path.exists():
            try:
                merged.update(json.loads(self.config_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
        if override:
            merged.update(override)
        try:
            merged["prune_threshold"] = merged["prune_importance_floor"] = float(
                merged.get("prune_importance_floor", merged.get("prune_threshold", 0.05)))
        except (ValueError, TypeError):
            merged["prune_threshold"] = merged["prune_importance_floor"] = 0.05
        # ephemeral max age: hours is canonical, days kept for compat (days*24 = hours)
        try:
            if "ephemeral_max_age_hours" not in merged or merged["ephemeral_max_age_hours"] is None:
                # migrate from old days key
                merged["ephemeral_max_age_hours"] = int(merged.get("ephemeral_max_age_days", 7)) * 24
            # keep days in sync for old clients
            merged["ephemeral_max_age_days"] = int(int(merged["ephemeral_max_age_hours"]) / 24) if int(merged["ephemeral_max_age_hours"]) % 24 == 0 else round(int(merged["ephemeral_max_age_hours"]) / 24, 2)
        except (ValueError, TypeError):
            merged["ephemeral_max_age_hours"] = 168
            merged["ephemeral_max_age_days"] = 7
        return merged

    def _save_config(self) -> None:
        """Write brain/config.json. The ONLY config file (no memory copy)."""
        try:
            # keep hours/days in sync (hours canonical)
            try:
                if "ephemeral_max_age_hours" in self.config:
                    h = int(self.config["ephemeral_max_age_hours"])
                    self.config["ephemeral_max_age_hours"] = h
                    self.config["ephemeral_max_age_days"] = int(h / 24) if h % 24 == 0 else round(h / 24, 2)
                elif "ephemeral_max_age_days" in self.config:
                    d = float(self.config["ephemeral_max_age_days"])
                    self.config["ephemeral_max_age_hours"] = int(d * 24)
            except (ValueError, TypeError):
                pass
            self.config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        except OSError:
            pass

    def reload_config(self) -> Dict[str, Any]:
        """Re-read brain/config.json from disk (hand-edits go live, no restart)."""
        self.config = self._load_config()
        return self.config

    def reset_config(self) -> Dict[str, Any]:
        self.config = dict(DEFAULTS)
        self._save_config()
        return self.config

    # ── Connections (store invariant; caller closes) ──

    def core_conn(self) -> sqlite3.Connection:
        return core_connect(str(self.core_path),
                            cache_size=int(self.config.get("sqlite_cache_size", -64000)))

    def agents_conn(self) -> sqlite3.Connection:
        return agent_store.connect(str(self.agents_path),
                                   cache_size=int(self.config.get("sqlite_cache_size", -64000)))

    def _targets(self, target: str) -> List[str]:
        if target == "core":
            return ["core"]
        if target == "agents":
            return ["agents"]
        return ["core", "agents"]

    def _conn_for(self, db: str) -> sqlite3.Connection:
        return self.core_conn() if db == "core" else self.agents_conn()

    def _path_for(self, db: str) -> Path:
        return self.core_path if db == "core" else self.agents_path

    # ── Inter-process lock (stdlib: msvcrt / fcntl) ──

    @contextmanager
    def locked(self, timeout_s: float = 120.0):
        """Exclusive inter-process lock for VACUUM/restore/snapshots/runs.

        Reentrant within one thread (scheduler holds it for whole runs while
        job steps take it again) — depth-counted, single underlying acquire.
        """
        import threading as _th
        depth = getattr(self._lock_state, "depth", 0)
        if depth > 0:
            self._lock_state.depth = depth + 1
            try:
                yield None
            finally:
                self._lock_state.depth = depth
            return
        fh = open(self.lock_path, "w")
        self._lock_state.depth = 1
        try:
            try:
                import msvcrt  # Windows
                start = time.time()
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.time() - start > timeout_s:
                            raise TimeoutError("memory lock timeout")
                        time.sleep(0.05)
                try:
                    yield fh
                finally:
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            except ImportError:  # POSIX
                import fcntl
                start = time.time()
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.time() - start > timeout_s:
                            raise TimeoutError("memory lock timeout")
                        time.sleep(0.05)
                try:
                    yield fh
                finally:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            self._lock_state.depth = 0
            fh.close()

    # ── Snapshots ──

    def _snapshot_name(self, db: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"snapshot_{db}_{ts}.db"

    def create_snapshot(self, db: str = "core") -> Dict[str, Any]:
        """Manual snapshot (always runs): backup-API copy + rotation."""
        src_path = self._path_for(db)
        if not src_path.exists():
            return {"status": "error", "message": f"DB file not found: {src_path}"}
        dest = self.snapshots_dir / self._snapshot_name(db)
        try:
            with self.locked():
                # Sanctioned raw-connect exception: backup staging endpoints.
                src = sqlite3.connect(str(src_path))
                try:
                    dst = sqlite3.connect(str(dest))
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                finally:
                    src.close()
            self._rotate_snapshots()
            return {"status": "success", "db": db, "filename": dest.name,
                    "size_bytes": dest.stat().st_size,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        except Exception as e:
            try:
                dest.unlink()
            except OSError:
                pass
            return {"status": "error", "message": f"Snapshot failed: {e}"}

    def _rotate_snapshots(self) -> None:
        try:
            keep = int(self.config.get("keep_last_snapshots", 10))
            snaps = sorted(self.snapshots_dir.glob("snapshot_*.db"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in snaps[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except (ValueError, OSError):
            pass

    def _snapshot_age(self, db: str) -> Optional[float]:
        """Age in seconds of the freshest snapshot for a DB (None if none)."""
        snaps = sorted(self.snapshots_dir.glob(f"snapshot_{db}_*.db"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not snaps:
            return None
        return time.time() - snaps[0].stat().st_mtime

    def ensure_pre_run_snapshot(self, dbs: List[str]) -> Dict[str, Any]:
        """Coalesced auto-snapshot (§2.11): at most one per DB per window.

        Returns per-DB {"snapshot_taken": name} or
        {"snapshot_skipped": "fresh_exists" | "toggle_off"}.
        """
        out: Dict[str, Any] = {}
        if not self.config.get("auto_snapshot_before_jobs", False):
            return {db: {"snapshot_skipped": "toggle_off"} for db in dbs}
        cooldown = float(self.config.get("snapshot_cooldown_s", 300))
        for db in dbs:
            age = self._snapshot_age(db)
            if age is not None and age < cooldown:
                out[db] = {"snapshot_skipped": "fresh_exists",
                           "fresh_age_s": round(age, 1)}
                continue
            res = self.create_snapshot(db)
            out[db] = ({"snapshot_taken": res["filename"]} if res.get("status") == "success"
                       else {"snapshot_error": res.get("message")})
        return out

    def list_snapshots(self, db: Optional[str] = None) -> List[Dict[str, Any]]:
        pattern = f"snapshot_{db}_*.db" if db else "snapshot_*.db"
        out = []
        for path in self.snapshots_dir.glob(pattern):
            out.append({"filename": path.name, "size_bytes": path.stat().st_size,
                        "created_at": datetime.fromtimestamp(
                            path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "mtime": int(path.stat().st_mtime)})
        out.sort(key=lambda x: -x["mtime"])
        return out

    def _jailed_snapshot(self, filename: str) -> Optional[Path]:
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return None
        p = (self.snapshots_dir / filename).resolve()
        try:
            if self.snapshots_dir.resolve() not in p.parents:
                return None
        except OSError:
            return None
        return p if p.exists() else None

    def restore_snapshot(self, db: str, snapshot_filename: str) -> Dict[str, Any]:
        """Rollback: pre-rollback backup (backup API) + restore + health verify."""
        snap = self._jailed_snapshot(snapshot_filename)
        if snap is None:
            return {"status": "error", "message": "snapshot not found or invalid"}
        dst_path = self._path_for(db)
        try:
            with self.locked():
                pre = self.snapshots_dir / \
                    f"prerollback_{db}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                if dst_path.exists():
                    # Sanctioned raw-connect exception: backup staging endpoints.
                    src = sqlite3.connect(str(dst_path))
                    try:
                        dst = sqlite3.connect(str(pre))
                        try:
                            src.backup(dst)
                        finally:
                            dst.close()
                    finally:
                        src.close()
                else:
                    pre = None
                # Sanctioned raw-connect exception: backup staging endpoints.
                src = sqlite3.connect(str(snap))
                try:
                    dst = sqlite3.connect(str(dst_path))
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                finally:
                    src.close()
            verify = self.health(target=db)
            ok = verify.get(db, {}).get("check", {}).get("ok")
            return {"status": "success", "db": db, "restored": snapshot_filename,
                    "pre_rollback_backup": pre.name if pre else None,
                    "health_ok": bool(ok)}
        except Exception as e:
            return {"status": "error", "message": f"Restore failed: {e}"}

    def delete_snapshot(self, snapshot_filename: str) -> Dict[str, Any]:
        snap = self._jailed_snapshot(snapshot_filename)
        if snap is None:
            return {"status": "error", "message": "snapshot not found or invalid"}
        try:
            size = snap.stat().st_size
            snap.unlink()
            return {"status": "success", "message": f"Deleted {snapshot_filename}",
                    "size_bytes": size}
        except OSError as e:
            return {"status": "error", "message": f"Delete failed: {e}"}

    # ── Shared job helpers ──

    def _purge_orphans(self, conn: sqlite3.Connection) -> Dict[str, int]:
        """Orphan sweep (runs after every mutating job, per DB)."""
        purged = {}
        for table, stmt in {
            "edges": "DELETE FROM edges WHERE from_node NOT IN (SELECT node_id FROM nodes)"
                     " OR to_node NOT IN (SELECT node_id FROM nodes)",
            "node_vectors": "DELETE FROM node_vectors WHERE node_id NOT IN (SELECT node_id FROM nodes)",
            "memory_layers": "DELETE FROM memory_layers WHERE node_id NOT IN (SELECT node_id FROM nodes)",
            "access_log": "DELETE FROM access_log WHERE node_id NOT IN (SELECT node_id FROM nodes)",
            "node_index": "DELETE FROM node_index WHERE node_id NOT IN (SELECT node_id FROM nodes)",
        }.items():
            cur = conn.execute(stmt)
            purged[table] = cur.rowcount if cur.rowcount and cur.rowcount >= 0 else 0
        return purged

    def purge_orphans(self, target: str = "both") -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                out[db] = self._purge_orphans(conn)
                conn.commit()
            except Exception as e:
                conn.rollback()
                out[db] = {"status": "error", "message": str(e)}
            finally:
                conn.close()
        return out

    # -- Ghost agent cleanup (agents.db: agents with 0 nodes) --
    def get_empty_agents(self, min_age_hours: Optional[int] = None) -> Dict[str, Any]:
        """Preview agents with zero nodes (agents.db only). Age-gated if configured."""
        if min_age_hours is None:
            min_age_hours = int(self.config.get("prune_empty_agents_min_age_hours", 24))
        cutoff = _now() - int(min_age_hours) * 3600 if min_age_hours and min_age_hours > 0 else 0
        conn = self.agents_conn()
        try:
            rows = conn.execute(
                """SELECT a.agent_id, a.slug, a.job_hint, a.created_at
                   FROM agents a
                   LEFT JOIN (SELECT DISTINCT agent_id AS aid FROM nodes) n ON n.aid = a.agent_id
                   WHERE n.aid IS NULL""").fetchall()
            all_empty = [dict(r) for r in rows]
            eligible = [r for r in all_empty if cutoff == 0 or int(r["created_at"] or 0) <= cutoff]
            return {"total_empty": len(all_empty), "eligible": len(eligible), "min_age_hours": min_age_hours,
                    "cutoff_ts": cutoff, "agents": eligible, "all": all_empty}
        finally:
            conn.close()

    def prune_empty_agents(self, target: str = "agents", dry_run: bool = False,
                           min_age_hours: Optional[int] = None) -> Dict[str, Any]:
        """Delete agents with 0 nodes (agents.db only). Respects min_age gate.

        Cleans: agents row (aliases cascade), mailbox_receipts for scope agent:<id>.
        Mailbox messages themselves are kept (other participants may exist).
        Returns per-DB shape {"agents": {...}, "core": {"status": "skipped"}}.
        """
        if target not in ("agents", "both", "all"):
            return {"core": {"status": "skipped"}, "agents": {"status": "skipped", "reason": "target not agents"}}
        if min_age_hours is None:
            min_age_hours = int(self.config.get("prune_empty_agents_min_age_hours", 24))
        # respect toggle unless dry_run or explicit manual call via dashboard
        # caller decides; this method always executes when called directly
        cutoff = _now() - int(min_age_hours) * 3600 if min_age_hours and min_age_hours > 0 else 0
        conn = self.agents_conn()
        try:
            rows = conn.execute(
                """SELECT a.agent_id, a.created_at FROM agents a
                   LEFT JOIN (SELECT DISTINCT agent_id AS aid FROM nodes) n ON n.aid = a.agent_id
                   WHERE n.aid IS NULL""").fetchall()
            candidates = [(r["agent_id"], r["created_at"]) for r in rows]
            eligible = [aid for aid, cts in candidates if cutoff == 0 or int(cts or 0) <= cutoff]
            if dry_run:
                conn.rollback()
                return {"status": "success", "dry_run": True, "would_delete": len(eligible),
                        "total_empty": len(candidates), "min_age_hours": min_age_hours,
                        "agents": eligible}
            deleted = 0
            for aid in eligible:
                conn.execute("DELETE FROM mailbox_receipts WHERE scope = ?", (f"agent:{aid}",))
                cur = conn.execute("DELETE FROM agents WHERE agent_id = ?", (aid,))
                deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
            return {"status": "success", "deleted": deleted, "total_empty": len(candidates),
                    "eligible": len(eligible), "min_age_hours": min_age_hours, "agents": eligible}
        except Exception as e:
            conn.rollback()
            return {"status": "error", "message": str(e)}
        finally:
            conn.close()

    def _maybe_prune_empty_agents(self) -> Dict[str, Any]:
        """Helper for maintenance cycle: runs only if prune_empty_agents toggle ON."""
        if not bool(self.config.get("prune_empty_agents", False)):
            return {"status": "skipped", "reason": "prune_empty_agents disabled"}
        # reuse prune with agents target
        res = self.prune_empty_agents(target="agents", dry_run=False)
        # shape for both DBs
        return {"agents": res, "core": {"status": "skipped"}}

    @staticmethod
    def _meta(row) -> Dict[str, Any]:
        try:
            return json.loads(row["metadata"] or "{}")
        except (ValueError, TypeError, KeyError):
            return {}

    def _is_agent_row(self, agent_id_val, meta: Dict[str, Any],
                      node_type: str) -> bool:
        """agents.db rows are agent work unless core-verified (core.db parity)."""
        if meta.get("attention_state") == "core_verified":
            return False
        if node_type == "AGENT_NOTE" or meta.get("agent_scoped"):
            return True
        return agent_id_val not in (None, "")

    def _relink_and_delete(self, conn: sqlite3.Connection, primary_id: str,
                           secondary_id: str, agent_id: Optional[str] = None) -> int:
        """Port of v2 _relink_and_delete + df-exact vector cleanup (v3 addition).

        agents.db callers pass agent_id so re-pointed edges keep their stamp.
        Orphan guard (2026-09-05: one edge pointing at a missing node used to
        brick the whole dedup with FOREIGN KEY failed — relinking it to the
        primary just moves the violation, and the rollback also skipped the
        orphan purge that would have cleaned it, so the job failed forever):
        edges whose surviving endpoint is gone are deleted, never relinked.
        """
        relinked = 0
        alive = {r[0] for r in conn.execute("SELECT node_id FROM nodes")}
        # FK guard: if primary was already deleted (concurrent or earlier in this run), keep secondary
        if primary_id not in alive:
            return relinked
        if secondary_id not in alive:
            return relinked
        for row in conn.execute("SELECT edge_id, to_node, edge_type FROM edges"
                                " WHERE from_node = ?", (secondary_id,)).fetchall():
            if row["to_node"] in (secondary_id, primary_id) or row["to_node"] not in alive:
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
                continue
            conflict = conn.execute(
                "SELECT edge_id FROM edges WHERE from_node = ? AND to_node = ?"
                " AND edge_type = ?", (primary_id, row["to_node"], row["edge_type"])).fetchone()
            if conflict:
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
            else:
                if agent_id is None:
                    conn.execute("UPDATE edges SET from_node = ? WHERE edge_id = ?",
                                 (primary_id, row["edge_id"]))
                else:
                    conn.execute("UPDATE edges SET from_node = ?, agent_id = ? WHERE edge_id = ?",
                                 (primary_id, agent_id, row["edge_id"]))
                relinked += 1
        for row in conn.execute("SELECT edge_id, from_node, edge_type FROM edges"
                                " WHERE to_node = ?", (secondary_id,)).fetchall():
            if row["from_node"] == secondary_id or row["from_node"] not in alive:
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
                continue
            conflict = conn.execute(
                "SELECT edge_id FROM edges WHERE from_node = ? AND to_node = ?"
                " AND edge_type = ?", (row["from_node"], primary_id, row["edge_type"])).fetchone()
            if conflict:
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
            else:
                if agent_id is None:
                    conn.execute("UPDATE edges SET to_node = ? WHERE edge_id = ?",
                                 (primary_id, row["edge_id"]))
                else:
                    conn.execute("UPDATE edges SET to_node = ?, agent_id = ? WHERE edge_id = ?",
                                 (primary_id, agent_id, row["edge_id"]))
                relinked += 1
        label = conn.execute("SELECT label, content FROM nodes WHERE node_id = ?",
                             (secondary_id,)).fetchone()
        if label:
            core_vectors.update_on_delete(conn, secondary_id,
                                          label["label"] or "", label["content"] or "")
        conn.execute("DELETE FROM nodes WHERE node_id = ?", (secondary_id,))
        return relinked

    @staticmethod
    def _merge_agent_metadata(primary_meta: Dict, secondary_meta: Dict) -> Dict:
        """Port of v2 _merge_agent_metadata (provenance-preserving merge)."""
        merged = dict(primary_meta)
        agent_ids = {str(m.get("agent_id")) for m in (primary_meta, secondary_meta)
                     if m.get("agent_id")}
        if len(agent_ids) > 1:
            merged["agent_ids"] = sorted(agent_ids)
            merged["agent_id"] = primary_meta.get("agent_id") or secondary_meta.get("agent_id")
        best = max((m.get("attention_state") for m in (primary_meta, secondary_meta)),
                   key=lambda s: ATTENTION_PRIORITY.get(s or "", 0))
        if best and best != merged.get("attention_state"):
            merged["attention_state"] = best
        return merged

    # ── Job: deduplicate ──

    def deduplicate(self, target: str = "both",
                    similarity_threshold: Optional[float] = None) -> Dict[str, Any]:
        """Exact + near-duplicate merge per scope (core.db whole / agents.db per agent).

        Never merges across scopes (core vs agent, agent vs agent). df-exact via
        vectors helpers (v3 addition — v2 had no df table). Capped scans report
        truncated:true + counts instead of v2's silent [:500]/[:800].
        One automatic retry on FOREIGN KEY clash (concurrent node delete).
        """
        threshold = (similarity_threshold if similarity_threshold is not None
                     else float(self.config.get("dedup_similarity_threshold", 0.85)))
        cap = int(self.config.get("dedup_scan_cap", 2000))
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                try:
                    out[db] = self._deduplicate_conn(conn, db, threshold, cap)
                except sqlite3.IntegrityError as e:
                    if "FOREIGN KEY" not in str(e):
                        raise
                    # A concurrent writer (dashboard curation, MCP) deleted a
                    # node between our SELECT and our relink UPDATE. Roll back
                    # and re-run on a fresh snapshot (2026-09-05: three live
                    # FULL runs failed exactly this way while the operator
                    # curated mid-run). One retry; a second clash errors
                    # honestly instead of looping.
                    conn.rollback()
                    out[db] = self._deduplicate_conn(conn, db, threshold, cap)
                    out[db]["retried_after_concurrent_write"] = True
                out[db]["orphans_purged"] = self._purge_orphans(conn)
                conn.commit()
            except Exception as e:
                conn.rollback()
                out[db] = {"status": "error", "message": f"Deduplication failed: {e}"}
            finally:
                conn.close()
        return out

    def _scope_key(self, db: str, row, meta: Dict[str, Any]):
        if db == "agents":
            return ("agent", row["agent_id"])
        is_agent = self._is_agent_row(None, meta, row["node_type"])
        return ("agent-note", meta.get("agent_id") or "?") if is_agent else ("core",)

    def _deduplicate_conn(self, conn: sqlite3.Connection, db: str,
                          threshold: float, cap: int) -> Dict[str, Any]:
        exact = semantic = relinked = 0
        truncated = False
        # core.db has no agent_id column — select a NULL placeholder instead
        aid = "n.agent_id" if db == "agents" else "NULL AS agent_id"
        rows = conn.execute(
            "SELECT n.node_id, " + aid + ", n.label, n.content, n.checksum,"
            " n.access_count, n.importance, n.node_type, n.metadata FROM nodes n").fetchall()
        groups: Dict[Any, List] = defaultdict(list)
        for r in rows:
            groups[self._scope_key(db, r, self._meta(r))].append(r)
        for group in groups.values():
            by_key: Dict[Any, List] = defaultdict(list)
            for n in group:
                by_key[(n["checksum"], (n["label"] or "").strip().lower(),
                        (n["content"] or "").strip().lower())].append(n)
            for dupes in by_key.values():
                if len(dupes) < 2:
                    continue
                primary = dupes[0]
                for secondary in dupes[1:]:
                    if db == "agents":
                        merged = self._merge_agent_metadata(
                            self._meta(primary), self._meta(secondary))
                        if merged != self._meta(primary):
                            conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?",
                                         (json.dumps(merged), primary["node_id"]))
                    relinked += self._relink_and_delete(
                        conn, primary["node_id"], secondary["node_id"],
                        secondary["agent_id"] if db == "agents" else None)
                    exact += 1
        conn.commit()
        # Semantic pass over stored compact vectors (no full-corpus fit)
        rows = conn.execute(
            "SELECT n.node_id, " + aid + ", n.label, n.content, n.access_count,"
            " n.importance, n.node_type, n.metadata, nv.vector, nv.magnitude"
            " FROM nodes n LEFT JOIN node_vectors nv ON n.node_id = nv.node_id").fetchall()
        groups = defaultdict(list)
        for r in rows:
            groups[self._scope_key(db, r, self._meta(r))].append(r)
        for group in groups.values():
            if len(group) <= 1:
                continue
            if len(group) > cap:
                truncated = True
                group = group[:cap]
            vecs = []
            for n in group:
                raw = n["vector"] if "vector" in n.keys() else None
                v = core_vectors.decode(raw) if raw else {}
                if not v:
                    text = (n["label"] or "") + " " + (n["content"] or "")
                    ltf = Counter(_tokenize(text))
                    v = {t: 1.0 for t in ltf}
                vecs.append(v)
            deleted = set()
            for i in range(len(group)):
                if group[i]["node_id"] in deleted:
                    continue
                for j in range(i + 1, len(group)):
                    if group[j]["node_id"] in deleted:
                        continue
                    sim = core_vectors.cosine(vecs[i], vecs[j])
                    if sim < threshold:
                        continue
                    a, b = group[i], group[j]
                    if (b["access_count"] + b["importance"]) > (a["access_count"] + a["importance"]):
                        primary, secondary = b, a
                    else:
                        primary, secondary = a, b
                    new_content = primary["content"] or ""
                    if (secondary["content"] or "") not in new_content:
                        new_content = f"{new_content} | {secondary['content']}"
                        conn.execute("UPDATE nodes SET content = ?, updated_at = ?"
                                     " WHERE node_id = ?",
                                     (new_content[:500], _now(), primary["node_id"]))
                    if db == "agents":
                        merged = self._merge_agent_metadata(
                            self._meta(primary), self._meta(secondary))
                        if merged != self._meta(primary):
                            conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?",
                                         (json.dumps(merged), primary["node_id"]))
                    relinked += self._relink_and_delete(
                        conn, primary["node_id"], secondary["node_id"],
                        secondary["agent_id"] if db == "agents" else None)
                    deleted.add(secondary["node_id"])
                    semantic += 1
        return {"status": "success", "exact_merged": exact,
                "semantic_merged": semantic, "total_merged": exact + semantic,
                "edges_relinked": relinked, "truncated": truncated, "scan_cap": cap}

    # ── Job: manage_tiers ──

    def manage_tiers(self, target: str = "both") -> Dict[str, Any]:
        """Decay short/long_term, prune stale+quiet below floor (no promotions).

        Promotions now owned by Observer (agents WORKING) and Core Helper (core WORKING).
        Decay uses MEMORY_DECAY per day; prune checks floor + max_unused_days + access<3
        + edgeless + not review_ready/protected. Deletes via nodes.delete_node (df-exact).
        """
        floor = float(self.config.get("prune_importance_floor", 0.05))
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                res = self._tiers_conn(conn, floor)
                res["orphans_purged"] = self._purge_orphans(conn)
                conn.commit()
                out[db] = res
            except Exception as e:
                conn.rollback()
                out[db] = {"status": "error", "message": f"Tier management failed: {e}"}
            finally:
                conn.close()
        return out

    def _tiers_conn(self, conn: sqlite3.Connection, floor: float) -> Dict[str, Any]:
        """Decay-only: no layer promotions (Observer + Core Helper own WORKING moves).

        Monitors short_term/long_term: decay importance by MEMORY_DECAY per day,
        then marks prune candidates where importance < floor and quiet (access<3)
        and old (updated_at <= max_unused_days). Review_ready / protected / edged
        are never pruned. Uses max_unused_days from config for age gate.
        """
        decayed = pruned = skipped_review = skipped_prot = skipped_age = 0
        max_unused_days = int(self.config.get("max_unused_days", 4))
        cutoff = _now() - max_unused_days * 86400
        for n in conn.execute(
                "SELECT n.node_id, n.importance, n.access_count, n.updated_at,"
                " n.node_type, n.metadata, ml.layer FROM nodes n"
                " JOIN memory_layers ml ON n.node_id = ml.node_id"
                " WHERE ml.layer IN ('short_term', 'long_term')").fetchall():
            decay = MEMORY_DECAY.get(n["layer"], MEMORY_DECAY["short_term"])
            days_old = max(0.0, (_now() - (n["updated_at"] or _now())) / 86400)
            new_imp = min(1.0, max(0.0, (n["importance"] or 0.0) * (decay ** days_old)))
            if abs(new_imp - (n["importance"] or 0.0)) > 0.01:
                conn.execute("UPDATE nodes SET importance = ? WHERE node_id = ?",
                             (new_imp, n["node_id"]))
                decayed += 1
            # prune candidate: below floor + quiet + stale + edgeless + not protected/review_ready
            if new_imp >= floor:
                continue
            if (n["access_count"] or 0) >= 3:
                continue
            if (n["updated_at"] or _now()) > cutoff:
                skipped_age += 1
                continue
            meta = self._meta(n)
            if meta.get("attention_state") == "review_ready":
                skipped_review += 1
                continue
            if n["node_type"] in PROTECTED_TYPES:
                skipped_prot += 1
                continue
            if conn.execute("SELECT COUNT(*) FROM edges WHERE from_node = ?"
                            " OR to_node = ?", (n["node_id"], n["node_id"])).fetchone()[0]:
                continue
            if core_nodes.delete_node(conn, n["node_id"]):
                pruned += 1
        return {"status": "success", "working_promoted": 0, "promoted": 0, "decayed": decayed,
                "pruned": pruned, "skipped_review_ready": skipped_review,
                "skipped_protected": skipped_prot, "skipped_age": skipped_age}

    # ── Job: agent working-memory regulator (agents.db only) ──

    def _working_weights(self) -> Tuple[float, float, float, float, int, int]:
        c = self.config
        return (float(c.get("agent_working_weight_access", 1.5)),
                float(c.get("agent_working_weight_importance", 4.0)),
                float(c.get("agent_working_weight_age", 0.15)),
                float(c.get("agent_working_max_age_hours", 48)),
                int(c.get("agent_working_high_water", 12)),
                int(c.get("agent_working_demote_batch", 5)))

    def get_agent_working_preview(self) -> Dict[str, Any]:
        """Observer preview over agents.db WORKING (core never included)."""
        wa, wi, wd, max_age_h, high_water, batch = self._working_weights()
        enabled = bool(self.config.get("agent_working_regulator_enabled", True))
        conn = self.agents_conn()
        try:
            now = _now()
            rows = conn.execute(
                "SELECT n.node_id, n.agent_id, n.node_type, n.label, n.content,"
                " n.importance, n.access_count, n.trust_level, n.created_at,"
                " n.metadata, ml.promoted_at FROM nodes n"
                " JOIN memory_layers ml ON n.node_id = ml.node_id"
                " WHERE ml.layer = 'working'").fetchall()
            preview = []
            for r in rows:
                meta = self._meta(r)
                promoted_at = r["promoted_at"] or r["created_at"] or now
                la = conn.execute("SELECT MAX(accessed_at) FROM access_log"
                                  " WHERE node_id = ?", (r["node_id"],)).fetchone()[0]
                base = max(promoted_at, la or 0)
                age_h = max(0.0, (now - base) / 3600.0)
                imp = r["importance"] if r["importance"] is not None else 0.5
                acc = r["access_count"] or 0
                preview.append({
                    "node_id": r["node_id"], "agent_id": r["agent_id"],
                    "label": r["label"], "content": (r["content"] or "")[:160],
                    "importance": imp, "trust_level": r["trust_level"],
                    "access_count": acc,
                    "attention_state": meta.get("attention_state", "agent_private"),
                    "is_review_ready": meta.get("attention_state") == "review_ready",
                    "promoted_at": promoted_at, "last_access": la or promoted_at,
                    "age_hours": round(age_h, 1),
                    "score": round(acc * wa + imp * wi - age_h * wd, 3),
                    "days_left": round(max(0.0, (max_age_h - age_h) / 24.0), 2),
                    "hours_left": round(max(0.0, max_age_h - age_h), 1)})
            preview.sort(key=lambda x: x["score"])
            for i, p in enumerate(preview):
                if p["is_review_ready"]:
                    p["action"] = "protected (review_ready)"
                elif i < batch and (len(preview) >= high_water
                                    or p["age_hours"] >= max_age_h):
                    p["action"] = "demote_next"
                elif p["age_hours"] >= max_age_h * 0.8:
                    p["action"] = "stale_soon"
                else:
                    p["action"] = "keep"
            return {"enabled": enabled, "high_water": high_water,
                    "max_age_hours": max_age_h, "weights": {"wa": wa, "wi": wi, "wd": wd},
                    "demote_batch": batch, "agent_working_count": len(preview),
                    "preview": preview[:100]}
        finally:
            conn.close()

    def regulate_agent_working_memory(self, dry_run: bool = False) -> Dict[str, Any]:
        """Demote low-score agents.db WORKING -> short_term (core untouched)."""
        if not self.config.get("agent_working_regulator_enabled", True):
            return {"agents": {"status": "skipped", "message": "regulator disabled",
                               "demoted": 0}}
        wa, wi, wd, max_age_h, high_water, batch = self._working_weights()
        conn = self.agents_conn()
        try:
            now = _now()
            rows = conn.execute(
                "SELECT n.node_id, n.importance, n.access_count, n.created_at,"
                " n.metadata, ml.promoted_at FROM nodes n"
                " JOIN memory_layers ml ON n.node_id = ml.node_id"
                " WHERE ml.layer = 'working'").fetchall()
            candidates = []
            for r in rows:
                if self._meta(r).get("attention_state") == "review_ready":
                    continue
                promoted_at = r["promoted_at"] or r["created_at"] or now
                la = conn.execute("SELECT MAX(accessed_at) FROM access_log"
                                  " WHERE node_id = ?", (r["node_id"],)).fetchone()[0]
                base = max(promoted_at, la or 0)
                age_h = max(0.0, (now - base) / 3600.0)
                imp = r["importance"] if r["importance"] is not None else 0.5
                candidates.append(((r["access_count"] or 0) * wa + imp * wi - age_h * wd,
                                   age_h, r["node_id"]))
            candidates.sort(key=lambda x: x[0])
            total = len(rows)
            if total < high_water and not any(a >= max_age_h for _, a, _ in candidates):
                return {"agents": {"status": "success", "demoted": 0, "dry_run": dry_run,
                                   "message": f"below high_water ({total}/{high_water})",
                                   "total_agent_working": total}}
            to_demote = [c for c in candidates if c[1] >= max_age_h][:batch]
            if total >= high_water:
                for c in candidates:
                    if len(to_demote) >= batch:
                        break
                    if c not in to_demote:
                        to_demote.append(c)
            ids = [nid for _, _, nid in to_demote]
            if not dry_run:
                for nid in ids:
                    conn.execute("UPDATE memory_layers SET layer = 'short_term',"
                                 " layer_order = 2, promoted_at = ? WHERE node_id = ?",
                                 (now, nid))
                conn.commit()
            out: Dict[str, Any] = {"status": "success", "demoted": len(ids),
                                   "demoted_ids": ids, "total_agent_working": total,
                                   "high_water": high_water,
                                   "max_age_hours": max_age_h, "dry_run": dry_run}
            if dry_run:
                out["would_demote"] = [{"node_id": nid, "score": round(s, 3),
                                        "age_hours": round(a, 1)} for s, a, nid in to_demote]
            return {"agents": out}
        except Exception as e:
            conn.rollback()
            return {"agents": {"status": "error", "message": str(e)}}
        finally:
            conn.close()

    # ── Job: core helper — working layer regulator (core.db only) ──

    def _core_helper_params(self) -> Tuple[float, float, float, float, int]:
        c = self.config
        return (
            float(c.get("core_helper_min_age_hours", 72)),
            float(c.get("core_helper_imp_threshold", 0.6)),
            float(c.get("core_helper_trust_low", 0.5)),
            float(c.get("core_helper_trust_high", 0.8)),
            int(c.get("core_helper_access_threshold", 4)),
        )

    def get_core_helper_preview(self) -> Dict[str, Any]:
        """Core Helper preview over core.db WORKING (Observer twin for Core)."""
        enabled = bool(self.config.get("core_helper_enabled", False))
        min_age_h, imp_thr, trust_low, trust_high, acc_thr = self._core_helper_params()
        conn = self.core_conn()
        try:
            now = _now()
            rows = conn.execute(
                "SELECT n.node_id, n.node_type, n.label, n.content,"
                " n.importance, n.access_count, n.trust_level, n.created_at,"
                " n.metadata, ml.promoted_at FROM nodes n"
                " JOIN memory_layers ml ON n.node_id = ml.node_id"
                " WHERE ml.layer = 'working'").fetchall()
            preview = []
            for r in rows:
                promoted_at = r["promoted_at"] or r["created_at"] or now
                la = conn.execute("SELECT MAX(accessed_at) FROM access_log WHERE node_id = ?", (r["node_id"],)).fetchone()[0]
                base = max(promoted_at, la or 0)
                age_h = max(0.0, (now - base) / 3600.0)
                imp = r["importance"] if r["importance"] is not None else 0.5
                trust = r["trust_level"] if r["trust_level"] is not None else 0.5
                acc = r["access_count"] or 0
                # decide action (configurable thresholds, access resolves overlap)
                if age_h < min_age_h:
                    action = "keep (young)"
                    target = "working"
                elif trust >= trust_high and imp >= imp_thr:
                    action = "keep (hot)"
                    target = "working"
                elif trust <= trust_low:
                    action = "to_short_term"
                    target = "short_term"
                elif trust >= trust_high and imp < imp_thr:
                    action = "to_short_term"
                    target = "short_term"
                elif trust_low < trust < trust_high and imp >= imp_thr:
                    if acc >= acc_thr:
                        action = "to_long_term"
                        target = "long_term"
                    else:
                        action = "to_short_term"
                        target = "short_term"
                elif imp < imp_thr:
                    action = "to_short_term"
                    target = "short_term"
                else:
                    action = "keep"
                    target = "working"
                preview.append({
                    "node_id": r["node_id"], "label": r["label"], "content": (r["content"] or "")[:160],
                    "node_type": r["node_type"], "importance": imp, "trust_level": trust,
                    "access_count": acc, "age_hours": round(age_h, 1),
                    "hours_left": round(max(0.0, min_age_h - age_h), 1),
                    "days_old": round(age_h / 24, 2),
                    "target_layer": target, "action": action,
                })
            # sort: moves first, then keep
            order = {"to_long_term": 0, "to_short_term": 1, "keep (hot)": 2, "keep (young)": 3, "keep": 4}
            preview.sort(key=lambda x: (order.get(x["action"], 9), -x["age_hours"]))
            move_counts = {"to_short_term": 0, "to_long_term": 0, "keep": 0}
            for p in preview:
                if p["action"] == "to_short_term":
                    move_counts["to_short_term"] += 1
                elif p["action"] == "to_long_term":
                    move_counts["to_long_term"] += 1
                else:
                    move_counts["keep"] += 1
            return {"enabled": enabled, "min_age_hours": min_age_h, "imp_threshold": imp_thr,
                    "trust_low": trust_low, "trust_high": trust_high, "access_threshold": acc_thr,
                    "core_working_count": len(preview), "move_counts": move_counts,
                    "preview": preview[:100]}
        finally:
            conn.close()

    def regulate_core_helper(self, dry_run: bool = False) -> Dict[str, Any]:
        """Move eligible core WORKING -> short/long_term (never archive, never demote other layers)."""
        if not bool(self.config.get("core_helper_enabled", False)):
            return {"core": {"status": "skipped", "message": "core helper disabled", "moved": 0}}
        min_age_h, imp_thr, trust_low, trust_high, acc_thr = self._core_helper_params()
        conn = self.core_conn()
        try:
            now = _now()
            preview = self.get_core_helper_preview()
            # get_core_helper_preview already filtered to working and computed actions; reuse logic but need fresh conn
            # Recompute to avoid double-fetch overhead: use preview list
            to_short = [p["node_id"] for p in preview["preview"] if p["action"] == "to_short_term"]
            to_long = [p["node_id"] for p in preview["preview"] if p["action"] == "to_long_term"]
            # Also need full lists beyond 100 cap -> rescan full
            if preview["core_working_count"] > 100:
                # full rescan for complete move set
                rows = conn.execute(
                    "SELECT n.node_id, n.importance, n.access_count, n.trust_level, n.created_at, ml.promoted_at"
                    " FROM nodes n JOIN memory_layers ml ON n.node_id = ml.node_id WHERE ml.layer='working'").fetchall()
                to_short, to_long = [], []
                for r in rows:
                    promoted_at = r["promoted_at"] or r["created_at"] or now
                    la = conn.execute("SELECT MAX(accessed_at) FROM access_log WHERE node_id = ?", (r["node_id"],)).fetchone()[0]
                    base = max(promoted_at, la or 0)
                    age_h = max(0.0, (now - base) / 3600.0)
                    if age_h < min_age_h:
                        continue
                    imp = r["importance"] if r["importance"] is not None else 0.5
                    trust = r["trust_level"] if r["trust_level"] is not None else 0.5
                    acc = r["access_count"] or 0
                    if trust >= trust_high and imp >= imp_thr:
                        continue
                    elif trust <= trust_low:
                        to_short.append(r["node_id"])
                    elif trust >= trust_high and imp < imp_thr:
                        to_short.append(r["node_id"])
                    elif trust_low < trust < trust_high and imp >= imp_thr:
                        if acc >= acc_thr:
                            to_long.append(r["node_id"])
                        else:
                            to_short.append(r["node_id"])
                    elif imp < imp_thr:
                        to_short.append(r["node_id"])
            if dry_run:
                return {"core": {"status": "success", "dry_run": True, "would_move_short": len(to_short),
                                 "would_move_long": len(to_long), "total_working": preview["core_working_count"],
                                 "would_short_ids": to_short[:20], "would_long_ids": to_long[:20]}}
            moved_short = moved_long = 0
            for nid in to_short:
                cur = conn.execute("UPDATE memory_layers SET layer='short_term', layer_order=2, promoted_at=? WHERE node_id=?", (now, nid))
                moved_short += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            for nid in to_long:
                cur = conn.execute("UPDATE memory_layers SET layer='long_term', layer_order=3, promoted_at=? WHERE node_id=?", (now, nid))
                moved_long += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
            return {"core": {"status": "success", "moved_short": moved_short, "moved_long": moved_long,
                             "moved_total": moved_short + moved_long, "total_working": preview["core_working_count"],
                             "dry_run": False}}
        except Exception as e:
            conn.rollback()
            return {"core": {"status": "error", "message": str(e)}}
        finally:
            conn.close()

    # ── Job: age_prune ──

    def prune_stale_unused(self, target: str = "both",
                           max_unused_days: Optional[int] = None) -> Dict[str, Any]:
        """Triple-gate prune on BOTH DBs (C-fix: v2's agent path skipped the
        importance/edge checks). Gates: stale + quiet + (agents: not review_ready;
        core: unprotected type + below floor + edgeless). Deletes via
        nodes.delete_node (df-exact, FK cascade)."""
        max_days = (max_unused_days if max_unused_days is not None
                    else int(self.config.get("max_unused_days", 4)))
        floor = float(self.config.get("prune_importance_floor", 0.05))
        cutoff = _now() - max_days * 86400
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                removed, skipped = [], Counter()
                for cand in conn.execute(
                        "SELECT node_id, label, node_type, updated_at, access_count,"
                        " importance, metadata FROM nodes"
                        " WHERE updated_at <= ? AND access_count <= 2", (cutoff,)).fetchall():
                    meta = self._meta(cand)
                    if meta.get("attention_state") == "review_ready":
                        skipped["review_ready"] += 1
                        continue
                    if cand["importance"] is not None and cand["importance"] >= floor:
                        skipped["important"] += 1
                        continue
                    if cand["node_type"] in PROTECTED_TYPES:
                        skipped["protected"] += 1
                        continue
                    if conn.execute("SELECT COUNT(*) FROM edges WHERE from_node = ?"
                                    " OR to_node = ?",
                                    (cand["node_id"], cand["node_id"])).fetchone()[0]:
                        skipped["connected"] += 1
                        continue
                    if core_nodes.delete_node(conn, cand["node_id"]):
                        removed.append({"node_id": cand["node_id"], "label": cand["label"],
                                        "node_type": cand["node_type"],
                                        "age_days": round((_now() - cand["updated_at"]) / 86400, 1)})
                res = {"status": "success", "max_unused_days": max_days,
                       "importance_floor": floor, "pruned_count": len(removed),
                       "removed_nodes": removed, "skipped": dict(skipped),
                       "orphans_purged": self._purge_orphans(conn)}
                conn.commit()
                out[db] = res
            except Exception as e:
                conn.rollback()
                out[db] = {"status": "error", "message": f"Age prune failed: {e}"}
            finally:
                conn.close()
        return out

    # ── Job: compact (ephemeral_events table, C13) ──

    def compact_ephemeral(self, target: str = "both",
                          keep_last: Optional[int] = None,
                          max_age_days: Optional[int] = None,
                          max_age_hours: Optional[int] = None) -> Dict[str, Any]:
        """TTL sweep over ephemeral_events + nodes (C13 unified).

        TTL ALWAYS applies; keep_last only caps the survivors. Never deletes labels.
        Hours is canonical (ephemeral_max_age_hours), days kept for compat.
        Scans BOTH tables for labels in ephemeral_labels: ephemeral_events (new
        telemetry) AND nodes (legacy v2 telemetry still stored as AGENT_NOTE).
        Without the nodes half, the scheduler goes in rounds doing nothing while
        nodes accumulate — the broken behavior reported (6h-old nodes kept despite
        TTL=1h, keep_last=2).
        """
        keep = keep_last if keep_last is not None else int(self.config.get("ephemeral_keep_last", 3))
        if max_age_hours is not None:
            ttl_hours = int(max_age_hours)
        elif max_age_days is not None:
            ttl_hours = int(max_age_days) * 24
        else:
            ttl_hours = int(self.config.get("ephemeral_max_age_hours", int(self.config.get("ephemeral_max_age_days", 7)) * 24))
        cutoff = _now() - ttl_hours * 3600
        allow = set(self.config.get("ephemeral_labels", []))
        ignored = set(self.config.get("ephemeral_ignored", []))
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                # ── ephemeral_events half (new telemetry) ──
                if not allow:
                    removed_ttl = 0
                else:
                    ph_allow = ",".join("?" * len(allow))
                    cur = conn.execute(
                        f"DELETE FROM ephemeral_events WHERE created_at < ? AND label IN ({ph_allow})",
                        (cutoff, *allow))
                    removed_ttl = cur.rowcount if cur.rowcount and cur.rowcount >= 0 else 0
                removed_cap, per_label = 0, {}
                labels = [r[0] for r in conn.execute("SELECT DISTINCT label FROM ephemeral_events")]
                for lab in labels:
                    if lab not in allow:
                        continue
                    keep_ids = [r[0] for r in conn.execute(
                        "SELECT id FROM ephemeral_events WHERE label = ?"
                        " ORDER BY created_at DESC LIMIT ?", (lab, keep))]
                    if not keep_ids:
                        continue
                    ph = ",".join("?" * len(keep_ids))
                    cur = conn.execute(
                        f"DELETE FROM ephemeral_events WHERE label = ? AND id NOT IN ({ph})",
                        (lab, *keep_ids))
                    n = cur.rowcount if cur.rowcount and cur.rowcount >= 0 else 0
                    removed_cap += n
                    per_label[lab] = n
                # ── nodes half (legacy telemetry as nodes) ──
                # Same TTL + keep_last semantics, but via nodes.delete_node for df-exact vectors.
                # Only labels in allow, never ignored, never ephemeral_events-only.
                nodes_ttl = nodes_cap = 0
                nodes_per_label: Dict[str, int] = {}
                orphans_purged = {}
                if allow:
                    placeholders = ",".join("?" * len(allow))
                    # Keep newest `keep` per label first so TTL never deletes below keep
                    keep_map: Dict[str, set] = {}
                    for lab in list(allow):
                        if lab in ignored:
                            continue
                        survivors = conn.execute(
                            "SELECT node_id FROM nodes WHERE label = ? ORDER BY created_at DESC LIMIT ?",
                            (lab, keep)).fetchall() if keep > 0 else []
                        keep_map[lab] = {x[0] for x in survivors}
                    # TTL: delete nodes older than cutoff where label in allow and not in keep
                    for lab in list(allow):
                        if lab in ignored:
                            continue
                        keep_ids = keep_map.get(lab, set())
                        if keep_ids:
                            ph_keep = ",".join("?" * len(keep_ids))
                            to_del = conn.execute(
                                f"SELECT node_id, label, content FROM nodes WHERE label = ? AND created_at < ? AND node_id NOT IN ({ph_keep})",
                                (lab, cutoff, *keep_ids)).fetchall()
                        else:
                            to_del = conn.execute(
                                "SELECT node_id, label, content FROM nodes WHERE label = ? AND created_at < ?",
                                (lab, cutoff)).fetchall()
                        for nd in to_del:
                            core_vectors.update_on_delete(conn, nd["node_id"], nd["label"] or "", nd["content"] or "")
                            conn.execute("DELETE FROM nodes WHERE node_id = ?", (nd["node_id"],))
                            nodes_ttl += 1
                            nodes_per_label[lab] = nodes_per_label.get(lab, 0) + 1
                    # keep_last cap on survivors per label (newest keep) — only for any remaining over keep
                    for lab in list(allow):
                        if lab in ignored:
                            continue
                        keep_ids = keep_map.get(lab, set())
                        if keep_ids:
                            ph = ",".join("?" * len(keep_ids))
                            extra = conn.execute(
                                f"SELECT node_id, label, content FROM nodes WHERE label = ? AND node_id NOT IN ({ph})",
                                (lab, *keep_ids)).fetchall()
                            for nd in extra:
                                core_vectors.update_on_delete(conn, nd["node_id"], nd["label"] or "", nd["content"] or "")
                                conn.execute("DELETE FROM nodes WHERE node_id = ?", (nd["node_id"],))
                                nodes_cap += 1
                                nodes_per_label[lab] = nodes_per_label.get(lab, 0) + 1
                        elif keep == 0:
                            extra = conn.execute(
                                "SELECT node_id, label, content FROM nodes WHERE label = ?", (lab,)).fetchall()
                            for nd in extra:
                                core_vectors.update_on_delete(conn, nd["node_id"], nd["label"] or "", nd["content"] or "")
                                conn.execute("DELETE FROM nodes WHERE node_id = ?", (nd["node_id"],))
                                nodes_cap += 1
                                nodes_per_label[lab] = nodes_per_label.get(lab, 0) + 1
                    if nodes_ttl or nodes_cap:
                        orphans_purged = self._purge_orphans(conn)
                conn.commit()
                out[db] = {"status": "success", "keep_last": keep,
                           "max_age_days": ttl_hours / 24, "max_age_hours": ttl_hours,
                           "removed_ttl": removed_ttl, "removed_cap": removed_cap,
                           "removed_total": removed_ttl + removed_cap,
                           "removed_per_label": per_label,
                           "nodes_removed_ttl": nodes_ttl, "nodes_removed_cap": nodes_cap,
                           "nodes_removed_total": nodes_ttl + nodes_cap,
                           "nodes_per_label": nodes_per_label,
                           "orphans_purged": orphans_purged}
            except Exception as e:
                conn.rollback()
                out[db] = {"status": "error", "message": f"Ephemeral compaction failed: {e}"}
            finally:
                conn.close()
        return out

    def sync_ephemeral_nodes(self, target: str = "both", dry_run: bool = False) -> Dict[str, Any]:
        """Bring DB up to date: purge old ephemeral nodes (v2 leftovers).

        Scans nodes where label IN ephemeral_labels (allowlist) and not in ephemeral_ignored.
        Applies same TTL (ephemeral_max_age_hours) + keep_last cap as compact_ephemeral,
        but on the graph nodes table. This cleans v2-migrated telemetry that lives as nodes
        (e.g. RUNTIME_SAMPLE, MOLTBOOK_HEARTBEAT as AGENT_NOTE) — otherwise Manager shows
        massive lists that Compact never touches (C13: telemetry vs graph split).
        Uses nodes.delete_node for df-exact vector cleanup, then purge orphans.
        """
        allow = set(self.config.get("ephemeral_labels", []))
        ignored = set(self.config.get("ephemeral_ignored", []))
        if not allow:
            out = {}
            for db in self._targets(target):
                out[db] = {"status": "success", "scanned": 0, "would_remove": 0, "removed": 0, "dry_run": dry_run, "message": "no ephemeral labels configured"}
            return out
        keep = int(self.config.get("ephemeral_keep_last", 3))
        ttl_hours = int(self.config.get("ephemeral_max_age_hours", int(self.config.get("ephemeral_max_age_days", 7)) * 24))
        cutoff = _now() - ttl_hours * 3600
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                # collect candidates: nodes with label in allowlist and not ignored
                placeholders = ",".join("?" * len(allow))
                rows = conn.execute(f"SELECT label, COUNT(*) as c FROM nodes WHERE label IN ({placeholders}) GROUP BY label", (*allow,)).fetchall()
                total_scanned = sum(r["c"] for r in rows)
                would_ttl = would_cap = 0
                per_label: Dict[str, Dict[str, int]] = {}
                # dry-run: count what would be removed
                for r in rows:
                    lab = r["label"]
                    if lab in ignored:
                        per_label[lab] = {"would_ttl": 0, "would_cap": 0, "total": r["c"]}
                        continue
                    ttl_cnt = conn.execute(f"SELECT COUNT(*) FROM nodes WHERE label = ? AND created_at < ?", (lab, cutoff)).fetchone()[0]
                    would_ttl += ttl_cnt
                    # survivors after TTL
                    survivors = conn.execute(f"SELECT COUNT(*) FROM nodes WHERE label = ? AND created_at >= ?", (lab, cutoff)).fetchone()[0]
                    # keep_last cap on survivors
                    if survivors > keep:
                        would_cap += survivors - keep
                    per_label[lab] = {"would_ttl": ttl_cnt, "would_cap": max(0, survivors - keep), "total": r["c"]}
                if dry_run:
                    out[db] = {"status": "success", "dry_run": True, "scanned": total_scanned,
                               "would_remove_ttl": would_ttl, "would_remove_cap": would_cap,
                               "would_remove_total": would_ttl + would_cap, "keep_last": keep,
                               "max_age_hours": ttl_hours, "per_label": per_label}
                    conn.rollback()
                    continue
                removed_ttl = removed_cap = 0
                for r in rows:
                    lab = r["label"]
                    if lab in ignored:
                        continue
                    # TTL: delete old nodes
                    to_del = conn.execute(f"SELECT node_id, label, content FROM nodes WHERE label = ? AND created_at < ?", (lab, cutoff)).fetchall()
                    for nd in to_del:
                        core_vectors.update_on_delete(conn, nd["node_id"], nd["label"] or "", nd["content"] or "")
                        conn.execute("DELETE FROM nodes WHERE node_id = ?", (nd["node_id"],))
                        removed_ttl += 1
                    # keep_last cap on survivors (newest keep)
                    survivors = conn.execute(f"SELECT node_id FROM nodes WHERE label = ? ORDER BY created_at DESC LIMIT ?", (lab, keep)).fetchall()
                    keep_ids = {x[0] for x in survivors}
                    if keep_ids:
                        ph = ",".join("?" * len(keep_ids))
                        extra = conn.execute(f"SELECT node_id, label, content FROM nodes WHERE label = ? AND node_id NOT IN ({ph})", (lab, *keep_ids)).fetchall()
                        for nd in extra:
                            core_vectors.update_on_delete(conn, nd["node_id"], nd["label"] or "", nd["content"] or "")
                            conn.execute("DELETE FROM nodes WHERE node_id = ?", (nd["node_id"],))
                            removed_cap += 1
                    elif keep == 0:
                        # keep 0 means delete all survivors as well
                        extra = conn.execute(f"SELECT node_id, label, content FROM nodes WHERE label = ?", (lab,)).fetchall()
                        for nd in extra:
                            core_vectors.update_on_delete(conn, nd["node_id"], nd["label"] or "", nd["content"] or "")
                            conn.execute("DELETE FROM nodes WHERE node_id = ?", (nd["node_id"],))
                            removed_cap += 1
                orphans = self._purge_orphans(conn)
                conn.commit()
                out[db] = {"status": "success", "dry_run": False, "scanned": total_scanned,
                           "removed_ttl": removed_ttl, "removed_cap": removed_cap,
                           "removed_total": removed_ttl + removed_cap, "keep_last": keep,
                           "max_age_hours": ttl_hours, "per_label": per_label, "orphans_purged": orphans}
            except Exception as e:
                conn.rollback()
                out[db] = {"status": "error", "message": f"Sync ephemeral nodes failed: {e}"}
            finally:
                conn.close()
        return out

    def get_ephemeral_stats(self, target: str = "both") -> Dict[str, Any]:
        """GROUP BY over ephemeral_events + nodes (unified telemetry view).

        Dashboard's '2 rows in list / 5 total' was ephemeral_events-only, so
        after restoring DB with 8 ephemeral labels it showed 2/5 (only
        CRON_MONITOR_REPORT exists in ephemeral_events). Now counts BOTH
        tables: ephemeral_events (new) + nodes (legacy v2 telemetry as
        AGENT_NOTE) so 'rows in list' reflects what compact will actually
        clean per config (TTL + keep_last).
        """
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                # ephemeral_events
                rows_e = conn.execute(
                    "SELECT label, COUNT(*) AS c, MIN(created_at) AS oldest, MAX(created_at) AS newest"
                    " FROM ephemeral_events GROUP BY label").fetchall()
                # nodes (legacy)
                rows_n = conn.execute(
                    "SELECT label, COUNT(*) AS c, MIN(created_at) AS oldest, MAX(created_at) AS newest"
                    " FROM nodes GROUP BY label").fetchall()
                merged: Dict[str, Dict[str, Any]] = {}
                for r in rows_e:
                    lab = r["label"]
                    try:
                        srows = conn.execute(
                            "SELECT body FROM ephemeral_events WHERE label=? ORDER BY created_at DESC LIMIT 2",
                            (lab,)).fetchall()
                        samples = [(s["body"] or "")[:120] for s in srows]
                    except Exception:
                        samples = []
                    merged[lab] = {"count": r["c"], "oldest": r["oldest"], "newest": r["newest"],
                                   "samples": samples, "ephemeral_events": r["c"], "nodes": 0}
                for r in rows_n:
                    lab = r["label"]
                    try:
                        srows = conn.execute(
                            "SELECT content FROM nodes WHERE label=? ORDER BY created_at DESC LIMIT 2",
                            (lab,)).fetchall()
                        nsamples = [(s["content"] or "")[:120] for s in srows]
                    except Exception:
                        nsamples = []
                    if lab in merged:
                        # merge counts, keep widest span and combined samples
                        prev = merged[lab]
                        merged[lab] = {
                            "count": prev["count"] + r["c"],
                            "oldest": min(prev["oldest"], r["oldest"]) if prev["oldest"] and r["oldest"] else prev["oldest"] or r["oldest"],
                            "newest": max(prev["newest"], r["newest"]) if prev["newest"] and r["newest"] else prev["newest"] or r["newest"],
                            "samples": (prev["samples"] + nsamples)[:2],
                            "ephemeral_events": prev["ephemeral_events"],
                            "nodes": r["c"],
                        }
                    else:
                        merged[lab] = {"count": r["c"], "oldest": r["oldest"], "newest": r["newest"],
                                       "samples": nsamples, "ephemeral_events": 0, "nodes": r["c"]}
                total = sum(v["count"] for v in merged.values())
                out[db] = {"total": total, "labels": merged}
            finally:
                conn.close()
        return out

    def discover_ephemeral_candidates(self, min_count: int = 3) -> Dict[str, Any]:
        """Labels in ephemeral_events + nodes NOT on either list (dashboard suggest).

        Candidates are labels not in ephemeral_labels (targeted) nor in
        ephemeral_ignored (explicitly kept) — operator decides which bucket.
        Scans BOTH ephemeral_events (telemetry table) AND nodes (legacy v2
        telemetry still stored as AGENT_NOTE) — otherwise the Ephemeral tab
        shows 0 candidates while agents.db is full of spammy nodes.
        Returns stats to explain why candidate was picked.
        """
        ephemeral = set(self.config.get("ephemeral_labels", []))
        ignored = set(self.config.get("ephemeral_ignored", []))
        stats = self.get_ephemeral_stats("both")
        cands = []
        seen = set()  # (db, label) already emitted from ephemeral_events
        for db in ("core", "agents"):
            for lab, info in stats.get(db, {}).get("labels", {}).items():
                if lab in ephemeral or lab in ignored:
                    continue
                if info["count"] >= min_count:
                    oldest = info.get("oldest")
                    newest = info.get("newest")
                    span_h = round((newest - oldest) / 3600, 1) if oldest and newest else 0
                    span_d = round(span_h / 24, 2)
                    rate = round(info["count"] / max(span_d, 0.1), 1) if span_d else info["count"]
                    cands.append({"label": lab, "count": info["count"], "db": db,
                                  "oldest": oldest, "newest": newest,
                                  "span_hours": span_h, "span_days": span_d,
                                  "per_day": rate,
                                  "samples": info.get("samples", []),
                                  "source": "ephemeral_events"})
                    seen.add((db, lab))
        # Also scan nodes table — v2 leftovers and live agent telemetry still write as nodes
        for db in ("core", "agents"):
            conn = self._conn_for(db)
            try:
                rows = conn.execute(
                    "SELECT label, COUNT(*) AS c, MIN(created_at) AS oldest, MAX(created_at) AS newest"
                    " FROM nodes GROUP BY label HAVING c >= ?", (min_count,)).fetchall()
                for r in rows:
                    lab = r["label"]
                    if lab in ephemeral or lab in ignored:
                        continue
                    if (db, lab) in seen:
                        continue
                    # fetch samples from nodes.content for context
                    try:
                        srows = conn.execute(
                            "SELECT content FROM nodes WHERE label=? ORDER BY created_at DESC LIMIT 2",
                            (lab,)).fetchall()
                        samples = [(s["content"] or "")[:120] for s in srows]
                    except Exception:
                        samples = []
                    oldest = r["oldest"]
                    newest = r["newest"]
                    span_h = round((newest - oldest) / 3600, 1) if oldest and newest else 0
                    span_d = round(span_h / 24, 2)
                    rate = round(r["c"] / max(span_d, 0.1), 1) if span_d else r["c"]
                    cands.append({"label": lab, "count": r["c"], "db": db,
                                  "oldest": oldest, "newest": newest,
                                  "span_hours": span_h, "span_days": span_d,
                                  "per_day": rate,
                                  "samples": samples,
                                  "source": "nodes"})
            finally:
                conn.close()
        # sort by count desc for decision priority
        cands.sort(key=lambda x: x["count"], reverse=True)
        return {"candidates": cands, "allowlist": sorted(ephemeral),
                "ephemeral_labels": sorted(ephemeral), "ignored_labels": sorted(ignored)}

    def set_ephemeral_allowlist(self, labels: List[str]) -> Dict[str, Any]:
        """Replace the ephemeral list (dashboard Ephemeral tab; brain config owns it)."""
        self.config["ephemeral_labels"] = sorted(set(labels))
        self._save_config()
        return {"ephemeral_labels": self.config["ephemeral_labels"]}

    def set_ephemeral_ignored(self, labels: List[str]) -> Dict[str, Any]:
        """Replace the Allowed/Ignored list — scanner skips these (not ephemeral, kept)."""
        self.config["ephemeral_ignored"] = sorted(set(labels))
        self._save_config()
        return {"ephemeral_ignored": self.config["ephemeral_ignored"]}

    def get_ephemeral_ignored(self) -> List[str]:
        return sorted(set(self.config.get("ephemeral_ignored", [])))

    # ── Job: vacuum / rebuild ──

    def vacuum_db(self, target: str = "both") -> Dict[str, Any]:
        """WAL checkpoint + VACUUM per DB on an isolated conn (under lock)."""
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            path = self._path_for(db)
            try:
                before = path.stat().st_size if path.exists() else 0
                with self.locked():
                    # Sanctioned raw-connect exception: isolated VACUUM conn.
                    conn = sqlite3.connect(str(path))
                    try:
                        conn.execute("PRAGMA foreign_keys=ON")
                        try:
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except sqlite3.Error:
                            pass
                        conn.execute("VACUUM")
                        conn.commit()
                    finally:
                        conn.close()
                after = path.stat().st_size if path.exists() else 0
                out[db] = {"status": "success", "before_bytes": before,
                           "after_bytes": after, "saved_bytes": before - after,
                           "before_mb": round(before / 1_048_576, 2),
                           "after_mb": round(after / 1_048_576, 2)}
            except Exception as e:
                out[db] = {"status": "error", "message": f"VACUUM failed: {e}"}
        return out

    def rebuild_vectors(self, target: str = "both") -> Dict[str, Any]:
        """Full TF-IDF rebuild per DB (migration / brain tick / manual only)."""
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                stats = core_vectors.rebuild_all(conn)
                conn.commit()
                out[db] = {"status": "success", **stats}
            except Exception as e:
                conn.rollback()
                out[db] = {"status": "error", "message": f"Vector rebuild failed: {e}"}
            finally:
                conn.close()
        return out

    # ── Job: contradictions (core + per-agent) ──

    def _contradiction_ledger_path(self) -> Path:
        return self.brain_dir / "contradiction_resolutions.jsonl"

    def _record_resolution(self, edge_id: str, action: str) -> None:
        try:
            with open(self._contradiction_ledger_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"edge_id": edge_id, "action": action,
                                     "at": _now()}) + "\n")
        except OSError:
            pass

    def _count_resolved(self) -> int:
        """Fix for v2's never-incrementing resolved count: append-only ledger."""
        try:
            with open(self._contradiction_ledger_path(), encoding="utf-8") as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0

    def detect_contradictions(self, target: str = "both") -> Dict[str, Any]:
        """Sentiment-clash scan (v2 algorithm port).

        Core: whole-DB scan. Agents: per-agent_id scan, never cross-agent
        (isolation; operator call 2026-09-05 — agents read their own notes, so
        an intra-agent clash confuses the agent fast and needs curation too).
        Capped scans report truncated:true + counts.
        """
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            out[db] = self._detect_core() if db == "core" else self._detect_agents()
        return out

    def _clash(self, a, b) -> Optional[Dict[str, Any]]:
        """Pure sentiment-clash math shared by the core + agents scans.

        Returns overlap/confidence metadata, or None when the pair is not a
        clash. Probability gate: 2+ shared meaningful words AND
        overlap/min_len >= contradiction_min_overlap_ratio (default 0.25) —
        pure word-count probability (operator 2026-09-05).
        """
        wa = set(_tokenize((a["label"] or "") + " " + (a["content"] or "")))
        wb = set(_tokenize((b["label"] or "") + " " + (b["content"] or "")))
        overlap = {w for w in (wa & wb) if len(w) > 3 and w not in STOPWORDS}
        if len(overlap) < 2:
            return None
        # probability gate: overlap must be substantial vs the shorter note
        wa_f = {w for w in wa if len(w) > 3 and w not in STOPWORDS}
        wb_f = {w for w in wb if len(w) > 3 and w not in STOPWORDS}
        min_len = min(len(wa_f), len(wb_f))
        if min_len == 0:
            return None
        ratio = len(overlap) / min_len
        need = float(self.config.get("contradiction_min_overlap_ratio", 0.25))
        if ratio < need:
            return None
        pos_a = len(wa & POSITIVE_WORDS)
        neg_a = len(wa & NEGATIVE_WORDS)
        pos_b = len(wb & POSITIVE_WORDS)
        neg_b = len(wb & NEGATIVE_WORDS)
        if not ((pos_a > neg_a and neg_b > pos_b)
                or (neg_a > pos_a and pos_b > neg_b)):
            return None
        imp = ((a["importance"] or 0.5) + (b["importance"] or 0.5)) / 2
        gap = abs((pos_a - neg_a) - (pos_b - neg_b))
        conf = round(min(0.98, 0.25 + min(0.6, len(overlap) * 0.15)
                         + min(0.35, gap * 0.12) + imp * 0.1), 2)
        high_value = imp >= 0.6 or a["node_type"] in ("PERSON", "PREFERENCE") \
            or b["node_type"] in ("PERSON", "PREFERENCE")
        return {"overlap_words": sorted(overlap)[:6],
                "overlap_count": len(overlap), "overlap_ratio": round(ratio, 3),
                "confidence": conf,
                "high_value": high_value, "importance_avg": round(imp, 3)}

    def _detect_core(self) -> Dict[str, Any]:
        """Core.db whole-DB scan (the original v2-port path, shapes unchanged)."""
        cap = int(self.config.get("contradiction_scan_cap", 2000))
        conn = self.core_conn()
        try:
            rows = conn.execute(
                "SELECT node_id, label, content, node_type, metadata, importance"
                " FROM nodes WHERE node_type IN ('FACT', 'PREFERENCE', 'TOPIC', 'AFFECT')").fetchall()
            cands = []
            for n in rows:
                meta = self._meta(n)
                if n["node_type"] == "AGENT_NOTE" and meta.get("attention_state") != "core_verified":
                    continue
                label = n["label"] or ""
                if label in self.config.get("ephemeral_labels", []) or \
                        _looks_like_json_log(n["content"] or ""):
                    continue
                cands.append(n)
            truncated = False
            if len(cands) > cap:
                truncated = True
                cands = cands[:cap]
            found = created = ignored = 0
            for i in range(len(cands)):
                for j in range(i + 1, len(cands)):
                    a, b = cands[i], cands[j]
                    cl = self._clash(a, b)
                    if not cl:
                        continue
                    status = "pending" if (cl["confidence"] >= 0.55
                                           or cl["high_value"]) else "ignored"
                    exists = conn.execute(
                        "SELECT edge_id FROM edges WHERE edge_type = 'CONTRADICTS'"
                        " AND ((from_node = ? AND to_node = ?) OR (from_node = ? AND to_node = ?))",
                        (a["node_id"], b["node_id"], b["node_id"], a["node_id"])).fetchone()
                    if exists:
                        continue
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO edges (edge_id, from_node, to_node,"
                            " edge_type, weight, created_at, metadata) VALUES (?, ?, ?,"
                            " 'CONTRADICTS', -0.8, ?, ?)",
                            (_edge_uuid(), a["node_id"], b["node_id"], _now(), json.dumps({
                                "detected_by": "BrainEngine", "method": "sentiment_clash",
                                "overlap_words": cl["overlap_words"],
                                "overlap_count": cl["overlap_count"],
                                "overlap_ratio": cl["overlap_ratio"],
                                "confidence": cl["confidence"], "status": status,
                                "importance_avg": cl["importance_avg"]})))
                        found += 1
                        if status == "pending":
                            created += 1
                        else:
                            ignored += 1
                    except sqlite3.IntegrityError:
                        pass
            conn.commit()
            return {"status": "success", "contradictions_found": found,
                    "edges_created": created, "edges_ignored": ignored,
                    "pending": created, "truncated": truncated,
                    "scan_cap": cap}
        except Exception as e:
            conn.rollback()
            return {"status": "error", "message": f"Contradiction detection failed: {e}"}
        finally:
            conn.close()

    def _detect_agents(self) -> Dict[str, Any]:
        """Agents.db per-agent scan (never cross-agent — isolation).

        Candidate types include AGENT_NOTE (the bulk of agents.db). Edges are
        stamped with the owning agent_id (NOT NULL). Telemetry filtered the
        same way as core (shared allowlist + json-log look).
        """
        cap = int(self.config.get("contradiction_scan_cap", 2000))
        allow = set(self.config.get("ephemeral_labels", []))
        conn = self.agents_conn()
        try:
            rows = conn.execute(
                "SELECT node_id, agent_id, label, content, node_type, importance"
                " FROM nodes WHERE node_type IN"
                " ('FACT', 'PREFERENCE', 'TOPIC', 'AFFECT', 'AGENT_NOTE')").fetchall()
            groups: Dict[str, List] = {}
            for n in rows:
                if (n["label"] or "") in allow or \
                        _looks_like_json_log(n["content"] or ""):
                    continue
                groups.setdefault(n["agent_id"], []).append(n)
            found = created = ignored = 0
            truncated = False
            per_agent: Dict[str, int] = {}
            for aid, cands in groups.items():
                if len(cands) > cap:
                    truncated = True
                    cands = cands[:cap]
                before = found
                for i in range(len(cands)):
                    for j in range(i + 1, len(cands)):
                        a, b = cands[i], cands[j]
                        cl = self._clash(a, b)
                        if not cl:
                            continue
                        status = "pending" if (cl["confidence"] >= 0.55
                                               or cl["high_value"]) else "ignored"
                        exists = conn.execute(
                            "SELECT edge_id FROM edges WHERE edge_type = 'CONTRADICTS'"
                            " AND ((from_node = ? AND to_node = ?) OR"
                            " (from_node = ? AND to_node = ?))",
                            (a["node_id"], b["node_id"],
                             b["node_id"], a["node_id"])).fetchone()
                        if exists:
                            continue
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO edges (edge_id, agent_id,"
                                " from_node, to_node, edge_type, weight, created_at,"
                                " metadata) VALUES (?, ?, ?, ?, 'CONTRADICTS',"
                                " -0.8, ?, ?)",
                                 (_edge_uuid(), aid, a["node_id"], b["node_id"],
                                 _now(), json.dumps({
                                     "detected_by": "BrainEngine",
                                     "method": "sentiment_clash",
                                     "overlap_words": cl["overlap_words"],
                                     "overlap_count": cl["overlap_count"],
                                     "overlap_ratio": cl["overlap_ratio"],
                                     "confidence": cl["confidence"],
                                     "status": status,
                                     "importance_avg": cl["importance_avg"]})))
                            found += 1
                            if status == "pending":
                                created += 1
                            else:
                                ignored += 1
                        except sqlite3.IntegrityError:
                            pass
                per_agent[aid] = found - before
            conn.commit()
            return {"status": "success", "contradictions_found": found,
                    "edges_created": created, "edges_ignored": ignored,
                    "pending": created, "truncated": truncated,
                    "scan_cap": cap, "agents_scanned": len(groups),
                    "per_agent": per_agent}
        except Exception as e:
            conn.rollback()
            return {"status": "error",
                    "message": f"Agent contradiction detection failed: {e}"}
        finally:
            conn.close()

    def get_contradictions(self, status: Optional[str] = None,
                           limit: int = 50, db: str = "core") -> Dict[str, Any]:
        """Curation queue per DB (resolved count comes from the ledger, C-fix).

        db="agents" rows carry agent_id (isolation: edges never span agents).
        """
        if db not in ("core", "agents"):
            return {"status": "error", "message": f"Unknown db {db!r}",
                    "contradictions": [], "counts": {}, "total": 0}
        low = float(self.config.get("contradiction_low_trust", 0.3))
        high = float(self.config.get("contradiction_high_trust", 0.8))
        conn = self._conn_for(db)
        try:
            cols = ("edge_id, from_node, to_node, weight, created_at, metadata"
                    + (", agent_id" if db == "agents" else ""))
            rows = conn.execute(f"SELECT {cols} FROM edges WHERE edge_type = 'CONTRADICTS'"
                                " ORDER BY created_at DESC").fetchall()
            out = []
            counts = {"pending": 0, "confirmed": 0, "ignored": 0,
                      "resolved": self._count_resolved()}
            for r in rows:
                try:
                    meta = json.loads(r["metadata"]) if r["metadata"] else {}
                except ValueError:
                    meta = {}
                st = meta.get("status", "pending")
                if st in ("pending", "confirmed", "ignored"):
                    counts[st] += 1
                else:
                    counts["pending"] += 1
                if status and status != "all" and meta.get("status", "pending") != status:
                    if not (status == "pending" and "status" not in meta):
                        continue
                frow = conn.execute("SELECT node_id, label, content, node_type, trust_level"
                                    " FROM nodes WHERE node_id = ?",
                                    (r["from_node"],)).fetchone()
                trow = conn.execute("SELECT node_id, label, content, node_type, trust_level"
                                    " FROM nodes WHERE node_id = ?", (r["to_node"],)).fetchone()
                ft = frow["trust_level"] if frow and frow["trust_level"] is not None else 0.5
                tt = trow["trust_level"] if trow and trow["trust_level"] is not None else 0.5
                suggested = None
                if ft < low and tt > high:
                    suggested = "keep_to"
                elif tt < low and ft > high:
                    suggested = "keep_from"
                out.append({"edge_id": r["edge_id"], "from_node": r["from_node"],
                            "to_node": r["to_node"], "weight": r["weight"],
                            "created_at": r["created_at"], "metadata": meta,
                            "db": db,
                            "agent_id": r["agent_id"] if db == "agents" else None,
                            "from_label": frow["label"] if frow else "?",
                            "from_content": (frow["content"] or "") if frow else "",
                            "from_type": frow["node_type"] if frow else "",
                            "from_trust": ft,
                            "to_label": trow["label"] if trow else "?",
                            "to_content": (trow["content"] or "") if trow else "",
                            "to_type": trow["node_type"] if trow else "",
                            "to_trust": tt, "suggested_action": suggested,
                            "auto_resolvable": suggested is not None})
                if len(out) >= limit:
                    break
            return {"contradictions": out, "counts": counts, "total": len(rows)}
        finally:
            conn.close()

    def update_contradiction_status(self, edge_id: str, new_status: str,
                                      db: str = "core") -> Dict[str, Any]:
        if new_status not in {"pending", "confirmed", "ignored", "resolved"}:
            return {"status": "error", "message": f"Invalid status {new_status}"}
        if db not in ("core", "agents"):
            return {"status": "error", "message": f"Unknown db {db!r}"}
        conn = self._conn_for(db)
        try:
            row = conn.execute("SELECT metadata FROM edges WHERE edge_id = ?"
                               " AND edge_type = 'CONTRADICTS'", (edge_id,)).fetchone()
            if not row:
                return {"status": "error", "message": "Edge not found"}
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except ValueError:
                meta = {}
            meta["status"] = new_status
            meta["status_updated_at"] = _now()
            conn.execute("UPDATE edges SET metadata = ? WHERE edge_id = ?",
                         (json.dumps(meta), edge_id))
            conn.commit()
            return {"status": "success", "edge_id": edge_id, "new_status": new_status}
        except Exception as e:
            conn.rollback()
            return {"status": "error", "message": str(e)}
        finally:
            conn.close()

    def resolve_contradiction(self, edge_id: str, action: str,
                                db: str = "core") -> Dict[str, Any]:
        """delete | keep_from | keep_to | merge (ports v2, df-exact deletes)."""
        if db not in ("core", "agents"):
            return {"status": "error", "message": f"Unknown db {db!r}"}
        conn = self._conn_for(db)
        try:
            row = conn.execute("SELECT from_node, to_node FROM edges WHERE edge_id = ?"
                               " AND edge_type = 'CONTRADICTS'", (edge_id,)).fetchone()
            if not row:
                return {"status": "error", "message": "Edge not found"}
            from_id, to_id = row["from_node"], row["to_node"]
            if action == "delete":
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (edge_id,))
                conn.commit()
                self._record_resolution(edge_id, action)
                return {"status": "success", "action": "delete"}
            if action in ("keep_from", "keep_to"):
                loser = to_id if action == "keep_from" else from_id
                if not core_nodes.delete_node(conn, loser):
                    return {"status": "error", "message": "Loser node missing"}
                conn.commit()
                self._record_resolution(edge_id, action)
                return {"status": "success", "action": action, "removed_node": loser}
            if action == "merge":
                f = conn.execute("SELECT content FROM nodes WHERE node_id = ?",
                                 (from_id,)).fetchone()
                t = conn.execute("SELECT content FROM nodes WHERE node_id = ?",
                                 (to_id,)).fetchone()
                if not f or not t:
                    return {"status": "error", "message": "Node missing"}
                merged = ((f["content"] or "") + " | " + (t["content"] or ""))[:800]
                conn.execute("UPDATE nodes SET content = ?, updated_at = ? WHERE node_id = ?",
                             (merged, _now(), from_id))
                core_vectors.refresh_on_content_change(
                    conn, from_id, f["content"] or "", f["content"] or "", merged, merged)
                if not core_nodes.delete_node(conn, to_id):
                    conn.rollback()
                    return {"status": "error", "message": "Node missing"}
                conn.commit()
                if self.config.get("auto_rebuild_vectors", True):
                    self.rebuild_vectors(db)
                self._record_resolution(edge_id, action)
                return {"status": "success", "action": "merge",
                        "kept": from_id, "removed": to_id}
            return {"status": "error", "message": f"Unknown action {action}"}
        except Exception as e:
            conn.rollback()
            return {"status": "error", "message": str(e)}
        finally:
            conn.close()

    def auto_resolve_low_trust(self, dry_run: bool = True,
                                 db: str = "core") -> Dict[str, Any]:
        """Opt-in auto-resolve capped at 20/run (v2 parity). Per DB."""
        if not self.config.get("contradiction_auto_resolve", False) and not dry_run:
            return {"status": "skipped",
                    "message": "Auto-resolve disabled (enable in config)"}
        data = self.get_contradictions(status="pending", limit=100, db=db)
        cands = [c for c in data.get("contradictions", []) if c.get("auto_resolvable")]
        if dry_run:
            return {"status": "success", "dry_run": True,
                    "candidates": cands, "count": len(cands), "db": db}
        resolved, details = 0, []
        for c in cands[:20]:
            if not c.get("suggested_action"):
                continue
            res = self.resolve_contradiction(c["edge_id"], c["suggested_action"],
                                             db=db)
            if res.get("status") == "success":
                resolved += 1
                details.append({"edge_id": c["edge_id"], "action": c["suggested_action"],
                                "from_trust": c["from_trust"], "to_trust": c["to_trust"]})
        if resolved and self.config.get("auto_rebuild_vectors", True):
            self.rebuild_vectors(db)
        return {"status": "success", "dry_run": False, "resolved": resolved,
                "details": details, "candidates_total": len(cands), "db": db}

    # ── Job: graduate (manual-only, via bridge) ──

    def graduate_agent_notes(self, node_ids: Optional[List[str]] = None,
                             agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Manual curation only (locked): explicit per-note or bulk Graduable.

        node_ids=None graduates ALL review_ready only (importance ignored per
        2026-09-07 — telemetry importance stays for ranking, not for graduation).
        Explicit per-note selection bypasses the gate (any agent note can be
        promoted). Graduated notes become core_verified via bridge.promote.
        """
        core = self.core_conn()
        agents = self.agents_conn()
        try:
            if node_ids:
                wanted = [(agent_id, nid) for nid in node_ids] if agent_id else None
                if wanted is None:
                    rows = agents.execute("SELECT agent_id, node_id FROM nodes"
                                          " WHERE node_type = 'AGENT_NOTE'").fetchall()
                    by_id = {r["node_id"]: r["agent_id"] for r in rows}
                    wanted = []
                    skipped = []
                    for nid in node_ids:
                        if nid in by_id:
                            wanted.append((by_id[nid], nid))
                        else:
                            skipped.append({"node_id": nid,
                                            "reason": "not found or not an agent note"})
                else:
                    skipped = []
                graduated = {}
                for aid, nid in wanted:
                    try:
                        graduated[nid] = agent_bridge.promote(core, agents, aid, nid)
                    except ValueError as e:
                        skipped.append({"node_id": nid, "reason": str(e)})
                out = {"status": "success", "graduated": len(graduated),
                       "graduated_ids": graduated}
                if skipped:
                    out["skipped"] = skipped
                return {"core": out, "agents": {"status": "success", "moved": len(graduated)}}
            rows = agents.execute(
                """SELECT agent_id, node_id FROM nodes WHERE node_type = 'AGENT_NOTE'
                   AND json_extract(metadata, '$.attention_state') = 'review_ready'""").fetchall()
            graduated = {}
            for r in rows:
                try:
                    graduated[r["node_id"]] = agent_bridge.promote(
                        core, agents, r["agent_id"], r["node_id"])
                except ValueError:
                    continue
            return {"core": {"status": "success", "graduated": len(graduated),
                             "graduated_ids": graduated},
                    "agents": {"status": "success", "moved": len(graduated)}}
        finally:
            core.close()
            agents.close()

    # ── Job: discover_links ──

    def discover_links(self, target: str = "both", offset: int = 0,
                       limit: Optional[int] = None) -> Dict[str, Any]:
        """Semantic link discovery per scope, paginated (no silent [:100]).

        Scopes: whole core.db / per-agent_id. Links only within a scope.
        Similarity band [floor, ceil): link, don't merge. agents.db edges
        stamped via agent_relate (NOT NULL safe).
        """
        floor = float(self.config.get("discover_link_floor", 0.50))
        ceil = float(self.config.get("discover_link_ceil", 0.85))
        cap = int(self.config.get("discover_scan_cap", 2000))
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                out[db] = self._discover_conn(conn, db, floor, ceil, cap, offset, limit)
                conn.commit()
            except Exception as e:
                conn.rollback()
                out[db] = {"status": "error", "message": f"Link discovery failed: {e}"}
            finally:
                conn.close()
        return out

    def _discover_conn(self, conn: sqlite3.Connection, db: str, floor: float,
                       ceil: float, cap: int, offset: int,
                       limit: Optional[int]) -> Dict[str, Any]:
        aid = "n.agent_id" if db == "agents" else "NULL AS agent_id"
        rows = conn.execute(
            "SELECT n.node_id, " + aid + ", n.label, n.content, n.node_type,"
            " n.metadata, nv.vector FROM nodes n"
            " LEFT JOIN node_vectors nv ON n.node_id = nv.node_id").fetchall()
        eph = set(self.config.get("ephemeral_labels", []))
        alive = [r for r in rows
                 if r["label"] not in eph and not _looks_like_json_log(r["content"] or "")]
        groups: Dict[Any, List] = defaultdict(list)
        for r in alive:
            key = ("agent", r["agent_id"]) if db == "agents" else ("core",)
            groups[key].append(r)
        created = 0
        scoped_total = 0
        truncated = False
        for group in groups.values():
            scoped_total += len(group)
            if len(group) <= 1:
                continue
            if len(group) > cap:
                truncated = True
                group = group[:cap]
            vecs = []
            for n in group:
                raw = n["vector"] if "vector" in n.keys() else None
                v = core_vectors.decode(raw) if raw else {}
                if not v:
                    v = {t: 1.0 for t in
                         set(_tokenize((n["label"] or "") + " " + (n["content"] or "")))}
                vecs.append(v)
            pairs = [(i, j) for i in range(len(group)) for j in range(i + 1, len(group))]
            if limit is not None:
                pairs = pairs[offset:offset + limit]
            elif offset:
                pairs = pairs[offset:]
            for i, j in pairs:
                a, b = group[i], group[j]
                sim = core_vectors.cosine(vecs[i], vecs[j])
                if not (floor <= sim < ceil):
                    continue
                exists = conn.execute(
                    "SELECT edge_id FROM edges WHERE (from_node = ? AND to_node = ?)"
                    " OR (from_node = ? AND to_node = ?)",
                    (a["node_id"], b["node_id"], b["node_id"], a["node_id"])).fetchone()
                if exists:
                    continue
                try:
                    if db == "agents":
                        agent_store.agent_relate(conn, a["agent_id"], a["node_id"],
                                                 b["node_id"], "RELATES_TO", round(sim, 2),
                                                 {"discovered_by": "BrainEngine",
                                                  "similarity": round(sim, 4)})
                    else:
                        core_edges.relate(conn, a["node_id"], b["node_id"], "RELATES_TO",
                                          round(sim, 2),
                                          {"discovered_by": "BrainEngine",
                                           "similarity": round(sim, 4)})
                    created += 1
                except (ValueError, sqlite3.IntegrityError):
                    pass
        return {"status": "success", "links_created": created,
                "scoped_nodes": scoped_total, "truncated": truncated, "scan_cap": cap,
                "offset": offset, "limit": limit}

    # ── Bloat / health / stats ──

    def get_bloat_metrics(self, target: str = "both") -> Dict[str, Any]:
        """Freelist + ephemeral-table + CONTRADICTS signals per DB (+ combined)."""
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
                total_b = page_count * page_size
                free_b = freelist * page_size
                # filtered ephemeral: only labels in Ephemeral list (both tables, unified)
                allow = set(self.config.get("ephemeral_labels", []))
                if allow:
                    ph = ",".join("?" * len(allow))
                    eph_e = conn.execute(f"SELECT COUNT(*) FROM ephemeral_events WHERE label IN ({ph})", (*allow,)).fetchone()[0]
                    eph_n = conn.execute(f"SELECT COUNT(*) FROM nodes WHERE label IN ({ph})", (*allow,)).fetchone()[0]
                    eph = eph_e + eph_n
                else:
                    eph = 0
                contr = conn.execute("SELECT COUNT(*) FROM edges"
                                     " WHERE edge_type = 'CONTRADICTS'").fetchone()[0]
                thresh = float(self.config.get("vacuum_freelist_threshold_pct", 15))
                min_pages = int(self.config.get("vacuum_freelist_min_pages", 50))
                pct = round((freelist / page_count * 100) if page_count else 0, 1)
                out[db] = {"page_count": page_count, "page_size": page_size,
                           "freelist_count": freelist,
                           "total_mb": round(total_b / 1_048_576, 2),
                           "free_mb": round(free_b / 1_048_576, 2),
                           "used_mb": round((total_b - free_b) / 1_048_576, 2),
                           "freelist_pct": pct, "ephemeral_events": eph,
                           "contradicts_total": contr,
                           "needs_vacuum": freelist > min_pages and pct > thresh,
                           "vacuum_threshold_pct": thresh, "vacuum_min_pages": min_pages}
            finally:
                conn.close()
        if target in ("both", "all"):
            out["combined"] = {
                "total_mb": round(sum(out[d]["total_mb"] for d in ("core", "agents")), 2),
                "needs_vacuum": any(out[d]["needs_vacuum"] for d in ("core", "agents"))}
        return out

    def _health_one(self, db: str) -> Dict[str, Any]:
        conn = self._conn_for(db)
        try:
            check = core_check(conn) if db == "core" else agent_store.check(conn)
            total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            node_types = dict(conn.execute(
                "SELECT node_type, COUNT(*) AS c FROM nodes GROUP BY node_type").fetchall())
            layers = dict(conn.execute(
                "SELECT layer, COUNT(*) AS c FROM memory_layers GROUP BY layer").fetchall())
            path = self._path_for(db)
            size = path.stat().st_size if path.exists() else 0
            snaps = self.list_snapshots(db)
            return {"db": db, "path": str(path), "check": check,
                    "total_nodes": total_nodes, "total_edges": total_edges,
                    "node_types": node_types, "layers": layers,
                    "db_size_bytes": size, "db_size_mb": round(size / 1_048_576, 2),
                    "snapshots": len(snaps),
                    "status": "healthy" if total_nodes > 0 else "empty"}
        finally:
            conn.close()

    def health(self, target: str = "both") -> Dict[str, Any]:
        """Per-DB + combined health (schema check, counts, snapshots)."""
        out = {db: self._health_one(db) for db in self._targets(target)}
        if target in ("both", "all"):
            out["combined"] = {
                "total_nodes": sum(out[d]["total_nodes"] for d in ("core", "agents")),
                "total_edges": sum(out[d]["total_edges"] for d in ("core", "agents")),
                "ok": bool(out["core"]["check"].get("ok")
                            and out["agents"]["check"].get("ok"))}
        return out

    def get_full_statistics(self, target: str = "both") -> Dict[str, Any]:
        """One-place overview per DB + combined (single connection each)."""
        out: Dict[str, Any] = {}
        for db in self._targets(target):
            conn = self._conn_for(db)
            try:
                total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                node_types = dict(conn.execute(
                    "SELECT node_type, COUNT(*) AS c FROM nodes GROUP BY node_type").fetchall())
                edge_types = dict(conn.execute(
                    "SELECT edge_type, COUNT(*) AS c FROM edges GROUP BY edge_type").fetchall())
                layers = dict(conn.execute(
                    "SELECT layer, COUNT(*) AS c FROM memory_layers GROUP BY layer").fetchall())
                sources = dict(conn.execute(
                    "SELECT source, COUNT(*) AS c FROM nodes GROUP BY source").fetchall())
                trust_avg = conn.execute("SELECT AVG(trust_level) FROM nodes").fetchone()[0] or 0
                imp_avg = conn.execute("SELECT AVG(importance) FROM nodes").fetchone()[0] or 0
                top_labels = [dict(r) for r in conn.execute(
                    "SELECT label, COUNT(*) AS cnt FROM nodes WHERE label != ''"
                    " GROUP BY label ORDER BY cnt DESC LIMIT 10").fetchall()]
                path = self._path_for(db)
                out[db] = {"total_nodes": total_nodes, "total_edges": total_edges,
                           "node_types": node_types, "edge_types": edge_types,
                           "layers": layers, "sources": sources,
                           "trust_avg": round(trust_avg, 3),
                           "importance_avg": round(imp_avg, 3),
                           "top_labels": top_labels,
                           "db_size_mb": round(path.stat().st_size / 1_048_576, 2)
                           if path.exists() else 0}
            finally:
                conn.close()
        if target in ("both", "all"):
            out["combined"] = {
                "total_nodes": sum(out[d]["total_nodes"] for d in ("core", "agents")),
                "total_edges": sum(out[d]["total_edges"] for d in ("core", "agents"))}
        return out

    # ── Reports & history ──

    def generate_markdown_report(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Combined audit log + per-DB journals in brain/logs/ (+ rotation).

        One run writes brain_run_<ts>.md (everything) plus
        brain_run_<ts>_<db>.md per database that the run touched, so each DB's
        story reads on its own (operator call 2026-09-05).
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"brain_run_{ts}.md"
        lines = [f"# Brain run {entry.get('timestamp', ts)}",
                 f"- target: {entry.get('target')}  |  jobs: {', '.join(entry.get('jobs', []))}",
                 f"- duration: {entry.get('duration_s', 0)}s"]
        snap = entry.get("snapshots")
        if snap:
            lines.append("- snapshots:")
            for db, info in snap.items():
                lines.append(f"  - {db}: {info}")
        for job, res in (entry.get("results") or {}).items():
            lines.append(f"## {job}")
            if isinstance(res, dict):
                for db in ("core", "agents"):
                    if db in res and isinstance(res[db], dict):
                        interesting = {k: v for k, v in res[db].items()
                                       if k not in ("status", "orphans_purged")
                                       and not isinstance(v, (list, dict))}
                        lines.append(f"- {db}: " + ", ".join(
                            f"{k}={v}" for k, v in interesting.items()))
        health = entry.get("health_after") or {}
        if health:
            lines.append("## health_after")
            for db in ("core", "agents", "combined"):
                if db in health:
                    lines.append(f"- {db}: {health[db]}")
        try:
            files = [name]
            (self.logs_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
            for db in ("core", "agents"):
                dbname, dblines = self._per_db_journal(entry, ts, db)
                if dblines is None:
                    continue
                (self.logs_dir / dbname).write_text("\n".join(dblines) + "\n",
                                                    encoding="utf-8")
                files.append(dbname)
            self._rotate_logs()
            return {"status": "success", "log_filename": name, "log_files": files}
        except OSError as e:
            return {"status": "error", "message": str(e)}

    @staticmethod
    def _per_db_journal(entry: Dict[str, Any], ts: str,
                        db: str) -> tuple:
        """Per-database journal lines, or (name, None) when untouched by the run."""
        sections = []
        for job, res in (entry.get("results") or {}).items():
            if isinstance(res, dict) and isinstance(res.get(db), dict):
                interesting = {k: v for k, v in res[db].items()
                               if k not in ("status", "orphans_purged")
                               and not isinstance(v, (list, dict))}
                sections.append((job, ", ".join(f"{k}={v}"
                                                for k, v in interesting.items())))
        health = (entry.get("health_after") or {}).get(db)
        if not sections and health is None:
            return f"brain_run_{ts}_{db}.md", None
        lines = [f"# Brain run {entry.get('timestamp', ts)} — {db}.db",
                 f"- target: {entry.get('target')}  |  jobs: {', '.join(entry.get('jobs', []))}",
                 f"- duration: {entry.get('duration_s', 0)}s"]
        snap = (entry.get("snapshots") or {}).get(db)
        lines.append(f"- snapshot: {snap if snap else 'none'}")
        for job, summary in sections:
            lines.append(f"## {job}")
            lines.append(f"- {summary}")
        if health is not None:
            lines.append("## health_after")
            lines.append(f"- {health}")
        return f"brain_run_{ts}_{db}.md", lines

    def _rotate_logs(self) -> None:
        try:
            keep = int(self.config.get("keep_last_logs", 30))
            logs = sorted(self.logs_dir.glob("brain_run_*.md"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for old in logs[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except (ValueError, OSError):
            pass

    def list_logs(self, limit: int = 20) -> List[Dict[str, Any]]:
        out = []
        for p in sorted(self.logs_dir.glob("brain_run_*.md"),
                        key=lambda q: q.stat().st_mtime, reverse=True)[:limit]:
            out.append({"filename": p.name, "size_bytes": p.stat().st_size,
                        "created_at": datetime.fromtimestamp(
                            p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
        return out

    def log_content(self, filename: str) -> Optional[str]:
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return None
        p = (self.logs_dir / filename).resolve()
        try:
            if self.logs_dir.resolve() not in p.parents:
                return None
        except OSError:
            return None
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    def get_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return data[-limit:]
        except (OSError, ValueError):
            return []

    def record_history(self, entry: Dict[str, Any], keep: int = 100) -> None:
        hist = self.get_history(keep)
        hist.append(entry)
        try:
            self.history_path.write_text(json.dumps(hist[-keep:], indent=2),
                                         encoding="utf-8")
        except OSError:
            pass

