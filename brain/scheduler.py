"""
BRAIN SCHEDULER — Autonomous Background Maintenance Runner for AshaMemory
==========================================================================
Schedules and runs background maintenance tasks at user-configured intervals.
Tracks job run history, durations, and metrics.
"""

import time
import json
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

from brain_engine import BrainEngine


# Canonical execution order: mutations first (merge/delete), edge creation LAST,
# so links are never created towards nodes that a later job would delete.
JOB_ORDER = {
    "dedup": 1,
    "compact": 2,
    "agent_working": 3,
    "age_prune": 4,
    "tiers": 5,
    "contradictions": 6,
    "graduation": 7,
    "discover": 8,
    "vacuum": 9,
}
DEFAULT_JOB_TYPES = ["dedup", "compact", "agent_working", "age_prune", "tiers", "contradictions", "discover"]
MUTATING_JOBS = {"dedup", "compact", "age_prune", "tiers", "graduation", "agent_working"}


class BrainScheduler:
    """
    Background scheduler for BrainEngine maintenance jobs.
    Runs in a daemon thread so it does not block main application loops.
    """

    def __init__(self, brain_dir: Optional[str] = None):
        self.brain_dir = Path(brain_dir or Path(__file__).parent).resolve()
        self.history_path = self.brain_dir / "job_history.json"
        self.engine = BrainEngine(brain_dir=str(self.brain_dir))

        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def get_config(self) -> Dict[str, Any]:
        """Fetch current scheduler configuration from BrainEngine config."""
        return self.engine.config

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update scheduler settings."""
        self.engine.config.update(updates)
        self.engine._save_config()
        return self.engine.config

    def start(self, interval_minutes: Optional[int] = None) -> bool:
        """Start the background scheduler thread."""
        if self.running:
            return True

        if interval_minutes:
            self.update_config({"interval_minutes": interval_minutes, "cron_enabled": True})

        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()
        print(f"[BrainScheduler] Scheduler started. Running every {self.get_config().get('interval_minutes', 60)} minutes.")
        return True

    def stop(self) -> bool:
        """Stop the background scheduler thread."""
        if not self.running:
            return True

        self.running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.update_config({"cron_enabled": False})
        print("[BrainScheduler] Scheduler stopped.")
        return True

    def run_job_now(self, job_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Executes maintenance job immediately (can be called manually or by timer).

        Execution order is canonical regardless of input order:
        dedup → age_prune → tiers → contradictions → graduation → discover.
        Mutating jobs run first; link discovery runs LAST so it never creates
        edges towards nodes that pruning would delete.

        After every job an orphan purge runs (dangling edges + index rows
        created by that job). After the run, if any mutating job executed and
        config 'auto_rebuild_vectors' is true, the TF-IDF vector index is
        rebuilt via AshaMemory.rebuild_vector_index().
        """
        start_time = time.time()
        job_types = job_types or DEFAULT_JOB_TYPES
        # Filter unknown jobs and enforce canonical order (link discovery last)
        job_types = [j for j in sorted(job_types, key=lambda j: JOB_ORDER.get(j, 99)) if j in JOB_ORDER]

        print(f"[BrainScheduler] Starting maintenance run on DB: {self.engine.db_path}")

        # Safety Snapshot before job run if configured
        snapshot_info = None
        if self.engine.config.get("auto_snapshot_before_jobs", True):
            snapshot_info = self.engine.create_snapshot()

        results = {}

        def _run(job_key, result_key, fn):
            if job_key not in job_types:
                return
            job_result = fn()
            if isinstance(job_result, dict):
                # End-of-job orphan sweep: catches orphans created by this job
                job_result["orphans_purged"] = self.engine.purge_orphans()
            results[result_key] = job_result

        if "dedup" in job_types:
            thresh = self.engine.config.get("dedup_similarity_threshold", 0.85)
            _run("dedup", "deduplicate", lambda: self.engine.deduplicate(similarity_threshold=thresh, auto_snapshot=False))

        if "compact" in job_types:
            keep = self.engine.config.get("ephemeral_keep_last", 3)
            max_age = self.engine.config.get("ephemeral_max_age_days", 7)
            _run("compact", "compact_ephemeral", lambda: self.engine.compact_ephemeral_logs(keep_last=keep, max_age_days=max_age, auto_snapshot=False))

        if "agent_working" in job_types:
            _run("agent_working", "regulate_agent_working", lambda: self.engine.regulate_agent_working_memory(auto_snapshot=False))

        if "age_prune" in job_types:
            max_days = self.engine.config.get("max_unused_days", 4)
            _run("age_prune", "age_prune", lambda: self.engine.prune_stale_unused_nodes(max_unused_days=max_days, auto_snapshot=False))

        if "tiers" in job_types:
            _run("tiers", "manage_tiers", lambda: self.engine.manage_tiers(auto_snapshot=False))

        if "contradictions" in job_types:
            _run("contradictions", "detect_contradictions", self.engine.detect_contradictions)

        if "graduation" in job_types:
            _run("graduation", "graduate_agent_notes", self.engine.graduate_agent_notes)

        if "discover" in job_types:
            _run("discover", "discover_links", self.engine.discover_links)

        # Vacuum after mutating jobs if freelist is bloated or vacuum_after_prune enabled
        if "vacuum" in job_types:
            results["vacuum"] = self.engine.vacuum_db()
        elif any(j in job_types for j in MUTATING_JOBS):
            # auto-vacuum when bloat exceeds threshold and configured
            if self.engine.config.get("vacuum_after_prune", True):
                try:
                    bloat = self.engine.get_bloat_metrics()
                    if bloat.get("needs_vacuum"):
                        results["vacuum"] = self.engine.vacuum_db()
                except Exception:
                    pass

        # Vector index rebuild after mutations: dedup/prune/tier/graduation
        # change node content/importance, leaving stored TF-IDF vectors stale.
        if any(j in job_types for j in MUTATING_JOBS):
            if self.engine.config.get("auto_rebuild_vectors", True):
                results["vector_index_rebuild"] = self.engine.rebuild_vectors()

        duration_s = round(time.time() - start_time, 3)

        job_log_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "target_db": str(self.engine.db_path),
            "duration_seconds": duration_s,
            "snapshot_taken": snapshot_info.get("filename") if snapshot_info and snapshot_info.get("status") == "success" else None,
            "job_types": job_types,
            "results": results,
            "health_after": self.engine.get_health_metrics(),
        }

        # Generate dated markdown audit log in brain/logs/
        report_info = self.engine.generate_markdown_report(job_log_entry)
        job_log_entry["markdown_log"] = report_info.get("log_filename")

        self._record_history(job_log_entry)
        print(f"[BrainScheduler] Completed maintenance run in {duration_s}s. Audit log: {report_info.get('log_filename')}")
        return job_log_entry

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Returns the most recent job run logs."""
        if not self.history_path.exists():
            return []
        try:
            history = json.loads(self.history_path.read_text(encoding="utf-8"))
            return history[-limit:]
        except Exception:
            return []

    def _scheduler_loop(self):
        """Internal daemon loop for recurring interval execution."""
        while not self._stop_event.is_set():
            interval_min = self.get_config().get("interval_minutes", 60)
            interval_sec = interval_min * 60

            # Wait for interval or until stopped
            if self._stop_event.wait(timeout=interval_sec):
                break

            if self.running:
                try:
                    self.run_job_now()
                except Exception as e:
                    print(f"[BrainScheduler] Error during scheduled job execution: {e}")

    def _record_history(self, entry: Dict[str, Any]):
        """Persists job run entries to job_history.json."""
        history = self.get_history(limit=100)
        history.append(entry)
        try:
            self.history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[BrainScheduler] Failed to save history: {e}")


if __name__ == "__main__":
    print("Testing BrainScheduler...")
    scheduler = BrainScheduler()
    res = scheduler.run_job_now(job_types=["dedup", "tiers"])
    print("Job Results:", json.dumps(res, indent=2))
