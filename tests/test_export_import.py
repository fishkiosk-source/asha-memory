"""test_export_import — Phase 8: migration trial on a mini-v2 oracle DB.

Builds a small v2 DB via AshaMemory (tmp only — v2 folder untouched) covering:
private/review_ready/verified agent notes, core + agent telemetry, cross-scope
edge, then runs the splitter and asserts reconciliation, aliases, moves,
health, and dry-run cleanliness.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

V2_DIR = "D:/opencode/asha_memory/asha_memory v2"
if V2_DIR not in sys.path:
    sys.path.insert(0, V2_DIR)

from asha_memory_v2 import AshaMemory  # noqa: E402 (oracle, writes to tmp only)

from tools.migrate_v2_to_v3 import migrate  # noqa: E402


def seed_mini_v2(d):
    Path(d).mkdir(parents=True, exist_ok=True)
    with open(Path(d) / "config.json", "w") as fh:
        json.dump({"internal_clock": False}, fh)
    mem = AshaMemory(base_path=d)
    core_fact = mem.remember("core knowledge stays core", "FACT", label="Core Fact")
    mem.remember('{"timestamp": 5, "status": "ok"}', "FACT", label="FEED_SNAPSHOT")
    priv = mem.agent_remember("scout-1", "private scratch note", label="Priv")
    ready = mem.agent_remember("scout-1", "ready finding note", label="Ready")
    mem.agent_set_attention("scout-1", ready, "review_ready")
    tele = mem.agent_remember("scout-1", '{"timestamp": 6, "load1m": 0.5}',
                              label="Agent Tele")
    ver = mem.agent_remember("scout-1", '{"timestamp": 7, "status": "ok"}',
                             label="Ver Tele")
    mem.promote_to_core("scout-1", ver)
    mem.agent_remember("Other_Agent", "other agent work here", label="Other")
    mem.relate(core_fact, priv, "RELATES_TO", 0.9)  # cross-scope edge (dropped)
    return mem


class TestMigrate(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            seed_mini_v2(str(Path(td) / "v2"))
            v3mem = str(Path(td) / "v3mem")
            rep = migrate(str(Path(td) / "v2"), v3mem,
                          v3_brain_dir=str(Path(td) / "v3brain"), dry_run=True)
            self.assertEqual(rep["status"], "success")
            self.assertTrue(rep["dry_run"])
            self.assertFalse(Path(v3mem).exists())
            self.assertTrue(rep["reconcile_nodes"]["ok"])
            self.assertTrue(rep["edges"]["reconcile_ok"])

    def test_full_trial(self):
        with tempfile.TemporaryDirectory() as td:
            seed_mini_v2(str(Path(td) / "v2"))
            v3mem = str(Path(td) / "v3mem")
            rep = migrate(str(Path(td) / "v2"), v3mem,
                          v3_brain_dir=str(Path(td) / "v3brain"))
            self.assertEqual(rep["status"], "success")
            self.assertTrue(rep["reconcile_nodes"]["ok"], rep["reconcile_nodes"])
            self.assertTrue(rep["edges"]["reconcile_ok"], rep["edges"])
            self.assertTrue(rep["health"]["ok"], rep["health"])
            # split: 2 core nodes (fact + verified) + 3 agent notes + 2 ephemeral
            self.assertEqual(rep["moved"]["core_nodes"], 2)
            self.assertEqual(rep["moved"]["agents_notes"], 3)
            self.assertEqual(rep["moved"]["ephemeral_core"], 1)
            self.assertEqual(rep["moved"]["ephemeral_agents"], 1)
            self.assertEqual(len(rep["moved"]["verified_telemetry_kept_core"]), 1)
            # cross-scope edge dropped + counted
            self.assertEqual(rep["edges"]["dropped_by_reason"].get("cross-scope"), 1)
            # registry: scout-1 valid -> preserved; Other_Agent -> canonical + alias
            canon = rep["agents"]["canonical"]
            self.assertEqual(canon["scout-1"], "scout-1")
            self.assertNotEqual(canon["Other_Agent"], "Other_Agent")
            # review queue + recall work on the migrated stores
            from src.agents import bridge as agent_bridge
            from src.agents import store as agent_store
            from src.core import recall as core_recall
            from src.core.store import connect as core_connect
            core = core_connect(str(Path(v3mem) / "core.db"))
            try:
                res = core_recall.recall(core, "core knowledge", mode="RELATED", bound=10)
                core.commit()
                self.assertIn("Core Fact", [n["label"] for n in res["nodes"]])
            finally:
                core.close()
            agents = agent_store.connect(str(Path(v3mem) / "agents.db"))
            try:
                q = agent_bridge.list_review_queue(agents)
                self.assertEqual(len(q), 1)
                self.assertEqual(q[0]["label"], "Ready")
                self.assertIsNotNone(agent_store.resolve_agent(agents, "Other_Agent"))
                eph = agents.execute("SELECT COUNT(*) FROM ephemeral_events").fetchone()[0]
                self.assertEqual(eph, 1)
            finally:
                agents.close()
            # configs written: brain-owned live config, memory file has no shared section
            mem_cfg = json.loads((Path(v3mem) / "config.json").read_text())
            self.assertNotIn("shared", mem_cfg)
            self.assertIn("core", mem_cfg)


if __name__ == "__main__":
    unittest.main()
