"""test_mcp — Phase 5: dispatch + 23-tool handlers + injection + stdio protocol."""

import io
import json
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.mcp import server, tools


def fresh_ctx(tmpdir):
    return tools.open_context(str(Path(tmpdir) / "memory"))


class TestSurface(unittest.TestCase):
    def test_23_canonical_tools(self):
        self.assertEqual(len(tools.TOOL_NAMES), 23)
        for name in tools.TOOL_NAMES:
            self.assertIn(name, tools._HANDLERS)
        defs = tools.definitions()
        names = [d["name"] for d in defs]
        for name in tools.TOOL_NAMES:
            self.assertIn(name, names)
        self.assertIn("register_skill", names)  # deprecated alias, uncounted

    def test_unknown_tool_and_bad_args(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                with self.assertRaises(ValueError):
                    tools.dispatch("nope", {}, ctx)
                with self.assertRaises(ValueError):
                    tools.dispatch("remember", {"content": "x"}, ctx)  # missing node_type
            finally:
                tools.close_context(ctx)


class TestCoreTools(unittest.TestCase):
    def test_crud_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                nid = tools.dispatch("remember", {"content": "mcp memory item",
                                                  "node_type": "FACT",
                                                  "label": "MCP Item"}, ctx)["node_id"]
                res = tools.dispatch("recall", {"query": "mcp memory",
                                                "mode": "RELATED"}, ctx)
                self.assertIn(nid, [n["node_id"] for n in res["nodes"]])
                self.assertIn("clock", res)
                got = tools.dispatch("get_node", {"node_id": nid}, ctx)
                self.assertEqual(got["content"], "mcp memory item")
                upd = tools.dispatch("update_node", {"node_id": nid,
                                                     "trust_level": 0.9,
                                                     "metadata": '{"k": 1}'}, ctx)
                self.assertEqual(upd["trust_level"], 0.9)
                eid = tools.dispatch("relate", {"from_id": nid, "to_id": nid,
                                                "edge_type": "RELATES_TO",
                                                "weight": 5.0}, ctx)["edge_id"]
                self.assertTrue(eid)
                self.assertEqual(
                    tools.dispatch("delete_node", {"node_id": nid}, ctx)["status"],
                    "deleted")
                self.assertEqual(
                    tools.dispatch("get_node", {"node_id": nid}, ctx)["error"],
                    "not found")
            finally:
                tools.close_context(ctx)

    def test_recall_aliases_and_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                before = ctx["core"].execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                with self.assertRaises(ValueError):
                    tools.dispatch("relate", {"from_id": "node_missing",
                                              "to_id": "node_missing",
                                              "edge_type": "RELATES_TO"}, ctx)
                after = ctx["core"].execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                self.assertEqual(before, after)  # nothing persisted
                tools.dispatch("remember", {"content": "alias probe",
                                            "node_type": "FACT"}, ctx)
                r = tools.dispatch("recall", {"query": "alias probe",
                                              "limit": 1}, ctx)
                self.assertLessEqual(len(r["nodes"]), 1)
            finally:
                tools.close_context(ctx)


class TestAgentTools(unittest.TestCase):
    def test_agent_flow_and_promote(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                aid = tools.dispatch("agent_ensure", {"job_hint": "Nightly Scout"},
                                     ctx)["agent_id"]
                self.assertIn(aid, [a["agent_id"] for a in
                                    tools.dispatch("agent_list", {}, ctx)["agents"]])
                nid = tools.dispatch("agent_remember",
                                     {"agent_id": aid, "content": "scouted item",
                                      "label": "S1"}, ctx)["node_id"]
                res = tools.dispatch("agent_recall", {"agent_id": aid,
                                                      "query": "scouted"}, ctx)
                self.assertIn(nid, [n["node_id"] for n in res["nodes"]])
                tools.dispatch("set_attention",
                               {"agent_id": aid, "agent_node_id": nid,
                                "attention_state": "review_ready"}, ctx)
                q = tools.dispatch("review_queue", {}, ctx)
                self.assertIn(nid, [n["node_id"] for n in q["notes"]])
                x = tools.dispatch("find_across_agents", {"query": "scouted"}, ctx)
                self.assertIn(nid, [n["node_id"] for n in x["results"]])
                p = tools.dispatch("promote_to_core",
                                   {"agent_id": aid, "agent_node_id": nid,
                                    "new_type": "FACT"}, ctx)
                self.assertEqual(p["status"], "promoted")
                with self.assertRaises(ValueError):
                    tools.dispatch("agent_recall", {"agent_id": "ghost",
                                                    "query": "x"}, ctx)
            finally:
                tools.close_context(ctx)

    def test_register_skill_alias(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                r = tools.dispatch("register_skill",
                                   {"name": "NAP", "description": "take naps"}, ctx)
                self.assertEqual(r["status"], "registered")
                self.assertIn("deprecated", r)
                got = tools.dispatch("get_node", {"node_id": r["node_id"]}, ctx)
                self.assertEqual(got["node_type"], "SKILL")
            finally:
                tools.close_context(ctx)


class TestInjection(unittest.TestCase):
    def test_empty_inbox_returns_same_object(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                r = {"status": "ok"}
                self.assertIs(tools.with_inbox_notice("core", r, ctx["agents"]), r)
            finally:
                tools.close_context(ctx)

    def test_pending_mail_decorates_once(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                aid = tools.dispatch("agent_ensure", {"job_hint": "Courier"}, ctx)["agent_id"]
                tools.dispatch("mailbox.send", {"from": f"agent:{aid}",
                                                "to": "core",
                                                "body": "ping core"}, ctx)
                r1 = tools.dispatch("recall", {"query": "nothing much here",
                                               "mode": "RELATED"}, ctx)
                self.assertIn("mailbox_notice", r1)
                self.assertIn(aid, r1["mailbox_notice"])
                r2 = tools.dispatch("recall", {"query": "nothing much here",
                                               "mode": "RELATED"}, ctx)
                self.assertNotIn("mailbox_notice", r2)  # already noted
                # agent scope derivation (canonical, incl. alias path)
                tools.dispatch("mailbox.send", {"from": "core", "to": f"agent:{aid}",
                                                "body": "ping agent"}, ctx)
                r3 = tools.dispatch("agent_recall", {"agent_id": aid,
                                                     "query": "zzz"}, ctx)
                self.assertIn("mailbox_notice", r3)
            finally:
                tools.close_context(ctx)

    def test_mailbox_trio(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                mid = tools.dispatch("mailbox.send", {"from": "user", "to": "core",
                                                      "body": "do the thing"}, ctx)["msg_id"]
                msgs = tools.dispatch("mailbox.read", {"scope": "core"}, ctx)["messages"]
                self.assertIn(mid, [m["msg_id"] for m in msgs])
                self.assertEqual(
                    tools.dispatch("mailbox.ack", {"scope": "core",
                                                   "msg_ids": [mid]}, ctx)["acked"], 1)
            finally:
                tools.close_context(ctx)


class TestSystemTools(unittest.TestCase):
    def test_profile_health_stats(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                tools.dispatch("remember", {"content": "sys probe",
                                            "node_type": "FACT"}, ctx)
                p = tools.dispatch("profile", {}, ctx)
                self.assertEqual(p["core_nodes"], 1)
                h = tools.dispatch("health", {}, ctx)
                self.assertTrue(h["ok"], h)
                s = tools.dispatch("stats", {"db": "all"}, ctx)
                self.assertEqual(s["core"]["nodes"], 1)
                self.assertEqual(s["agents"]["nodes"], 0)
            finally:
                tools.close_context(ctx)

    def test_export_vacuum_compact(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                dest = str(Path(td) / "out.tar.gz")
                r = tools.dispatch("export", {"path": dest}, ctx)
                self.assertEqual(r["status"], "exported")
                self.assertTrue(Path(dest).exists())
                with tarfile.open(dest) as tar:
                    self.assertIn("core.db", tar.getnames())
                    self.assertIn("agents.db", tar.getnames())
                self.assertTrue(Path(str(dest) + ".manifest.json").exists())
                v = tools.dispatch("vacuum", {}, ctx)
                self.assertIn("before_mb", v["core"])
                ctx["agents"].execute(
                    "INSERT INTO ephemeral_events (label, body, created_at)"
                    " VALUES ('FEED_SNAPSHOT', '{}', 1)")
                c = tools.dispatch("compact", {}, ctx)
                self.assertEqual(c["agents"]["removed_ttl"], 1)
                with self.assertRaises(ValueError):
                    tools.dispatch("export", {"path": str(Path(td) / "nope" / "x.tar.gz")},
                                   ctx)
            finally:
                tools.close_context(ctx)


class TestServerProtocol(unittest.TestCase):
    def _call(self, srv, obj):
        buf = io.StringIO()
        with redirect_stdout(buf):
            srv.handle_line(json.dumps(obj))
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_initialize_list_call_resources_ping(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = fresh_ctx(td)
            try:
                srv = server.MCPServer(ctx)
                init = self._call(srv, {"jsonrpc": "2.0", "id": 1,
                                        "method": "initialize", "params": {}})
                self.assertEqual(init["result"]["serverInfo"]["name"], "asha-memory")
                lst = self._call(srv, {"jsonrpc": "2.0", "id": 2,
                                       "method": "tools/list", "params": {}})
                self.assertEqual(len(lst["result"]["tools"]), 24)  # 23 + alias
                call = self._call(srv, {"jsonrpc": "2.0", "id": 3,
                                        "method": "tools/call",
                                        "params": {"name": "remember",
                                                   "arguments": {"content": "hi",
                                                                 "node_type": "FACT"}}})
                self.assertIn("node_id", json.loads(call["result"]["content"][0]["text"]))
                bad = self._call(srv, {"jsonrpc": "2.0", "id": 4,
                                       "method": "tools/call",
                                       "params": {"name": "nope", "arguments": {}}})
                self.assertEqual(bad["error"]["code"], server.TOOL_EXECUTION_ERROR)
                res = self._call(srv, {"jsonrpc": "2.0", "id": 5,
                                       "method": "resources/list", "params": {}})
                uris = [r["uri"] for r in res["result"]["resources"]]
                self.assertNotIn("asha://skills", uris)  # registry deleted
                self.assertIn("asha://memory/health", uris)
                ping = self._call(srv, {"jsonrpc": "2.0", "id": 6,
                                        "method": "ping", "params": {}})
                self.assertEqual(ping["result"], {})
                unknown = self._call(srv, {"jsonrpc": "2.0", "id": 7,
                                           "method": "frobnicate", "params": {}})
                self.assertEqual(unknown["error"]["code"], server.METHOD_NOT_FOUND)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    srv.handle_line("{invalid json")
                err = json.loads(buf.getvalue().strip().splitlines()[-1])
                self.assertEqual(err["error"]["code"], server.PARSE_ERROR)
            finally:
                tools.close_context(ctx)


if __name__ == "__main__":
    unittest.main()
