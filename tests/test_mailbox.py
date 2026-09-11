"""test_mailbox — Phase 5: unicast + broadcast receipts, validation, states."""

import tempfile
import unittest
from pathlib import Path

from src.agents import mailbox, store


def fresh_conn(tmpdir):
    conn = store.connect(str(Path(tmpdir) / "agents.db"))
    store.ensure_schema(conn)
    conn.commit()
    return conn


def mkagents(conn, *hints):
    return [store.agent_ensure(conn, h)["agent_id"] for h in hints]


class TestMailboxSend(unittest.TestCase):
    def test_unicast_core_agent(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                (a,) = mkagents(conn, "Scout")
                r = mailbox.send(conn, "core", f"agent:{a}", "hello scout")
                conn.commit()
                self.assertTrue(r["msg_id"].startswith("msg_"))
                n = conn.execute("SELECT COUNT(*) FROM mailbox_receipts WHERE msg_id = ?",
                                 (r["msg_id"],)).fetchone()[0]
                self.assertEqual(n, 1)
            finally:
                conn.close()

    def test_agent_to_agent_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a, b = mkagents(conn, "Alpha", "Beta")
                r = mailbox.send(conn, f"agent:{a}", f"agent:{b}", "peer note")
                conn.commit()
                inbox = mailbox.check_inbox(conn, f"agent:{b}")
                self.assertEqual(len(inbox), 1)
                self.assertEqual(inbox[0]["from_scope"], f"agent:{a}")
            finally:
                conn.close()

    def test_agent_broadcast_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                (a,) = mkagents(conn, "Spammer")
                with self.assertRaises(ValueError) as cm:
                    mailbox.send(conn, f"agent:{a}", "broadcast", "spam")
                self.assertIn("send to core", str(cm.exception))
            finally:
                conn.close()

    def test_user_scopes(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                (a,) = mkagents(conn, "Scout")
                mailbox.send(conn, "user", f"agent:{a}", "tasking")
                mailbox.send(conn, f"agent:{a}", "user", "reply")
                mailbox.send(conn, "user", "core", "note to core")
                conn.commit()
                self.assertEqual(len(mailbox.check_inbox(conn, f"agent:{a}")), 1)
                self.assertEqual(len(mailbox.check_inbox(conn, "user")), 1)
                self.assertEqual(len(mailbox.check_inbox(conn, "core")), 1)
            finally:
                conn.close()

    def test_unknown_agent_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                with self.assertRaises(ValueError) as cm:
                    mailbox.send(conn, "core", "agent:ghost", "hi")
                self.assertIn("agent_ensure", str(cm.exception))
                with self.assertRaises(ValueError):
                    mailbox.send(conn, "agent:ghost", "core", "hi")
                with self.assertRaises(ValueError):
                    mailbox.send(conn, "core", "bogus", "hi")
                with self.assertRaises(ValueError):
                    mailbox.send(conn, "core", "agent:x", "   ")
            finally:
                conn.close()


class TestBroadcastReceipts(unittest.TestCase):
    def test_fanout_and_independent_ack(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                a, b = mkagents(conn, "Alpha", "Beta")
                mailbox.send(conn, "core", "broadcast", "all hands")
                conn.commit()
                for scope in ("core", f"agent:{a}", f"agent:{b}"):
                    self.assertEqual(len(mailbox.check_inbox(conn, scope)), 1)
                # one agent acks; others still pending
                self.assertEqual(mailbox.ack(conn, f"agent:{a}")["acked"], 1)
                conn.commit()
                self.assertEqual(mailbox.read(conn, f"agent:{a}"), [])
                self.assertEqual(len(mailbox.read(conn, f"agent:{b}")), 1)
                self.assertEqual(len(mailbox.read(conn, "core")), 1)
            finally:
                conn.close()


class TestStates(unittest.TestCase):
    def test_noted_read_acked_transitions_and_mirror(self):
        with tempfile.TemporaryDirectory() as td:
            conn = fresh_conn(td)
            try:
                (a,) = mkagents(conn, "Scout")
                mid = mailbox.send(conn, "core", f"agent:{a}", "handle this")["msg_id"]
                conn.commit()
                scope = f"agent:{a}"
                self.assertEqual(len(mailbox.read(conn, scope, state="pending")), 1)
                self.assertEqual(len(mailbox.check_inbox(conn, scope)), 1)  # marks noted
                conn.commit()
                row = conn.execute("SELECT noted_at FROM mailbox_receipts"
                                   " WHERE msg_id = ?", (mid,)).fetchone()
                self.assertIsNotNone(row["noted_at"])
                # read() marks read + mirrors onto unicast mailbox row
                mailbox.read(conn, scope)
                conn.commit()
                mrow = conn.execute("SELECT read_at, acked_at FROM mailbox WHERE msg_id = ?",
                                    (mid,)).fetchone()
                self.assertIsNotNone(mrow["read_at"])
                self.assertIsNone(mrow["acked_at"])
                self.assertEqual(mailbox.ack(conn, scope)["acked"], 1)
                conn.commit()
                mrow = conn.execute("SELECT acked_at FROM mailbox WHERE msg_id = ?",
                                    (mid,)).fetchone()
                self.assertIsNotNone(mrow["acked_at"])
                self.assertEqual(mailbox.read(conn, scope, state="pending"), [])
                self.assertEqual(len(mailbox.read(conn, scope, state="all")), 1)
                with self.assertRaises(ValueError):
                    mailbox.read(conn, scope, state="bogus")
            finally:
                conn.close()

    def test_format_injection(self):
        msg = mailbox.format_injection(
            [{"from_scope": "core"}, {"from_scope": "agent:x"}], "agent:y")
        self.assertIn("Hey Agent y", msg)
        self.assertIn("2 message(s)", msg)
        self.assertIn("mailbox.read / mailbox.ack", msg)
        self.assertTrue(mailbox.format_injection([{"from_scope": "user"}], "core")
                        .startswith("Hey Core"))


if __name__ == "__main__":
    unittest.main()
