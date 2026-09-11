"""test_bridge_promote — Phase 4: review queue, cross-agent search, idempotent move."""

import tempfile
import unittest
from pathlib import Path

from src.agents import bridge, store
from src.core.migrate import migrate as core_migrate
from src.core.store import connect as core_connect


def fresh_pair(tmpdir):
    core = core_connect(str(Path(tmpdir) / "core.db"))
    core_migrate(core)
    core.commit()
    agents = store.connect(str(Path(tmpdir) / "agents.db"))
    store.ensure_schema(agents)
    agents.commit()
    return core, agents


class TestReviewQueue(unittest.TestCase):
    def test_sql_limit_and_stats(self):
        with tempfile.TemporaryDirectory() as td:
            core, agents = fresh_pair(td)
            try:
                a = store.agent_ensure(conn=agents, job_hint="Scout")["agent_id"]
                for i in range(5):
                    store.agent_remember(agents, a, f"finding number {i} ready",
                                         label=f"Finding {i}")
                ready = [n["node_id"] for n in
                         store.agent_digest(agents, a, limit=5)]
                for nid in ready[:3]:
                    store.agent_set_attention(agents, a, nid, "review_ready")
                agents.commit()
                q = bridge.list_review_queue(agents, limit=2)
                self.assertEqual(len(q), 2)  # SQL LIMIT honored (C18)
                self.assertTrue(all(n["metadata"]["attention_state"] == "review_ready"
                                    for n in q))
                stats = bridge.review_stats(agents)
                self.assertEqual(stats, {"total": 5, "review_ready": 3,
                                         "graduable": 3, "private": 2})
            finally:
                core.close()
                agents.close()


class TestSearchAcrossAgents(unittest.TestCase):
    def test_search_skips_verified_and_ranks(self):
        with tempfile.TemporaryDirectory() as td:
            core, agents = fresh_pair(td)
            try:
                a = store.agent_ensure(agents, "Alpha")["agent_id"]
                b = store.agent_ensure(agents, "Beta")["agent_id"]
                store.agent_remember(agents, a, "quantum lattice coherence alpha",
                                     label="QA")
                store.agent_remember(agents, b, "quantum lattice probe beta",
                                     label="QB")
                store.agent_remember(agents, b, "gardening roses bloom",
                                     label="Garden")
                agents.commit()
                hits = bridge.search_all_agents(agents, "quantum lattice", bound=10)
                self.assertEqual(len(hits), 2)
                self.assertEqual({h["_agent_id"] for h in hits}, {a, b})
                sims = [h["_similarity"] for h in hits]
                self.assertEqual(sims, sorted(sims, reverse=True))
                # promoted (verified) notes disappear from cross-agent search
                cid = bridge.promote(core, agents, a,
                                     store.agent_digest(agents, a, 10)[0]["node_id"])
                hits2 = bridge.search_all_agents(agents, "quantum lattice", bound=10)
                self.assertEqual(len(hits2), 1)
                self.assertEqual(hits2[0]["_agent_id"], b)
                self.assertTrue(cid)
            finally:
                core.close()
                agents.close()


