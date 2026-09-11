"""test_dashboard — Phase 7: HTTP matrix over a live in-process server.

Covers: shell + 12 tabs + no-CDN/static assertions, ?db= matrix, all endpoint
shapes, auth on/off (?token= rejected), path-traversal rejection, deleted
routes (humantools/switch_db/db_bytes/manager_commit), mailbox roundtrip,
snapshot lifecycle, job runs + history. No browser needed.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from brain.engine import BrainEngine
from brain.scheduler import BrainScheduler
from dashboard import server as dash
from src.agents import store as agent_store
from src.core import nodes as core_nodes
from src.core.migrate import migrate as core_migrate
from src.core.store import connect as core_connect

STATIC = Path("dashboard/static")


def seed(tmpdir):
    mem = Path(tmpdir) / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    core = core_connect(str(mem / "core.db"))
    core_migrate(core)
    core_nodes.remember_many(core, [
        {"content": "I love Python programming dearly", "node_type": "FACT",
         "label": "Python Love", "trust": 0.9, "importance": 0.8},
        {"content": "I hate Python programming utterly", "node_type": "FACT",
         "label": "Python Hate", "trust": 0.2, "importance": 0.8},
        {"content": "dashboard probe content here", "node_type": "FACT",
         "label": "Dashboard Probe"},
    ])
    core.commit()
    core.close()
    agents = agent_store.connect(str(mem / "agents.db"))
    agent_store.ensure_schema(agents)
    aid = agent_store.agent_ensure(agents, "Dash Scout")["agent_id"]
    agent_store.agent_remember(agents, aid, "scouted dashboard finding", label="S1")
    n2 = agent_store.agent_remember(agents, aid, "second finding ready", label="S2")
    agent_store.agent_set_attention(agents, aid, n2, "review_ready")
    agents.execute("INSERT INTO ephemeral_events (label, body, created_at)"
                   " VALUES ('FEED_SNAPSHOT', '{}', 1)")
    agents.commit()
    agents.close()
    return str(mem), aid


class DashCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory(prefix="asha_v3_dash_")
        mem, cls.aid = seed(cls.td.name)
        brain_dir = str(Path(cls.td.name) / "brain")
        eng = BrainEngine(core_path=str(Path(mem) / "core.db"),
                          agents_path=str(Path(mem) / "agents.db"),
                          brain_dir=brain_dir)
        dash.configure(eng, BrainScheduler(engine=eng))
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        cls.td.cleanup()

    def req(self, method, path, body=None, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Api-Token", token)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get_content_type(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, "error", e.read()

    def get(self, path, token=None):
        return self.req("GET", path, None, token)

    def post(self, path, body=None, token=None):
        return self.req("POST", path, body or {}, token)

    def jget(self, path, token=None):
        code, _, raw = self.get(path, token)
        self.assertEqual(code, 200, f"{path}: {raw[:200]}")
        return json.loads(raw)

    def jpost(self, path, body=None, token=None):
        code, _, raw = self.post(path, body, token)
        self.assertEqual(code, 200, f"{path}: {raw[:200]}")
        return json.loads(raw)


class TestShellAndStatic(DashCase):
    def test_shell_has_12_tabs_and_modules(self):
        code, ctype, raw = self.get("/")
        self.assertEqual(code, 200)
        html = raw.decode()
        for tab in ("overview", "maintenance", "graduate", "observer", "contradicts",
                    "ephemeral", "graph", "manager", "system", "statistics",
                    "config", "mail"):
            self.assertIn(f"/static/modules/{tab}.js", html, tab)
        self.assertIn("/static/app.js", html)

    def test_no_cdn_or_legacy_baggage_in_static(self):
        import re
        banned = ("http://", "https://", "cdn", "sql.js", "postMessage",
                  "manager_commit", "db_bytes", "switch_db", "D3", "d3.")
        for path in [STATIC / "app.js", STATIC / "dashboard.html",
                     *sorted((STATIC / "modules").glob("*.js"))]:
            text = path.read_text(encoding="utf-8")
            # strip HTML comments + JS line comments (docs may name the ban)
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
            text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
            for bad in banned:
                self.assertNotIn(bad, text, f"{path.name} contains {bad!r}")

    def test_static_jail(self):
        code, _, _ = self.get("/static/app.js")
        self.assertEqual(code, 200)
        for evil in ("/static/../server.py", "/static/%2e%2e/server.py",
                     "/static/modules/../../stepsdone.md"):
            code, _, _ = self.get(evil)
            self.assertEqual(code, 404, evil)

    def test_deleted_routes_gone(self):
        for gone in ("/humantools/asha_graph.html", "/api/switch_db",
                     "/api/db_bytes", "/api/manager_commit"):
            code, _, _ = self.get(gone)
            self.assertEqual(code, 404, gone)
        code, _, _ = self.post("/api/switch_db", {})
        self.assertEqual(code, 404)


class TestCoreEndpoints(DashCase):
    def test_health_ping_head(self):
        d = self.jget("/api/health")
        self.assertEqual(d["status"], "ok")
        self.assertIn("core_db", d)
        self.assertTrue(self.jget("/api/ping")["pong"])
        code, _, _ = self.req("HEAD", "/api/health")
        self.assertEqual(code, 200)

    def test_status_shape(self):
        d = self.jget("/api/status?db=all")
        self.assertIn("core", d["health"])
        self.assertIn("agents", d["health"])
        self.assertIn("running", d["scheduler"])
        self.assertIn("history", d)

    def test_db_matrix(self):
        for endpoint in ("statistics", "bloat"):
            all_d = self.jget(f"/api/{endpoint}?db=all")
            self.assertIn("combined", all_d)
            core_d = self.jget(f"/api/{endpoint}?db=core")
            self.assertNotIn("combined", core_d)
            ag_d = self.jget(f"/api/{endpoint}?db=agents")
            self.assertNotIn("combined", ag_d)
        self.assertGreater(all_d.get("total_nodes", 1) if "total_nodes" in all_d
                           else 1, 0)

    def test_run_job_and_history(self):
        before = len(self.jget("/api/history?limit=100")["history"])
        r = self.jpost("/api/run_job", {"jobs": ["tiers"], "target": "core"})
        self.assertEqual(r["run"]["status"], "success")
        self.assertEqual(r["run"]["jobs"], ["tiers"])
        after = self.jget("/api/history?limit=100")["history"]
        self.assertEqual(len(after), before + 1)

    def test_scheduler_toggle(self):
        r = self.jpost("/api/scheduler", {"enabled": True, "interval_minutes": 9999,
                                          "max_unused_days": 4})
        self.assertTrue(r["is_running"])
        r = self.jpost("/api/scheduler", {"enabled": False})
        self.assertFalse(r["is_running"])

    def test_config_whitelist_and_coerce(self):
        r = self.jpost("/api/config", {"max_unused_days": "9", "nope_key": 1,
                                       "auto_snapshot_before_jobs": True})
        self.assertEqual(r["config"]["max_unused_days"], 9)
        self.assertNotIn("nope_key", r["config"])
        self.assertNotIn("memory_synced", r)  # single config: brain file only
        r = self.jpost("/api/config", {"reload": True})
        self.assertTrue(r.get("reloaded"))
        self.assertEqual(r["config"]["max_unused_days"], 9)
        r = self.jpost("/api/config_reset", {})
        self.assertEqual(r["config"]["max_unused_days"], 4)

    def test_snapshots_lifecycle(self):
        r = self.jpost("/api/create_snapshot", {"db": "core"})
        self.assertEqual(r["core"]["status"], "success")
        fname = r["core"]["filename"]
        self.assertTrue(fname.startswith("snapshot_core_"))
        snaps = self.jget("/api/snapshots?db=core")["snapshots"]
        self.assertIn(fname, [s["filename"] for s in snaps])
        rr = self.jpost("/api/restore_snapshot", {"db": "core", "filename": fname})
        self.assertEqual(rr["status"], "success")
        self.assertTrue(rr["health_ok"])
        dd = self.jpost("/api/delete_snapshot", {"filename": fname})
        self.assertEqual(dd["status"], "success")

    def test_logs_flow(self):
        self.jpost("/api/run_job", {"jobs": ["tiers"], "target": "core"})
        logs = self.jget("/api/logs")["logs"]
        self.assertGreaterEqual(len(logs), 1)
        c = self.jget(f"/api/log_content?file={logs[0]['filename']}")
        self.assertIn("Brain run", c["content"])
        code, _, _ = self.get("/api/log_content?file=../x.md")
        self.assertEqual(code, 404)


class TestCurationEndpoints(DashCase):
    def test_graduate_flow(self):
        d = self.jget("/api/graduate_preview?limit=10")
        self.assertEqual(d["review_ready"], 1)
        nid = d["graduable_preview"][0]["node_id"]
        r = self.jpost("/api/graduate", {"node_ids": [nid]})
        self.assertEqual(r["core"]["graduated"], 1)
        d2 = self.jget("/api/graduate_preview?limit=10")
        self.assertEqual(d2["review_ready"], 0)

    def test_observer(self):
        d = self.jget("/api/agent_working_preview")
        self.assertIn("preview", d)
        r = self.jpost("/api/regulate_agent_working", {"dry_run": True})
        self.assertIn("agents", r)

    def test_contradictions_flow(self):
        d = self.jget("/api/contradictions?status=pending&limit=10")
        self.assertGreaterEqual(len(d["contradictions"]), 1)
        eid = d["contradictions"][0]["edge_id"]
        r = self.jpost("/api/contradiction_action",
                       {"edge_id": eid, "action": "confirm"})
        self.assertEqual(r["new_status"], "confirmed")
        r = self.jpost("/api/contradiction_auto_resolve", {"dry_run": True})
        self.assertIn("count", r)

    def test_ephemeral(self):
        d = self.jget("/api/ephemeral_candidates?db=all&min_count=1")
        self.assertIn("allowlist", d)
        r = self.jpost("/api/ephemeral_allowlist",
                       {"action": "add", "label": "TEST_LABEL"})
        self.assertIn("TEST_LABEL", r["ephemeral_labels"])
        r = self.jpost("/api/ephemeral_allowlist",
                       {"action": "remove", "label": "TEST_LABEL"})
        self.assertNotIn("TEST_LABEL", r["ephemeral_labels"])
        r = self.jpost("/api/compact_ephemeral", {"db": "agents"})
        self.assertIn("agents", r)

    def test_graph_and_manager(self):
        g = self.jget("/api/graph?db=core&limit=50")
        self.assertGreaterEqual(len(g["nodes"]), 3)
        self.assertIn("edges", g)
        n = self.jget("/api/nodes?db=core&q=Probe&limit=5")
        self.assertEqual(n["total"], 1)
        nid = n["nodes"][0]["node_id"]
        u = self.jpost("/api/node_update",
                       {"db": "core", "node_id": nid,
                        "fields": {"label": "Dashboard Probe Edited"}})
        self.assertEqual(u["status"], "updated")
        self.assertEqual(u["node"]["label"], "Dashboard Probe Edited")
        dd = self.jpost("/api/node_delete", {"db": "core", "node_id": nid})
        self.assertEqual(dd["status"], "deleted")
        with self.assertRaises(AssertionError):
            self.jpost("/api/node_update",
                       {"db": "core", "node_id": nid, "fields": {"bogus": 1}})

    def test_agents_and_mailbox(self):
        ag = self.jget("/api/agents")
        self.assertEqual(len(ag["agents"]), 1)
        s = self.jpost("/api/mailbox/send",
                       {"from": "user", "to": "core", "body": "hello core"})
        self.assertIn("msg_id", s)
        m = self.jget("/api/mailbox?scope=user&state=all&limit=10")
        self.assertIsInstance(m["messages"], list)
        m2 = self.jget("/api/mailbox?scope=core&state=pending&limit=10")
        # seed creates 1 review_ready note => 1 REVIEW_READY reminder + hello core
        # review reminders are stored as mailbox msgs with kind=review_ready
        self.assertEqual(len(m2["messages"]), 2)
        # ack only the hello core? ack all pending (both)
        a = self.jpost("/api/mailbox/ack", {"scope": "core"})
        self.assertEqual(a["acked"], 2)

    def test_vacuum_rebuild_check(self):
        v = self.jpost("/api/vacuum", {"db": "core"})
        self.assertEqual(v["core"]["status"], "success")
        r = self.jpost("/api/rebuild_vectors", {"db": "core"})
        self.assertEqual(r["core"]["status"], "success")
        c = self.jpost("/api/check_vacuum", {"db": "core"})
        self.assertIn("triggered", c)


class TestAuthMatrix(DashCase):
    def test_z_auth_matrix(self):
        # enable token (open for now)
        self.jpost("/api/config", {"dashboard_token": "s3cret"})
        try:
            code, _, _ = self.get("/api/status?db=all")
            self.assertEqual(code, 401)
            # ?token= must NOT work (C8)
            code, _, _ = self.get("/api/status?db=all&token=s3cret")
            self.assertEqual(code, 401)
            # header works
            d = self.jget("/api/status?db=all", token="s3cret")
            self.assertIn("health", d)
            # health/ping stay open (liveness probes)
            self.assertEqual(self.get("/api/health")[0], 200)
            self.assertEqual(self.get("/api/ping")[0], 200)
            # wrong token rejected
            code, _, _ = self.get("/api/status?db=all", token="wrong")
            self.assertEqual(code, 401)
        finally:
            self.jpost("/api/config", {"dashboard_token": ""}, token="s3cret")
        code, _, _ = self.get("/api/status?db=all")
        self.assertEqual(code, 200)


class TestRowEndpoints(DashCase):
    def test_nodes_type_filter(self):
        d = self.jget("/api/nodes?db=core&type=FACT&limit=50")
        self.assertGreaterEqual(d["total"], 1)
        for n in d["nodes"]:
            self.assertEqual(n["node_type"], "FACT")
            self.assertIn("last_access", n)
            self.assertIn("layer", n)

    def test_node_add_core_roundtrip(self):
        r = self.jpost("/api/node_add",
                       {"db": "core", "node_type": "TOPIC",
                        "label": "Row Probe", "content": "row probe content"})
        self.assertEqual(r["status"], "created")
        nid = r["node_id"]
        n = self.jget(f"/api/nodes?db=core&q={nid}&limit=1")
        self.assertEqual(n["nodes"][0]["label"], "Row Probe")
        dd = self.jpost("/api/node_delete", {"db": "core", "node_id": nid})
        self.assertEqual(dd["status"], "deleted")

    def test_node_add_agents_needs_agent(self):
        code, _, raw = self.post("/api/node_add",
                                 {"db": "agents", "content": "orphan note"})
        self.assertEqual(code, 400)
        r = self.jpost("/api/node_add",
                       {"db": "agents", "agent_id": self.aid,
                        "content": "managed agent note", "label": "Managed"})
        self.assertEqual(r["status"], "created")
        dd = self.jpost("/api/node_delete",
                        {"db": "agents", "node_id": r["node_id"]})
        self.assertEqual(dd["status"], "deleted")

    def test_edge_crud(self):
        a = self.jpost("/api/node_add",
                       {"db": "core", "content": "edge endpoint one",
                        "label": "EdgeOne"})["node_id"]
        b = self.jpost("/api/node_add",
                       {"db": "core", "content": "edge endpoint two",
                        "label": "EdgeTwo"})["node_id"]
        try:
            e = self.jpost("/api/edge_add",
                           {"db": "core", "from_node": a, "to_node": b,
                            "edge_type": "SUPPORTS", "weight": 0.7})
            self.assertEqual(e["status"], "created")
            lst = self.jget(f"/api/edges?db=core&node={a}&type=SUPPORTS&limit=10")
            self.assertEqual(lst["total"], 1)
            self.assertEqual(lst["edges"][0]["from_label"], "EdgeOne")
            self.assertEqual(lst["edges"][0]["to_label"], "EdgeTwo")
            dd = self.jpost("/api/edge_delete",
                            {"db": "core", "edge_id": e["edge_id"]})
            self.assertEqual(dd["status"], "deleted")
            code, _, _ = self.post("/api/edge_add",
                                   {"db": "core", "from_node": a,
                                    "to_node": "node_missing", "edge_type": "X_NOPE"})
            self.assertEqual(code, 400)
        finally:
            self.jpost("/api/node_delete", {"db": "core", "node_id": a})
            self.jpost("/api/node_delete", {"db": "core", "node_id": b})


class TestBrainDirDefault(unittest.TestCase):
    """Regression 2026-09-05: dashboard must root its engine at repo brain/.

    start_dashboard used Path(__file__).parent (= dashboard/) as brain_dir, so
    every launch created dashboard/config.json with defaults and split
    logs/snapshots/history away from brain/.
    """

    def test_default_is_repo_brain(self):
        got = dash._default_brain_dir()
        self.assertEqual(got.name, "brain")
        self.assertTrue((got / "engine.py").exists())
        self.assertNotEqual(got.resolve(), Path(dash.__file__).resolve().parent)

    def test_start_dashboard_uses_repo_brain(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            mem = Path(td) / "memory"
            mem.mkdir(parents=True, exist_ok=True)
            srv = dash.start_dashboard(memory_dir=str(mem), brain_dir=str(Path(td) / "brain"),
                                       host="127.0.0.1", port=0)
            try:
                eng = dash._ENGINE
                self.assertEqual(Path(eng.config_path).parent.resolve(),
                                 (Path(td) / "brain").resolve())
                self.assertFalse((Path(mem) / "config.json").exists())
            finally:
                srv.server_close()
                dash._ENGINE = None
                dash._SCHEDULER = None


if __name__ == "__main__":
    unittest.main()
