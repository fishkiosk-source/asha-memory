"""test_nodes_edges — Phase 2: CRUD, validation, clamps, index/vector upkeep,
auto-link, FACT contradiction detection, edge ops.
"""

import tempfile
import unittest
from pathlib import Path

from src.core import edges, nodes
from src.core.layers import get_layer
from src.core.migrate import check, migrate
from src.core.store import connect
from src.core.vectors import decode, get_df, get_ndocs, get_vector


def fresh_conn(tmpdir):
    conn = connect(str(Path(tmpdir) / "core.db"))
    migrate(conn)
    conn.commit()
    return conn


class TestNodes(unittest.TestCase):
    def test_remember_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nid = nodes.remember(conn, "Sam prefers dark mode", "PREFERENCE",
                                     label="Sam UI", trust=0.9, importance=0.7,
                                     metadata={"k": "v"})
                conn.commit()
                got = nodes.get_node(conn, nid)
                self.assertEqual(got["label"], "Sam UI")
                self.assertEqual(got["trust_level"], 0.9)
                self.assertEqual(got["metadata"], {"k": "v"})
                self.assertEqual(got["layer"], "working")
                self.assertEqual(got["access_count"], 0)
                # upkeep rows exist
                self.assertTrue(conn.execute(
                    "SELECT 1 FROM node_index WHERE node_id = ?",
                    (nid,)).fetchone())
                self.assertTrue(conn.execute(
                    "SELECT 1 FROM node_vectors WHERE node_id = ?",
                    (nid,)).fetchone())
            finally:
                conn.close()

    def test_remember_delegates_and_validates(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                with self.assertRaises(ValueError):
                    nodes.remember(conn, "x", "NOPE")
                ids = nodes.remember_many(conn, [
                    {"content": "aaa bbb", "node_type": "FACT"},
                    {"content": "ccc ddd", "node_type": "TOPIC", "label": "T"},
                ])
                self.assertEqual(len(ids), 2)
                self.assertNotEqual(ids[0], ids[1])
                conn.commit()
                self.assertEqual(get_ndocs(conn), 2)
            finally:
                conn.close()

    def test_trust_importance_clamped(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nid = nodes.remember(conn, "clamp me", "FACT", trust=9.0, importance=-3.0)
                conn.commit()
                got = nodes.get_node(conn, nid)
                self.assertEqual(got["trust_level"], 1.0)
                self.assertEqual(got["importance"], 0.0)
            finally:
                conn.close()

    def test_update_node_refreshes_index_and_vectors(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nid = nodes.remember(conn, "original wubbalub content here", "FACT",
                                     label="Wubbalub")
                conn.commit()
                self.assertGreater(get_df(conn, "wubbalub"), 0)
                # label changes too: the old label still carries 'wubbalub' otherwise
                upd = nodes.update_node(conn, nid, label="Quuxor",
                                        content="totally quuxor change now",
                                        trust_level=0.2, metadata={"s": 1})
                conn.commit()
                self.assertEqual(upd["label"], "Quuxor")
                self.assertEqual(upd["trust_level"], 0.2)
                self.assertEqual(upd["metadata"], {"s": 1})
                self.assertEqual(get_df(conn, "wubbalub"), 0)
                self.assertGreater(get_df(conn, "quuxor"), 0)
                vec, mag = get_vector(conn, nid)
                self.assertIn("quuxor", vec)
                self.assertNotIn("wubbalub", vec)
                self.assertGreater(mag, 0.0)
                with self.assertRaises(ValueError):
                    nodes.update_node(conn, nid, bogus=1)
                self.assertIsNone(nodes.update_node(conn, "node_missing", label="x"))
            finally:
                conn.close()

    def test_delete_node_cleans_vectors_and_cascades(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = nodes.remember(conn, "alpha delete target marker", "FACT")
                b = nodes.remember(conn, "beta stays here marker", "FACT")
                conn.commit()
                ndocs_before = get_ndocs(conn)
                self.assertTrue(nodes.delete_node(conn, a))
                conn.commit()
                self.assertFalse(nodes.delete_node(conn, a))
                self.assertIsNone(nodes.get_node(conn, a))
                self.assertEqual(get_ndocs(conn), ndocs_before - 1)
                self.assertFalse(conn.execute(
                    "SELECT 1 FROM node_vectors WHERE node_id = ?", (a,)).fetchone())
                self.assertIsNotNone(nodes.get_node(conn, b))
                self.assertTrue(check(conn)["ok"])
            finally:
                conn.close()

    def test_bump_access_writes_count_and_log_only(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nid = nodes.remember(conn, "bump target item", "FACT")
                conn.commit()
                self.assertTrue(nodes.bump_access(conn, nid))
                conn.commit()
                got = nodes.get_node(conn, nid)
                self.assertEqual(got["access_count"], 1)
                self.assertEqual(get_layer(conn, nid), "working")  # no promotion
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM access_log WHERE node_id = ?",
                    (nid,)).fetchone()[0], 1)
                self.assertFalse(nodes.bump_access(conn, "node_missing"))
            finally:
                conn.close()

    def test_auto_link_creates_relates_to(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = nodes.remember(conn, "zephyr quantum lattice zephyr quantum",
                                   "FACT", label="Zephyr Alpha")
                b = nodes.remember(conn, "zephyr quantum lattice zephyr compass",
                                   "FACT", label="Zephyr Beta")
                conn.commit()
                linked = edges.edges_between(conn, b, a) + edges.edges_between(conn, a, b)
                kinds = {e["edge_type"] for e in linked}
                self.assertIn("RELATES_TO", kinds)
            finally:
                conn.close()

    def test_fact_contradiction_pair_links(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nodes.remember(conn, "I love Python programming dearly", "FACT",
                               label="Python love")
                nid2 = nodes.remember(conn, "I hate Python programming utterly", "FACT",
                                      label="Python hate")
                conn.commit()
                got = nodes.get_node(conn, nid2)
                self.assertTrue(got["metadata"].get("contradiction_flag"))
                rows = conn.execute(
                    "SELECT * FROM edges WHERE edge_type = 'CONTRADICTS'").fetchall()
                self.assertTrue(rows)
                self.assertLess(rows[0]["weight"], 0.0)
            finally:
                conn.close()

    def test_ephemeral_nodes_do_not_link(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nodes.remember(conn, '{"timestamp": 1, "status": "ok"}', "FACT",
                               label="FEED_SNAPSHOT")
                conn.commit()
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM edges").fetchone()[0], 0)
            finally:
                conn.close()

    def test_resolve_ref(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nid = nodes.remember(conn, "resolvable item", "FACT",
                                     label="UniqueResolvableLabel")
                conn.commit()
                self.assertEqual(nodes.resolve_ref(conn, nid), nid)
                self.assertEqual(nodes.resolve_ref(conn, "UniqueResolvableLabel"), nid)
                self.assertEqual(nodes.resolve_ref(conn, "UniqueResolv"), nid)
                self.assertIsNone(nodes.resolve_ref(conn, "no such thing xyz"))
            finally:
                conn.close()


class TestEdges(unittest.TestCase):
    def test_relate_neighbors_delete(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = nodes.remember(conn, "edge endpoint alpha omicron", "FACT")
                b = nodes.remember(conn, "edge endpoint beta omicron", "FACT")
                conn.commit()
                eid = edges.relate(conn, a, b, "SUPPORTS", weight=0.6)
                conn.commit()
                nbs = edges.neighbors(conn, a)
                # auto-link already made a RELATES_TO (shared keywords); SUPPORTS joins it
                kinds = {(n[0], n[2]) for n in nbs}
                self.assertIn((b, "SUPPORTS"), kinds)
                self.assertIn((b, "RELATES_TO"), kinds)
                sup = [n for n in nbs if n[0] == b and n[2] == "SUPPORTS"][0]
                self.assertAlmostEqual(sup[1], 0.6)
                self.assertEqual(sup[3], "out")
                self.assertTrue(edges.delete_edge(conn, eid))
                conn.commit()
                kinds_after = {(n[0], n[2]) for n in edges.neighbors(conn, a)}
                self.assertNotIn((b, "SUPPORTS"), kinds_after)
                self.assertIn((b, "RELATES_TO"), kinds_after)
            finally:
                conn.close()

    def test_relate_validation_and_clamp(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = nodes.remember(conn, "clamp endpoint", "FACT")
                conn.commit()
                with self.assertRaises(ValueError):
                    edges.relate(conn, a, "node_missing", "RELATES_TO")
                with self.assertRaises(ValueError):
                    edges.relate(conn, a, a, "NOPE")
                eid = edges.relate(conn, a, a, "RELATES_TO", weight=5.0)
                conn.commit()
                row = conn.execute("SELECT weight FROM edges WHERE edge_id = ?",
                                   (eid,)).fetchone()
                self.assertEqual(row["weight"], 1.0)  # clamped (C16)
                self.assertIn("PROMOTED_FROM", edges.EDGE_TYPES)
            finally:
                conn.close()

    def test_vector_codec_roundtrip(self):
        vec = {"alpha": 1.5, "beta": 0.25}
        s = nodes.vectors.encode(vec)
        back = decode(s)
        self.assertAlmostEqual(back["alpha"], 1.5, places=5)
        self.assertAlmostEqual(back["beta"], 0.25, places=5)


if __name__ == "__main__":
    unittest.main()
