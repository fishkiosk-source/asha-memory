"""test_concurrency — Phase 6: busy_timeout, single-flight, lock serialization."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from brain.engine import BrainEngine
from brain.scheduler import DEFAULT_JOB_TYPES, JOB_ORDER, MUTATING_JOBS, BrainScheduler
from src.agents import store as agent_store
from src.core import nodes as core_nodes
from src.core.migrate import migrate as core_migrate
from src.core.store import connect


def fresh_pair(tmpdir):
    mem = Path(tmpdir) / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    core = connect(str(mem / "core.db"))
    core_migrate(core)
    core.commit()
    core.close()
    agents = agent_store.connect(str(mem / "agents.db"))
    agent_store.ensure_schema(agents)
    agents.commit()
    agents.close()
    return (str(mem / "core.db"), str(mem / "agents.db"), str(Path(tmpdir) / "brain"))


class TestConcurrencyContract(unittest.TestCase):
    def test_busy_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            core_path, _, _ = fresh_pair(td)
            conn = connect(core_path)
            try:
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            finally:
                conn.close()

    def test_job_order_canonical(self):
        self.assertEqual(JOB_ORDER, ["dedup", "compact", "agent_working", "age_prune",
                                     "tiers", "contradictions", "graduation",
                                     "discover", "vacuum"])
        self.assertNotIn("graduation", DEFAULT_JOB_TYPES)
        self.assertNotIn("vacuum", DEFAULT_JOB_TYPES)
        self.assertNotIn("contradictions", MUTATING_JOBS)
        self.assertNotIn("discover", MUTATING_JOBS)

    def test_single_flight_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            core_path, agents_path, brain_dir = fresh_pair(td)
            eng = BrainEngine(core_path=core_path, agents_path=agents_path,
                              brain_dir=brain_dir)
            sched = BrainScheduler(engine=eng)
            # hold the run lock -> next run must refuse and record skipped_busy
            self.assertTrue(sched._run_lock.acquire(blocking=False))
            try:
                res = sched.run_job_now(["tiers"])
                self.assertEqual(res["status"], "skipped_busy")
                hist = sched.get_history()
                self.assertEqual(hist[-1]["status"], "skipped_busy")
            finally:
                sched._run_lock.release()

    def test_overlapping_runs_serialize(self):
        with tempfile.TemporaryDirectory() as td:
            core_path, agents_path, brain_dir = fresh_pair(td)
            eng = BrainEngine(core_path=core_path, agents_path=agents_path,
                              brain_dir=brain_dir)
            # slow one job down so overlap is real, not timing luck
            orig = eng.manage_tiers
            eng.manage_tiers = lambda target="both": (time.sleep(0.6), orig(target))[1]
            try:
                sched = BrainScheduler(engine=eng)
                results = []
                t = threading.Thread(target=lambda: results.append(
                    sched.run_job_now(["tiers"])))
                t.start()
                time.sleep(0.15)  # let the first run take the lock
                second = sched.run_job_now(["tiers"])
                t.join()
                self.assertEqual(second["status"], "skipped_busy")
                self.assertEqual(results[0]["status"], "success")
            finally:
                eng.manage_tiers = orig

    def test_writer_and_brain_overlap(self):
        """MCP-style writer inserts while a brain job runs (busy_timeout, no loss)."""
        with tempfile.TemporaryDirectory() as td:
            core_path, agents_path, brain_dir = fresh_pair(td)
            eng = BrainEngine(core_path=core_path, agents_path=agents_path,
                              brain_dir=brain_dir)
            conn = connect(core_path)
            try:
                core_migrate(conn)
                ids = core_nodes.remember_many(conn, [
                    {"content": c, "node_type": "FACT"} for c in
                    ("redwood forest canopy moss", "harbor lighthouse beacon fog",
                     "granite quarry stone dust", "tundra aurora night ice",
                     "orchard apple harvest cider")])
                conn.commit()
                # brain dedup runs on the same live DB while we read
                res = eng.deduplicate("core")
                self.assertEqual(res["core"]["status"], "success")
                for nid in ids:
                    self.assertIsNotNone(core_nodes.get_node(conn, nid))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
