"""test_recall_modes — Phase 3: parity vs v2 oracle + DSL + offset.

Strategy: the SAME deterministic fixture is seeded into a throwaway v2 DB
(under tempfile — the v2 folder itself is never touched, look-up only) and
into a v3 DB. Timestamps/access counters are re-stamped per label in setUp
so every test is independent of recall side-effects (bumps). Ordered label
lists must match exactly; SEMANTIC similarities must match to 3dp after
vectors.rebuild_all (incremental staleness converged — documents §9.1 trade).

v2 clock disabled via pre-written config (no TODAY node in the oracle DB).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

V2_DIR = "D:/opencode/asha_memory/asha_memory v2"
if V2_DIR not in sys.path:
    sys.path.insert(0, V2_DIR)

from asha_memory_v2 import AshaMemory  # noqa: E402  (oracle only, writes to tmp)

from src.core import nodes, recall  # noqa: E402
from src.core.migrate import migrate  # noqa: E402
from src.core.store import connect as v3_connect  # noqa: E402
from src.core.vectors import rebuild_all  # noqa: E402
from src.core import edges as v3_edges  # noqa: E402

BASE = 1_700_000_000
BOUND = 50

# (label, node_type, content, trust, importance, metadata)
FIXTURE = [
    ("Sam Rivera", "PERSON", "Sam Rivera is a designer", 0.9, 0.9, {}),
    ("Dark Mode", "PREFERENCE", "Sam Rivera prefers dark mode interfaces", 0.9, 0.8, {}),
    ("Night Shifts", "FACT", "Sam Rivera works nights often", 0.9, 0.5, {}),
    ("Memory Systems", "TOPIC", "Memory Systems organize recall", 0.8, 0.9, {}),
    ("Graphs Beat Lists", "FACT", "Graphs beat lists for recall", 0.9, 0.9, {}),
    ("Vectors Add Meaning", "FACT", "Vectors add meaning beyond keywords", 0.8, 0.6, {}),
    ("Two-deep Hop Note", "FACT", "Two-deep hop note linked onward", 0.8, 0.7, {}),
    ("Launch Day", "EVENT", "Launch Day shipped the memory graph", 0.7, 0.7, {}),
    ("First Recall", "EVENT", "First Recall answered a question", 0.7, 0.6, {}),
    ("Python Love", "FACT", "I love Python programming dearly", 0.6, 0.6, {}),
    ("Python Hate", "FACT", "I hate Python programming utterly", 0.6, 0.6, {}),
    ("Quantum Lattice A", "FACT", "quantum lattice coherence study alpha", 0.9, 0.9, {}),
    ("Quantum Lattice B", "FACT", "quantum lattice probe results beta", 0.5, 0.5, {}),
    ("Path Alpha", "TOPIC", "alpha beginnings sunrise", 0.8, 0.8, {}),
    ("Path Middle", "FACT", "middle passages river", 0.8, 0.8, {}),
    ("Path Omega", "FACT", "omega endings sunset", 0.8, 0.8, {}),
    ("Private Agent Note", "AGENT_NOTE", "private scratchpad sketch", 0.5, 0.5,
     {"attention_state": "agent_private", "agent_scoped": True}),
    ("Verified Agent Note", "AGENT_NOTE", "verified finding for core", 0.9, 0.9,
     {"attention_state": "core_verified"}),
    ("Stale Trivia", "FACT", "stale trivia nobody reads", 0.3, 0.01, {}),
    ("Unrelated Gardening", "FACT", "gardening roses bloom slowly", 0.5, 0.5, {}),
]

LINKS = [
    ("Sam Rivera", "Dark Mode", "RELATES_TO"),
    ("Sam Rivera", "Night Shifts", "RELATES_TO"),
    ("Memory Systems", "Graphs Beat Lists", "RELATES_TO"),
    ("Memory Systems", "Vectors Add Meaning", "RELATES_TO"),
    ("Graphs Beat Lists", "Two-deep Hop Note", "RELATES_TO"),
    ("Memory Systems", "Launch Day", "RELATES_TO"),
    ("Memory Systems", "First Recall", "RELATES_TO"),
    ("Path Alpha", "Path Middle", "RELATES_TO"),
    ("Path Middle", "Path Omega", "RELATES_TO"),
]


def seed_v2(d):
    Path(d).mkdir(parents=True, exist_ok=True)
    with open(Path(d) / "config.json", "w") as fh:
        json.dump({"internal_clock": False}, fh)
    mem = AshaMemory(base_path=d)
    ids = {}
    for label, ntype, content, trust, imp, meta in FIXTURE:
        ids[label] = mem.remember(
            content, ntype, label=label, trust=trust, importance=imp, metadata=dict(meta))
    for a, b, et in LINKS:
        mem.relate(ids[a], ids[b], et, 0.9)
    # Converge v2's mixed-vintage per-insert vectors to final-idf (v3 test side
    # calls rebuild_all after seeding; without this, similarities differ ~3e-3)
    mem.rebuild_vector_index()
    return mem


def seed_v3(conn):
    ids = nodes.remember_many(conn, [
        {"content": c, "node_type": t, "label": lab, "trust": tr,
         "importance": im, "metadata": dict(m)}
        for lab, t, c, tr, im, m in FIXTURE])
    idmap = {lab: nid for (lab, *_), nid in zip(FIXTURE, ids)}
    for a, b, et in LINKS:
        v3_edges.relate(conn, idmap[a], idmap[b], et, 0.9)
    conn.commit()
    rebuild_all(conn)
    conn.commit()
    return idmap


def restamp_v2(mem):
    # v2 _core_conn is a context manager (commits on clean exit)
    with mem._core_conn() as conn:
        for i, (lab, *_rest) in enumerate(FIXTURE):
            if lab == "Stale Trivia":
                ts = BASE - 40 * 86400
            else:
                ts = BASE + i * 100
            conn.execute("UPDATE nodes SET created_at = ?, updated_at = ?, access_count = 0"
                         " WHERE label = ?", (ts, ts, lab))
        conn.execute("DELETE FROM access_log")
        conn.execute("DELETE FROM query_log")


def restamp_v3(conn):
    for i, (lab, *_rest) in enumerate(FIXTURE):
        ts = BASE - 40 * 86400 if lab == "Stale Trivia" else BASE + i * 100
        conn.execute("UPDATE nodes SET created_at = ?, updated_at = ?, access_count = 0"
                     " WHERE label = ?", (ts, ts, lab))
    conn.execute("DELETE FROM access_log")
    conn.execute("DELETE FROM query_log")
    conn.commit()


def v2_labels(res):
    return [n.label for n in res.nodes]


def v3_labels(res):
    return [n["label"] for n in res["nodes"]]


class TestRecallParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory(prefix="asha_v3_parity_")
        v2d = str(Path(cls.td.name) / "v2data")
        cls.mem = seed_v2(v2d)
        cls.conn = v3_connect(str(Path(cls.td.name) / "core.db"))
        migrate(cls.conn)
        seed_v3(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.td.cleanup()

    def setUp(self):
        restamp_v2(self.mem)
        restamp_v3(self.conn)

    def assertParity(self, v2res, v3res):
        self.assertEqual(v3_labels(v3res), v2_labels(v2res),
                         f"v3={v3_labels(v3res)} v2={v2_labels(v2res)}")

    def test_related(self):
        self.assertParity(self.mem.recall("quantum lattice", mode="RELATED", bound=BOUND),
                          recall.recall(self.conn, "quantum lattice", mode="RELATED", bound=BOUND))

    def test_semantic(self):
        v2res = self.mem.recall("quantum lattice coherence", mode="SEMANTIC", bound=BOUND)
        v3res = recall.recall(self.conn, "quantum lattice coherence", mode="SEMANTIC", bound=BOUND)
        self.assertParity(v2res, v3res)
        for v2n, v3n in zip(v2res.nodes, v3res["nodes"]):
            self.assertAlmostEqual(v3n["metadata"]["_similarity"],
                                   round(v2n.metadata["_similarity"], 4), places=3)

    def test_recent(self):
        self.assertParity(self.mem.recall("100000", mode="RECENT", bound=BOUND),
                          recall.recall(self.conn, "100000", mode="RECENT", bound=BOUND))

    def test_who_is(self):
        self.assertParity(self.mem.recall("Sam Rivera", mode="WHO_IS", bound=BOUND),
                          recall.recall(self.conn, "Sam Rivera", mode="WHO_IS", bound=BOUND))

    def test_what_about(self):
        self.assertParity(self.mem.recall("Memory Systems", mode="WHAT_ABOUT", bound=BOUND),
                          recall.recall(self.conn, "Memory Systems", mode="WHAT_ABOUT", bound=BOUND))

    def test_cluster(self):
        self.assertParity(self.mem.recall("Path Alpha", mode="CLUSTER", bound=BOUND),
                          recall.recall(self.conn, "Path Alpha", mode="CLUSTER", bound=BOUND))

    def test_timeline(self):
        self.assertParity(self.mem.recall("Memory Systems", mode="TIMELINE", bound=BOUND),
                          recall.recall(self.conn, "Memory Systems", mode="TIMELINE", bound=BOUND))

    def test_path(self):
        self.assertParity(self.mem.recall("Path Alpha -> Path Omega", mode="PATH", bound=BOUND),
                          recall.recall(self.conn, "Path Alpha -> Path Omega", mode="PATH", bound=BOUND))

    def test_prune(self):
        v2res = self.mem.recall("0.5", mode="PRUNE", bound=BOUND)
        v3res = recall.recall(self.conn, "0.5", mode="PRUNE", bound=BOUND)
        self.assertIn("Stale Trivia", v2_labels(v2res))
        self.assertParity(v2res, v3res)

    def test_visibility(self):
        v2res = self.mem.recall("100000", mode="RECENT", bound=BOUND)
        v3res = recall.recall(self.conn, "100000", mode="RECENT", bound=BOUND)
        for labels in (v2_labels(v2res), v3_labels(v3res)):
            self.assertNotIn("Private Agent Note", labels)
            self.assertIn("Verified Agent Note", labels)
        self.assertParity(v2res, v3res)

    def test_dsl_person(self):
        v2res = self.mem.query('FIND PERSON "Sam Rivera"')
        v3res = recall.recall(self.conn, 'FIND PERSON "Sam Rivera"', bound=BOUND)
        self.assertEqual(v3res["mode"], "WHO_IS")
        self.assertParity(v2res, v3res)

    def test_dsl_semantic(self):
        v2res = self.mem.query('FIND SEMANTIC "quantum lattice"')
        v3res = recall.recall(self.conn, 'FIND SEMANTIC "quantum lattice"', bound=BOUND)
        self.assertEqual(v3res["mode"], "SEMANTIC")
        self.assertParity(v2res, v3res)

    def test_dsl_path(self):
        v2res = self.mem.query('FIND PATH "Path Alpha" -> "Path Omega"')
        v3res = recall.recall(self.conn, 'FIND PATH "Path Alpha" -> "Path Omega"', bound=BOUND)
        self.assertEqual(v3res["mode"], "PATH")
        self.assertParity(v2res, v3res)

    def test_dsl_timeline(self):
        v2res = self.mem.query('FIND TIMELINE "Memory Systems"')
        v3res = recall.recall(self.conn, 'FIND TIMELINE "Memory Systems"', bound=BOUND)
        self.assertEqual(v3res["mode"], "TIMELINE")
        self.assertParity(v2res, v3res)

    def test_dsl_cluster(self):
        # NOTE v2 wart, kept for DSL-string parity: the (PERSON|TOPIC|EVENT|FACT)
        # rule matches prefixes first, so FIND TOPIC "X" CLUSTER -> WHAT_ABOUT
        # in BOTH engines. The CLUSTER branch is reachable with other types:
        v2res = self.mem.query('FIND PREFERENCE "Dark Mode" CLUSTER')
        v3res = recall.recall(self.conn, 'FIND PREFERENCE "Dark Mode" CLUSTER', bound=BOUND)
        self.assertEqual(v2res.mode, "CLUSTER")
        self.assertEqual(v3res["mode"], "CLUSTER")
        self.assertParity(v2res, v3res)

    def test_dsl_falls_back_to_related(self):
        pq = recall.parse_query("just some words")
        self.assertEqual(pq.mode, "RELATED")
        self.assertEqual(pq.source, "just some words")

    def test_offset_pagination(self):
        # restamp between calls: recall bumps updated_at, which would reorder RECENT
        full = recall.recall(self.conn, "100000", mode="RECENT", bound=BOUND)
        restamp_v3(self.conn)
        page = recall.recall(self.conn, "100000", mode="RECENT", bound=5, offset=2)
        self.assertEqual([n["label"] for n in page["nodes"]],
                         [n["label"] for n in full["nodes"]][2:7])
        self.assertEqual(page["total_found"], full["total_found"])

    def test_cache_and_query_log(self):
        cache = recall.LRUCache(capacity=10)
        r1 = recall.recall(self.conn, "quantum lattice", mode="RELATED",
                           bound=BOUND, cache=cache)
        self.assertEqual(cache.misses, 1)
        r2 = recall.recall(self.conn, "quantum lattice", mode="RELATED",
                           bound=BOUND, cache=cache)
        self.assertEqual(cache.hits, 1)
        self.assertEqual(v3_labels(r1), v3_labels(r2))
        self.conn.commit()
        n = self.conn.execute("SELECT COUNT(*) FROM query_log").fetchone()[0]
        self.assertGreaterEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
