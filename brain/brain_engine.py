"""
BRAIN ENGINE — Independent Maintenance & Management System for AshaMemory v2
=============================================================================
Provides automated & manual DB resolution, safety backups, deduplication,
tier promotion/decay, contradiction resolution, agent note graduation,
and semantic serendipity link discovery.
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import hashlib
import math
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from collections import Counter, defaultdict

# Ensure parent directory is in sys.path so asha_memory_v2 can be imported
PARENT_DIR = Path(__file__).parent.parent.resolve()
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

DEFAULT_CONFIG = {
    "last_db_path": "",
    "cron_enabled": False,
    "interval_minutes": 60,
    "auto_snapshot_before_jobs": True,
    "dedup_similarity_threshold": 0.85,
    "prune_importance_floor": 0.05,
    "auto_rebuild_vectors": True,
    "max_unused_days": 4,
    # Ephemeral / telemetry log compaction — prevents FEED_SNAPSHOT / RUNTIME_SAMPLE style bloat
    "ephemeral_labels": ["FEED_SNAPSHOT", "RUNTIME_SAMPLE", "TIME_ENTRY", "DAILY_STATE", "CRON_SUPERVISOR_REPORT", "BRAIN_MAINTENANCE_REPORT", "BRAIN_HISTORY"],
    "ephemeral_keep_last": 3,
    "ephemeral_max_age_days": 7,
    "ephemeral_min_importance": 0.6,  # below this, ephemeral logs are eligible even when "protected" type
    "vacuum_after_prune": True,
    # Auto VACUUM freelist threshold — configurable trigger for automatic vacuum
    "vacuum_freelist_threshold_pct": 15,
    "vacuum_freelist_min_pages": 50,
    # Contradiction auto-resolve (opt-in): trust gap keep high-trust
    "contradiction_auto_resolve": False,
    # P2-4 dashboard auth (optional)
    "dashboard_token": None,
    # P2-2 alias: core prune_threshold (keep in sync with prune_importance_floor)
    "prune_threshold": 0.05,
    "contradiction_low_trust": 0.3,
    "contradiction_high_trust": 0.8,
    # P3-3 rotation
    "keep_last_snapshots": 10,
    "keep_last_logs": 30,
    # P1-1: SQLite cache size (KB negative) — configurable per Asha feedback
    "sqlite_cache_size": -64000,
    # Agent working memory regulator (agent-only, core untouched)
    "agent_working_regulator_enabled": True,
    "agent_working_high_water": 12,
    "agent_working_demote_batch": 5,
    "agent_working_max_age_hours": 48,
    "agent_working_weight_access": 1.5,
    "agent_working_weight_importance": 4.0,
    "agent_working_weight_age": 0.15,
}

try:
    from asha_memory_v2 import AshaMemory, TfidfVectorizer, _now, _edge_uuid, MEMORY_LAYERS
    from shared_lexicon import _tokenize, POSITIVE_WORDS, NEGATIVE_WORDS, _looks_like_json_log, STOPWORDS
except ImportError:
    # Fallback: ensure shared_lexicon is importable even when executed directly
    try:
        from shared_lexicon import _tokenize, POSITIVE_WORDS, NEGATIVE_WORDS, _looks_like_json_log, STOPWORDS  # type: ignore
    except ImportError:
        _tokenize = lambda text: [w.lower() for w in re.findall(r"\b[\w']{2,}\b", text.lower()) if len(w) >= 2]  # unified fallback
        POSITIVE_WORDS = set()
        NEGATIVE_WORDS = set()
        STOPWORDS = set()
        _looks_like_json_log = lambda content: False  # type: ignore
    AshaMemory = None
    TfidfVectorizer = None
    _now = lambda: int(time.time())
    _edge_uuid = lambda: f"edge_{int(time.time()*1000)}"
    MEMORY_LAYERS = {
        "working": {"decay": 1.0, "boost": 0.0, "capacity": 20},
        "short_term": {"decay": 0.97, "boost": 0.10, "capacity": 500},
        "long_term": {"decay": 0.995, "boost": 0.05, "capacity": 5000},
        "archive": {"decay": 1.0, "boost": 0.0, "capacity": None},
    }

# Sentiment lists now imported from shared_lexicon (single source) — see try/except above


class BrainEngine:
    """
    Independent maintenance engine operating on AshaMemory databases.
    Decoupled from runtime AI recall loops.
    """

    def __init__(self, db_path: Optional[str] = None, brain_dir: Optional[str] = None):
        self.brain_dir = Path(brain_dir or Path(__file__).parent).resolve()
        self.snapshots_dir = self.brain_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.brain_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.brain_dir / "brain_config.json"

        # Load or initialize config
        self.config = self._load_config()

        # Resolve target DB path
        self.db_path = self.resolve_db_path(db_path)
        self._ensure_schema()
        self.config["last_db_path"] = str(self.db_path)
        # P0-3: unified ephemeral allowlist — refresh from core config after DB resolution
        try:
            self._refresh_ephemeral_from_core()
        except Exception:
            pass
        self._save_config()

    def _ensure_schema(self):
        """Ensures target database has valid AshaMemory v2 tables initialized."""
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            if AshaMemory:
                try:
                    AshaMemory(base_path=str(self.db_path.parent))
                except Exception as e:
                    print(f"[BrainEngine] Schema initialization error: {e}")
        else:
            try:
                conn = self._connect_db()
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'")
                table_exists = cursor.fetchone()
                conn.close()
                if not table_exists and AshaMemory:
                    AshaMemory(base_path=str(self.db_path.parent))
            except Exception as e:
                print(f"[BrainEngine] Schema check error: {e}")

    def _connect_db(self) -> sqlite3.Connection:
        """Open DB with WAL + foreign_keys + cache_size (single place)."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        try:
            cs = int(self.config.get("sqlite_cache_size", -64000))
            conn.execute(f"PRAGMA cache_size={cs}")
        except Exception:
            try:
                conn.execute("PRAGMA cache_size=-64000")
            except Exception:
                pass
        return conn

    # ──────────────────────────────────────────────────────────────────────────
    # CORE vs AGENT-NOTE CLASSIFICATION
    # ──────────────────────────────────────────────────────────────────────────
    # Agent notes (raw agent work, attention-scoped) are NOT core memory. They
    # live in the same core.db but stay outside the main AI's recall scope.
    # All maintenance must respect that boundary: never merge, link, or
    # graduate across it. Mirrors asha_memory_v2.AshaMemory._is_core_visible.
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _node_metadata(row) -> Dict[str, Any]:
        """Parse the metadata column of a sqlite3.Row or dict row."""
        raw = row["metadata"] if "metadata" in row.keys() else None
        if isinstance(raw, str):
            try:
                return json.loads(raw) or {}
            except Exception:
                return {}
        return raw or {}

    def is_agent_note(self, row) -> bool:
        """
        True if the node is agent-scoped work (AGENT_NOTE type or agent_scoped
        metadata), excluding notes already promoted/graduated to core memory
        (attention_state == 'core_verified').
        """
        meta = self._node_metadata(row)
        if meta.get("attention_state") == "core_verified":
            return False
        if row["node_type"] == "AGENT_NOTE":
            return True
        return bool(meta.get("agent_scoped"))

    def _classify_rows(self, rows) -> Tuple[List, List]:
        """Split sqlite rows into (core_rows, agent_note_rows)."""
        core, agent = [], []
        for row in rows:
            (agent if self.is_agent_note(row) else core).append(row)
        return core, agent

    # ── Ephemeral / telemetry detection ──────────────────────────────────────
    def _is_ephemeral_label(self, label: str) -> bool:
        """True if label is in the configured ephemeral list (FEED_SNAPSHOT, etc.)."""
        labels = self.config.get("ephemeral_labels", [])
        return label in labels

    def _looks_like_json_log(self, content: str) -> bool:
        """Delegate to shared_lexicon (single heuristic)."""
        return _looks_like_json_log(content)  # imported from shared_lexicon

    def _is_ephemeral_row(self, row) -> bool:
        """Ephemeral = telemetry log that should be capped/rolled, not kept forever."""
        label = row["label"] if "label" in row.keys() else ""
        if self._is_ephemeral_label(label):
            return True
        # fallback: unlabeled JSON logs with same shape (e.g. capped content still JSON)
        content = row["content"] if "content" in row.keys() else ""
        return self._looks_like_json_log(content)

    def get_ephemeral_stats(self, conn: sqlite3.Connection = None) -> Dict[str, Any]:
        """Per-label counts for ephemeral nodes + total JSON-log nodes."""
        should_close = False
        if conn is None:
            conn = self._connect_db()
            should_close = True
        try:
            stats = {}
            labels = self.config.get("ephemeral_labels", [])
            for label in labels:
                c = conn.execute("SELECT COUNT(*) FROM nodes WHERE label = ?", (label,)).fetchone()[0]
                if c:
                    stats[label] = c
            # catch unlabeled JSON logs not in list
            try:
                json_cnt = conn.execute("SELECT COUNT(*) FROM nodes WHERE content LIKE '{\"%timestamp\"%' OR content LIKE '{%\"load1m\"%'").fetchone()[0]
            except Exception:
                json_cnt = 0
            stats["_total_ephemeral_labels"] = sum(stats.values())
            stats["_json_log_nodes"] = json_cnt
            stats["_keep_last"] = self.config.get("ephemeral_keep_last", 3)
            stats["_max_age_days"] = self.config.get("ephemeral_max_age_days", 7)
            return stats
        finally:
            if should_close:
                conn.close()

    # ── Ephemeral auto-discovery (safe, suggest-only) ──────────────────────────
    def discover_ephemeral_candidates(self, min_count: int = 3, min_json_ratio: float = 0.6) -> Dict[str, Any]:
        """Scan labels not yet in allowlist and suggest likely telemetry logs.
        Heuristics: high frequency, JSON shape, low importance, few edges, UPPER_SNAKE label.
        Returns candidates for dashboard Ephemeral tab — never auto-deletes.
        """
        conn = self._connect_db()
        try:
            allow = set(self.config.get("ephemeral_labels", []))
            # all labels with count
            rows = conn.execute("SELECT label, COUNT(*) as cnt FROM nodes WHERE label IS NOT NULL AND label != '' GROUP BY label HAVING cnt >= ? ORDER BY cnt DESC", (min_count,)).fetchall()
            candidates = []
            for r in rows:
                label = r["label"]
                if label in allow:
                    continue
                cnt = r["cnt"]
                # sample up to 5 nodes for this label
                samples = conn.execute("SELECT content, importance, node_type, source FROM nodes WHERE label = ? LIMIT 5", (label,)).fetchall()
                json_hits = sum(1 for s in samples if self._looks_like_json_log(s["content"] or ""))
                json_ratio = json_hits / max(len(samples), 1)
                avg_imp = sum((s["importance"] or 0) for s in samples) / max(len(samples), 1)
                # edge ratio — how many of the sampled nodes have edges
                edge_cnt = 0
                for s in samples:
                    # need node_id — fetch separately for edge check
                    pass
                # get node_ids for edge check
                node_ids = [x["node_id"] for x in conn.execute("SELECT node_id FROM nodes WHERE label = ? LIMIT 5", (label,)).fetchall()]
                edge_hits = 0
                for nid in node_ids:
                    ec = conn.execute("SELECT COUNT(*) FROM edges WHERE from_node = ? OR to_node = ?", (nid, nid)).fetchone()[0]
                    if ec > 0:
                        edge_hits += 1
                edge_ratio = edge_hits / max(len(node_ids), 1)
                # UPPER_SNAKE heuristic
                is_upper_snake = bool(label and label.upper() == label and ("_" in label or label.isupper()))
                # burst heuristic — many rows share same updated_at day?
                score = 0
                reasons = []
                if json_ratio >= min_json_ratio:
                    score += 3
                    reasons.append(f"json {json_ratio:.0%}")
                if cnt >= 10:
                    score += 2
                    reasons.append(f"freq {cnt}")
                elif cnt >= min_count:
                    score += 1
                if avg_imp < 0.5:
                    score += 1
                    reasons.append(f"low imp {avg_imp:.2f}")
                if edge_ratio < 0.2:
                    score += 1
                    reasons.append("few edges")
                if is_upper_snake:
                    score += 1
                    reasons.append("UPPER_SNAKE")
                # only suggest if score >=3 and (json_ratio high or high freq + low imp)
                if score >= 3 and (json_ratio >= 0.4 or cnt >= 10):
                    candidates.append({
                        "label": label,
                        "count": cnt,
                        "json_ratio": round(json_ratio, 2),
                        "avg_importance": round(avg_imp, 3),
                        "edge_ratio": round(edge_ratio, 2),
                        "score": score,
                        "reasons": ", ".join(reasons),
                        "sample_content": (samples[0]["content"] or "")[:120] if samples else "",
                    })
            candidates.sort(key=lambda x: (-x["score"], -x["count"]))
            return {"candidates": candidates[:20], "total_labels_scanned": len(rows), "min_count": min_count, "allowlist": sorted(allow)}
        finally:
            conn.close()

    def add_ephemeral_label(self, label: str) -> Dict[str, Any]:
        label = (label or "").strip()
        if not label:
            return {"status": "error", "message": "Label required"}
        lst = self.config.get("ephemeral_labels", [])
        if label in lst:
            return {"status": "success", "message": "Already in allowlist", "ephemeral_labels": lst}
        lst.append(label)
        self.config["ephemeral_labels"] = lst
        self._save_config()
        # also sync to core config.json for unified allowlist P0-3
        try:
            core_cfg = self.db_path.parent / "config.json"
            if core_cfg.exists():
                core_data = json.loads(core_cfg.read_text(encoding="utf-8"))
                core_list = core_data.get("ephemeral_labels", [])
                if label not in core_list:
                    core_list.append(label)
                    core_data["ephemeral_labels"] = sorted(set(core_list))
                    core_cfg.write_text(json.dumps(core_data, indent=2), encoding="utf-8")
        except Exception:
            pass
        return {"status": "success", "ephemeral_labels": lst}

    def remove_ephemeral_label(self, label: str) -> Dict[str, Any]:
        lst = self.config.get("ephemeral_labels", [])
        if label not in lst:
            return {"status": "error", "message": "Not in allowlist"}
        lst = [x for x in lst if x != label]
        self.config["ephemeral_labels"] = lst
        self._save_config()
        try:
            core_cfg = self.db_path.parent / "config.json"
            if core_cfg.exists():
                core_data = json.loads(core_cfg.read_text(encoding="utf-8"))
                core_list = core_data.get("ephemeral_labels", [])
                if label in core_list:
                    core_list = [x for x in core_list if x != label]
                    core_data["ephemeral_labels"] = core_list
                    core_cfg.write_text(json.dumps(core_data, indent=2), encoding="utf-8")
        except Exception:
            pass
        return {"status": "success", "ephemeral_labels": lst}

    def get_full_statistics(self) -> Dict[str, Any]:
        """Aggregated stats for dedicated Statistics tab — one place overview."""
        conn = self._connect_db()
        try:
            total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            node_types = dict(conn.execute("SELECT node_type, COUNT(*) as c FROM nodes GROUP BY node_type").fetchall())
            edge_types = dict(conn.execute("SELECT edge_type, COUNT(*) as c FROM edges GROUP BY edge_type").fetchall())
            layers = dict(conn.execute("SELECT layer, COUNT(*) as c FROM memory_layers GROUP BY layer").fetchall())
            sources = dict(conn.execute("SELECT source, COUNT(*) as c FROM nodes GROUP BY source").fetchall())
            # trust/importance buckets
            trust_avg = conn.execute("SELECT AVG(trust_level) FROM nodes").fetchone()[0] or 0
            imp_avg = conn.execute("SELECT AVG(importance) FROM nodes").fetchone()[0] or 0
            # core vs agent
            rows = conn.execute("SELECT node_type, metadata FROM nodes").fetchall()
            core, agent = self._classify_rows(rows)
            # top labels
            top_labels = [dict(r) for r in conn.execute("SELECT label, COUNT(*) as cnt FROM nodes WHERE label != '' GROUP BY label ORDER BY cnt DESC LIMIT 10").fetchall()]
            bloat = {}
            try:
                bloat = self.get_bloat_metrics()
            except Exception:
                pass
            ephemeral = {}
            try:
                ephemeral = self.get_ephemeral_stats(conn)
            except Exception:
                pass
            return {
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "node_types": node_types,
                "edge_types": edge_types,
                "layers": layers,
                "sources": sources,
                "trust_avg": round(trust_avg, 3),
                "importance_avg": round(imp_avg, 3),
                "core_nodes": len(core),
                "agent_note_nodes": len(agent),
                "top_labels": top_labels,
                "bloat": bloat,
                "ephemeral": ephemeral,
                "db_size_mb": round(self.db_path.stat().st_size / (1024*1024), 2) if self.db_path.exists() else 0,
            }
        finally:
            conn.close()

    def _merge_agent_metadata(self, primary_meta: Dict, secondary_meta: Dict) -> Dict:
        """
        Merge two agent-note metadata dicts, preserving provenance and the
        most prominent attention state (core_verified > review_ready > agent_private).
        """
        merged = dict(primary_meta)
        agent_ids = set()
        for meta in (primary_meta, secondary_meta):
            aid = meta.get("agent_id")
            if aid:
                agent_ids.add(str(aid))
        if len(agent_ids) > 1:
            merged["agent_ids"] = sorted(agent_ids)
            merged["agent_id"] = primary_meta.get("agent_id") or secondary_meta.get("agent_id")
        priority = {"core_verified": 3, "review_ready": 2, "agent_private": 1}
        states = [m.get("attention_state") for m in (primary_meta, secondary_meta)]
        best = max(states, key=lambda s: priority.get(s, 0))
        if best and best != merged.get("attention_state"):
            merged["attention_state"] = best
        return merged

    def _relink_and_delete(self, conn: sqlite3.Connection, primary_id: str,
                           secondary_id: str) -> int:
        """
        Re-point all edges from secondary to primary, deleting primary with the
        secondary node. Conflicting edges (same from/to/type already pointing at
        primary) and self-loops are dropped instead of re-pointed, because the
        edges table enforces UNIQUE(from_node, to_node, edge_type).
        """
        relinked = 0

        for row in conn.execute(
                "SELECT edge_id, to_node, edge_type FROM edges WHERE from_node = ?",
                (secondary_id,)).fetchall():
            if row["to_node"] == secondary_id or row["to_node"] == primary_id:
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
                continue
            conflict = conn.execute(
                "SELECT edge_id FROM edges WHERE from_node = ? AND to_node = ? AND edge_type = ?",
                (primary_id, row["to_node"], row["edge_type"])).fetchone()
            if conflict:
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
            else:
                conn.execute("UPDATE edges SET from_node = ? WHERE edge_id = ?",
                             (primary_id, row["edge_id"]))
                relinked += 1

        for row in conn.execute(
                "SELECT edge_id, from_node, edge_type FROM edges WHERE to_node = ?",
                (secondary_id,)).fetchall():
            if row["from_node"] == secondary_id:
                continue  # self-loop already removed above
            conflict = conn.execute(
                "SELECT edge_id FROM edges WHERE from_node = ? AND to_node = ? AND edge_type = ?",
                (row["from_node"], primary_id, row["edge_type"])).fetchone()
            if conflict:
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
            else:
                conn.execute("UPDATE edges SET to_node = ? WHERE edge_id = ?",
                             (primary_id, row["edge_id"]))
                relinked += 1

        conn.execute("DELETE FROM nodes WHERE node_id = ?", (secondary_id,))
        # Explicit orphan cleanup (FK cascades are not enforced without PRAGMA foreign_keys=ON)
        conn.execute("DELETE FROM node_vectors WHERE node_id = ?", (secondary_id,))
        conn.execute("DELETE FROM memory_layers WHERE node_id = ?", (secondary_id,))
        conn.execute("DELETE FROM access_log WHERE node_id = ?", (secondary_id,))
        conn.execute("DELETE FROM node_index WHERE node_id = ?", (secondary_id,))
        return relinked

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 1: DB PATH RESOLUTION & SAFETY SNAPSHOTS
    # ──────────────────────────────────────────────────────────────────────────

    def resolve_db_path(self, custom_path: Optional[str] = None) -> Path:
        """
        Auto-discovers or sets the target SQLite core.db path.
        Priority:
        1. Explicit custom_path passed to method
        2. Environment variable ASHA_MEMORY_DB_PATH
        3. Configured path in brain_config.json
        4. Auto-discovery candidates in standard locations
        """
        if custom_path:
            p = Path(custom_path).resolve()
            if p.exists() and p.is_file():
                return p
            elif p.is_dir():
                cand = p / "core.db"
                if cand.exists():
                    return cand

        # Environment variable override
        env_path = os.environ.get("ASHA_MEMORY_DB_PATH")
        if env_path:
            ep = Path(env_path).resolve()
            if ep.exists():
                return ep

        # Saved config path
        saved = self.config.get("last_db_path")
        if saved and Path(saved).exists():
            return Path(saved).resolve()

        # Auto-discovery candidates
        search_dirs = [
            self.brain_dir.parent / "asha_memory" / "core.db",
            self.brain_dir.parent / "core.db",
            Path.cwd() / "asha_memory" / "core.db",
            Path.cwd() / "core.db",
            self.brain_dir / "core.db",
        ]

        for cand in search_dirs:
            if cand.exists() and cand.is_file():
                return cand.resolve()

        # Fallback: Default path (creates directory if missing)
        fallback = self.brain_dir.parent / "asha_memory" / "core.db"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback.resolve()

    def find_available_databases(self) -> List[Dict[str, Any]]:
        """Scans workspace directories to find all existing AshaMemory SQLite .db files."""
        found = []
        root_dir = self.brain_dir.parent

        try:
            for path in root_dir.glob("**/*.db"):
                if "snapshots" in path.parts:
                    continue  # Skip snapshot backups
                size_bytes = path.stat().st_size
                mtime = int(path.stat().st_mtime)
                # Check if it has Asha schema
                is_asha = False
                try:
                    conn = sqlite3.connect(str(path))
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'")
                    if cursor.fetchone():
                        is_asha = True
                    conn.close()
                except Exception:
                    pass

                found.append({
                    "path": str(path.resolve()),
                    "filename": path.name,
                    "size_bytes": size_bytes,
                    "mtime": mtime,
                    "mtime_str": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "is_asha_db": is_asha,
                    "is_current": str(path.resolve()) == str(self.db_path),
                })
        except Exception as e:
            print(f"[BrainEngine] Error scanning databases: {e}")

        found.sort(key=lambda x: (-x["is_asha_db"], -x["mtime"]))
        return found

    def set_target_db(self, db_path: str) -> bool:
        """Switch the current target database. Handles file or directory containing core.db, relative or absolute."""
        if not db_path or not str(db_path).strip():
            return False
        raw = str(db_path).strip().strip('"').strip("'")
        # Try as given, then resolve
        candidates = []
        try:
            p = Path(raw)
            # If relative, try relative to brain parent and cwd
            if not p.is_absolute():
                candidates.append((Path.cwd() / raw).resolve())
                candidates.append((self.brain_dir.parent / raw).resolve())
                candidates.append((self.brain_dir / raw).resolve())
            candidates.append(p.resolve())
            # Also try with core.db appended if it's a directory
            expanded = []
            for cand in candidates:
                expanded.append(cand)
                if cand.is_dir():
                    expanded.append((cand / "core.db").resolve())
                # If cand is file without .db extension but exists as dir+core.db
                if not cand.exists() and cand.suffix == "":
                    expanded.append((cand / "core.db").resolve())
            for target in expanded:
                if target.exists() and target.is_file():
                    # Validate it's an Asha DB (has nodes table) or at least exists
                    try:
                        # quick check for nodes table
                        conn = sqlite3.connect(str(target))
                        cur = conn.cursor()
                        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'")
                        has_nodes = cur.fetchone() is not None
                        conn.close()
                        if not has_nodes:
                            # allow but warn - still switch, will init schema
                            pass
                    except Exception:
                        pass
                    self.db_path = target
                    self.config["last_db_path"] = str(target)
                    self._save_config()
                    try:
                        self._refresh_ephemeral_from_core()
                    except Exception:
                        pass
                    # ensure schema exists
                    try:
                        self._ensure_schema()
                    except Exception:
                        pass
                    return True
        except Exception as e:
            print(f"[BrainEngine] set_target_db error for '{db_path}': {e}")
        return False

    def create_snapshot(self, db_path: Optional[str] = None) -> Dict[str, Any]:
        """Creates a timestamped safety backup of the database before any mutation."""
        target_db = Path(db_path or self.db_path).resolve()
        if not target_db.exists():
            return {"status": "error", "message": f"Database file not found at {target_db}"}

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_name = f"snapshot_{target_db.stem}_{timestamp}.db"
        dest_path = self.snapshots_dir / snapshot_name

        try:
            # Use SQLite backup API for live-safe backup
            src_conn = sqlite3.connect(str(target_db))
            dst_conn = sqlite3.connect(str(dest_path))
            src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()

            snapshot_info = {
                "status": "success",
                "filename": snapshot_name,
                "path": str(dest_path),
                "original_db": str(target_db),
                "timestamp": timestamp,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "size_bytes": dest_path.stat().st_size,
            }
            # P3-3 rotation: prune old snapshots
            try:
                keep = int(self.config.get("keep_last_snapshots", 10))
                snaps = sorted(self.snapshots_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
                for old in snaps[keep:]:
                    try:
                        old.unlink()
                    except Exception:
                        pass
            except Exception:
                pass
            return snapshot_info
        except Exception as e:
            return {"status": "error", "message": f"Snapshot failed: {str(e)}"}

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """Lists all existing safety backups in the snapshots folder."""
        snapshots = []
        for path in self.snapshots_dir.glob("*.db"):
            size_bytes = path.stat().st_size
            mtime = int(path.stat().st_mtime)
            snapshots.append({
                "filename": path.name,
                "path": str(path.resolve()),
                "size_bytes": size_bytes,
                "created_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "mtime": mtime,
            })
        snapshots.sort(key=lambda x: -x["mtime"])
        return snapshots

    def restore_snapshot(self, snapshot_filename: str) -> Dict[str, Any]:
        """1-Click Rollback: Restores a snapshot file to the current target database path."""
        snapshot_path = self.snapshots_dir / snapshot_filename
        if not snapshot_path.exists():
            return {"status": "error", "message": f"Snapshot file {snapshot_filename} not found"}

        try:
            # Create safety backup of current state before rollback
            backup_before_rollback = f"prerollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            dest_prerollback = self.snapshots_dir / backup_before_rollback
            shutil.copy2(str(self.db_path), str(dest_prerollback))

            # Perform restore
            src_conn = sqlite3.connect(str(snapshot_path))
            dst_conn = sqlite3.connect(str(self.db_path))
            src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()

            return {
                "status": "success",
                "message": f"Restored {snapshot_filename} to {self.db_path}",
                "pre_rollback_backup": backup_before_rollback,
            }
        except Exception as e:
            return {"status": "error", "message": f"Restore failed: {str(e)}"}

    def delete_snapshot(self, snapshot_filename: str) -> Dict[str, Any]:
        """Delete an unwanted snapshot backup."""
        # guard traversal
        if ".." in snapshot_filename or "/" in snapshot_filename or "\\" in snapshot_filename:
            return {"status": "error", "message": "Invalid filename"}
        snapshot_path = self.snapshots_dir / snapshot_filename
        if not snapshot_path.exists():
            return {"status": "error", "message": f"Snapshot file {snapshot_filename} not found"}
        # never delete prerollback? allow but warn — keep check simple
        try:
            size = snapshot_path.stat().st_size
            snapshot_path.unlink()
            return {"status": "success", "message": f"Deleted {snapshot_filename}", "size_bytes": size}
        except Exception as e:
            return {"status": "error", "message": f"Delete failed: {str(e)}"}

    def commit_manager_db(self, data: bytes) -> Dict[str, Any]:
        """Apply Manager edits directly to DB — validates SQLite, snapshots before overwrite."""
        if not data or len(data) < 1024:
            return {"status": "error", "message": "Invalid DB data (too small)"}
        # quick SQLite header check
        if data[:16] != b"SQLite format 3\x00":
            return {"status": "error", "message": "Not a valid SQLite file"}
        # safety snapshot before mutate
        snap = None
        try:
            snap = self.create_snapshot()
        except Exception:
            pass
        # write to temp then atomic replace
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
                tf.write(data)
                tf_path = Path(tf.name)
            # validate temp DB has nodes table
            conn = sqlite3.connect(str(tf_path))
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'")
            if not cur.fetchone():
                conn.close()
                tf_path.unlink(missing_ok=True)
                return {"status": "error", "message": "DB missing nodes table"}
            conn.execute("PRAGMA integrity_check")
            check = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            if check and check[0] != "ok":
                tf_path.unlink(missing_ok=True)
                return {"status": "error", "message": f"Integrity check failed: {check[0]}"}
            # atomic replace
            shutil.copy2(str(tf_path), str(self.db_path))
            tf_path.unlink(missing_ok=True)
            return {"status": "success", "message": "DB updated from Manager", "snapshot": snap.get("filename") if snap and snap.get("status")=="success" else None, "size_bytes": len(data)}
        except Exception as e:
            return {"status": "error", "message": f"Commit failed: {str(e)}"}

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 2: DEDUPLICATION & TIER LIFECYCLE MANAGEMENT
    # ──────────────────────────────────────────────────────────────────────────

    def deduplicate(self, similarity_threshold: float = 0.85, auto_snapshot: bool = True) -> Dict[str, Any]:
        """
        Scans memory graph for exact checksum/label duplicates and near-duplicate TF-IDF content.
        Merges redundant nodes, consolidates content, updates edge pointers, and removes duplicates.

        Core memory and agent notes are deduplicated as separate scopes: a node is
        only ever merged with another node of the same scope, so agent work can
        never be absorbed into core memory (or vice versa). Agent metadata
        (agent_id, attention_state) is preserved when agent notes merge.
        """
        if auto_snapshot:
            self.create_snapshot()

        exact_merged = 0
        semantic_merged = 0
        exact_merged_agent = 0
        semantic_merged_agent = 0
        edges_relinked = 0

        conn = self._connect_db()

        try:
            # 1. Exact Checksum / Label & Content Deduplication (per scope)
            cursor = conn.execute("SELECT node_id, label, content, checksum, access_count, importance, node_type, metadata FROM nodes")
            nodes = cursor.fetchall()
            core_rows, agent_rows = self._classify_rows(nodes)

            for group, is_agent in ((core_rows, False), (agent_rows, True)):
                checksum_map = defaultdict(list)
                for n in group:
                    key = (n["checksum"], n["label"].strip().lower(), n["content"].strip().lower())
                    checksum_map[key].append(n)

                for key, dupes in checksum_map.items():
                    if len(dupes) > 1:
                        primary = dupes[0]
                        for secondary in dupes[1:]:
                            if is_agent:
                                primary_meta = self._node_metadata(primary)
                                secondary_meta = self._node_metadata(secondary)
                                merged_meta = self._merge_agent_metadata(primary_meta, secondary_meta)
                                if merged_meta != primary_meta:
                                    conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?",
                                                 (json.dumps(merged_meta), primary["node_id"]))
                            edges_relinked += self._relink_and_delete(conn, primary["node_id"], secondary["node_id"])
                            if is_agent:
                                exact_merged_agent += 1
                            else:
                                exact_merged += 1

            conn.commit()

            # 2. Near-Duplicate Vector Similarity Merging (per scope)
            cursor = conn.execute("""
                SELECT n.node_id, n.label, n.content, n.access_count, n.importance,
                       n.node_type, n.metadata, nv.vector
                FROM nodes n
                LEFT JOIN node_vectors nv ON n.node_id = nv.node_id
            """)
            remaining = cursor.fetchall()
            core_rows, agent_rows = self._classify_rows(remaining)

            for group, is_agent in ((core_rows, False), (agent_rows, True)):
                if not TfidfVectorizer or len(group) <= 1:
                    continue
                v = TfidfVectorizer()
                texts = [(n["label"] or "") + " " + (n["content"] or "") for n in group]
                v.fit(texts)

                vectors = []
                for n in group:
                    if n["vector"]:
                        vec = json.loads(n["vector"])
                    else:
                        vec = v.transform((n["label"] or "") + " " + (n["content"] or ""))
                    vectors.append(vec)

                deleted = set()
                for i in range(len(group)):
                    if group[i]["node_id"] in deleted:
                        continue
                    for j in range(i + 1, len(group)):
                        if group[j]["node_id"] in deleted:
                            continue

                        node_a, node_b = group[i], group[j]
                        sim = v.cosine_similarity(vectors[i], vectors[j])

                        if sim >= similarity_threshold:
                            # Keep primary (node with higher access or importance)
                            if (node_b["access_count"] + node_b["importance"]) > (node_a["access_count"] + node_a["importance"]):
                                primary, secondary = node_b, node_a
                            else:
                                primary, secondary = node_a, node_b

                            # Merge content if secondary has unique text
                            new_content = primary["content"]
                            if secondary["content"] not in primary["content"]:
                                new_content = f"{primary['content']} | {secondary['content']}"

                            conn.execute("UPDATE nodes SET content = ?, updated_at = ? WHERE node_id = ?",
                                         (new_content[:500], _now(), primary["node_id"]))

                            # Preserve agent provenance when merging agent notes
                            if is_agent:
                                primary_meta = self._node_metadata(primary)
                                secondary_meta = self._node_metadata(secondary)
                                merged_meta = self._merge_agent_metadata(primary_meta, secondary_meta)
                                if merged_meta != primary_meta:
                                    conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?",
                                                 (json.dumps(merged_meta), primary["node_id"]))

                            edges_relinked += self._relink_and_delete(conn, primary["node_id"], secondary["node_id"])
                            deleted.add(secondary["node_id"])
                            if is_agent:
                                semantic_merged_agent += 1
                            else:
                                semantic_merged += 1

            conn.commit()
            conn.close()

            return {
                "status": "success",
                "exact_merged": exact_merged,
                "semantic_merged": semantic_merged,
                "total_merged": exact_merged + semantic_merged,
                "exact_merged_agent": exact_merged_agent,
                "semantic_merged_agent": semantic_merged_agent,
                "total_merged_agent": exact_merged_agent + semantic_merged_agent,
                "edges_relinked": edges_relinked,
            }
        except Exception as e:
            conn.close()
            return {"status": "error", "message": f"Deduplication failed: {str(e)}"}

    def manage_tiers(self, auto_snapshot: bool = True) -> Dict[str, Any]:
        """
        Manages memory node lifecycles across layers (working -> short_term -> long_term -> archive).
        Promotes hot nodes, applies exponential decay to stale nodes, and prunes dead nodes.

        Core memory and agent notes share the tier lifecycle (both are memory) but
        are tracked and reported separately. Agent notes awaiting review
        (attention_state == 'review_ready') are never pruned here.
        """
        if auto_snapshot:
            self.create_snapshot()

        promoted_core = 0
        promoted_agent = 0
        decayed_core = 0
        decayed_agent = 0
        pruned_core = 0
        pruned_agent = 0
        skipped_review_ready = 0
        skipped_protected = 0

        conn = self._connect_db()

        try:
            # 1. Promote hot short_term nodes to long_term
            cursor = conn.execute("""
                SELECT n.node_id, n.access_count, n.importance, n.node_type, n.metadata, ml.layer
                FROM nodes n
                JOIN memory_layers ml ON n.node_id = ml.node_id
                WHERE ml.layer = 'short_term'
            """)
            for row in cursor.fetchall():
                if row["access_count"] >= 3 or row["importance"] >= 0.8:
                    conn.execute("""
                        UPDATE memory_layers
                        SET layer = 'long_term', layer_order = 3, promoted_at = ?
                        WHERE node_id = ?
                    """, (_now(), row["node_id"]))
                    if self.is_agent_note(row):
                        promoted_agent += 1
                    else:
                        promoted_core += 1

            # 2. Layer Decay Calculation
            cursor = conn.execute("""
                SELECT n.node_id, n.importance, n.access_count, n.updated_at,
                       n.node_type, n.metadata, ml.layer
                FROM nodes n
                JOIN memory_layers ml ON n.node_id = ml.node_id
                WHERE ml.layer IN ('short_term', 'long_term')
            """)
            nodes_to_decay = cursor.fetchall()
            for n in nodes_to_decay:
                is_agent = self.is_agent_note(n)
                layer_cfg = MEMORY_LAYERS.get(n["layer"], MEMORY_LAYERS["short_term"])
                decay_factor = layer_cfg["decay"]
                days_old = max(0, (_now() - n["updated_at"]) / 86400)
                new_imp = n["importance"] * (decay_factor ** days_old)
                new_imp = min(1.0, max(0.0, new_imp))

                if abs(new_imp - n["importance"]) > 0.01:
                    conn.execute("UPDATE nodes SET importance = ? WHERE node_id = ?", (new_imp, n["node_id"]))
                    if is_agent:
                        decayed_agent += 1
                    else:
                        decayed_core += 1

                # 3. Prune candidate identification (importance < 0.05 & access_count < 3 & node_type != 'PERSON')
                if new_imp < 0.05 and n["access_count"] < 3 and n["layer"] == "short_term":
                    # Agent notes awaiting core review must not be silently pruned
                    if is_agent and self._node_metadata(n).get("attention_state") == "review_ready":
                        skipped_review_ready += 1
                        continue
                    # Core entity types are never pruned here — that decision
                    # belongs to age pruning / the main core
                    if not is_agent and n["node_type"] in ("PERSON", "SKILL", "BOUNDARY", "FACT", "CORE_REF"):
                        skipped_protected += 1
                        continue
                    # Check if node has critical edge links before deleting
                    edge_check = conn.execute("SELECT COUNT(*) as c FROM edges WHERE from_node = ? OR to_node = ?",
                                              (n["node_id"], n["node_id"])).fetchone()
                    if edge_check["c"] == 0:
                        conn.execute("DELETE FROM nodes WHERE node_id = ?", (n["node_id"],))
                        # Explicit orphan cleanup (FK cascades are not enforced
                        # without PRAGMA foreign_keys=ON)
                        conn.execute("DELETE FROM memory_layers WHERE node_id = ?", (n["node_id"],))
                        conn.execute("DELETE FROM node_vectors WHERE node_id = ?", (n["node_id"],))
                        conn.execute("DELETE FROM access_log WHERE node_id = ?", (n["node_id"],))
                        conn.execute("DELETE FROM node_index WHERE node_id = ?", (n["node_id"],))
                        if is_agent:
                            pruned_agent += 1
                        else:
                            pruned_core += 1

            conn.commit()
            conn.close()

            return {
                "status": "success",
                "promoted": promoted_core,
                "decayed": decayed_core,
                "pruned": pruned_core,
                "promoted_core": promoted_core,
                "promoted_agent": promoted_agent,
                "decayed_core": decayed_core,
                "decayed_agent": decayed_agent,
                "pruned_core": pruned_core,
                "pruned_agent": pruned_agent,
                "skipped_review_ready": skipped_review_ready,
                "skipped_protected": skipped_protected,
            }
        except Exception as e:
            conn.close()
            return {"status": "error", "message": f"Tier management failed: {str(e)}"}

    # ── Agent WORKING regulator (agent-only, core untouched) ──────────────────
    def get_agent_working_preview(self) -> Dict[str, Any]:
        """Observer preview: all agent WORKING nodes with score / days-left.

        Score = acc*Wa + imp*Wi - age_h*Wd   (config tunable)
        Days left = (max_age_h - age_h)/24 . Core notes never included.
        """
        conn = self._connect_db()
        try:
            wa = float(self.config.get("agent_working_weight_access", 1.5))
            wi = float(self.config.get("agent_working_weight_importance", 4.0))
            wd = float(self.config.get("agent_working_weight_age", 0.15))
            max_age_h = float(self.config.get("agent_working_max_age_hours", 48))
            high_water = int(self.config.get("agent_working_high_water", 12))
            enabled = bool(self.config.get("agent_working_regulator_enabled", True))
            now = _now()
            # fetch all working agent notes with layer info
            rows = conn.execute("""
                SELECT n.node_id, n.node_type, n.label, n.content, n.importance, n.access_count,
                       n.trust_level, n.created_at, n.updated_at, n.metadata,
                       ml.promoted_at, ml.layer
                FROM nodes n JOIN memory_layers ml ON n.node_id = ml.node_id
                WHERE ml.layer = 'working'
            """).fetchall()
            agent_rows = [r for r in rows if self.is_agent_note(r)]
            # attach last_access from access_log
            preview = []
            for r in agent_rows:
                meta = self._node_metadata(r)
                promoted_at = r["promoted_at"] or r["created_at"] or now
                # last_access = max access_log else promoted_at
                la_row = conn.execute("SELECT MAX(accessed_at) as la FROM access_log WHERE node_id=?", (r["node_id"],)).fetchone()
                last_access = la_row["la"] if la_row and la_row["la"] else promoted_at
                base_ts = max(promoted_at, last_access)
                age_h = max(0.0, (now - base_ts) / 3600.0)
                imp = r["importance"] if r["importance"] is not None else 0.5
                acc = r["access_count"] or 0
                score = round(acc * wa + imp * wi - age_h * wd, 3)
                days_left = round(max(0.0, (max_age_h - age_h) / 24.0), 2)
                hours_left = round(max(0.0, max_age_h - age_h), 1)
                # rank will be set after sort
                preview.append({
                    "node_id": r["node_id"],
                    "label": r["label"],
                    "content": (r["content"] or "")[:160],
                    "importance": imp,
                    "trust_level": r["trust_level"],
                    "access_count": acc,
                    "agent_id": meta.get("agent_id"),
                    "attention_state": meta.get("attention_state", "agent_private"),
                    "promoted_at": promoted_at,
                    "last_access": last_access,
                    "age_hours": round(age_h, 1),
                    "score": score,
                    "days_left": days_left,
                    "hours_left": hours_left,
                    "is_review_ready": meta.get("attention_state") == "review_ready",
                })
            # sort low score first — most likely to be demoted
            preview.sort(key=lambda x: x["score"])
            # mark next batch
            demote_batch = int(self.config.get("agent_working_demote_batch", 5))
            for i, p in enumerate(preview):
                if p["is_review_ready"]:
                    p["action"] = "protected (review_ready)"
                elif i < demote_batch and (len(preview) >= high_water or p["age_hours"] >= max_age_h):
                    p["action"] = "demote_next"
                elif p["age_hours"] >= max_age_h * 0.8:
                    p["action"] = "stale_soon"
                else:
                    p["action"] = "keep"
            enabled = enabled and True
            return {
                "enabled": enabled,
                "high_water": high_water,
                "max_age_hours": max_age_h,
                "weights": {"wa": wa, "wi": wi, "wd": wd},
                "demote_batch": demote_batch,
                "agent_working_count": len(preview),
                "preview": preview[:100],
            }
        except Exception as e:
            return {"error": str(e), "preview": []}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def regulate_agent_working_memory(self, dry_run: bool = False, auto_snapshot: bool = True) -> Dict[str, Any]:
        """Deterministic janitor: demotes low-score agent WORKING notes to short_term.

        Core working is never touched (is_agent_note guard). Runs in maintenance
        window or when high-water hit. Preserves node_ids/edges.
        """
        if not self.config.get("agent_working_regulator_enabled", True):
            return {"status": "skipped", "message": "regulator disabled", "demoted": 0}
        if auto_snapshot and not dry_run:
            self.create_snapshot()
        conn = self._connect_db()
        try:
            wa = float(self.config.get("agent_working_weight_access", 1.5))
            wi = float(self.config.get("agent_working_weight_importance", 4.0))
            wd = float(self.config.get("agent_working_weight_age", 0.15))
            max_age_h = float(self.config.get("agent_working_max_age_hours", 48))
            high_water = int(self.config.get("agent_working_high_water", 12))
            batch = int(self.config.get("agent_working_demote_batch", 5))
            now = _now()
            rows = conn.execute("""
                SELECT n.node_id, n.node_type, n.importance, n.access_count, n.created_at, n.metadata,
                       ml.promoted_at, ml.layer
                FROM nodes n JOIN memory_layers ml ON n.node_id = ml.node_id
                WHERE ml.layer = 'working'
            """).fetchall()
            agent_rows = [r for r in rows if self.is_agent_note(r)]
            # never touch review_ready
            candidates = []
            for r in agent_rows:
                meta = self._node_metadata(r)
                if meta.get("attention_state") == "review_ready":
                    continue
                promoted_at = r["promoted_at"] or r["created_at"] or now
                la_row = conn.execute("SELECT MAX(accessed_at) as la FROM access_log WHERE node_id=?", (r["node_id"],)).fetchone()
                last_access = la_row["la"] if la_row and la_row["la"] else promoted_at
                base_ts = max(promoted_at, last_access)
                age_h = max(0.0, (now - base_ts) / 3600.0)
                imp = r["importance"] if r["importance"] is not None else 0.5
                acc = r["access_count"] or 0
                score = acc * wa + imp * wi - age_h * wd
                candidates.append((score, age_h, r))
            candidates.sort(key=lambda x: x[0])  # low score first
            total_agent_working = len(agent_rows)
            # trigger only if high_water hit or any stale > max_age
            has_stale = any(age >= max_age_h for _, age, _ in candidates)
            if total_agent_working < high_water and not has_stale:
                conn.close()
                return {"status": "success", "demoted": 0, "message": f"below high_water ({total_agent_working}/{high_water}) and no stale > {max_age_h}h", "total_agent_working": total_agent_working}
            # pick batch: stale first then low score
            stale = [c for c in candidates if c[1] >= max_age_h]
            to_demote = []
            # stale always demoted first
            for c in stale:
                if len(to_demote) < batch:
                    to_demote.append(c)
            # then fill with lowest scores if still under batch and still over high_water
            if total_agent_working >= high_water:
                for c in candidates:
                    if c in to_demote:
                        continue
                    if len(to_demote) >= batch:
                        break
                    to_demote.append(c)
            demoted_ids = []
            for score, age_h, r in to_demote:
                if dry_run:
                    demoted_ids.append(r["node_id"])
                else:
                    conn.execute("UPDATE memory_layers SET layer='short_term', layer_order=2, promoted_at=? WHERE node_id=?", (now, r["node_id"]))
                    demoted_ids.append(r["node_id"])
            if not dry_run:
                conn.commit()
            conn.close()
            out = {"status": "success", "demoted": len(demoted_ids), "demoted_ids": demoted_ids,
                   "total_agent_working": total_agent_working, "high_water": high_water, "max_age_hours": max_age_h, "dry_run": dry_run}
            if dry_run:
                # include preview of would-demote with scores
                out["would_demote"] = [{"node_id": r["node_id"], "score": round(s,3), "age_hours": round(a,1)} for s,a,r in to_demote]
            return out
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return {"status": "error", "message": str(e)}

    def prune_stale_unused_nodes(self, max_unused_days: int = 4, auto_snapshot: bool = True) -> Dict[str, Any]:
        """
        Tracks and cleanly removes unused nodes older than max_unused_days (default 4+ days old).

        Core memory and agent notes are handled differently:
        - Agent notes (cron/worker garbage) follow staleness rules (age + low access),
          except notes awaiting core review (attention_state == 'review_ready').
        - Core nodes are only pruned when ALL of the following hold:
            * node type is not protected (PERSON, SKILL, BOUNDARY, FACT, CORE_REF)
            * importance is below the floor (config 'prune_importance_floor', default 0.05)
            * the node has zero edges (no longer referenced by the graph)
        Cleans up associated graph edges and orphaned index rows.
        """
        if auto_snapshot:
            self.create_snapshot()

        now_sec = _now()
        cutoff_sec = now_sec - (max_unused_days * 86400)
        importance_floor = self.config.get("prune_importance_floor", 0.05)
        protected_types = ("PERSON", "SKILL", "BOUNDARY", "FACT", "CORE_REF")
        removed_nodes = []
        removed_core = 0
        removed_agent = 0
        skipped_review_ready = 0
        skipped_protected = 0
        skipped_important = 0
        skipped_connected = 0

        conn = self._connect_db()

        try:
            cursor = conn.execute("""
                SELECT n.node_id, n.label, n.node_type, n.created_at, n.updated_at,
                       n.access_count, n.importance, n.metadata
                FROM nodes n
                WHERE n.updated_at <= ?
                  AND n.access_count <= 2
            """, (cutoff_sec,))

            candidates = cursor.fetchall()
            for cand in candidates:
                nid = cand["node_id"]
                is_agent = self.is_agent_note(cand)

                if is_agent:
                    if self._node_metadata(cand).get("attention_state") == "review_ready":
                        skipped_review_ready += 1
                        continue
                else:
                    # Core memory safety: protected types, importance floor,
                    # and edge connectivity checks before anything is deleted.
                    if cand["node_type"] in protected_types:
                        skipped_protected += 1
                        continue
                    if cand["importance"] >= importance_floor:
                        skipped_important += 1
                        continue
                    edge_check = conn.execute(
                        "SELECT COUNT(*) as c FROM edges WHERE from_node = ? OR to_node = ?",
                        (nid, nid)).fetchone()
                    if edge_check["c"] > 0:
                        skipped_connected += 1
                        continue

                conn.execute("DELETE FROM edges WHERE from_node = ? OR to_node = ?", (nid, nid))
                conn.execute("DELETE FROM memory_layers WHERE node_id = ?", (nid,))
                conn.execute("DELETE FROM node_vectors WHERE node_id = ?", (nid,))
                conn.execute("DELETE FROM access_log WHERE node_id = ?", (nid,))
                conn.execute("DELETE FROM node_index WHERE node_id = ?", (nid,))
                conn.execute("DELETE FROM nodes WHERE node_id = ?", (nid,))
                removed_nodes.append({
                    "node_id": nid,
                    "label": cand["label"],
                    "node_type": cand["node_type"],
                    "scope": "agent" if is_agent else "core",
                    "age_days": round((now_sec - cand["updated_at"]) / 86400, 1)
                })
                if is_agent:
                    removed_agent += 1
                else:
                    removed_core += 1

            conn.commit()
            conn.close()

            return {
                "status": "success",
                "max_unused_days": max_unused_days,
                "importance_floor": importance_floor,
                "pruned_count": len(removed_nodes),
                "removed_core": removed_core,
                "removed_agent": removed_agent,
                "skipped_review_ready": skipped_review_ready,
                "skipped_protected": skipped_protected,
                "skipped_important": skipped_important,
                "skipped_connected": skipped_connected,
                "removed_nodes": removed_nodes,
            }
        except Exception as e:
            conn.close()
            return {"status": "error", "message": f"Age prune failed: {str(e)}"}

    def purge_orphans(self) -> Dict[str, Any]:
        """
        Removes dangling edges and orphaned index rows left behind by mutations.

        FK cascades are not enforced (no PRAGMA foreign_keys=ON), so dedup
        merges, tier pruning and age pruning can leave rows referencing deleted
        nodes. This must run at the END of every job (not just the start) so it
        catches orphans created by that job itself.
        """
        conn = self._connect_db()
        try:
            edges_removed = conn.execute("""
                DELETE FROM edges
                WHERE from_node NOT IN (SELECT node_id FROM nodes)
                   OR to_node NOT IN (SELECT node_id FROM nodes)
            """).rowcount
            vectors_removed = conn.execute(
                "DELETE FROM node_vectors WHERE node_id NOT IN (SELECT node_id FROM nodes)").rowcount
            layers_removed = conn.execute(
                "DELETE FROM memory_layers WHERE node_id NOT IN (SELECT node_id FROM nodes)").rowcount
            access_removed = conn.execute(
                "DELETE FROM access_log WHERE node_id NOT IN (SELECT node_id FROM nodes)").rowcount
            index_removed = conn.execute(
                "DELETE FROM node_index WHERE node_id NOT IN (SELECT node_id FROM nodes)").rowcount
            conn.commit()
            conn.close()
            return {
                "status": "success",
                "edges_removed": edges_removed,
                "node_vectors_removed": vectors_removed,
                "memory_layers_removed": layers_removed,
                "access_log_removed": access_removed,
                "node_index_removed": index_removed,
                "total_removed": edges_removed + vectors_removed + layers_removed + access_removed + index_removed,
            }
        except Exception as e:
            conn.close()
            return {"status": "error", "message": f"Orphan purge failed: {str(e)}"}

    def rebuild_vectors(self) -> Dict[str, Any]:
        """
        Rebuilds the TF-IDF vector index via AshaMemory.rebuild_vector_index().

        Content merges (dedup) and prunes leave stored node_vectors stale, and
        the auto_rebuild flag only fires on memory-side writes — the brain
        mutates the DB directly, so maintenance must trigger the rebuild itself.

        P2-6: Uses static helper AshaMemory.rebuild_vector_index_for_path when
        available to avoid AshaMemory(base_path=parent) side-effect (creates
        config.json). Falls back to instance method for older installs.
        """
        try:
            t0 = time.time()
            # Prefer static helper (no config creation)
            if AshaMemory is not None and hasattr(AshaMemory, "rebuild_vector_index_for_path"):
                res = AshaMemory.rebuild_vector_index_for_path(str(self.db_path))
                if res.get("status") == "success":
                    return res
                # fall through to instance method if static failed
            if AshaMemory is None:
                return {"status": "error", "message": "asha_memory_v2 not importable"}
            mem = AshaMemory(base_path=str(self.db_path.parent))
            mem.rebuild_vector_index()
            conn = self._connect_db()
            count = conn.execute("SELECT COUNT(*) FROM node_vectors").fetchone()[0]
            conn.close()
            return {
                "status": "success",
                "vectors_rebuilt": count,
                "duration_s": round(time.time() - t0, 3),
            }
        except Exception as e:
            return {"status": "error", "message": f"Vector rebuild failed: {str(e)}"}

    def vacuum_db(self) -> Dict[str, Any]:
        """Reclaim freelist space (SQLite VACUUM). Shrinks file after heavy deletes."""
        try:
            before = self.db_path.stat().st_size if self.db_path.exists() else 0
            # VACUUM cannot run inside a transaction with other connections; use isolated connection
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("PRAGMA foreign_keys=ON")
            # Ensure WAL checkpoint before vacuum
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            conn.execute("VACUUM")
            conn.close()
            after = self.db_path.stat().st_size if self.db_path.exists() else 0
            saved = before - after
            return {
                "status": "success",
                "before_bytes": before,
                "after_bytes": after,
                "saved_bytes": saved,
                "saved_mb": round(saved / (1024 * 1024), 2),
                "before_mb": round(before / (1024 * 1024), 2),
                "after_mb": round(after / (1024 * 1024), 2),
            }
        except Exception as e:
            return {"status": "error", "message": f"VACUUM failed: {str(e)}"}

    def compact_ephemeral_logs(self, keep_last: int = None, max_age_days: int = None,
                               auto_snapshot: bool = True) -> Dict[str, Any]:
        """
        Cap append-only telemetry logs (FEED_SNAPSHOT, RUNTIME_SAMPLE, etc.).
        Keeps only the N most recent per label and drops any older than max_age_days.
        Unlike prune_stale_unused_nodes, this ignores protected-type / edge checks
        for ephemeral labels — those logs are signal, not knowledge, and must not
        accumulate edges. Also removes orphaned CONTRADICTS edges between ephemeral logs.
        """
        if auto_snapshot:
            self.create_snapshot()

        keep_last = keep_last if keep_last is not None else self.config.get("ephemeral_keep_last", 3)
        max_age_days = max_age_days if max_age_days is not None else self.config.get("ephemeral_max_age_days", 7)
        now_sec = int(time.time())
        cutoff_sec = now_sec - (max_age_days * 86400)

        removed = []
        removed_per_label = Counter()
        skipped_by_age = 0
        edges_removed = 0

        conn = self._connect_db()
        try:
            ephemeral_labels = self.config.get("ephemeral_labels", [])
            for label in ephemeral_labels:
                rows = conn.execute(
                    "SELECT node_id, label, node_type, created_at, updated_at, importance, content, metadata "
                    "FROM nodes WHERE label = ? ORDER BY updated_at DESC, created_at DESC", (label,)
                ).fetchall()
                if len(rows) <= keep_last:
                    # still check age cutoff for even the kept ones
                    for r in rows:
                        if r["updated_at"] < cutoff_sec or r["created_at"] < cutoff_sec:
                            # if we keep only N, but some are older than TTL, still drop if beyond N? already covered
                            pass
                    continue
                # keep first keep_last, delete the rest (plus any beyond TTL even within keep_last)
                keep_ids = set(r["node_id"] for r in rows[:keep_last])
                # also drop any of the keep set that is older than TTL (treat TTL as hard cap)
                for r in rows[:keep_last]:
                    if r["updated_at"] < cutoff_sec and r["created_at"] < cutoff_sec:
                        # age TTL overrides keep_last - but don't delete if it would leave 0
                        if len(keep_ids) > 1:
                            keep_ids.remove(r["node_id"])
                for r in rows:
                    if r["node_id"] in keep_ids:
                        continue
                    nid = r["node_id"]
                    # count edges that will go
                    ec = conn.execute("SELECT COUNT(*) FROM edges WHERE from_node = ? OR to_node = ?", (nid, nid)).fetchone()[0]
                    edges_removed += ec
                    conn.execute("DELETE FROM edges WHERE from_node = ? OR to_node = ?", (nid, nid))
                    conn.execute("DELETE FROM node_vectors WHERE node_id = ?", (nid,))
                    conn.execute("DELETE FROM memory_layers WHERE node_id = ?", (nid,))
                    conn.execute("DELETE FROM access_log WHERE node_id = ?", (nid,))
                    conn.execute("DELETE FROM node_index WHERE node_id = ?", (nid,))
                    conn.execute("DELETE FROM nodes WHERE node_id = ?", (nid,))
                    removed.append({"node_id": nid, "label": label, "age_days": round((now_sec - r["updated_at"]) / 86400, 1)})
                    removed_per_label[label] += 1

            conn.commit()
            # purge any residual orphans (e.g. CONTRADICTS between two ephemeral logs where one side survived)
            purge = self.purge_orphans()
            # also clean stale CONTRADICTS that are ephemeral-ephemeral false positives among survivors
            try:
                # remove CONTRADICTS where both ends are ephemeral survivors — those are noise (JSON shared keys)
                stale = conn.execute("""
                    SELECT e.edge_id FROM edges e
                    JOIN nodes n1 ON e.from_node = n1.node_id
                    JOIN nodes n2 ON e.to_node = n2.node_id
                    WHERE e.edge_type='CONTRADICTS'
                      AND n1.label IN ({seq}) AND n2.label IN ({seq})
                """.format(seq=",".join("?" * len(ephemeral_labels))), (*ephemeral_labels, *ephemeral_labels)).fetchall() if ephemeral_labels else []
                for row in stale:
                    conn.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
                    edges_removed += 1
                conn.commit()
            except Exception:
                pass
            conn.close()
            return {
                "status": "success",
                "keep_last": keep_last,
                "max_age_days": max_age_days,
                "removed_total": len(removed),
                "removed_per_label": dict(removed_per_label),
                "edges_removed": edges_removed,
                "orphans_purged": purge,
                "removed_nodes": removed[:20],
            }
        except Exception as e:
            conn.close()
            return {"status": "error", "message": f"Ephemeral compaction failed: {str(e)}"}

    def get_bloat_metrics(self) -> Dict[str, Any]:
        """Freelist / ephemeral / contradiction bloat signals for dashboard."""
        try:
            conn = self._connect_db()
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
            total_bytes = page_count * page_size
            free_bytes = freelist * page_size
            used_bytes = total_bytes - free_bytes
            ephemeral = self.get_ephemeral_stats(conn)
            # contradiction noise
            try:
                total_contr = conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type='CONTRADICTS'").fetchone()[0]
            except Exception:
                total_contr = 0
            # ephemeral-ephemeral contradicts
            ephemeral_labels = self.config.get("ephemeral_labels", [])
            try:
                seq = ",".join("?" * len(ephemeral_labels))
                eph_contr = conn.execute(f"""
                    SELECT COUNT(*) FROM edges e
                    JOIN nodes n1 ON e.from_node=n1.node_id
                    JOIN nodes n2 ON e.to_node=n2.node_id
                    WHERE e.edge_type='CONTRADICTS' AND n1.label IN ({seq}) AND n2.label IN ({seq})
                """, (*ephemeral_labels, *ephemeral_labels)).fetchone()[0] if ephemeral_labels else 0
            except Exception:
                eph_contr = 0
            conn.close()
            thresh_pct = self.config.get("vacuum_freelist_threshold_pct", 15)
            min_pages = self.config.get("vacuum_freelist_min_pages", 50)
            try:
                thresh_pct = float(thresh_pct)
            except Exception:
                thresh_pct = 15
            try:
                min_pages = int(min_pages)
            except Exception:
                min_pages = 50
            pct = round((freelist / page_count * 100) if page_count else 0, 1)
            return {
                "page_count": page_count,
                "page_size": page_size,
                "freelist_count": freelist,
                "total_mb": round(total_bytes / (1024*1024), 2),
                "free_mb": round(free_bytes / (1024*1024), 2),
                "used_mb": round(used_bytes / (1024*1024), 2),
                "freelist_pct": pct,
                "ephemeral": ephemeral,
                "contradicts_total": total_contr,
                "contradicts_ephemeral": eph_contr,
                "needs_vacuum": freelist > min_pages and pct > thresh_pct,
                "vacuum_threshold_pct": thresh_pct,
                "vacuum_min_pages": min_pages,
            }
        except Exception as e:
            return {"error": str(e)}

    def generate_markdown_report(self, run_summary: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generates a human/AI readable dated markdown report saved in brain/logs/.
        Example filename: 2026-08-12_141000_maintenance_report.md
        """
        dt_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"{dt_str}_maintenance_report.md"
        log_file = self.logs_dir / filename

        db_path_str = str(self.db_path)
        snapshot = run_summary.get("snapshot_taken", "None")
        duration = run_summary.get("duration_seconds", 0)
        results = run_summary.get("results", {})
        health = run_summary.get("health_after", {})

        md_lines = [
            f"# Asha Memory Maintenance Audit Report",
            f"**Date/Time**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`  ",
            f"**Target Database**: `{db_path_str}`  ",
            f"**Safety Snapshot**: `{snapshot}`  ",
            f"**Duration**: `{duration} seconds`  \n",
            "---",
            "## 📊 Post-Maintenance Graph Metrics",
            f"- **Total Nodes**: `{health.get('total_nodes', 0)}`  ",
            f"  - **Core Memory Nodes**: `{health.get('core_nodes', 0)}`  ",
            f"  - **Agent Note Nodes**: `{health.get('agent_note_nodes', 0)}`",
            f"- **Total Edges**: `{health.get('total_edges', 0)}`",
            f"- **Database Size**: `{health.get('db_size_mb', 0.0)} MB`\n",
            "---",
            "## 🧹 Execution Details\n",
        ]

        if "deduplicate" in results:
            d = results["deduplicate"]
            md_lines.append(f"### 🔍 Deduplication")
            md_lines.append(f"- Core Memory Merged: `{d.get('exact_merged', 0)}` exact / `{d.get('semantic_merged', 0)}` near-duplicate")
            md_lines.append(f"- Agent Notes Merged: `{d.get('exact_merged_agent', 0)}` exact / `{d.get('semantic_merged_agent', 0)}` near-duplicate")
            md_lines.append(f"- Edges Re-linked: `{d.get('edges_relinked', 0)}`\n")

        if "age_prune" in results:
            ap = results["age_prune"]
            md_lines.append(f"### ⏳ Age-Based Inactivity Pruning (Unused Nodes)")
            md_lines.append(f"- Max Unused Age Threshold: `{ap.get('max_unused_days', 4)} days`")
            md_lines.append(f"- Importance Floor: `{ap.get('importance_floor', 0.05)}`")
            md_lines.append(f"- Stale Core Nodes Removed: `{ap.get('removed_core', 0)}`")
            md_lines.append(f"- Stale Agent Notes Removed: `{ap.get('removed_agent', 0)}`")
            md_lines.append(f"- Protected Types Skipped: `{ap.get('skipped_protected', 0)}`")
            md_lines.append(f"- Above Importance Floor Skipped: `{ap.get('skipped_important', 0)}`")
            md_lines.append(f"- Still Connected Skipped: `{ap.get('skipped_connected', 0)}`")
            md_lines.append(f"- Review-Ready Notes Protected: `{ap.get('skipped_review_ready', 0)}`")
            md_lines.append(f"- Stale Nodes Cleanly Removed: `{ap.get('pruned_count', 0)}`")
            if ap.get("removed_nodes"):
                md_lines.append("  - **Removed Nodes List**:")
                for item in ap.get("removed_nodes")[:10]:
                    scope_tag = "agent-note" if item.get("scope") == "agent" else "core"
                    md_lines.append(f"    - `[{scope_tag}] [{item['node_type']}]` **{item['label']}** ({item['age_days']} days old)")
            md_lines.append("")

        if "manage_tiers" in results:
            t = results["manage_tiers"]
            md_lines.append(f"### 🚀 Tier Lifecycle Management")
            md_lines.append(f"- Core Nodes Promoted to Long-Term: `{t.get('promoted_core', 0)}`")
            md_lines.append(f"- Agent Notes Promoted to Long-Term: `{t.get('promoted_agent', 0)}`")
            md_lines.append(f"- Stale Core Nodes Decayed: `{t.get('decayed_core', 0)}`")
            md_lines.append(f"- Stale Agent Notes Decayed: `{t.get('decayed_agent', 0)}`")
            md_lines.append(f"- Low Importance Pruned: `{t.get('pruned_core', 0)}` core / `{t.get('pruned_agent', 0)}` agent")
            md_lines.append(f"- Review-Ready Notes Protected: `{t.get('skipped_review_ready', 0)}`")
            md_lines.append(f"- Protected Types Skipped: `{t.get('skipped_protected', 0)}`\n")

        if "detect_contradictions" in results:
            c = results["detect_contradictions"]
            md_lines.append(f"### ⚠️ Contradiction Resolution")
            md_lines.append(f"- Contradictions Found: `{c.get('contradictions_found', 0)}`")
            md_lines.append(f"- CONTRADICTS Edges Created: `{c.get('edges_created', 0)}`\n")

        if "graduate_agent_notes" in results:
            g = results["graduate_agent_notes"]
            md_lines.append(f"### 🎓 Agent Note Graduation")
            md_lines.append(f"- Notes Graduated to Core Memory: `{g.get('graduated', 0)}`\n")

        if "discover_links" in results:
            dl = results["discover_links"]
            md_lines.append(f"### 🔗 Semantic Link Discovery")
            md_lines.append(f"- Core RELATES_TO Links: `{dl.get('links_created_core', 0)}`")
            md_lines.append(f"- Agent-Note RELATES_TO Links: `{dl.get('links_created_agent', 0)}`\n")

        if "compact_ephemeral" in results:
            ce = results["compact_ephemeral"]
            md_lines.append(f"### 🗜️ Ephemeral Log Compaction")
            md_lines.append(f"- Keep Last: `{ce.get('keep_last', 3)}` per label, Max Age: `{ce.get('max_age_days', 7)} days`")
            md_lines.append(f"- Removed: `{ce.get('removed_total', 0)}` nodes (`{', '.join([f'{k}:{v}' for k,v in ce.get('removed_per_label', {}).items()]) or 'none'}`)")
            md_lines.append(f"- Edges Removed: `{ce.get('edges_removed', 0)}`\n")

        if "vacuum" in results:
            v = results["vacuum"]
            if v.get("status") == "success":
                md_lines.append(f"### 🧹 VACUUM")
                md_lines.append(f"- Before: `{v.get('before_mb', 0)} MB` → After: `{v.get('after_mb', 0)} MB` (saved `{v.get('saved_mb', 0)} MB`)\n")
            else:
                md_lines.append(f"### 🧹 VACUUM")
                md_lines.append(f"- ERROR: `{v.get('message')}`\n")

        # Ephemeral / bloat summary from health_after
        bloat = health.get("bloat", {}) if isinstance(health, dict) else {}
        ephem = health.get("ephemeral", {}) if isinstance(health, dict) else {}
        if bloat or ephem:
            md_lines.append(f"### 📦 Bloat Signals")
            md_lines.append(f"- Freelist: `{bloat.get('freelist_count', 0)} pages ({bloat.get('freelist_pct', 0)}%)` — {'needs VACUUM' if bloat.get('needs_vacuum') else 'ok'}")
            md_lines.append(f"- Ephemeral Logs: `{ephem.get('_total_ephemeral_labels', 0)}` tracked labels (keep_last={ephem.get('_keep_last', 3)})")
            md_lines.append(f"- CONTRADICTS Total: `{health.get('contradicts_total', 0)}`\n")

        # End-of-job orphan sweeps (dangling edges + index rows)
        purge_total = 0
        for key, r in results.items():
            if isinstance(r, dict) and isinstance(r.get("orphans_purged"), dict) \
                    and r["orphans_purged"].get("status") == "success":
                purge_total += r["orphans_purged"].get("total_removed", 0)
        md_lines.append(f"- **Orphan Rows Purged After Jobs**: `{purge_total}`")

        if "vector_index_rebuild" in results:
            v = results["vector_index_rebuild"]
            if v.get("status") == "success":
                md_lines.append(f"### 🔄 TF-IDF Vector Index Rebuild")
                md_lines.append(f"- Vectors Rebuilt: `{v.get('vectors_rebuilt', 0)}`")
                md_lines.append(f"- Duration: `{v.get('duration_s', 0)}s`\n")
            else:
                md_lines.append(f"### 🔄 TF-IDF Vector Index Rebuild")
                md_lines.append(f"- ERROR: `{v.get('message')}`\n")

        md_lines.append("---\n*Generated automatically by Asha Memory Brain Engine.*")

        md_content = "\n".join(md_lines)
        log_file.write_text(md_content, encoding="utf-8")
        # P3-3 rotation: prune old logs
        try:
            keep = int(self.config.get("keep_last_logs", 30))
            logs = sorted(self.logs_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in logs[keep:]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass

        return {
            "status": "success",
            "log_filename": filename,
            "log_path": str(log_file),
        }

    def list_markdown_logs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Returns list of generated markdown report files in brain/logs/."""
        reports = []
        for path in self.logs_dir.glob("*.md"):
            mtime = int(path.stat().st_mtime)
            reports.append({
                "filename": path.name,
                "path": str(path.resolve()),
                "created_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "mtime": mtime,
                "size_bytes": path.stat().st_size,
            })
        reports.sort(key=lambda x: -x["mtime"])
        return reports[:limit]

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 3: ADVANCED AI BRAIN FEATURES
    # ──────────────────────────────────────────────────────────────────────────

    def detect_contradictions(self) -> Dict[str, Any]:
        """
        Scans nodes for opposing sentiment patterns on identical subjects/topics.
        Establishes weighted CONTRADICTS directed edges with scored pending curation.
        Edges carry metadata {status: pending|ignored, confidence, overlap_words, ...}
        so dashboard can curate instead of blind delete.
        """
        conn = self._connect_db()

        contradictions_found = 0
        edges_created = 0
        edges_ignored = 0

        try:
            cursor = conn.execute("""
                SELECT node_id, label, content, node_type, metadata, importance, trust_level, access_count
                FROM nodes WHERE node_type IN ('FACT', 'PREFERENCE', 'TOPIC', 'AFFECT')
            """)
            # Exclude agent notes and ephemeral telemetry logs (JSON feeds) — they are not knowledge
            nodes = [n for n in cursor.fetchall() if not self.is_agent_note(n) and not self._is_ephemeral_row(n)]

            for i in range(len(nodes)):
                for j in range(i + 1, len(nodes)):
                    a, b = nodes[i], nodes[j]

                    words_a = set(_tokenize(a["label"] + " " + a["content"]))
                    words_b = set(_tokenize(b["label"] + " " + b["content"]))

                    overlap = words_a & words_b
                    # Filter 1-3 letter noise (it, is, and, etc.) + stopwords — fixes false positives
                    meaningful_overlap = {w for w in overlap if len(w) > 3 and w not in STOPWORDS}
                    if len(meaningful_overlap) < 2:
                        continue
                    overlap = meaningful_overlap
                    pos_a = len(words_a & POSITIVE_WORDS)
                    neg_a = len(words_a & NEGATIVE_WORDS)
                    pos_b = len(words_b & POSITIVE_WORDS)
                    neg_b = len(words_b & NEGATIVE_WORDS)

                    if (pos_a > neg_a and neg_b > pos_b) or (neg_a > pos_a and pos_b > neg_b):
                        # confidence scoring — overlap + sentiment gap + importance
                        imp_a = a["importance"] if a["importance"] is not None else 0.5
                        imp_b = b["importance"] if b["importance"] is not None else 0.5
                        imp_avg = (imp_a + imp_b) / 2
                        sentiment_gap = abs((pos_a - neg_a) - (pos_b - neg_b))
                        overlap_score = min(0.6, len(overlap) * 0.15)
                        sentiment_score = min(0.35, sentiment_gap * 0.12)
                        imp_bonus = imp_avg * 0.1
                        confidence = round(min(0.98, 0.25 + overlap_score + sentiment_score + imp_bonus), 2)
                        # auto-triage: low confidence + low importance → ignored (not pending)
                        is_high_value = imp_avg >= 0.6 or a["node_type"] in ("PERSON", "PREFERENCE") or b["node_type"] in ("PERSON", "PREFERENCE")
                        status = "pending" if (confidence >= 0.55 or is_high_value) else "ignored"
                        overlap_words = sorted(list(overlap))[:6]
                        meta = {
                            "detected_by": "BrainEngine",
                            "method": "sentiment_clash",
                            "overlap_words": overlap_words,
                            "overlap_count": len(overlap),
                            "pos_a": pos_a, "neg_a": neg_a, "pos_b": pos_b, "neg_b": neg_b,
                            "confidence": confidence,
                            "status": status,
                            "importance_avg": round(imp_avg, 3),
                        }
                        # skip if CONTRADICTS already exists in either direction
                        exists = conn.execute("SELECT edge_id, metadata FROM edges WHERE (from_node = ? AND to_node = ? AND edge_type='CONTRADICTS') OR (from_node = ? AND to_node = ? AND edge_type='CONTRADICTS')", (a["node_id"], b["node_id"], b["node_id"], a["node_id"])).fetchone()
                        if exists:
                            continue
                        try:
                            conn.execute("""
                                INSERT OR IGNORE INTO edges
                                (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                                VALUES (?, ?, ?, 'CONTRADICTS', -0.8, ?, ?)
                            """, (_edge_uuid(), a["node_id"], b["node_id"], _now(), json.dumps(meta)))
                            contradictions_found += 1
                            if status == "pending":
                                edges_created += 1
                            else:
                                edges_ignored += 1
                        except sqlite3.IntegrityError:
                            pass

            conn.commit()
            conn.close()
            return {
                "status": "success",
                "contradictions_found": contradictions_found,
                "edges_created": edges_created,
                "edges_ignored": edges_ignored,
                "pending": edges_created,
            }
        except Exception as e:
            conn.close()
            return {"status": "error", "message": f"Contradiction detection failed: {str(e)}"}

    # ── Contradiction curation (dashboard) ─────────────────────────────────
    def get_contradictions(self, status: str = None, limit: int = 50) -> Dict[str, Any]:
        """List CONTRADICTS edges with node snippets for curation UI."""
        conn = self._connect_db()
        try:
            rows = conn.execute("SELECT edge_id, from_node, to_node, weight, created_at, metadata FROM edges WHERE edge_type='CONTRADICTS' ORDER BY created_at DESC").fetchall()
            out = []
            for r in rows:
                try:
                    meta = json.loads(r["metadata"]) if r["metadata"] else {}
                except Exception:
                    meta = {}
                # filter by status if requested
                if status and meta.get("status", "pending") != status:
                    # allow legacy edges without status -> pending
                    if not (status == "pending" and "status" not in meta):
                        continue
                # fetch node snippets
                from_row = conn.execute("SELECT node_id, label, content, node_type, importance, trust_level FROM nodes WHERE node_id = ?", (r["from_node"],)).fetchone()
                to_row = conn.execute("SELECT node_id, label, content, node_type, importance, trust_level FROM nodes WHERE node_id = ?", (r["to_node"],)).fetchone()
                from_trust = from_row["trust_level"] if from_row and from_row["trust_level"] is not None else 0.5
                to_trust = to_row["trust_level"] if to_row and to_row["trust_level"] is not None else 0.5
                low = self.config.get("contradiction_low_trust", 0.3)
                high = self.config.get("contradiction_high_trust", 0.8)
                try:
                    low = float(low)
                    high = float(high)
                except Exception:
                    low, high = 0.3, 0.8
                suggested = None
                if from_trust < low and to_trust > high:
                    suggested = "keep_to"
                elif to_trust < low and from_trust > high:
                    suggested = "keep_from"
                out.append({
                    "edge_id": r["edge_id"],
                    "from_node": r["from_node"],
                    "to_node": r["to_node"],
                    "weight": r["weight"],
                    "created_at": r["created_at"],
                    "metadata": meta,
                    "from_label": from_row["label"] if from_row else "?",
                    "from_content": (from_row["content"] or "")[:180] if from_row else "",
                    "from_type": from_row["node_type"] if from_row else "",
                    "from_trust": from_trust,
                    "to_label": to_row["label"] if to_row else "?",
                    "to_content": (to_row["content"] or "")[:180] if to_row else "",
                    "to_type": to_row["node_type"] if to_row else "",
                    "to_trust": to_trust,
                    "suggested_action": suggested,
                    "auto_resolvable": suggested is not None,
                })
                if len(out) >= limit:
                    break
            # counts by status
            counts = {"pending": 0, "confirmed": 0, "ignored": 0, "resolved": 0}
            for r in rows:
                try:
                    m = json.loads(r["metadata"]) if r["metadata"] else {}
                except Exception:
                    m = {}
                s = m.get("status", "pending")
                if s in counts:
                    counts[s] += 1
                else:
                    counts["pending"] += 1
            conn.close()
            return {"contradictions": out, "counts": counts, "total": len(rows)}
        except Exception as e:
            conn.close()
            return {"error": str(e), "contradictions": []}

    def update_contradiction_status(self, edge_id: str, new_status: str) -> Dict[str, Any]:
        valid = {"pending", "confirmed", "ignored", "resolved"}
        if new_status not in valid:
            return {"status": "error", "message": f"Invalid status {new_status}"}
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute("SELECT metadata FROM edges WHERE edge_id = ? AND edge_type='CONTRADICTS'", (edge_id,)).fetchone()
            if not row:
                conn.close()
                return {"status": "error", "message": "Edge not found"}
            try:
                meta = json.loads(row[0]) if row[0] else {}
            except Exception:
                meta = {}
            meta["status"] = new_status
            meta["status_updated_at"] = _now()
            conn.execute("UPDATE edges SET metadata = ? WHERE edge_id = ?", (json.dumps(meta), edge_id))
            conn.commit()
            conn.close()
            return {"status": "success", "edge_id": edge_id, "new_status": new_status}
        except Exception as e:
            conn.close()
            return {"status": "error", "message": str(e)}

    def resolve_contradiction(self, edge_id: str, action: str) -> Dict[str, Any]:
        """Resolve by deleting edge (and optionally merging/keeping nodes). Actions: delete, keep_from, keep_to, merge."""
        conn = self._connect_db()
        try:
            row = conn.execute("SELECT from_node, to_node, metadata FROM edges WHERE edge_id = ? AND edge_type='CONTRADICTS'", (edge_id,)).fetchone()
            if not row:
                conn.close()
                return {"status": "error", "message": "Edge not found"}
            from_id, to_id = row["from_node"], row["to_node"]
            if action == "delete":
                conn.execute("DELETE FROM edges WHERE edge_id = ?", (edge_id,))
                conn.commit()
                conn.close()
                return {"status": "success", "action": "delete"}
            elif action in ("keep_from", "keep_to"):
                # delete the loosing node and its edges — mark edge resolved before delete
                loser = to_id if action == "keep_from" else from_id
                # remove edges involving loser
                conn.execute("DELETE FROM edges WHERE from_node = ? OR to_node = ?", (loser, loser))
                conn.execute("DELETE FROM node_vectors WHERE node_id = ?", (loser,))
                conn.execute("DELETE FROM memory_layers WHERE node_id = ?", (loser,))
                conn.execute("DELETE FROM access_log WHERE node_id = ?", (loser,))
                conn.execute("DELETE FROM node_index WHERE node_id = ?", (loser,))
                conn.execute("DELETE FROM nodes WHERE node_id = ?", (loser,))
                conn.commit()
                conn.close()
                # also purge orphans
                self.purge_orphans()
                return {"status": "success", "action": action, "removed_node": loser}
            elif action == "merge":
                # merge to_node into from_node: concat content, keep from_node
                f = conn.execute("SELECT content, label FROM nodes WHERE node_id = ?", (from_id,)).fetchone()
                t = conn.execute("SELECT content, label FROM nodes WHERE node_id = ?", (to_id,)).fetchone()
                if not f or not t:
                    conn.close()
                    return {"status": "error", "message": "Node missing"}
                merged = (f["content"] or "") + " | " + (t["content"] or "")
                conn.execute("UPDATE nodes SET content = ?, updated_at = ? WHERE node_id = ?", (merged[:800], _now(), from_id))
                # delete loser to_node
                conn.execute("DELETE FROM edges WHERE from_node = ? OR to_node = ?", (to_id, to_id))
                conn.execute("DELETE FROM node_vectors WHERE node_id = ?", (to_id,))
                conn.execute("DELETE FROM memory_layers WHERE node_id = ?", (to_id,))
                conn.execute("DELETE FROM access_log WHERE node_id = ?", (to_id,))
                conn.execute("DELETE FROM node_index WHERE node_id = ?", (to_id,))
                conn.execute("DELETE FROM nodes WHERE node_id = ?", (to_id,))
                conn.commit()
                conn.close()
                self.purge_orphans()
                if self.config.get("auto_rebuild_vectors", True):
                    self.rebuild_vectors()
                return {"status": "success", "action": "merge", "kept": from_id, "removed": to_id}
            else:
                conn.close()
                return {"status": "error", "message": f"Unknown action {action}"}
        except Exception as e:
            conn.close()
            return {"status": "error", "message": str(e)}

    def auto_resolve_low_trust(self, dry_run: bool = True) -> Dict[str, Any]:
        """Opt-in auto-resolve: pending contradictions where trust gap low<0.3 vs high>0.8."""
        if not self.config.get("contradiction_auto_resolve", False) and not dry_run:
            return {"status": "skipped", "message": "Auto-resolve disabled (enable in config)"}
        data = self.get_contradictions(status="pending", limit=100)
        candidates = [c for c in data.get("contradictions", []) if c.get("auto_resolvable")]
        if dry_run:
            return {"status": "success", "dry_run": True, "candidates": candidates, "count": len(candidates)}
        resolved = 0
        details = []
        for c in candidates[:20]:  # cap per run
            action = c.get("suggested_action")
            if not action:
                continue
            res = self.resolve_contradiction(c["edge_id"], action)
            if res.get("status") == "success":
                resolved += 1
                details.append({"edge_id": c["edge_id"], "action": action, "from_trust": c["from_trust"], "to_trust": c["to_trust"]})
        if resolved and self.config.get("auto_rebuild_vectors", True):
            self.rebuild_vectors()
        return {"status": "success", "dry_run": False, "resolved": resolved, "details": details, "candidates_total": len(candidates)}

    def graduate_agent_notes(self, node_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Evaluates worker AGENT_NOTE nodes set to 'review_ready'.
        Promotes validated notes with sufficient trust/access into permanent CORE memory nodes.
        If node_ids is provided, only those specific nodes are graduated (explicit per-node action).
        """
        conn = self._connect_db()

        graduated = 0
        graduated_ids: List[str] = []
        skipped: List[Dict[str, str]] = []

        explicit = node_ids is not None and len(node_ids) > 0
        wanted = set(node_ids) if explicit else None

        try:
            cursor = conn.execute("""
                SELECT node_id, label, content, trust_level, importance, node_type, metadata
                FROM nodes
            """)
            notes = [n for n in cursor.fetchall() if self.is_agent_note(n)]

            # If explicit list, filter to only those nodes (preserve order of input)
            if explicit:
                wanted_map = {n["node_id"]: n for n in notes}
                filtered = []
                for nid in node_ids:
                    row = wanted_map.get(nid)
                    if row is None:
                        skipped.append({"node_id": nid, "reason": "not found or not an agent note"})
                    else:
                        filtered.append(row)
                notes = filtered

            for note in notes:
                meta = json.loads(note["metadata"]) if note["metadata"] else {}
                attention = meta.get("attention_state", "agent_private")

                # Explicit single-node graduation bypasses trust/importance gate — user explicitly chose it
                if explicit:
                    # Already verified -> skip
                    if attention == "core_verified":
                        skipped.append({"node_id": note["node_id"], "reason": "already core_verified"})
                        continue
                else:
                    if not (attention == "review_ready" or (note["trust_level"] >= 0.7 and note["importance"] >= 0.6)):
                        continue

                # Graduate to CORE node
                meta["attention_state"] = "core_verified"
                meta["graduated_at"] = _now()
                meta["graduated_by"] = "BrainEngine"

                conn.execute("""
                    UPDATE nodes
                    SET node_type = 'FACT', source = 'CORE', trust_level = 0.95,
                        updated_at = ?, metadata = ?
                    WHERE node_id = ?
                """, (_now(), json.dumps(meta), note["node_id"]))
                graduated += 1
                graduated_ids.append(note["node_id"])

            conn.commit()
            conn.close()
            out: Dict[str, Any] = {"status": "success", "graduated": graduated}
            if explicit:
                out["graduated_ids"] = graduated_ids
                if skipped:
                    out["skipped"] = skipped
            return out
        except Exception as e:
            conn.close()
            return {"status": "error", "message": f"Agent note graduation failed: {str(e)}"}

    def graduate_single_note(self, node_id: str) -> Dict[str, Any]:
        """Convenience: graduate a single agent note by id."""
        if not node_id or not node_id.strip():
            return {"status": "error", "message": "node_id required"}
        return self.graduate_agent_notes(node_ids=[node_id.strip()])

    def discover_links(self) -> Dict[str, Any]:
        """
        Discovers latent semantic associations between unlinked nodes and creates RELATES_TO candidate links.
        Links are only created within the same scope (core-to-core, agent-to-agent);
        agent notes are never linked into core memory.
        """
        conn = self._connect_db()

        links_created_core = 0
        links_created_agent = 0

        try:
            cursor = conn.execute("SELECT node_id, label, content, node_type, metadata FROM nodes")
            all_nodes = cursor.fetchall()
            # Exclude ephemeral telemetry from serendipity — they are logs, not knowledge
            all_nodes = [r for r in all_nodes if not self._is_ephemeral_row(r)]
            core_rows, agent_rows = self._classify_rows(all_nodes)

            for group, is_agent in ((core_rows[:100], False), (agent_rows[:100], True)):
                nodes = group
                if not TfidfVectorizer or len(nodes) <= 1:
                    continue
                v = TfidfVectorizer()
                v.fit([(n["label"] or "") + " " + (n["content"] or "") for n in nodes])

                for i in range(len(nodes)):
                    for j in range(i + 1, len(nodes)):
                        a, b = nodes[i], nodes[j]
                        # Check if edge already exists
                        existing = conn.execute("""
                            SELECT edge_id FROM edges
                            WHERE (from_node = ? AND to_node = ?) OR (from_node = ? AND to_node = ?)
                        """, (a["node_id"], b["node_id"], b["node_id"], a["node_id"])).fetchone()

                        if not existing:
                            vec_a = v.transform((a["label"] or "") + " " + (a["content"] or ""))
                            vec_b = v.transform((b["label"] or "") + " " + (b["content"] or ""))
                            sim = v.cosine_similarity(vec_a, vec_b)

                            if 0.50 <= sim < 0.85:  # Moderate similarity (link, don't merge)
                                conn.execute("""
                                    INSERT OR IGNORE INTO edges
                                    (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                                    VALUES (?, ?, ?, 'RELATES_TO', ?, ?, ?)
                                """, (_edge_uuid(), a["node_id"], b["node_id"], round(sim, 2), _now(),
                                      json.dumps({"discovered_by": "SerendipityEngine", "similarity": sim})))
                                if is_agent:
                                    links_created_agent += 1
                                else:
                                    links_created_core += 1

            conn.commit()
            conn.close()
            return {
                "status": "success",
                "links_created": links_created_core + links_created_agent,
                "links_created_core": links_created_core,
                "links_created_agent": links_created_agent,
            }
        except Exception as e:
            conn.close()
            return {"status": "error", "message": f"Link discovery failed: {str(e)}"}

    # ──────────────────────────────────────────────────────────────────────────
    # METRICS & CONFIG HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def get_health_metrics(self) -> Dict[str, Any]:
        """Returns comprehensive health, node count, tier distribution, and graph statistics."""
        conn = self._connect_db()

        try:
            total_nodes = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
            total_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]

            node_types = dict(conn.execute("SELECT node_type, COUNT(*) as c FROM nodes GROUP BY node_type").fetchall())
            layers = dict(conn.execute("SELECT layer, COUNT(*) as c FROM memory_layers GROUP BY layer").fetchall())

            # Core vs agent-note split (agent notes are attention-scoped work, not core memory)
            scope_rows = conn.execute("SELECT node_type, metadata FROM nodes").fetchall()
            core_rows, agent_rows = self._classify_rows(scope_rows)
            core_count = len(core_rows)
            agent_count = len(agent_rows)

            def _type_breakdown(rows) -> Dict[str, int]:
                counts = Counter(r["node_type"] for r in rows)
                return dict(counts)

            db_size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0

            # Bloat signals (freelist, ephemeral)
            bloat = {}
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
                thresh_pct = self.config.get("vacuum_freelist_threshold_pct", 15)
                min_pages = self.config.get("vacuum_freelist_min_pages", 50)
                try:
                    thresh_pct = float(thresh_pct)
                except Exception:
                    thresh_pct = 15
                try:
                    min_pages = int(min_pages)
                except Exception:
                    min_pages = 50
                pct = round((freelist / page_count * 100) if page_count else 0, 1)
                bloat = {
                    "page_count": page_count,
                    "page_size": page_size,
                    "freelist_count": freelist,
                    "freelist_pct": pct,
                    "needs_vacuum": freelist > min_pages and pct > thresh_pct,
                    "vacuum_threshold_pct": thresh_pct,
                    "vacuum_min_pages": min_pages,
                }
            except Exception:
                bloat = {}
            try:
                ephemeral = self.get_ephemeral_stats(conn)
            except Exception:
                ephemeral = {}
            try:
                bloat_contr = conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type='CONTRADICTS'").fetchone()[0]
            except Exception:
                bloat_contr = 0

            conn.close()

            return {
                "db_path": str(self.db_path),
                "db_size_bytes": db_size_bytes,
                "db_size_mb": round(db_size_bytes / (1024 * 1024), 2),
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "node_types": node_types,
                "layers": layers,
                "core_nodes": core_count,
                "agent_note_nodes": agent_count,
                "core_type_breakdown": _type_breakdown(core_rows),
                "agent_note_type_breakdown": _type_breakdown(agent_rows),
                "snapshot_count": len(self.list_snapshots()),
                "bloat": bloat,
                "ephemeral": ephemeral,
                "contradicts_total": bloat_contr,
                "status": "healthy" if total_nodes > 0 else "empty",
            }
        except Exception as e:
            conn.close()
            return {"status": "error", "message": str(e), "db_path": str(self.db_path)}

    def _load_config(self) -> Dict[str, Any]:
        merged = dict(DEFAULT_CONFIG)
        if self.config_path.exists():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                merged.update(loaded)
            except Exception:
                pass
        # Read-through: ephemeral allowlist + prune threshold unified with core config.json when target DB exists (P2-2)
        try:
            # db_path may not be resolved yet during __init__ (resolve_db_path called after)
            db_parent = getattr(self, "db_path", None)
            if db_parent:
                core_cfg = Path(db_parent).parent / "config.json"
                if core_cfg.exists():
                    core_data = json.loads(core_cfg.read_text(encoding="utf-8"))
                    if "ephemeral_labels" in core_data and isinstance(core_data["ephemeral_labels"], list):
                        merged["ephemeral_labels"] = core_data["ephemeral_labels"]
                    # P2-2 alias: core prune_threshold -> brain prune_importance_floor
                    if "prune_threshold" in core_data:
                        try:
                            merged["prune_importance_floor"] = float(core_data["prune_threshold"])
                        except Exception:
                            pass
                    # also handle core's alias prune_importance_floor (from P2-2 core write-through)
                    elif "prune_importance_floor" in core_data:
                        try:
                            merged["prune_importance_floor"] = float(core_data["prune_importance_floor"])
                        except Exception:
                            pass
        except Exception:
            pass
        # Keep alias in sync for backwards compat
        try:
            if "prune_importance_floor" in merged:
                merged["prune_threshold"] = merged["prune_importance_floor"]
        except Exception:
            pass
        return merged

    def _refresh_ephemeral_from_core(self):
        """Re-read ephemeral allowlist + prune alias from core config.json (call after DB switch). P2-2"""
        try:
            core_cfg = self.db_path.parent / "config.json"
            if core_cfg.exists():
                core_data = json.loads(core_cfg.read_text(encoding="utf-8"))
                if "ephemeral_labels" in core_data:
                    self.config["ephemeral_labels"] = core_data["ephemeral_labels"]
                if "prune_threshold" in core_data:
                    try:
                        self.config["prune_importance_floor"] = float(core_data["prune_threshold"])
                        self.config["prune_threshold"] = float(core_data["prune_threshold"])
                    except Exception:
                        pass
                elif "prune_importance_floor" in core_data:
                    try:
                        self.config["prune_importance_floor"] = float(core_data["prune_importance_floor"])
                    except Exception:
                        pass
        except Exception:
            pass

    def reset_config(self) -> Dict[str, Any]:
        """Reset all settings to defaults, keeping the active DB path."""
        last = self.config.get("last_db_path", "")
        self.config = dict(DEFAULT_CONFIG)
        self.config["last_db_path"] = last
        self._save_config()
        return self.config

    def _save_config(self):
        try:
            self.config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[BrainEngine] Config save error: {e}")


if __name__ == "__main__":
    # Self-test when run directly
    print("Testing BrainEngine...")
    engine = BrainEngine()
    print("Target DB Path:", engine.db_path)
    metrics = engine.get_health_metrics()
    print("Health Metrics:", json.dumps(metrics, indent=2))
