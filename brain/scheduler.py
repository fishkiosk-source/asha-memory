"""brain.scheduler — interval runner (canonical order, single-flight, history).

Port of v2 brain/scheduler.py for the dual-DB engine. Locked constants below
are REAL (v2 values): JOB_ORDER (9 jobs, inputs sorted regardless of order),
DEFAULT_JOB_TYPES (excludes graduation,vacuum), MUTATING_JOBS (excludes
contradictions/discover/vacuum — no vector rebuild after those by design).

Single-flight: run_job_now refuses while a run is active (thread-guard) and
records {"status": "skipped_busy"} in job_history.json. The whole run holds
the inter-process memory/.lock (engine.locked) so dashboard button-mashing
queues instead of running parallel backup() storms.
"""

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .engine import BrainEngine

JOB_ORDER = [
    "dedup", "compact", "agent_working", "core_helper", "age_prune", "tiers",
    "contradictions", "graduation", "discover", "prune_empty_agents", "vacuum",
]

DEFAULT_JOB_TYPES = [
    "dedup", "compact", "agent_working", "core_helper", "age_prune", "tiers",
    "contradictions", "discover", "prune_empty_agents",
]

MUTATING_JOBS = {"dedup", "compact", "age_prune", "tiers", "graduation", "agent_working", "core_helper", "prune_empty_agents"}

JOB_RESULT_KEYS = {
    "dedup": "deduplicate",
    "compact": "compact_ephemeral",
    "agent_working": "regulate_agent_working",
    "core_helper": "regulate_core_helper",
    "age_prune": "age_prune",
    "tiers": "manage_tiers",
    "contradictions": "detect_contradictions",
    "graduation": "graduate_agent_notes",
    "discover": "discover_links",
    "prune_empty_agents": "prune_empty_agents",
    "vacuum": "vacuum",
}


class BrainScheduler:
    """Daemon + manual dual-DB job runner with single-flight guard."""

    def __init__(self, engine: Optional[BrainEngine] = None,
                 brain_dir: Optional[str] = None,
                 interval_minutes: Optional[int] = None):
        self.brain_dir = Path(brain_dir) if brain_dir else Path(__file__).resolve().parent
        self.engine = engine or BrainEngine(brain_dir=str(self.brain_dir))
        if interval_minutes is not None:
            self.engine.config["interval_minutes"] = interval_minutes
            self.engine._save_config()
        self._run_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.running = False

    # ── Daemon ──

    def start(self, interval_minutes: Optional[int] = None) -> bool:
        if self.running:
            return True
        if interval_minutes:
            self.engine.config.update({"interval_minutes": interval_minutes,
                                       "cron_enabled": True})
            self.engine._save_config()
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        if not self.running:
            return True
        self.running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.engine.config["cron_enabled"] = False
        self.engine._save_config()
        return True

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            interval_sec = int(self.engine.config.get("interval_minutes", 60)) * 60
            if self._stop_event.wait(timeout=interval_sec):
                break
            if self.running:
                try:
                    self.run_job_now()
                except Exception:
                    pass

    # ── Manual / dashboard runs ──

    def _job_dbs(self, job: str, target: str) -> List[str]:
        dbs = self.engine._targets(target)
        if job in ("agent_working", "prune_empty_agents"):
            dbs = [d for d in dbs if d == "agents"]
        if job == "core_helper":
            dbs = [d for d in dbs if d == "core"]
        return dbs

    def run_job_now(self, jobs: Optional[List[str]] = None,
                    target: str = "both") -> Dict[str, Any]:
        """Sorted-order dual-DB run with coalesced snapshots + single flight."""
        if not self._run_lock.acquire(blocking=False):
            entry = {"status": "skipped_busy",
                     "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "jobs": jobs or DEFAULT_JOB_TYPES, "target": target}
            self.engine.record_history(entry)
            return entry
        try:
            with self.engine.locked():
                return self._run_locked(jobs or DEFAULT_JOB_TYPES, target)
        finally:
            self._run_lock.release()

    def _run_locked(self, jobs: List[str], target: str) -> Dict[str, Any]:
        start = time.time()
        ordered = [j for j in sorted(jobs, key=lambda j: JOB_ORDER.index(j)
                                     if j in JOB_ORDER else 99) if j in JOB_ORDER]
        touched = sorted({db for j in ordered for db in self._job_dbs(j, target)})
        snapshots = self.engine.ensure_pre_run_snapshot(touched)
        results: Dict[str, Any] = {}

        def _run(job: str, fn) -> None:
            if job not in ordered:
                return
            res = fn()
            if isinstance(res, dict):
                for db in self._job_dbs(job, target):
                    if isinstance(res.get(db), dict) and \
                            res[db].get("status") == "success" and job in MUTATING_JOBS:
                        res[db]["orphans_purged"] = self._purge_db(db)
            results[JOB_RESULT_KEYS[job]] = res

        cfg = self.engine.config
        _run("dedup", lambda: self.engine.deduplicate(
            target, float(cfg.get("dedup_similarity_threshold", 0.85))))
        _run("compact", lambda: self.engine.compact_ephemeral(
            target, int(cfg.get("ephemeral_keep_last", 3)),
            None, int(cfg.get("ephemeral_max_age_hours", int(cfg.get("ephemeral_max_age_days", 7))*24))))
        _run("agent_working", self.engine.regulate_agent_working_memory)
        _run("core_helper", self.engine.regulate_core_helper)
        _run("age_prune", lambda: self.engine.prune_stale_unused(
            target, int(cfg.get("max_unused_days", 4))))
        _run("tiers", lambda: self.engine.manage_tiers(target))
        _run("contradictions", lambda: self.engine.detect_contradictions(target))
        _run("graduation", lambda: self.engine.graduate_agent_notes())
        _run("discover", lambda: self.engine.discover_links(target))
        _run("prune_empty_agents", lambda: self.engine.prune_empty_agents(target) if bool(self.engine.config.get("prune_empty_agents", False)) else {"agents": {"status": "skipped", "reason": "prune_empty_agents disabled"}, "core": {"status": "skipped"}})

        ran_mutating = any(j in ordered for j in MUTATING_JOBS)
        if "vacuum" in ordered:
            results["vacuum"] = self.engine.vacuum_db(target)
        elif ran_mutating and cfg.get("vacuum_after_prune", True):
            bloated = [db for db in touched
                       if self.engine.get_bloat_metrics(db).get(db, {}).get("needs_vacuum")]
            if bloated:
                results["vacuum"] = self.engine.vacuum_db(
                    "both" if set(bloated) == {"core", "agents"} else bloated[0])
        if ran_mutating and cfg.get("auto_rebuild_vectors", True):
            results["vector_index_rebuild"] = self.engine.rebuild_vectors(target)

        health_after = self.engine.health(target)
        entry = {"status": "success",
                 "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "target": target, "jobs": ordered,
                 "duration_s": round(time.time() - start, 3),
                 "snapshots": snapshots, "results": results,
                 "health_after": health_after}
        report = self.engine.generate_markdown_report(entry)
        entry["markdown_log"] = report.get("log_filename")
        entry["markdown_logs"] = report.get("log_files", [report.get("log_filename")])
        self.engine.record_history(entry)
        return entry

    def _purge_db(self, db: str) -> Dict[str, int]:
        conn = self.engine._conn_for(db)
        try:
            out = self.engine._purge_orphans(conn)
            conn.commit()
            return out
        except Exception:
            conn.rollback()
            return {}
        finally:
            conn.close()

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.engine.get_history(limit)