class TestPromote(unittest.TestCase):
    def _seed(self, agents, label="Promote Me", body="promotable insight regardless"):
        a = store.agent_ensure(agents, "Promoter")["agent_id"]
        nid = store.agent_remember(agents, a, body, label=label,
                                   metadata={"importance": "high"})
        agents.commit()
        return a, nid

    def test_move_provenance_and_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            core, agents = fresh_pair(td)
            try:
                a, nid = self._seed(agents)
                cid = bridge.promote(core, agents, a, nid, new_type="FACT",
                                     new_label="Promoted Fact")
                # source clean-removed, no tombstone
                self.assertIsNone(store.agent_get_node(agents, a, nid))
                self.assertEqual(
                    agents.execute("SELECT COUNT(*) FROM nodes WHERE node_id = ?",
                                   (nid,)).fetchone()[0], 0)
                # core row carries provenance
                row = core.execute("SELECT * FROM nodes WHERE node_id = ?",
                                   (cid,)).fetchone()
                self.assertEqual(row["node_type"], "FACT")
                self.assertEqual(row["label"], "Promoted Fact")
                import json
                meta = json.loads(row["metadata"])
                self.assertEqual(meta["attention_state"], "core_verified")
                self.assertEqual(meta["promoted_from"], {"agent_id": a, "node_id": nid})
                self.assertIn("promoted_at", meta)
                self.assertGreaterEqual(row["trust_level"], 0.8)
                # layers + vectors maintained (v2 graduate path forgot these)
                self.assertTrue(core.execute(
                    "SELECT 1 FROM memory_layers WHERE node_id = ?",
                    (cid,)).fetchone())
                self.assertTrue(core.execute(
                    "SELECT 1 FROM node_vectors WHERE node_id = ?",
                    (cid,)).fetchone())
                # core recall sees it
                from src.core import recall as core_recall
                res = core_recall.recall(core, "promotable insight", mode="RELATED",
                                         bound=10)
                core.commit()
                self.assertIn(cid, [n["node_id"] for n in res["nodes"]])
            finally:
                core.close()
                agents.close()

    def test_idempotent_retry(self):
        with tempfile.TemporaryDirectory() as td:
            core, agents = fresh_pair(td)
            try:
                a, nid = self._seed(agents)
                c1 = bridge.promote(core, agents, a, nid)
                # source already gone; simulate crash-retry by re-inserting a
                # same-ID source row, then promote again -> converges, no double
                agents.execute(
                    "INSERT INTO nodes (node_id, agent_id, node_type, label, content,"
                    " source, trust_level, created_at, updated_at, access_count,"
                    " importance, checksum, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, a, "AGENT_NOTE", "Promote Me", "promotable insight regardless",
                     f"AGENT_{a}", 0.5, 1, 1, 0, 0.5, "abc",
                     '{"attention_state":"agent_private"}'))
                agents.commit()
                c2 = bridge.promote(core, agents, a, nid)
                self.assertEqual(c1, c2)
                n = core.execute("SELECT COUNT(*) FROM nodes WHERE node_id = ?",
                                 (c1,)).fetchone()[0]
                self.assertEqual(n, 1)
            finally:
                core.close()
                agents.close()

    def test_collision_mints_new_id(self):
        with tempfile.TemporaryDirectory() as td:
            core, agents = fresh_pair(td)
            try:
                a, nid = self._seed(agents)
                # squat the node_id in core.db first (direct row insert)
                core.execute(
                    "INSERT INTO nodes (node_id, node_type, label, content, source,"
                    " trust_level, created_at, updated_at, access_count, importance,"
                    " checksum, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, "FACT", "Squatter", "squatter content here", "CORE",
                     0.5, 1, 1, 0, 0.5, "deadbeef", "{}"))
                core.commit()
                cid = bridge.promote(core, agents, a, nid)
                self.assertNotEqual(cid, nid)
                self.assertTrue(cid.startswith("node_"))
                import json
                meta = json.loads(core.execute(
                    "SELECT metadata FROM nodes WHERE node_id = ?",
                    (cid,)).fetchone()["metadata"])
                self.assertEqual(meta["original_agents_node_id"], nid)
            finally:
                core.close()
                agents.close()

    def test_isolation_and_type_validation(self):
        with tempfile.TemporaryDirectory() as td:
            core, agents = fresh_pair(td)
            try:
                a, nid = self._seed(agents)
                b = store.agent_ensure(agents, "Intruder")["agent_id"]
                with self.assertRaises(ValueError):
                    bridge.promote(core, agents, b, nid)
                with self.assertRaises(ValueError):
                    bridge.promote(core, agents, "ghost", nid)
                with self.assertRaises(ValueError):
                    bridge.promote(core, agents, a, "node_missing")
                with self.assertRaises(ValueError):
                    bridge.promote(core, agents, a, nid, new_type="AGENT_NOTE")
                with self.assertRaises(ValueError):
                    bridge.promote(core, agents, a, nid, new_type="NOPE")
            finally:
                core.close()
                agents.close()

    def test_get_agent_note_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            core, agents = fresh_pair(td)
            try:
                a, nid = self._seed(agents)
                b = store.agent_ensure(agents, "Other")["agent_id"]
                self.assertIsNotNone(bridge.get_agent_note(agents, a, nid))
                self.assertIsNone(bridge.get_agent_note(agents, b, nid))
            finally:
                core.close()
                agents.close()


if __name__ == "__main__":
    unittest.main()
