"""test_agents_store — Phase 4: agents.db schema, identity, scoped CRUD, isolation."""

import tempfile
import unittest
from pathlib import Path

from src.agents import store
from src.agents.store import UNKNOWN_AGENT_HINT


def fresh_conn(tmpdir):
    conn = store.connect(str(Path(tmpdir) / "agents.db"))
    store.ensure_schema(conn)
    conn.commit()
    return conn


class TestAgentsSchema(unittest.TestCase):
    def test_ensure_and_check(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                rep = store.check(conn)
                self.assertTrue(rep["ok"], rep)
            finally:
                conn.close()

    def test_rerun_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                self.assertTrue(store.ensure_schema(conn)["ok"])
                self.assertTrue(store.check(conn)["ok"])
            finally:
                conn.close()


class TestIdentity(unittest.TestCase):
    def test_ensure_idempotent_on_slug(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                first = store.agent_ensure(conn, "Nightly Scout")
                second = store.agent_ensure(conn, "nightly  scout!!")
                self.assertTrue(first["created"])
                self.assertFalse(second["created"])
                self.assertEqual(first["agent_id"], second["agent_id"])
                self.assertTrue(first["agent_id"].startswith("agent_nightly-scout_"))
            finally:
                conn.close()

    def test_preferred_id_accepted_and_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                r = store.agent_ensure(conn, "Helper", preferred_id="helper-01")
                self.assertEqual(r["agent_id"], "helper-01")
                with self.assertRaises(ValueError):
                    store.agent_ensure(conn, "Other", preferred_id="helper-01")  # taken
                with self.assertRaises(ValueError):
                    store.agent_ensure(conn, "Other", preferred_id="BAD ID!")  # regex
            finally:
                conn.close()

    def test_unknown_rejected_everywhere(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                with self.assertRaises(ValueError) as cm:
                    store.agent_remember(conn, "ghost", "hello")
                self.assertIn("agent_ensure", str(cm.exception))
                with self.assertRaises(ValueError):
                    store.agent_recall(conn, "ghost", "hello")
                with self.assertRaises(ValueError):
                    store.agent_relate(conn, "ghost", "a", "b")
                self.assertIn("agent_ensure", UNKNOWN_AGENT_HINT)
            finally:
                conn.close()

    def test_alias_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                r = store.agent_ensure(conn, "Scout")
                store.add_alias(conn, "old-freeform-id", r["agent_id"])
                conn.commit()
                self.assertEqual(store.resolve_agent(conn, "old-freeform-id")["agent_id"],
                                 r["agent_id"])
                self.assertIsNone(store.resolve_agent(conn, "nobody"))
                self.assertEqual(len(store.agent_list(conn)), 1)
            finally:
                conn.close()


class TestScopedCrud(unittest.TestCase):
    def test_remember_recall_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = store.agent_ensure(conn, "Alpha")["agent_id"]
                b = store.agent_ensure(conn, "Beta")["agent_id"]
                na = store.agent_remember(conn, a, "alpha zephyr compass notes here",
                                          label="Alpha Note")
                nb = store.agent_remember(conn, b, "beta zephyr compass notes here",
                                          label="Beta Note")
                conn.commit()
                ra = store.agent_recall(conn, a, "zephyr compass", bound=10)
                rb = store.agent_recall(conn, b, "zephyr compass", bound=10)
                self.assertEqual([n["node_id"] for n in ra["nodes"]], [na])
                self.assertEqual([n["node_id"] for n in rb["nodes"]], [nb])
                for n in ra["nodes"] + rb["nodes"]:
                    self.assertIn("agent_id", n)
                # cross-agent fetch fails closed
                self.assertIsNone(store.agent_get_node(conn, a, nb))
                self.assertEqual(store.agent_get_node(conn, b, nb)["label"], "Beta Note")
                self.assertFalse(store.agent_delete_node(conn, a, nb))
            finally:
                conn.close()

    def test_always_agent_note_and_caps(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = store.agent_ensure(conn, "Cap")["agent_id"]
                nid = store.agent_remember(conn, a, "x" * 900, label="Long")
                conn.commit()
                got = store.agent_get_node(conn, a, nid)
                self.assertEqual(got["node_type"], "AGENT_NOTE")
                self.assertTrue(len(got["content"]) <= 800)
                self.assertEqual(got["source"], f"AGENT_{a}")
                self.assertEqual(got["metadata"]["attention_state"], "agent_private")
            finally:
                conn.close()

    def test_attention_flow(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = store.agent_ensure(conn, "Attn")["agent_id"]
                b = store.agent_ensure(conn, "Other")["agent_id"]
                nid = store.agent_remember(conn, a, "review me please", label="R")
                conn.commit()
                self.assertTrue(store.agent_set_attention(conn, a, nid, "review_ready"))
                conn.commit()
                self.assertEqual(store.agent_get_node(conn, a, nid)["metadata"]["attention_state"],
                                 "review_ready")
                self.assertFalse(store.agent_set_attention(conn, b, nid, "agent_private"))
                with self.assertRaises(ValueError):
                    store.agent_set_attention(conn, a, nid, "core_verified")
            finally:
                conn.close()

    def test_relate_stamps_agent_and_isolates(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = store.agent_ensure(conn, "RelA")["agent_id"]
                b = store.agent_ensure(conn, "RelB")["agent_id"]
                n1 = store.agent_remember(conn, a, "relink one", label="R1")
                n2 = store.agent_remember(conn, a, "relink two", label="R2")
                m1 = store.agent_remember(conn, b, "other side", label="M1")
                conn.commit()
                eid = store.agent_relate(conn, a, n1, n2, "SUPPORTS", 0.7)
                row = conn.execute("SELECT agent_id FROM edges WHERE edge_id = ?",
                                   (eid,)).fetchone()
                self.assertEqual(row["agent_id"], a)
                with self.assertRaises(ValueError):
                    store.agent_relate(conn, a, n1, m1)
            finally:
                conn.close()

    def test_digest(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a = store.agent_ensure(conn, "Dig")["agent_id"]
                d1 = store.agent_remember(conn, a, "first note", label="D1")
                d2 = store.agent_remember(conn, a, "second note", label="D2")
                conn.execute("UPDATE nodes SET updated_at = updated_at - 100 WHERE node_id = ?",
                             (d1,))
                conn.commit()
                d = store.agent_digest(conn, a, limit=10)
                self.assertEqual([n["label"] for n in d], ["D2", "D1"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
