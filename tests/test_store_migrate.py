"""test_store_migrate — Phase 2: chain, FTS backfill, lexicon seed, index presence.

Grows in Phase 6: health() wiring.
"""

import tempfile
import unittest
from pathlib import Path

from src.agents.store import connect as agents_connect
from src.core import nodes
from src.core.lexicon import _sanitize_fts_query
from src.core.migrate import check, get_user_version, migrate
from src.core.store import USER_VERSION_V3, connect as core_connect
from src.core.vectors import get_ndocs


def fresh_conn(tmpdir, name="core.db"):
    conn = core_connect(str(Path(tmpdir) / name))
    migrate(conn)
    conn.commit()
    return conn


class TestConnectInvariant(unittest.TestCase):
    def test_core_pragmas(self):
        with tempfile.TemporaryDirectory() as td:
            conn = core_connect(str(Path(td) / "core.db"))
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            finally:
                conn.close()

    def test_agents_pragmas(self):
        with tempfile.TemporaryDirectory() as td:
            conn = agents_connect(str(Path(td) / "agents.db"))
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                conn.close()


class TestMigrationChain(unittest.TestCase):
    def test_fresh_runs_1_to_4(self):
        with tempfile.TemporaryDirectory() as td:
            conn = core_connect(str(Path(td) / "core.db"))
            try:
                self.assertEqual(get_user_version(conn), 0)
                rep = migrate(conn)
                self.assertEqual(rep["from"], 0)
                self.assertEqual(rep["to"], USER_VERSION_V3)
                self.assertEqual(rep["ran"], [1, 2, 3, 4])
            finally:
                conn.close()

    def test_rerun_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                rep = migrate(conn)
                self.assertEqual(rep["ran"], [])
                self.assertEqual(rep["from"], rep["to"])
            finally:
                conn.close()

    def test_check_ok_on_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                rep = check(conn)
                self.assertTrue(rep["ok"], rep)
                self.assertEqual(rep["schema_meta"].get("lexicon_version"), "3")
                self.assertEqual(rep["schema_meta"].get("version"), "3.0")
            finally:
                conn.close()

    def test_v3_tables_and_indexes_present(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                rep = check(conn)
                self.assertEqual(rep["missing_tables"], [])
                for idx in ("idx_nodes_type", "idx_node_index_word",
                            "idx_access_log_node_time", "idx_node_vectors_mag",
                            "idx_query_log_time_mode", "idx_ephemeral_label_time"):
                    self.assertNotIn(idx, rep["missing_indexes"], idx)
                self.assertEqual(rep["missing_triggers"], [])
            finally:
                conn.close()

    def test_fts_backfill_recovers_deleted_entry(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nid = nodes.remember(conn, "Zephyr quasar lattice harmonics",
                                     "FACT", label="Zephyr Quasar")
                conn.commit()
                conn.execute("DELETE FROM node_fts WHERE node_id = ?", (nid,))
                conn.commit()
                before = check(conn)
                self.assertEqual(before["orphans"]["fts_missing"], 1)
                # roll back to v2 so migrate() actually runs the v3+v4 steps
                conn.execute("PRAGMA user_version = 2")
                rep = migrate(conn)
                conn.commit()
                self.assertEqual(rep["ran"], [3, 4])
                after = check(conn)
                self.assertEqual(after["orphans"]["fts_missing"], 0)
                q = _sanitize_fts_query("Zephyr quasar")
                hits = conn.execute(
                    "SELECT n.node_id FROM nodes n JOIN node_fts f ON f.node_id = n.node_id"
                    " WHERE node_fts MATCH ?", (q,)).fetchall()
                self.assertTrue(any(h["node_id"] == nid for h in hits))
            finally:
                conn.close()

    def test_remember_feeds_ndocs(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nodes.remember_many(conn, [
                    {"content": "alpha beta gamma", "node_type": "FACT"},
                    {"content": "delta epsilon zeta", "node_type": "FACT"},
                ])
                conn.commit()
                self.assertEqual(get_ndocs(conn), 2)
                self.assertTrue(check(conn)["ok"])
            finally:
                conn.close()

    def test_bump_does_not_reindex_fts_but_edit_does(self):
        """C20: nodes_au fires on label/content only, never on access bumps."""
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                nid = nodes.remember(conn, "wxyzzy trigger probe", "FACT",
                                     label="Wxyzzy Probe")
                conn.commit()
                trig = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
                    " AND name = 'nodes_au'").fetchone()[0]
                self.assertIn("UPDATE OF", trig)
                before = conn.execute("SELECT COUNT(*) FROM node_fts WHERE node_id = ?",
                                      (nid,)).fetchone()[0]
                self.assertEqual(before, 1)
                nodes.bump_access(conn, nid)
                conn.commit()
                after = conn.execute("SELECT COUNT(*) FROM node_fts WHERE node_id = ?",
                                     (nid,)).fetchone()[0]
                self.assertEqual(after, 1)  # no reindex on bump
                nodes.update_node(conn, nid, label="Wxyzzy Changed",
                                        content="totally different wording here")
                conn.commit()
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM node_fts WHERE node_id = ?",
                                 (nid,)).fetchone()[0], 1)
                hits = conn.execute(
                    "SELECT node_id FROM node_fts WHERE node_fts MATCH 'changed'").fetchall()
                self.assertTrue(any(h["node_id"] == nid for h in hits))
                stale = conn.execute(
                    "SELECT node_id FROM node_fts WHERE node_fts MATCH 'probe'").fetchall()
                self.assertFalse(any(h["node_id"] == nid for h in stale))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
