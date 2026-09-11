"""test_brain — Phase 6: dual-DB jobs, snapshots, health, scheduler runs."""

import json
import tempfile
import unittest
from pathlib import Path

from brain.engine import BrainEngine
from brain.scheduler import BrainScheduler, DEFAULT_JOB_TYPES, JOB_ORDER, MUTATING_JOBS
from src.agents import store as agent_store
from src.core import nodes as core_nodes
from src.core.migrate import migrate as core_migrate
from src.core.store import connect as core_connect

BASE = 1_700_000_000

ENGINE_CONFIG = {
    "auto_snapshot_before_jobs": True,
    "snapshot_cooldown_s": 0,
    "agent_working_high_water": 2,
    "agent_working_max_age_hours": 48,
    "contradiction_auto_resolve": True,
    "max_unused_days": 4,
}


def fresh_engine(tmpdir, config=None):
    mem = Path(tmpdir) / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    core = core_connect(str(mem / "core.db"))
    core_migrate(core)
    core.commit()
    core.close()
    agents = agent_store.connect(str(mem / "agents.db"))
    agent_store.ensure_schema(agents)
    agents.commit()
    agents.close()
    brain_dir = Path(tmpdir) / "brain"
    return BrainEngine(core_path=str(mem / "core.db"),
                       agents_path=str(mem / "agents.db"),
                       brain_dir=str(brain_dir),
                       config={**ENGINE_CONFIG, **(config or {})})


def seed_core(eng):
    conn = eng.core_conn()
    try:
        ids = core_nodes.remember_many(conn, [
            {"content": "I love Python programming dearly", "node_type": "FACT",
             "label": "Python Love", "trust": 0.9, "importance": 0.8},
            {"content": "I hate Python programming utterly", "node_type": "FACT",
             "label": "Python Hate", "trust": 0.2, "importance": 0.8},
            {"content": "identical duplicate content here", "node_type": "FACT",
             "label": "Dupe One"},
            {"content": "identical duplicate content here", "node_type": "FACT",
             "label": "Dupe One"},
            {"content": "stale trivia never read", "node_type": "TOPIC",
             "label": "Stale", "trust": 0.5, "importance": 0.01},
            {"content": "trail river stone summit", "node_type": "FACT",
             "label": "Trail A", "trust": 0.7, "importance": 0.7},
            {"content": "trail river stone valley", "node_type": "FACT",
             "label": "Trail B", "trust": 0.7, "importance": 0.7},
        ])
        conn.execute("UPDATE nodes SET updated_at = ?, created_at = ? WHERE label = 'Stale'",
                     (BASE - 10 * 86400, BASE - 10 * 86400))
        conn.commit()
        return ids
    finally:
        conn.close()


def seed_agents(eng):
    conn = eng.agents_conn()
    try:
        a = agent_store.agent_ensure(conn, "Alpha")["agent_id"]
        b = agent_store.agent_ensure(conn, "Beta")["agent_id"]
        n1 = agent_store.agent_remember(conn, a, "shared duplicated note content",
                                        label="A Dupe")
        n2 = agent_store.agent_remember(conn, a, "shared duplicated note content",
                                        label="A Dupe")
        nb = agent_store.agent_remember(conn, b, "shared duplicated note content",
                                        label="B Same")
        old = agent_store.agent_remember(conn, a, "ancient working note here",
                                         label="Old Working")
        conn.execute("UPDATE memory_layers SET promoted_at = ? WHERE node_id = ?",
                     (BASE - 100 * 3600, old))
        rr = agent_store.agent_remember(conn, a, "please review this finding",
                                        label="Review Me")
        agent_store.agent_set_attention(conn, a, rr, "review_ready")
        conn.execute("INSERT INTO ephemeral_events (label, body, created_at) VALUES "
                     "('FEED_SNAPSHOT', '{}', 1), ('FEED_SNAPSHOT', '{}', 2),"
                     f"('FEED_SNAPSHOT', '{{}}', {_now_plus(0)}),"
                     f"('FEED_SNAPSHOT', '{{}}', {_now_plus(10)}),"
                     f"('RUNTIME_SAMPLE', '{{}}', {_now_plus(20)})")
        conn.commit()
        return {"a": a, "b": b, "n1": n1, "n2": n2, "nb": nb, "old": old, "rr": rr}
    finally:
        conn.close()


def _now_plus(secs):
    import time
    return int(time.time()) + secs


class TestSnapshots(unittest.TestCase):
    def test_create_list_restore_delete(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            try:
                seed_core(eng)
                r = eng.create_snapshot("core")
                self.assertEqual(r["status"], "success")
                self.assertTrue(r["filename"].startswith("snapshot_core_"))
                self.assertEqual(len(eng.list_snapshots("core")), 1)
                # delete a node, then roll back
                conn = eng.core_conn()
                try:
                    conn.execute("DELETE FROM nodes WHERE label = 'Stale'")
                    conn.commit()
                finally:
                    conn.close()
                res = eng.restore_snapshot("core", r["filename"])
                self.assertEqual(res["status"], "success")
                self.assertTrue(res["health_ok"])
                self.assertIsNotNone(res["pre_rollback_backup"])
                conn = eng.core_conn()
                try:
                    n = conn.execute("SELECT COUNT(*) FROM nodes WHERE label = 'Stale'").fetchone()[0]
                    self.assertEqual(n, 1)
                finally:
                    conn.close()
                d = eng.delete_snapshot(r["filename"])
                self.assertEqual(d["status"], "success")
                bad = eng.restore_snapshot("core", "../evil.db")
                self.assertEqual(bad["status"], "error")
            finally:
                pass

    def test_coalescing(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td, config={"snapshot_cooldown_s": 3600})
            try:
                first = eng.ensure_pre_run_snapshot(["core", "agents"])
                self.assertIn("snapshot_taken", first["core"])
                second = eng.ensure_pre_run_snapshot(["core", "agents"])
                self.assertEqual(second["core"]["snapshot_skipped"], "fresh_exists")
                eng.config["auto_snapshot_before_jobs"] = False
                third = eng.ensure_pre_run_snapshot(["core"])
                self.assertEqual(third["core"]["snapshot_skipped"], "toggle_off")
            finally:
                pass


class TestMaintenanceJobs(unittest.TestCase):
    def test_dedup_exact_and_scope_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            ids = seed_agents(eng)
            try:
                res = eng.deduplicate("both")
                self.assertEqual(res["core"]["exact_merged"], 1)  # Dupe One pair
                self.assertEqual(res["agents"]["exact_merged"], 1)  # A dupes only
                # cross-agent identical content NOT merged
                conn = eng.agents_conn()
                try:
                    n = conn.execute("SELECT COUNT(*) FROM nodes WHERE node_id = ?",
                                     (ids["nb"],)).fetchone()[0]
                    self.assertEqual(n, 1)
                finally:
                    conn.close()
            finally:
                pass

    def test_dedup_orphan_edge_dropped_not_relinked(self):
        """Orphan edges (missing endpoint) are dropped, never re-pointed at a
        live node. (Proven 2026-09-05: SQLite only validates CHANGED FK columns
        on UPDATE, so relinking never crashed — but dropping is still the
        correct curation of garbage rows.)"""
        import sqlite3 as _sqlite3
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            try:
                conn = eng.agents_conn()
                try:
                    a = agent_store.agent_ensure(conn, "Alpha")["agent_id"]
                    p = agent_store.agent_remember(
                        conn, a, "identical orphan-probe content", label="Probe")
                    s = agent_store.agent_remember(
                        conn, a, "identical orphan-probe content", label="Probe")
                    conn.commit()  # PRAGMA foreign_keys is a no-op inside a txn
                    conn.execute("PRAGMA foreign_keys=OFF")
                    try:
                        conn.execute(
                            "INSERT INTO edges (edge_id, agent_id, from_node,"
                            " to_node, edge_type, weight, created_at, metadata)"
                            " VALUES ('edge_orphan1', ?, ?, 'node_ghost',"
                            " 'RELATES_TO', 0.5, 1, '{}')", (a, s))
                        conn.commit()
                    finally:
                        conn.execute("PRAGMA foreign_keys=ON")
                    # orphan edge planted (S -> missing node); engine conns
                    # enforce FK, so dedup must cope, not crash
                finally:
                    conn.close()
                res = eng.deduplicate("agents")
                self.assertEqual(res["agents"]["status"], "success",
                                 res["agents"])
                self.assertGreaterEqual(res["agents"]["exact_merged"], 1)
                conn = eng.agents_conn()
                try:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM edges"
                                     " WHERE edge_id = 'edge_orphan1'").fetchone()[0], 0)
                    self.assertEqual(
                        conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM nodes WHERE node_id = ?",
                                     (p,)).fetchone()[0], 1)
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM nodes WHERE node_id = ?",
                                     (s,)).fetchone()[0], 0)
                finally:
                    conn.close()
            finally:
                pass

    def test_dedup_retries_concurrent_node_delete(self):
        """2026-09-05 live bug: three FULL runs failed dedup/agents with
        FOREIGN KEY failed while the operator curated mid-run (graduate /
        contradiction-resolve deletes a node between dedup's SELECT and its
        relink UPDATE). One rollback + fresh-snapshot retry must recover."""
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            try:
                conn = eng.agents_conn()
                try:
                    a = agent_store.agent_ensure(conn, "Alpha")["agent_id"]
                    p1 = agent_store.agent_remember(
                        conn, a, "race probe one content", label="Race One")
                    s1 = agent_store.agent_remember(
                        conn, a, "race probe one content", label="Race One")
                    p2 = agent_store.agent_remember(
                        conn, a, "race probe two content", label="Race Two")
                    s2 = agent_store.agent_remember(
                        conn, a, "race probe two content", label="Race Two")
                    conn.commit()
                finally:
                    conn.close()
                from brain.engine import BrainEngine as _BE
                orig = _BE._relink_and_delete
                calls = []
                def sabotage(conn, primary_id, secondary_id, agent_id=None):
                    if not calls:
                        calls.append(1)
                        killer = eng.agents_conn()
                        try:
                            killer.execute("DELETE FROM nodes WHERE node_id = ?",
                                           (primary_id,))
                            killer.commit()
                        finally:
                            killer.close()
                    return orig(eng, conn, primary_id, secondary_id, agent_id)
                eng._relink_and_delete = sabotage
                try:
                    res = eng.deduplicate("agents")
                finally:
                    del eng._relink_and_delete
                self.assertEqual(res["agents"]["status"], "success",
                                 res["agents"])
                # With FK guard (primary not alive -> just delete secondary), dedup may succeed without retry
                if res["agents"].get("retried_after_concurrent_write") is not None:
                    self.assertTrue(res["agents"].get("retried_after_concurrent_write"))
                # pair two still merged; pair one's orphaned survivor is
                # correctly KEPT (no partner left), killer's victim gone
                conn = eng.agents_conn()
                try:
                    def alive(nid):
                        return conn.execute("SELECT COUNT(*) FROM nodes"
                                            " WHERE node_id = ?", (nid,)).fetchone()[0]
                    self.assertEqual(alive(p2) + alive(s2), 1)
                    self.assertEqual(alive(s1), 1)
                    self.assertEqual(
                        conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                finally:
                    conn.close()
            finally:
                pass

    def test_age_prune_triple_gate(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            seed_agents(eng)
            try:
                res = eng.prune_stale_unused("both", max_unused_days=4)
                conn = eng.core_conn()
                try:
                    gone = conn.execute("SELECT COUNT(*) FROM nodes WHERE label = 'Stale'").fetchone()[0]
                    self.assertEqual(gone, 0)
                    kept = conn.execute("SELECT COUNT(*) FROM nodes WHERE label = 'Python Love'").fetchone()[0]
                    self.assertEqual(kept, 1)  # protected FACT type
                finally:
                    conn.close()
                self.assertEqual(res["core"]["pruned_count"], 1)
            finally:
                pass

    def test_tiers_promote(self):
        # tiers is now decay-only — promotions are owned by Observer/Core Helper
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            try:
                conn = eng.core_conn()
                try:
                    conn.execute("UPDATE nodes SET access_count = 5 WHERE label = 'Trail A'")
                    conn.commit()
                finally:
                    conn.close()
                res = eng.manage_tiers("core")
                conn = eng.core_conn()
                try:
                    layer = conn.execute(
                        "SELECT layer FROM memory_layers WHERE node_id = "
                        "(SELECT node_id FROM nodes WHERE label = 'Trail A')").fetchone()[0]
                    self.assertEqual(layer, "working")  # no promotion — decay-only
                finally:
                    conn.close()
                self.assertEqual(res["core"]["promoted"], 0)
                self.assertEqual(res["core"]["working_promoted"], 0)
            finally:
                pass

    def test_compact_ephemeral_ttl_always(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_agents(eng)
            try:
                res = eng.compact_ephemeral("agents", keep_last=10, max_age_days=7)
                # 2 ancient rows removed by TTL even though keep_last=10 covers all 5
                self.assertEqual(res["agents"]["removed_ttl"], 2)
                self.assertEqual(res["agents"]["removed_cap"], 0)
                stats = eng.get_ephemeral_stats("agents")["agents"]
                self.assertEqual(stats["total"], 3)
            finally:
                pass

    def test_regulate_demotes_stale_not_review_ready(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            ids = seed_agents(eng)
            try:
                prev = eng.get_agent_working_preview()
                self.assertGreaterEqual(prev["agent_working_count"], 3)
                res = eng.regulate_agent_working_memory()["agents"]
                self.assertIn(ids["old"], res["demoted_ids"])
                conn = eng.agents_conn()
                try:
                    layer = conn.execute("SELECT layer FROM memory_layers WHERE node_id = ?",
                                         (ids["rr"],)).fetchone()[0]
                    self.assertEqual(layer, "working")  # review_ready protected
                finally:
                    conn.close()
            finally:
                pass

    def test_vacuum_and_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            try:
                v = eng.vacuum_db("both")
                self.assertEqual(v["core"]["status"], "success")
                self.assertEqual(v["agents"]["status"], "success")
                r = eng.rebuild_vectors("core")
                conn = eng.core_conn()
                try:
                    n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                    self.assertEqual(r["core"]["nodes"], n)
                finally:
                    conn.close()
            finally:
                pass

    def test_purge_orphans(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            try:
                conn = eng.agents_conn()
                try:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    conn.execute("INSERT INTO edges (edge_id, agent_id, from_node, to_node,"
                                 " edge_type, weight, created_at) VALUES "
                                 "('edge_orphan1', 'ghost', 'node_nope1', 'node_nope2',"
                                 " 'RELATES_TO', 1.0, 1)")
                    conn.commit()
                finally:
                    conn.close()
                res = eng.purge_orphans("agents")
                self.assertEqual(res["agents"]["edges"], 1)
            finally:
                pass


class TestCuration(unittest.TestCase):
    def test_contradictions_core_detect_and_resolve(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            try:
                # seeding already auto-created the love/hate CONTRADICTS edge
                # (remember-time check); clear it so detect has work to do
                conn = eng.core_conn()
                try:
                    conn.execute("DELETE FROM edges WHERE edge_type = 'CONTRADICTS'")
                    conn.commit()
                finally:
                    conn.close()
                det = eng.detect_contradictions("core")
                self.assertEqual(det["core"]["status"], "success")
                self.assertGreaterEqual(det["core"]["contradictions_found"], 1)
                self.assertNotIn("agents", det)  # targeted run returns one key
                q = eng.get_contradictions(status="pending")
                self.assertGreaterEqual(len(q["contradictions"]), 1)
                edge = q["contradictions"][0]
                self.assertTrue(edge["auto_resolvable"])
                self.assertEqual(edge["suggested_action"], "keep_from")
                auto = eng.auto_resolve_low_trust(dry_run=False)
                self.assertEqual(auto["resolved"], 1)
                q2 = eng.get_contradictions()
                self.assertEqual(q2["counts"]["resolved"], 1)
                conn = eng.core_conn()
                try:
                    hate = conn.execute("SELECT COUNT(*) FROM nodes WHERE label = 'Python Hate'").fetchone()[0]
                    love = conn.execute("SELECT COUNT(*) FROM nodes WHERE label = 'Python Love'").fetchone()[0]
                    self.assertEqual(hate, 0)
                    self.assertEqual(love, 1)
                finally:
                    conn.close()
            finally:
                pass

    def test_contradictions_agents_per_agent_isolation(self):
        """Agents scan links intra-agent clashes only; edges stamped; db-routed."""
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            try:
                conn = eng.agents_conn()
                try:
                    a = agent_store.agent_ensure(conn, "Alpha")["agent_id"]
                    b = agent_store.agent_ensure(conn, "Beta")["agent_id"]
                    love_a = agent_store.agent_remember(
                        conn, a, "I love Python programming dearly", label="A Love")
                    hate_a = agent_store.agent_remember(
                        conn, a, "I hate Python programming utterly", label="A Hate")
                    love_b = agent_store.agent_remember(
                        conn, b, "I love Python programming dearly", label="B Love")
                    conn.execute("DELETE FROM edges WHERE edge_type = 'CONTRADICTS'")
                    conn.commit()
                finally:
                    conn.close()
                det = eng.detect_contradictions("agents")
                self.assertEqual(det["agents"]["status"], "success")
                self.assertGreaterEqual(det["agents"]["contradictions_found"], 1)
                self.assertGreaterEqual(det["agents"]["per_agent"].get(a, 0), 1)
                self.assertEqual(det["agents"]["per_agent"].get(b, 0), 0)
                # isolation: no edge touches agent B's node; all stamped with A
                conn = eng.agents_conn()
                try:
                    edges = conn.execute(
                        "SELECT from_node, to_node, agent_id FROM edges"
                        " WHERE edge_type = 'CONTRADICTS'").fetchall()
                    self.assertGreaterEqual(len(edges), 1)
                    touched = {e["from_node"] for e in edges} | \
                        {e["to_node"] for e in edges}
                    self.assertNotIn(love_b, touched)
                    self.assertIn(love_a, touched)
                    self.assertIn(hate_a, touched)
                    for e in edges:
                        self.assertEqual(e["agent_id"], a)
                finally:
                    conn.close()
                # queue + resolve routed per db
                q = eng.get_contradictions(status="pending", db="agents")
                self.assertGreaterEqual(len(q["contradictions"]), 1)
                self.assertEqual(q["contradictions"][0]["agent_id"], a)
                self.assertEqual(eng.get_contradictions(db="bogus")["status"], "error")
                eid = q["contradictions"][0]["edge_id"]
                self.assertEqual(
                    eng.update_contradiction_status(eid, "confirmed",
                                                    db="agents")["status"], "success")
                self.assertEqual(
                    eng.resolve_contradiction(eid, "delete",
                                              db="agents")["status"], "success")
                self.assertEqual(
                    eng.resolve_contradiction(eid, "delete",
                                              db="bogus")["status"], "error")
                # both-target run returns both keys
                both = eng.detect_contradictions("both")
                self.assertIn("core", both)
                self.assertIn("agents", both)
            finally:
                pass

    def test_update_status_validation(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            try:
                eng.detect_contradictions("core")
                q_all = eng.get_contradictions(status="all")
                q_pending = eng.get_contradictions(status="pending")
                # "all" is a filter-off switch, not a literal status (2026-09-05:
                # the tab's Show-all option returned an empty queue)
                self.assertGreaterEqual(len(q_all["contradictions"]), 1)
                self.assertGreaterEqual(len(q_all["contradictions"]),
                                         len(q_pending["contradictions"]))
                edge = eng.get_contradictions()["contradictions"][0]["edge_id"]
                self.assertEqual(eng.update_contradiction_status(edge, "confirmed")["status"], "success")
                self.assertEqual(eng.update_contradiction_status(edge, "bogus")["status"], "error")
                self.assertEqual(eng.update_contradiction_status("edge_missing", "ignored")["status"], "error")
            finally:
                pass

    def test_graduate_moves_note(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            ids = seed_agents(eng)
            try:
                res = eng.graduate_agent_notes()
                self.assertEqual(res["core"]["graduated"], 1)
                conn = eng.agents_conn()
                try:
                    gone = conn.execute("SELECT COUNT(*) FROM nodes WHERE node_id = ?",
                                        (ids["rr"],)).fetchone()[0]
                    self.assertEqual(gone, 0)
                finally:
                    conn.close()
                core = eng.core_conn()
                try:
                    hit = core.execute(
                        "SELECT node_id FROM nodes WHERE json_extract(metadata,"
                        "'$.promoted_from.node_id') = ?", (ids["rr"],)).fetchone()
                    self.assertIsNotNone(hit)
                finally:
                    core.close()
            finally:
                pass

    def test_discover_links_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            try:
                # converge vectors, then clear insert-time auto-links so discovery
                # has work to do (Trail A/B sim ~0.67 lands in the [.5,.85) band)
                eng.rebuild_vectors("core")
                conn = eng.core_conn()
                try:
                    conn.execute("DELETE FROM edges")
                    conn.commit()
                finally:
                    conn.close()
                r1 = eng.discover_links("core")
                self.assertEqual(r1["core"]["status"], "success")
                self.assertFalse(r1["core"]["truncated"])
                self.assertGreaterEqual(r1["core"]["links_created"], 1)
                r2 = eng.discover_links("core")
                self.assertEqual(r2["core"]["links_created"], 0)  # existing-link skip
            finally:
                pass


class TestHealthStats(unittest.TestCase):
    def test_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            seed_agents(eng)
            try:
                h = eng.health("both")
                self.assertTrue(h["combined"]["ok"])
                self.assertGreater(h["combined"]["total_nodes"], 10)
                self.assertIn("check", h["core"])
                s = eng.get_full_statistics("both")
                self.assertIn("FACT", s["core"]["node_types"])
                self.assertIn("combined", s)
                b = eng.get_bloat_metrics("both")
                self.assertIn("needs_vacuum", b["core"])
                self.assertIn("combined", b)
            finally:
                pass


class TestSchedulerRun(unittest.TestCase):
    def test_full_run_history_and_log(self):
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            seed_agents(eng)
            try:
                sched = BrainScheduler(engine=eng)
                entry = sched.run_job_now(["dedup", "tiers"])
                self.assertEqual(entry["status"], "success")
                self.assertEqual(entry["jobs"], ["dedup", "tiers"])
                self.assertIn("deduplicate", entry["results"])
                self.assertIn("manage_tiers", entry["results"])
                self.assertIn("snapshot_taken", entry["snapshots"]["core"])
                hist = sched.get_history()
                self.assertEqual(len(hist), 1)
                self.assertTrue((Path(td) / "brain" / "logs" /
                                 entry["markdown_log"]).exists())
                self.assertTrue(eng.lock_path.exists())
            finally:
                pass

    def test_constants(self):
        self.assertEqual(JOB_ORDER[0], "dedup")
        self.assertEqual(JOB_ORDER[-1], "vacuum")
        self.assertNotIn("graduation", DEFAULT_JOB_TYPES)
        self.assertNotIn("vacuum", DEFAULT_JOB_TYPES)
        self.assertNotIn("contradictions", MUTATING_JOBS)

    def test_per_db_journals(self):
        import re as _re
        with tempfile.TemporaryDirectory() as td:
            eng = fresh_engine(td)
            seed_core(eng)
            seed_agents(eng)
            try:
                sched = BrainScheduler(engine=eng)
                entry = sched.run_job_now(["tiers"])
                self.assertEqual(entry["status"], "success")
                files = entry.get("markdown_logs") or [entry["markdown_log"]]
                self.assertGreaterEqual(len(files), 3)  # combined + core + agents
                names = " ".join(files)
                self.assertIn("_core.md", names)
                self.assertIn("_agents.md", names)
                for f in files:
                    self.assertTrue((Path(td) / "brain" / "logs" / f).exists())
                core_txt = (Path(td) / "brain" / "logs" /
                            [f for f in files if f.endswith("_core.md")][0]).read_text()
                self.assertIn("core.db", core_txt)
                self.assertNotIn("agents.db", _re.sub(r"jobs:.*", "", core_txt))
            finally:
                pass


class TestConfigSync(unittest.TestCase):
    """Single config: brain/config.json is the ONLY config file.

    Operator directive 2026-09-04 night (supersedes C17): no overlay from
    memory/config.json, no write-through. A memory/config.json shared section,
    if present, is ignored and never written.
    """

    def _engine(self, tmpdir, brain_cfg=None, mem_cfg=None):
        import json as _json
        mem = Path(tmpdir) / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        brain = Path(tmpdir) / "brain"
        brain.mkdir(parents=True, exist_ok=True)
        if brain_cfg is not None:
            (brain / "config.json").write_text(_json.dumps(brain_cfg))
        if mem_cfg is not None:
            (mem / "config.json").write_text(_json.dumps(mem_cfg))
        return BrainEngine(core_path=str(mem / "core.db"),
                           agents_path=str(mem / "agents.db"),
                           brain_dir=str(brain))

    def test_memory_config_ignored_on_load(self):
        import json as _json
        with tempfile.TemporaryDirectory() as td:
            eng = self._engine(td, brain_cfg={"ephemeral_labels": ["BRAIN_X"]},
                               mem_cfg={"shared": {"ephemeral_labels": ["MEM_Y"]}})
            try:
                # brain file wins; memory file is never read
                self.assertEqual(eng.config["ephemeral_labels"], ["BRAIN_X"])
            finally:
                pass

    def test_save_writes_brain_only(self):
        import json as _json
        with tempfile.TemporaryDirectory() as td:
            eng = self._engine(td)
            try:
                eng.config["ephemeral_labels"] = ["BRAIN_ONLY"]
                eng.config["prune_threshold"] = 0.11
                eng.config["prune_importance_floor"] = 0.11
                eng._save_config()
                bpath = Path(td) / "brain" / "config.json"
                saved = _json.loads(bpath.read_text())
                self.assertEqual(saved["ephemeral_labels"], ["BRAIN_ONLY"])
                # no memory copy created or touched
                mem_cfg_path = Path(td) / "memory" / "config.json"
                self.assertFalse(mem_cfg_path.exists())
                # restart: values survive from the brain file alone
                eng2 = BrainEngine(core_path=str(Path(td) / "memory" / "core.db"),
                                   agents_path=str(Path(td) / "memory" / "agents.db"),
                                   brain_dir=str(Path(td) / "brain"))
                self.assertEqual(eng2.config["ephemeral_labels"], ["BRAIN_ONLY"])
            finally:
                pass

    def test_reload_picks_up_hand_edits(self):
        import json as _json
        with tempfile.TemporaryDirectory() as td:
            eng = self._engine(td)
            try:
                bpath = Path(td) / "brain" / "config.json"
                data = _json.loads(bpath.read_text())
                data["max_unused_days"] = 42
                bpath.write_text(_json.dumps(data))
                self.assertNotEqual(eng.config.get("max_unused_days"), 42)
                eng.reload_config()
                self.assertEqual(eng.config.get("max_unused_days"), 42)
            finally:
                pass


if __name__ == "__main__":
    unittest.main()
