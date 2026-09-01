"""
BRAIN DASHBOARD — Pure Python Web Management Dashboard for AshaMemory
======================================================================
Standalone, zero-dependency HTTP web server providing a modern dashboard
styled to match humantools/asha_manager.html (clean, slick, dark).
Features: Overview, Maintenance, Graduate (own tab), Graph, Manager
(embedded live DB), System (snapshots/logs/history), Config.
"""

import sys
import json
import os
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

# Ensure brain module is importable
BRAIN_DIR = Path(__file__).parent.resolve()
if str(BRAIN_DIR) not in sys.path:
    sys.path.insert(0, str(BRAIN_DIR))

from brain_engine import BrainEngine, DEFAULT_CONFIG
from scheduler import BrainScheduler

GLOBAL_SCHEDULER = None


class BrainDashboardHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler providing REST API and embedded UI Dashboard."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
        elif path in ("/api/health", "/api/ping"):
            # lightweight health check for AI/human — no DB scan
            import time as _t
            is_ping = path == "/api/ping"
            payload = {"pong": True} if is_ping else {
                "running": True,
                "status": "ok",
                "timestamp": int(_t.time()),
                "iso": __import__("datetime").datetime.now().isoformat(),
                "db_path": str(GLOBAL_SCHEDULER.engine.db_path) if GLOBAL_SCHEDULER and GLOBAL_SCHEDULER.engine else None,
                "pid": os.getpid(),
                "port": self.server.server_address[1],
            }
            self._send_json(payload)
        elif path == "/api/status":
            self._send_json(self._get_dashboard_data())
        elif path == "/api/config":
            engine = GLOBAL_SCHEDULER.engine
            self._send_json({"config": engine.config, "defaults": DEFAULT_CONFIG})
        elif path == "/api/databases":
            engine = GLOBAL_SCHEDULER.engine
            self._send_json({"databases": engine.find_available_databases()})
        elif path == "/api/snapshots":
            engine = GLOBAL_SCHEDULER.engine
            self._send_json({"snapshots": engine.list_snapshots()})
        elif path == "/api/history":
            self._send_json({"history": GLOBAL_SCHEDULER.get_history()})
        elif path == "/api/logs":
            engine = GLOBAL_SCHEDULER.engine
            self._send_json({"logs": engine.list_markdown_logs()})
        elif path == "/api/log_content":
            query = urllib.parse.parse_qs(parsed.query)
            filename = query.get("file", [""])[0]
            engine = GLOBAL_SCHEDULER.engine
            # prevent traversal
            if ".." in filename or "/" in filename or "\\" in filename:
                self._send_json({"error": "Invalid filename"}, status=400)
                return
            log_path = engine.logs_dir / filename
            if log_path.exists() and log_path.is_file():
                content = log_path.read_text(encoding="utf-8")
                self._send_json({"filename": filename, "content": content})
            else:
                self._send_json({"error": "File not found"}, status=404)
        elif path == "/api/bloat":
            engine = GLOBAL_SCHEDULER.engine
            self._send_json(engine.get_bloat_metrics())
        elif path == "/api/ephemeral_candidates":
            engine = GLOBAL_SCHEDULER.engine
            try:
                min_count = int(urllib.parse.parse_qs(parsed.query).get("min_count", ["3"])[0])
            except Exception:
                min_count = 3
            self._send_json(engine.discover_ephemeral_candidates(min_count=min_count))
        elif path == "/api/statistics":
            engine = GLOBAL_SCHEDULER.engine
            self._send_json(engine.get_full_statistics())
        elif path == "/api/contradictions":
            engine = GLOBAL_SCHEDULER.engine
            try:
                qs = urllib.parse.parse_qs(parsed.query)
                status = qs.get("status", [None])[0]
                limit = int(qs.get("limit", ["50"])[0])
                self._send_json(engine.get_contradictions(status=status, limit=limit))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        elif path == "/api/db_bytes":
            engine = GLOBAL_SCHEDULER.engine
            db_path = engine.db_path
            if not db_path.exists():
                self.send_error(404, "DB not found")
                return
            try:
                data = db_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{db_path.name}"')
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return
        elif path == "/api/graduate_preview":
            engine = GLOBAL_SCHEDULER.engine
            try:
                import sqlite3
                conn = sqlite3.connect(str(engine.db_path))
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT node_id,label,content,trust_level,importance,node_type,metadata,updated_at FROM nodes").fetchall()
                conn.close()
                agent_notes = [r for r in rows if engine.is_agent_note(r)]
                review_ready = []
                graduable = []
                private = []
                for r in agent_notes:
                    try:
                        meta = json.loads(r["metadata"]) if r["metadata"] else {}
                    except Exception:
                        meta = {}
                    att = meta.get("attention_state", "agent_private")
                    if att == "review_ready":
                        review_ready.append(r)
                    elif att == "agent_private":
                        private.append(r)
                    is_grad = att == "review_ready" or ( (r["trust_level"] or 0) >= 0.7 and (r["importance"] or 0) >= 0.6)
                    if is_grad:
                        graduable.append(r)
                # build lightweight preview (limit 50) - use engine's agent notes directly (no AshaMemory queue)
                notes_preview = []
                for r in review_ready[:100]:
                    notes_preview.append({"node_id": r["node_id"], "label": r["label"], "content": r["content"], "trust_level": r["trust_level"], "importance": r["importance"], "metadata": r["metadata"], "updated_at": r["updated_at"]})
                self._send_json({
                    "total_agent_notes": len(agent_notes),
                    "review_ready": len(review_ready),
                    "agent_private": len(private),
                    "graduable": len(graduable),
                    "total": len(notes_preview),
                    "notes": notes_preview,
                    "graduable_preview": [
                        {"node_id": r["node_id"], "label": r["label"], "content": (r["content"] or "")[:200], "trust_level": r["trust_level"], "importance": r["importance"], "attention": (json.loads(r["metadata"]).get("attention_state") if r["metadata"] else "agent_private") if r["metadata"] else "agent_private", "updated_at": r["updated_at"]}
                        for r in graduable[:50]
                    ]
                })
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        elif path.startswith("/humantools/"):
            try:
                rel = path[len("/humantools/"):]
                if ".." in rel or rel.startswith("/"):
                    self.send_error(404, "Not Found")
                    return
                ht_dir = Path(__file__).parent.parent / "humantools"
                file_path = ht_dir / rel
                if not file_path.exists() or not file_path.is_file():
                    self.send_error(404, "Not Found")
                    return
                data = file_path.read_bytes()
                ctype = "text/html" if file_path.suffix == ".html" else "application/octet-stream"
                if file_path.suffix == ".js":
                    ctype = "application/javascript"
                if file_path.suffix == ".css":
                    ctype = "text/css"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return
        else:
            self.send_error(404, "Not Found")

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html", "/api/health", "/api/ping", "/api/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if path.startswith("/api") else "text/html")
            self.end_headers()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # manager_commit is binary SQLite upload — handle raw bytes
        if path == "/api/manager_commit":
            engine = GLOBAL_SCHEDULER.engine
            content_len = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_len) if content_len > 0 else b""
            ctype = self.headers.get("Content-Type", "")
            # support JSON {data: base64} fallback
            if ctype.startswith("application/json"):
                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                    import base64
                    b64 = payload.get("data", "")
                    if b64:
                        body_bytes = base64.b64decode(b64)
                except Exception:
                    pass
            res = engine.commit_manager_db(body_bytes)
            # on success auto-discover + rebuild vectors as requested
            if res.get("status") == "success":
                try:
                    disc = engine.discover_links()
                    res["discover"] = disc
                except Exception as e:
                    res["discover"] = {"status": "error", "message": str(e)}
                try:
                    if engine.config.get("auto_rebuild_vectors", True):
                        res["vector_rebuild"] = engine.rebuild_vectors()
                except Exception as e:
                    res["vector_rebuild"] = {"status": "error", "message": str(e)}
                # snapshot already taken inside commit
            self._send_json(res)
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            payload = {}

        engine = GLOBAL_SCHEDULER.engine

        if path == "/api/switch_db":
            db_path = payload.get("db_path", "")
            success = engine.set_target_db(db_path)
            engine._ensure_schema()
            if success:
                self._send_json({"status": "success", "message": f"Switched to {db_path}", "health": engine.get_health_metrics()})
            else:
                self._send_json({"status": "error", "message": f"Failed to switch to {db_path}"}, status=400)

        elif path == "/api/run_job":
            job_types = payload.get("jobs", None)
            res = GLOBAL_SCHEDULER.run_job_now(job_types=job_types)
            self._send_json({"status": "success", "run": res})

        elif path == "/api/scheduler":
            enabled = payload.get("enabled", False)
            interval_min = int(payload.get("interval_minutes", 60))
            max_days = int(payload.get("max_unused_days", 4))

            engine.config["max_unused_days"] = max_days
            engine._save_config()

            if enabled:
                GLOBAL_SCHEDULER.start(interval_minutes=interval_min)
            else:
                GLOBAL_SCHEDULER.stop()

            self._send_json({"status": "success", "config": GLOBAL_SCHEDULER.get_config(), "is_running": GLOBAL_SCHEDULER.running})

        elif path == "/api/ephemeral_allowlist":
            action = payload.get("action", "")
            label = payload.get("label", "")
            if action == "add":
                res = engine.add_ephemeral_label(label)
            elif action == "remove":
                res = engine.remove_ephemeral_label(label)
            else:
                res = {"status": "error", "message": "action must be add or remove"}
            self._send_json(res)
        elif path == "/api/contradiction_action":
            edge_id = payload.get("edge_id", "")
            action = payload.get("action", "")
            # normalize aliases
            alias_map = {"confirm": "confirmed", "ignore": "ignored"}
            action_norm = alias_map.get(action, action)
            if not edge_id or not action:
                self._send_json({"status": "error", "message": "edge_id and action required"}, status=400)
            elif action_norm in ("confirmed", "ignored", "pending", "resolved"):
                res = engine.update_contradiction_status(edge_id, action_norm)
                self._send_json(res)
            elif action in ("delete", "keep_from", "keep_to", "merge"):
                res = engine.resolve_contradiction(edge_id, action)
                # rebuild vectors if merge/keep removed nodes and auto_rebuild enabled
                if res.get("status") == "success" and action in ("keep_from", "keep_to", "merge") and engine.config.get("auto_rebuild_vectors", True):
                    res["vector_rebuild"] = engine.rebuild_vectors()
                self._send_json(res)
            else:
                self._send_json({"status": "error", "message": f"Unknown action {action}"}, status=400)
        elif path == "/api/contradiction_auto_resolve":
            try:
                dry = payload.get("dry_run", True)
                # allow ?dry_run=false via payload
                if isinstance(dry, str):
                    dry = dry.lower() in ("true","1","yes")
                res = engine.auto_resolve_low_trust(dry_run=bool(dry))
                self._send_json(res)
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, status=500)
        elif path == "/api/check_vacuum":
            # manual check: if freelist over threshold, vacuum + rebuild
            try:
                bloat = engine.get_bloat_metrics()
                if bloat.get("needs_vacuum"):
                    vac = engine.vacuum_db()
                    res = {"triggered": True, "bloat": bloat, "vacuum": vac}
                    if engine.config.get("auto_rebuild_vectors", True):
                        res["vector_rebuild"] = engine.rebuild_vectors()
                    res["bloat_after"] = engine.get_bloat_metrics()
                    self._send_json(res)
                else:
                    self._send_json({"triggered": False, "bloat": bloat})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        elif path == "/api/config":
            allowed = ("interval_minutes", "max_unused_days", "dedup_similarity_threshold",
                       "prune_importance_floor", "auto_snapshot_before_jobs", "auto_rebuild_vectors",
                       "ephemeral_keep_last", "ephemeral_max_age_days", "vacuum_after_prune",
                       "vacuum_freelist_threshold_pct", "vacuum_freelist_min_pages",
                       "contradiction_auto_resolve", "contradiction_low_trust", "contradiction_high_trust")
            try:
                for key in allowed:
                    if key in payload:
                        val = payload[key]
                        if isinstance(val, bool):
                            engine.config[key] = val
                        elif key in ("dedup_similarity_threshold", "prune_importance_floor", "contradiction_low_trust", "contradiction_high_trust"):
                            engine.config[key] = float(val)
                        else:
                            # allow int or bool strings
                            try:
                                engine.config[key] = int(val)
                            except Exception:
                                engine.config[key] = val
                engine._save_config()
                self._send_json({"status": "success", "config": engine.config})
            except Exception as e:
                self._send_json({"status": "error", "message": f"Invalid config values: {str(e)}"}, status=400)

        elif path == "/api/config_reset":
            res = engine.reset_config()
            self._send_json({"status": "success", "config": res})

        elif path == "/api/create_snapshot":
            res = engine.create_snapshot()
            self._send_json(res)

        elif path == "/api/rebuild_vectors":
            res = engine.rebuild_vectors()
            self._send_json(res)

        elif path == "/api/restore_snapshot":
            filename = payload.get("filename", "")
            if ".." in filename or "/" in filename or "\\" in filename:
                self._send_json({"status": "error", "message": "Invalid filename"}, status=400)
                return
            res = engine.restore_snapshot(filename)
            self._send_json(res)

        elif path == "/api/delete_snapshot":
            filename = payload.get("filename", "")
            if ".." in filename or "/" in filename or "\\" in filename:
                self._send_json({"status": "error", "message": "Invalid filename"}, status=400)
                return
            res = engine.delete_snapshot(filename)
            self._send_json(res)

        elif path == "/api/vacuum":
            res = engine.vacuum_db()
            self._send_json(res)

        elif path == "/api/compact_ephemeral":
            keep_last = payload.get("keep_last")
            max_age_days = payload.get("max_age_days")
            res = engine.compact_ephemeral_logs(keep_last=keep_last, max_age_days=max_age_days)
            if payload.get("vacuum", True) and res.get("status") == "success" and res.get("removed_total", 0) > 0:
                vac = engine.vacuum_db()
                res["vacuum"] = vac
                if engine.config.get("auto_rebuild_vectors", True):
                    res["vector_rebuild"] = engine.rebuild_vectors()
            self._send_json(res)

        elif path == "/api/bloat":
            self._send_json(engine.get_bloat_metrics())
        elif path == "/api/graduate":
            try:
                res = engine.graduate_agent_notes()
                if res.get("graduated", 0) > 0 and engine.config.get("auto_rebuild_vectors", True):
                    res["vector_rebuild"] = engine.rebuild_vectors()
                self._send_json(res)
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, status=500)
        else:
            self.send_error(404, "Not Found")

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))

    def _get_dashboard_data(self) -> dict:
        engine = GLOBAL_SCHEDULER.engine
        try:
            bloat = engine.get_bloat_metrics()
        except Exception:
            bloat = {}
        return {
            "health": engine.get_health_metrics(),
            "bloat": bloat,
            "scheduler": {
                "running": GLOBAL_SCHEDULER.running,
                "config": GLOBAL_SCHEDULER.get_config(),
            },
            "databases": engine.find_available_databases(),
            "snapshots": engine.list_snapshots(),
            "history": GLOBAL_SCHEDULER.get_history(limit=5),
            "logs": engine.list_markdown_logs(limit=5),
        }


# ──────────────────────────────────────────────────────────────────────────────
# EMBEDDED DASHBOARD HTML/CSS/JS — matches humantools/asha_manager.html style
# ──────────────────────────────────────────────────────────────────────────────

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Asha Brain — Memory Maintenance Console</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,'Segoe UI',monospace;background:#0d0d0d;color:#c0c0c0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{background:#1a1a1a;border-bottom:1px solid #333;padding:12px 20px;display:flex;align-items:center;gap:14px;flex-shrink:0}
h1{font-size:16px;font-weight:600;color:#e0e0e0;letter-spacing:0.5px}
h1 span{color:#888;font-weight:400}
h1 small{color:#666;font-weight:400;font-size:11px;margin-left:8px;letter-spacing:0.2px}
.header-actions{display:flex;align-items:center;gap:8px;margin-left:auto}
.badge-pill{font-size:11px;padding:4px 10px;border-radius:10px;border:1px solid #333;background:#222;color:#999;display:inline-flex;align-items:center;gap:6px;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge-pill.on{border-color:#2a5a3a;color:#5d8;background:#1a2a1f}
.badge-pill.off{border-color:#5a2a2a;color:#e88;background:#2a1a1a}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.dot.on{background:#5d8;box-shadow:0 0 6px #5d8}
.dot.off{background:#e55;box-shadow:0 0 6px #e55}
#status{font-size:12px;padding:8px 20px;background:#111;border-bottom:1px solid #222;color:#888;flex-shrink:0}
#status.error{color:#e55}#status.ok{color:#5d8}#status.loading{color:#8bf}#status.warn{color:#ea6}
#stats-bar{background:#151515;border-bottom:1px solid #2a2a2a;padding:8px 20px;display:flex;gap:24px;font-size:12px;color:#999;flex-shrink:0;flex-wrap:wrap}
.stat{display:flex;gap:5px;align-items:center}.stat span:first-child{color:#666}.stat span:last-child{color:#e0e0e0;font-weight:500}
.stat.warn span:last-child{color:#ea6}.stat.bad span:last-child{color:#e55}
.tabs{display:flex;background:#111;border-bottom:1px solid #2a2a2a;flex-shrink:0;padding:0 12px;overflow-x:auto;scrollbar-width:thin}
.tab{padding:10px 16px;font-size:13px;color:#777;cursor:pointer;border-bottom:2px solid transparent;user-select:none;white-space:nowrap}
.tab:hover{color:#ccc}.tab.active{color:#e0e0e0;border-bottom-color:#6af}
.tab .badge{background:#2a2a2a;color:#999;font-size:10px;padding:1px 7px;border-radius:8px;margin-left:6px}
.tab.active .badge{background:#1a3a5a;color:#8bf}
.panel{display:none;flex:1;overflow:hidden;flex-direction:column}.panel.active{display:flex}
.toolbar{padding:8px 16px;background:#151515;border-bottom:1px solid #222;display:flex;gap:8px;align-items:center;flex-wrap:wrap;flex-shrink:0}
.toolbar input,.toolbar select{background:#1e1e1e;border:1px solid #333;color:#ccc;padding:5px 10px;border-radius:3px;font-size:12px;font-family:inherit}
.toolbar input::placeholder{color:#555}.toolbar input:focus,.toolbar select:focus{outline:none;border-color:#6af}
.toolbar .spacer{margin-left:auto}
.btn{background:#2a2a2a;border:1px solid #444;color:#ccc;padding:5px 12px;border-radius:3px;cursor:pointer;font-size:12px;font-family:inherit}
.btn:hover{background:#333;border-color:#666}
.btn.primary{background:#1a3a5a;border-color:#2a5a8a;color:#8bf}
.btn.primary:hover{background:#1d4468}
.btn.danger{background:#3a1a1a;border-color:#5a2a2a;color:#e88}
.btn.danger:hover{background:#4a2020}
.btn.small{padding:3px 9px;font-size:11px}
.btn:disabled{opacity:.35;cursor:default}
.table-wrap{flex:1;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#1a1a1a;color:#999;text-align:left;padding:8px 10px;font-weight:500;position:sticky;top:0;z-index:1;border-bottom:1px solid #333;white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #222;color:#bbb;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
tr:hover td{background:#1a1a1a}
td.empty{color:#555;text-align:center;padding:32px 16px}
.mono{font-family:monospace;font-size:11px}
.chip{display:inline-block;padding:1px 7px;border-radius:3px;font-size:10px;font-weight:500;background:#1e1e1e;border:1px solid #333;color:#8bf}
.iframe-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#0d0d0d}
.iframe-wrap iframe{flex:1;width:100%;border:none;background:#0d0d0d}
.card{background:#151515;border:1px solid #222;border-radius:6px;padding:14px 16px}
.card-title{font-size:12px;color:#e0e0e0;font-weight:600;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.card-title .hint{color:#666;font-weight:400;font-size:11px;margin-left:auto}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px 16px;overflow:auto}
.grid .full{grid-column:1 / -1}
.form-grid{display:grid;grid-template-columns:140px 1fr;gap:8px 12px;font-size:12px;align-items:center}
.form-grid label{color:#888}
.switch-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #222}
.switch-row:last-child{border-bottom:none}
.switch-label{font-size:12px;color:#ccc}
.switch-label small{display:block;color:#666;font-size:11px;margin-top:2px}
.switch{appearance:none;-webkit-appearance:none;width:36px;height:20px;flex:none;border-radius:999px;background:#2a2a2a;position:relative;cursor:pointer;transition:background .2s;border:1px solid #333}
.switch::after{content:'';position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#888;transition:left .2s,background .2s}
.switch:checked{background:#1a3a5a;border-color:#2a5a8a}
.switch:checked::after{left:18px;background:#8bf}
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);z-index:100;justify-content:center;align-items:center}
.modal-overlay.show{display:flex}
.modal{background:#1a1a1a;border:1px solid #333;border-radius:8px;max-width:820px;width:92%;max-height:88vh;display:flex;flex-direction:column}
.modal-header{display:flex;align-items:center;gap:10px;padding:14px 20px;border-bottom:1px solid #2a2a2a}
.modal-header h2{font-size:14px;color:#e0e0e0}
.modal-body{padding:16px 20px;overflow:auto;flex:1}
.modal-close{background:#2a2a2a;border:1px solid #444;color:#ccc;padding:6px 16px;border-radius:4px;cursor:pointer;font-size:12px;margin:12px 20px 16px auto;display:block}
.modal-close:hover{background:#333;border-color:#666}
#toast{position:fixed;bottom:16px;right:16px;background:#1a1a1a;border:1px solid #333;padding:10px 16px;border-radius:6px;color:#ccc;box-shadow:0 8px 24px rgba(0,0,0,.5);opacity:0;transform:translateY(8px);transition:opacity .2s,transform .2s;pointer-events:none;z-index:1000;font-size:12px;max-width:420px}
#toast.show{opacity:1;transform:translateY(0)}
@media(max-width:900px){.grid{grid-template-columns:1fr} header{flex-wrap:wrap} .header-actions{margin-left:0}}
</style>
</head>
<body>

<header>
  <h1>ASHA <span>Brain</span> <small>Memory Maintenance Console</small></h1>
  <div class="header-actions">
    <span class="badge-pill" id="badge-db" title="Active database">DB: —</span>
    <span class="badge-pill off" id="badge-sched"><span class="dot off"></span> Scheduler off</span>
    <button class="btn small" onclick="fetchStatus()">Refresh</button>
  </div>
</header>
<div id="status">Loading…</div>
<div id="stats-bar">
  <div class="stat"><span>Nodes</span><span id="s-nodes">—</span></div>
  <div class="stat"><span>Core / Agent</span><span id="s-core-agent">—</span></div>
  <div class="stat"><span>Edges</span><span id="s-edges">—</span></div>
  <div class="stat"><span>DB Size</span><span id="s-size">—</span></div>
  <div class="stat"><span>Snapshots</span><span id="s-snaps">—</span></div>
  <div class="stat" id="st-bloat" style="display:none"><span>Bloat</span><span id="s-bloat">—</span></div>
</div>

<div class="tabs">
  <div class="tab active" data-panel="overview">Overview</div>
  <div class="tab" data-panel="maintenance">Maintenance</div>
  <div class="tab" data-panel="graduate">Graduate <span class="badge" id="b-grad">0</span></div>
  <div class="tab" data-panel="contradicts">Contradicts <span class="badge" id="b-contra">0</span></div>
  <div class="tab" data-panel="ephemeral">Ephemeral</div>
  <div class="tab" data-panel="graph">Graph</div>
  <div class="tab" data-panel="manager">Manager</div>
  <div class="tab" data-panel="system">System</div>
  <div class="tab" data-panel="statistics">Statistics</div>
  <div class="tab" data-panel="config">Config</div>
</div>

<div id="panels" style="flex:1;display:flex;flex-direction:column;overflow:hidden">

<!-- Overview -->
<div class="panel active" id="panel-overview">
  <div class="grid">
    <div class="card">
      <div class="card-title">Target Database <span class="hint mono" id="ov-db-path">—</span></div>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <select id="db-select" style="flex:1;background:#1e1e1e;border:1px solid #333;color:#ccc;padding:6px 10px;border-radius:3px;font-size:12px"></select>
      </div>
      <div style="display:flex;gap:8px">
        <input type="text" id="custom-db-input" placeholder="Paste full DB path…" style="flex:1">
        <button class="btn" onclick="applyCustomDB()">Switch</button>
        <button class="btn" onclick="copyDBPath()" title="Copy path">Copy</button>
        <a id="btn-download-db" class="btn" href="/api/db_bytes" download>Download</a>
      </div>
      <div style="margin-top:10px;font-size:11px;color:#666" id="ov-db-meta"></div>
    </div>
    <div class="card">
      <div class="card-title">Health &amp; Bloat <span class="hint" id="ov-health-hint">—</span></div>
      <div id="ov-bloat" style="font-size:12px;line-height:1.7;color:#bbb"></div>
      <div style="margin-top:10px;display:flex;gap:8px">
        <button class="btn small" onclick="switchTab('maintenance')">Maintenance</button>
        <button class="btn small" onclick="switchTab('graduate')">Graduate</button>
        <button class="btn small" onclick="switchTab('graph')">Graph</button>
      </div>
    </div>
    <div class="card full">
      <div class="card-title">Recent Execution History <span class="hint">last 5 runs</span></div>
      <div class="table-wrap" style="max-height:220px">
        <table><thead><tr><th>Timestamp</th><th>Jobs</th><th>Duration</th><th>Log</th></tr></thead><tbody id="ov-history"><tr><td class="empty" colspan="4">Loading…</td></tr></tbody></table>
      </div>
    </div>
  </div>
</div>

<!-- Maintenance -->
<div class="panel" id="panel-maintenance">
  <div class="toolbar">
    <span style="color:#888;font-size:12px">Instant Maintenance — mutations first, discovery last</span>
    <span class="spacer"></span>
    <span class="hint" id="bloat-hint" style="font-size:11px;color:#666"></span>
  </div>
  <div style="padding:14px 16px;display:flex;flex-wrap:wrap;gap:8px">
    <button class="btn" onclick="runJob(['dedup'])">Deduplicate</button>
    <button class="btn" onclick="runJob(['age_prune'])">Prune Stale</button>
    <button class="btn" onclick="runJob(['tiers'])">Manage Tiers</button>
    <button class="btn" onclick="runJob(['contradictions'])">Contradictions</button>
    <button class="btn" onclick="runJob(['discover'])">Discover Links</button>
    <button class="btn primary" onclick="runJob(['dedup','age_prune','tiers','contradictions','discover'])">Run FULL Maintenance</button>
    <button class="btn" onclick="rebuildVectors()">Rebuild Vectors</button>
    <button class="btn" onclick="createSnapshot()">Snapshot Now</button>
  </div>
  <div style="padding:0 16px 10px;display:flex;flex-wrap:wrap;gap:8px;border-top:1px solid #222;padding-top:10px;margin:0 16px">
    <button class="btn" style="border-color:#5a4a1a;color:#ea6" onclick="compactEphemeral()">Compact Ephemeral Logs</button>
    <button class="btn" style="border-color:#2a5a3a;color:#5d8" onclick="vacuumDB()">VACUUM (reclaim freelist)</button>
  </div>
  <div id="maint-result" style="margin:14px 16px;padding:12px;background:#111;border:1px solid #222;border-radius:4px;font-family:monospace;font-size:11px;color:#999;white-space:pre-wrap;max-height:220px;overflow:auto;display:none"></div>
  <div style="flex:1"></div>
</div>

<!-- Graduate -->
<div class="panel" id="panel-graduate">
  <div class="toolbar">
    <span style="color:#888;font-size:12px">Manual review — core_verified promotion (not part of automated maintenance)</span>
    <span class="spacer"></span>
    <button class="btn" onclick="loadGraduatePreview()">Refresh</button>
    <button class="btn primary" id="btn-graduate" onclick="doGraduate()">Graduate Now</button>
  </div>
  <div style="padding:10px 16px;display:flex;gap:16px;flex-wrap:wrap;font-size:12px" id="grad-stats">
    <div class="stat"><span>Total agent notes</span><span id="g-total">—</span></div>
    <div class="stat"><span>Review-ready</span><span id="g-ready">—</span></div>
    <div class="stat"><span>Graduable</span><span id="g-graduable">—</span></div>
    <div class="stat"><span>Private</span><span id="g-private">—</span></div>
    <div class="stat" style="color:#666">Graduable = review_ready OR trust≥0.7 &amp; importance≥0.6</div>
  </div>
  <div class="table-wrap">
    <table><thead><tr><th>Label</th><th>Content</th><th>Attention</th><th>Trust</th><th>Imp.</th><th>Updated</th></tr></thead><tbody id="grad-tbody"><tr><td class="empty" colspan="6">Click Refresh to load preview</td></tr></tbody></table>
  </div>
</div>

<!-- Contradicts -->
<div class="panel" id="panel-contradicts">
  <div class="toolbar">
    <span style="color:#888;font-size:12px">Contradiction Curation — pending review, confirm/ignore/resolve (never bulk-delete)</span>
    <select id="contra-status" style="background:#1e1e1e;border:1px solid #333;color:#ccc;padding:4px 8px;border-radius:3px;font-size:12px">
      <option value="pending">pending</option><option value="confirmed">confirmed</option><option value="ignored">ignored</option><option value="">all</option>
    </select>
    <button class="btn small" onclick="loadContradictions()">Refresh</button>
    <button class="btn small" onclick="runJob(['contradictions'])">Re-scan</button>
    <button class="btn small" style="border-color:#2a5a3a;color:#5d8" onclick="autoResolveDry()">Preview Auto-resolve</button>
    <button class="btn small primary" onclick="autoResolveExec()">Auto-resolve low-trust</button>
    <span class="spacer"></span>
    <span id="contra-counts" style="font-size:11px;color:#666"></span>
  </div>
  <div style="padding:6px 16px;font-size:11px;color:#666;border-bottom:1px solid #222">Opt-in auto-resolve: when enabled in Config, pending contradictions where one side <span id="contra-thresh-low">0.3</span> and other ><span id="contra-thresh-high">0.8</span> trust → suggested <span class="chip">keep high-trust</span> one-click. Enable in Config → Contradiction Auto-resolve.</div>
  <div class="table-wrap">
    <table><thead><tr><th>Confidence</th><th>From (label/type/trust)</th><th>To (label/type/trust)</th><th>Overlap</th><th>Status</th><th>Actions</th></tr></thead><tbody id="contra-tbody"><tr><td class="empty" colspan="6">Click Refresh to load</td></tr></tbody></table>
  </div>
</div>

<!-- Ephemeral -->
<div class="panel" id="panel-ephemeral">
  <div class="toolbar">
    <span style="color:#888;font-size:12px">Ephemeral Allowlist — safe suggest-only auto-detect</span>
    <span class="spacer"></span>
    <button class="btn" onclick="loadEphemeralCandidates()">Scan Candidates</button>
    <button class="btn primary" onclick="compactEphemeral()">Compact Now</button>
  </div>
  <div style="padding:10px 16px;display:flex;gap:16px;flex-wrap:wrap;font-size:12px" id="eph-allow">
    <span style="color:#666">Allowlist:</span>
    <span id="eph-list" style="color:#8bf">—</span>
  </div>
  <div style="display:flex;gap:16px;padding:0 16px 8px;flex-wrap:wrap;font-size:12px">
    <div class="card" style="flex:1;min-width:280px">
      <div class="card-title">Current Allowlist</div>
      <div id="eph-current" style="display:flex;flex-wrap:wrap;gap:6px"></div>
      <div style="margin-top:10px;display:flex;gap:8px">
        <input type="text" id="eph-custom" placeholder="LABEL_NAME" style="flex:1">
        <button class="btn small" onclick="addEphemeralLabel()">Add</button>
      </div>
    </div>
    <div class="card" style="flex:1;min-width:280px">
      <div class="card-title">Auto-Detect Candidates <span class="hint">never auto-deletes</span></div>
      <div style="font-size:11px;color:#666;margin-bottom:6px">Heuristics: high freq + JSON shape + low importance + few edges + UPPER_SNAKE. Click Add to approve.</div>
      <div class="table-wrap" style="max-height:260px"><table><thead><tr><th>Label</th><th>Count</th><th>JSON%</th><th>Score</th><th>Reasons</th><th></th></tr></thead><tbody id="eph-candidates"><tr><td class="empty" colspan="6">Click Scan Candidates</td></tr></tbody></table></div>
    </div>
  </div>
  <div style="flex:1"></div>
</div>

<!-- Graph -->
<div class="panel" id="panel-graph">
  <div class="toolbar">
    <span style="color:#888;font-size:12px">Live Graph — embedded ASHA Graph (no download needed)</span>
    <button class="btn primary" onclick="loadGraphDB()">Load Active DB</button>
    <button class="btn" onclick="reloadGraphFrame()">Reload View</button>
    <span class="spacer"></span>
    <span style="color:#666;font-size:11px" id="graph-status">Frame ready — click Load Active DB</span>
  </div>
  <div class="iframe-wrap"><iframe id="graph-frame" src="/humantools/asha_graph.html?embedded=1"></iframe></div>
</div>

<!-- Manager -->
<div class="panel" id="panel-manager">
  <div class="toolbar">
    <span style="color:#888;font-size:12px">Live Manager — embedded ASHA Manager (no download needed)</span>
    <button class="btn primary" onclick="loadManagerDB()">Load Active DB</button>
    <button class="btn" onclick="reloadManagerFrame()">Reload View</button>
    <button class="btn primary" id="btn-manager-apply" onclick="applyManagerEdits()" title="Apply Manager edits directly to DB, then Discover Links + Rebuild Vectors">Apply to DB</button>
    <span class="spacer"></span>
    <span style="color:#666;font-size:11px" id="manager-status">Frame ready — click Load Active DB</span>
  </div>
  <div class="iframe-wrap"><iframe id="manager-frame" src="/humantools/asha_manager.html?embedded=1"></iframe></div>
</div>

<!-- System -->
<div class="panel" id="panel-system">
  <div style="flex:1;overflow:auto;padding:14px 16px;display:flex;flex-direction:column;gap:16px">
    <div class="card">
      <div class="card-title">Snapshots &amp; Rollback <span class="hint">brain/snapshots/</span></div>
      <div class="table-wrap" style="max-height:220px"><table><thead><tr><th>Filename</th><th>Created</th><th>Size</th><th></th></tr></thead><tbody id="table-snapshots"><tr><td class="empty" colspan="4">Loading…</td></tr></tbody></table></div>
    </div>
    <div class="card">
      <div class="card-title">Audit Logs <span class="hint">brain/logs/</span></div>
      <div class="table-wrap" style="max-height:220px"><table><thead><tr><th>Filename</th><th>Created</th><th>Size</th><th></th></tr></thead><tbody id="table-logs"><tr><td class="empty" colspan="4">Loading…</td></tr></tbody></table></div>
    </div>
    <div class="card">
      <div class="card-title">Execution History <span class="hint">job_history.json</span></div>
      <div class="table-wrap" style="max-height:260px"><table><thead><tr><th>Timestamp</th><th>Jobs</th><th>Duration</th><th>DB</th><th>Log</th></tr></thead><tbody id="table-history"><tr><td class="empty" colspan="5">Loading…</td></tr></tbody></table></div>
    </div>
  </div>
</div>

<!-- Statistics -->
<div class="panel" id="panel-statistics">
  <div style="flex:1;overflow:auto;padding:14px 16px;display:flex;flex-direction:column;gap:16px">
    <div class="toolbar" style="margin:-14px -16px 0;border-radius:6px 6px 0 0">
      <span style="color:#888;font-size:12px">Full DB Statistics — one place</span>
      <span class="spacer"></span>
      <button class="btn small" onclick="loadStatistics()">Refresh</button>
    </div>
    <div class="grid" style="padding:0">
      <div class="card">
        <div class="card-title">Overview</div>
        <div id="stats-overview" style="font-size:12px;line-height:1.7;color:#bbb">Loading…</div>
      </div>
      <div class="card">
        <div class="card-title">Bloat &amp; Freelist</div>
        <div id="stats-bloat" style="font-size:12px;line-height:1.7;color:#bbb">Loading…</div>
      </div>
      <div class="card">
        <div class="card-title">Node Types</div>
        <div id="stats-nodetypes" style="font-size:11px;line-height:1.7">Loading…</div>
      </div>
      <div class="card">
        <div class="card-title">Edge Types</div>
        <div id="stats-edgetypes" style="font-size:11px;line-height:1.7">Loading…</div>
      </div>
      <div class="card">
        <div class="card-title">Layers</div>
        <div id="stats-layers" style="font-size:11px;line-height:1.7">Loading…</div>
      </div>
      <div class="card">
        <div class="card-title">Sources</div>
        <div id="stats-sources" style="font-size:11px;line-height:1.7">Loading…</div>
      </div>
      <div class="card full">
        <div class="card-title">Top Labels (by count)</div>
        <div class="table-wrap" style="max-height:220px"><table><thead><tr><th>Label</th><th>Count</th></tr></thead><tbody id="stats-toplabels"><tr><td class="empty" colspan="2">Loading…</td></tr></tbody></table></div>
      </div>
    </div>
  </div>
</div>

<!-- Config -->
<div class="panel" id="panel-config">
  <div style="flex:1;overflow:auto;padding:14px 16px">
    <div class="grid" style="padding:0">
      <div class="card">
        <div class="card-title">Scheduler &amp; Pruning</div>
        <div class="form-grid">
          <label>Interval (min)</label><input type="number" id="interval-input" value="60" min="5" max="1440">
          <label>Unused Age (days)</label><input type="number" id="max-days-input" value="4" min="1" max="30">
          <label>Dedup Similarity</label><input type="number" id="dedup-threshold-input" value="0.85" min="0.1" max="1" step="0.01">
          <label>Prune Importance Floor</label><input type="number" id="prune-floor-input" value="0.05" min="0" max="1" step="0.01">
          <label>Keep Last (ephemeral)</label><input type="number" id="keep-last-input" value="3" min="1" max="20">
          <label>Max Age (ephemeral days)</label><input type="number" id="max-age-input" value="7" min="1" max="30">
          <label>Vacuum Freelist %</label><input type="number" id="vacuum-pct-input" value="15" min="1" max="90">
          <label>Vacuum Min Pages</label><input type="number" id="vacuum-pages-input" value="50" min="10" max="1000">
          <label>Contra Low Trust</label><input type="number" id="contra-low-input" value="0.3" min="0" max="1" step="0.05">
          <label>Contra High Trust</label><input type="number" id="contra-high-input" value="0.8" min="0" max="1" step="0.05">
        </div>
        <div style="margin-top:10px;display:flex;gap:8px">
          <button class="btn small" onclick="checkVacuum()">Check &amp; Auto-Vacuum if over threshold</button>
          <span id="vacuum-check-res" style="font-size:11px;color:#666;align-self:center"></span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Toggles</div>
        <div class="switch-row"><div><div class="switch-label">Auto Snapshot Before Jobs <small>Safety backup before each maintenance run</small></div></div><input type="checkbox" class="switch" id="snapshot-toggle" checked></div>
        <div class="switch-row"><div><div class="switch-label">Auto Rebuild Vectors <small>TF-IDF rebuild after mutating maintenance</small></div></div><input type="checkbox" class="switch" id="rebuild-toggle" checked></div>
        <div class="switch-row"><div><div class="switch-label">Vacuum After Prune <small>Auto VACUUM when freelist is bloated</small></div></div><input type="checkbox" class="switch" id="vacuum-toggle" checked></div>
        <div class="switch-row"><div><div class="switch-label">Contradiction Auto-resolve <small>Opt-in: if pending trust gap low&lt;0.3 vs high&gt;0.8, suggest keep high-trust</small></div></div><input type="checkbox" class="switch" id="contra-auto-toggle"></div>
        <div style="margin-top:14px;display:flex;gap:8px">
          <button class="btn" id="btn-toggle-sched" onclick="toggleScheduler()">Enable Scheduler</button>
          <button class="btn primary" onclick="saveConfig()">Save Config</button>
          <button class="btn" onclick="resetConfig()">Reset Defaults</button>
        </div>
      </div>
    </div>
  </div>
</div>

</div>

<div class="modal-overlay" id="log-modal"><div class="modal"><div class="modal-header"><h2 id="log-title">Audit Log</h2><span style="margin-left:auto"></span></div><div class="modal-body"><pre id="log-content" style="font-family:monospace;font-size:11px;line-height:1.6;white-space:pre-wrap;color:#bbb"></pre></div><button class="modal-close" onclick="closeLogModal()">Close</button></div></div>
<div id="toast">Done</div>

<script>
let currentDB="";let schedulerRunning=false;
const $=id=>document.getElementById(id);
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function ago(ts){if(ts==null)return "-";let sec=Math.floor((Date.now()-ts*1000)/1000);if(sec<10)return "now";if(sec<60)return sec+"s";let m=Math.floor(sec/60);if(m<60)return m+"m";let h=Math.floor(m/60);if(h<24)return h+"h";return Math.floor(h/24)+"d";}
function switchTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.panel===name));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active', p.id==='panel-'+name));
  if(name==='graduate') loadGraduatePreview();
  if(name==='contradicts') loadContradictions();
  if(name==='ephemeral') {renderEphemeralAllowlist(); loadEphemeralCandidates();}
  if(name==='statistics') loadStatistics();
  if(name==='graph') {$('graph-frame').style.height='100%';}
  if(name==='manager') {$('manager-frame').style.height='100%';}
}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>switchTab(t.dataset.panel)));

async function fetchStatus(){
  try{
    const res=await fetch('/api/status');const data=await res.json();renderStatus(data);
    $('status').textContent='System ready — '+new Date().toLocaleTimeString();$('status').className='ok';
  }catch(e){$('status').textContent='Status fetch failed: '+e.message;$('status').className='error'}
}
function renderStatus(data){
  const h=data.health||{};const b=data.bloat||h.bloat||{};
  $('s-nodes').textContent=h.total_nodes||0;
  $('s-core-agent').textContent=(h.core_nodes||0)+' / '+(h.agent_note_nodes||0);
  $('s-edges').textContent=h.total_edges||0;
  $('s-size').textContent=(h.db_size_mb||0).toFixed(2)+' MB';
  $('s-snaps').textContent=(data.snapshots||[]).length;
  // bloat pill
  const freelistPct=b.freelist_pct ?? h.bloat?.freelist_pct ?? 0;
  const needsVacuum=b.needs_vacuum || h.bloat?.needs_vacuum;
  const ephem=h.ephemeral||b.ephemeral||{};
  const totalEphem=ephem._total_ephemeral_labels||0;
  const st=$('st-bloat'),sb=$('s-bloat');
  if(totalEphem>0||needsVacuum){
    st.style.display='flex';st.className=needsVacuum?'stat bad':'stat warn';
    sb.textContent=(needsVacuum?('VACUUM '+freelistPct+'%'):totalEphem+' ephemeral');
  } else {st.style.display='none'}
  // header badges
  $('badge-db').textContent='DB: '+(h.db_path||'None');$('badge-db').title=h.db_path||'None';
  schedulerRunning=data.scheduler&&data.scheduler.running;
  const sbadge=$('badge-sched');
  sbadge.className=schedulerRunning?'badge-pill on':'badge-pill off';
  sbadge.innerHTML=(schedulerRunning?'<span class="dot on"></span> Scheduler on':'<span class="dot off"></span> Scheduler off');
  const btn=$('btn-toggle-sched');if(btn){btn.textContent=schedulerRunning?'Stop Scheduler':'Enable Scheduler';btn.className=schedulerRunning?'btn danger':'btn'}
  // config inputs
  const cfg=(data.scheduler&&data.scheduler.config)||{};
  $('interval-input').value=cfg.interval_minutes??60;
  $('max-days-input').value=cfg.max_unused_days??4;
  $('dedup-threshold-input').value=cfg.dedup_similarity_threshold??0.85;
  $('prune-floor-input').value=cfg.prune_importance_floor??0.05;
  $('keep-last-input').value=cfg.ephemeral_keep_last??3;
  $('max-age-input').value=cfg.ephemeral_max_age_days??7;
  $('vacuum-pct-input').value=cfg.vacuum_freelist_threshold_pct??15;
  $('vacuum-pages-input').value=cfg.vacuum_freelist_min_pages??50;
  $('contra-low-input').value=cfg.contradiction_low_trust??0.3;
  $('contra-high-input').value=cfg.contradiction_high_trust??0.8;
  $('snapshot-toggle').checked=cfg.auto_snapshot_before_jobs!==false;
  $('rebuild-toggle').checked=cfg.auto_rebuild_vectors!==false;
  $('vacuum-toggle').checked=cfg.vacuum_after_prune!==false;
  $('contra-auto-toggle').checked=cfg.contradiction_auto_resolve===true;
  // update threshold labels in contradicts tab
  try{$('contra-thresh-low').textContent=cfg.contradiction_low_trust??0.3; $('contra-thresh-high').textContent=cfg.contradiction_high_trust??0.8;}catch(e){}
  renderEphemeralAllowlist(cfg);
  // overview
  $('ov-db-path').textContent=h.db_path||'None';
  $('ov-db-meta').textContent=(h.total_nodes||0)+' nodes · '+(h.total_edges||0)+' edges · '+(h.db_size_mb||0).toFixed(2)+' MB';
  const ovBloat=$('ov-bloat');
  if(ovBloat){
    const jsonLogs=ephem._json_log_nodes||0;const contr=h.contradicts_total??b.contradicts_total??0;
    ovBloat.innerHTML='Ephemeral logs: <span class="chip">'+totalEphem+' tracked</span> JSON logs: <span class="chip">'+jsonLogs+'</span> CONTRADICTS: <span class="chip">'+contr+'</span><br>Freelist: <span style="color:'+(needsVacuum?'#ea6':'#666')+'">'+freelistPct+'%</span>'+(needsVacuum?' <span style="color:#ea6">— VACUUM recommended</span>':' — ok');
  }
  $('ov-health-hint').textContent=h.status||'';
  // bloat hint maintenance
  const bh=$('bloat-hint');if(bh){bh.textContent=needsVacuum?('Freelist '+freelistPct+'% — VACUUM recommended'):('Freelist '+freelistPct+'% — ok');bh.style.color=needsVacuum?'#ea6':'#666'}
  // databases
  const sel=$('db-select');sel.innerHTML='';
  (data.databases||[]).forEach(db=>{
    const opt=document.createElement('option');opt.value=db.path;opt.textContent=db.filename+' — '+db.path+(db.is_current?'  [ACTIVE]':'');if(db.is_current)opt.selected=true;sel.appendChild(opt);
  });
  sel.onchange=()=>selectDatabase(sel.value);
  // snapshots - tbody has id directly (was table id before)
  const snapTbody=document.getElementById('table-snapshots');
  if(snapTbody){
    snapTbody.innerHTML='';
    if(data.snapshots&&data.snapshots.length){
      data.snapshots.slice(0,8).forEach(s=>{
        const tr=document.createElement('tr');const sizeMB=(s.size_bytes/(1024*1024)).toFixed(2);
        tr.innerHTML='<td class="mono">'+esc(s.filename)+'</td><td>'+esc(s.created_at)+'</td><td>'+sizeMB+' MB</td><td><button class="btn small" onclick="restoreSnapshot(\\''+esc(s.filename)+'\\')">Restore</button> <button class="btn small danger" onclick="deleteSnapshot(\\''+esc(s.filename)+'\\')">Delete</button></td>';snapTbody.appendChild(tr);
      });
    } else snapTbody.innerHTML='<tr><td class="empty" colspan="4">No snapshots</td></tr>';
  }
  // logs
  const logsTbody=document.getElementById('table-logs');
  if(logsTbody){
    logsTbody.innerHTML='';
    if(data.logs&&data.logs.length){
      data.logs.slice(0,8).forEach(l=>{
        const tr=document.createElement('tr');const sizeKB=(l.size_bytes/1024).toFixed(1);
        tr.innerHTML='<td class="mono">'+esc(l.filename)+'</td><td>'+esc(l.created_at)+'</td><td><button class="btn small" onclick="viewLog(\\''+esc(l.filename)+'\\')">View</button></td>';logsTbody.appendChild(tr);
      });
    } else logsTbody.innerHTML='<tr><td class="empty" colspan="4">No audit logs yet</td></tr>';
  }
  // history overview + system
  const histData=data.history||[];
  [['ov-history',4],['table-history',5]].forEach(([id,cols])=>{
    const tb=document.getElementById(id);if(!tb)return;tb.innerHTML='';
    if(histData.length){
      histData.slice(-5).reverse().forEach(entry=>{
        const tr=document.createElement('tr');
        const jobsStr=(entry.job_types||[]).join(', ');
        const mdLog=entry.markdown_log?'<a href="#" style="color:#8bf" onclick="viewLog(\\''+esc(entry.markdown_log)+'\\');return false">'+esc(entry.markdown_log)+'</a>':'<span style="color:#555">—</span>';
        if(id==='ov-history'){
          tr.innerHTML='<td>'+esc(entry.timestamp)+'</td><td><span class="chip">'+esc(jobsStr)+'</span></td><td>'+esc(entry.duration_seconds)+'s</td><td>'+mdLog+'</td>';
        } else {
          const dbShort=(entry.target_db||'').split('/').pop()||entry.target_db||'—';
          tr.innerHTML='<td>'+esc(entry.timestamp)+'</td><td><span class="chip">'+esc(jobsStr)+'</span></td><td>'+esc(entry.duration_seconds)+'s</td><td class="mono" title="'+esc(entry.target_db||'')+'">'+esc(dbShort)+'</td><td>'+mdLog+'</td>';
        }
        tb.appendChild(tr);
      });
    } else tb.innerHTML='<tr><td class="empty" colspan="'+cols+'">No history yet</td></tr>';
  });
  // graduate badge + contradicts badge
  fetchGraduateBadge(); fetchContradictionBadge();
}
async function fetchGraduateBadge(){
  try{const r=await fetch('/api/graduate_preview');const d=await r.json();$('b-grad').textContent=d.graduable??d.total??0;}catch(e){}
}
// log viewer
async function viewLog(filename){
  const res=await fetch('/api/log_content?file='+encodeURIComponent(filename));
  const data=await res.json();
  $('log-title').textContent='Audit Log — '+(data.filename||filename);
  $('log-content').textContent=data.content||data.error||'Log could not be loaded.';
  $('log-modal').classList.add('show');
}
function closeLogModal(){$('log-modal').classList.remove('show');}
document.getElementById('log-modal').addEventListener('click',e=>{if(e.target.id==='log-modal')closeLogModal();});
// DB switching
async function selectDatabase(path){
  if(!path) return;
  const res=await fetch('/api/switch_db',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({db_path:path})});
  const data=await res.json();
  showToast(data.message||'Switched active database');
  fetchStatus();
}
async function applyCustomDB(){const p=$('custom-db-input').value.trim();if(p)selectDatabase(p);}
function copyDBPath(){const t=$('ov-db-path').textContent;if(t&&t!=='None'){navigator.clipboard.writeText(t).then(()=>showToast('DB path copied'))}}
// maintenance
async function runJob(jobs){
  showToast('Running: '+jobs.join(', ')+' …');$('maint-result').style.display='block';$('maint-result').textContent='Running…';
  try{
    const res=await fetch('/api/run_job',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jobs})});
    const data=await res.json();const run=data.run||{};
    $('maint-result').textContent=JSON.stringify(run,null,2);
    showToast('Maintenance complete in '+(run.duration_seconds||'?')+'s — '+(run.job_types||jobs).join(', '));
  }catch(e){showToast('Maintenance failed: '+e.message);$('maint-result').textContent='Failed: '+e.message}
  fetchStatus();
}
async function createSnapshot(){showToast('Creating snapshot…');await fetch('/api/create_snapshot',{method:'POST'});showToast('Snapshot created');fetchStatus();}
async function rebuildVectors(){
  showToast('Rebuilding vectors…');
  const res=await fetch('/api/rebuild_vectors',{method:'POST'});const data=await res.json();
  if(data&&data.status==='success') showToast('Vectors rebuilt: '+data.vectors_rebuilt+' in '+data.duration_s+'s');
  else showToast('Rebuild failed: '+(data?data.message:'error'));
  fetchStatus();
}
async function restoreSnapshot(filename){
  if(!confirm('Restore snapshot '+filename+'? Current DB will be replaced.')) return;
  showToast('Restoring…');
  await fetch('/api/restore_snapshot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename})});
  showToast('Database restored');fetchStatus();
}
async function vacuumDB(){
  if(!confirm('Run VACUUM? Reclaims freelist space and locks DB briefly.')) return;
  showToast('Running VACUUM…');
  const res=await fetch('/api/vacuum',{method:'POST'});const data=await res.json();
  if(data.status==='success') showToast('VACUUM: '+data.before_mb+' → '+data.after_mb+' MB (saved '+data.saved_mb+' MB)');
  else showToast('VACUUM failed: '+(data.message||'error'));
  fetchStatus();
}
async function compactEphemeral(){
  if(!confirm('Compact ephemeral logs? Keeps last 3 per label + 7-day TTL.')) return;
  showToast('Compacting ephemeral logs…');
  const res=await fetch('/api/compact_ephemeral',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vacuum:true})});
  const data=await res.json();
  if(data.status==='success'){const vac=data.vacuum?' VACUUM '+data.vacuum.saved_mb+' MB saved':'';showToast('Compacted '+data.removed_total+' nodes'+vac);} else showToast('Compaction failed: '+(data.message||'error'));
  fetchStatus();
}
async function deleteSnapshot(filename){
  if(!confirm('Delete snapshot '+filename+'? This cannot be undone.')) return;
  const res=await fetch('/api/delete_snapshot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename})});
  const d=await res.json();
  if(d.status==='success'){showToast('Deleted '+filename);fetchStatus();}
  else showToast(d.message||'Delete failed');
}
async function applyManagerEdits(){
  const iframe=$('manager-frame');
  if(!iframe || !iframe.contentWindow){showToast('Manager not loaded');return}
  $('manager-status').textContent='Requesting DB from Manager…';
  iframe.contentWindow.postMessage({type:'asha-manager-request-export'}, '*');
}
window.addEventListener('message', function(ev){
  if(ev.data && ev.data.type==='asha-manager-commit' && ev.data.buffer){
    (async function(){
      const buf = ev.data.buffer;
      let bytes;
      if(buf instanceof ArrayBuffer) bytes=new Uint8Array(buf);
      else if(buf instanceof Uint8Array) bytes=buf;
      else if(Array.isArray(buf)) bytes=new Uint8Array(buf);
      else bytes=new Uint8Array(buf);
      $('manager-status').textContent='Applying '+ (bytes.length/1024).toFixed(1) +' KB to DB…';
      try{
        const res=await fetch('/api/manager_commit',{method:'POST', headers:{'Content-Type':'application/octet-stream'}, body: bytes});
        const d=await res.json();
        if(d.status==='success'){
          const disc=d.discover? (d.discover.links_created||d.discover.links_created_core||0) : 0;
          showToast('Manager applied + discover '+disc+' links + vectors rebuilt');$('manager-status').textContent='Applied — discover '+disc+' + vectors';
          fetchStatus();
        } else {showToast(d.message||'Apply failed'); $('manager-status').textContent='Apply failed';}
      }catch(e){showToast('Apply failed: '+e.message); $('manager-status').textContent='Apply failed';}
    })();
  }
});
// scheduler/config
async function toggleScheduler(){
  const interval=$('interval-input').value;const maxDays=$('max-days-input').value;
  await fetch('/api/scheduler',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!schedulerRunning,interval_minutes:interval,max_unused_days:maxDays})});
  showToast(schedulerRunning?'Scheduler stopped':'Scheduler started');fetchStatus();
}
async function saveConfig(){
  const body={
    interval_minutes:parseInt($('interval-input').value)||60,
    max_unused_days:parseInt($('max-days-input').value)||4,
    dedup_similarity_threshold:parseFloat($('dedup-threshold-input').value)||0.85,
    prune_importance_floor:parseFloat($('prune-floor-input').value)||0.05,
    ephemeral_keep_last:parseInt($('keep-last-input').value)||3,
    ephemeral_max_age_days:parseInt($('max-age-input').value)||7,
    vacuum_freelist_threshold_pct:parseInt($('vacuum-pct-input').value)||15,
    vacuum_freelist_min_pages:parseInt($('vacuum-pages-input').value)||50,
    contradiction_low_trust:parseFloat($('contra-low-input').value)||0.3,
    contradiction_high_trust:parseFloat($('contra-high-input').value)||0.8,
    contradiction_auto_resolve:$('contra-auto-toggle').checked,
    auto_snapshot_before_jobs:$('snapshot-toggle').checked,
    auto_rebuild_vectors:$('rebuild-toggle').checked,
    vacuum_after_prune:$('vacuum-toggle').checked
  };
  const res=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await res.json();showToast(data.status==='success'?'Configuration saved':'Save failed: '+(data.message||''));fetchStatus();
}
async function checkVacuum(){
  $('vacuum-check-res').textContent='Checking…';
  const res=await fetch('/api/check_vacuum',{method:'POST'});
  const d=await res.json();
  if(d.triggered){
    const v=d.vacuum;const vb=d.bloat_after||{};
    $('vacuum-check-res').textContent='VACUUM fired: '+v.before_mb+'→'+v.after_mb+' MB (saved '+v.saved_mb+' MB) — now '+ (vb.freelist_pct??'?')+'%';
    if(d.vector_rebuild) $('vacuum-check-res').textContent+=' + vectors rebuilt';
    showToast('Auto-VACUUM: saved '+v.saved_mb+' MB');fetchStatus();
  } else if(d.bloat){
    $('vacuum-check-res').textContent='No need: '+d.bloat.freelist_pct+'% (threshold '+d.bloat.vacuum_threshold_pct+'% / '+d.bloat.vacuum_min_pages+' pages)';
  } else $('vacuum-check-res').textContent=d.error||'check failed';
}
async function resetConfig(){
  if(!confirm('Reset all brain settings to defaults? (active DB path is kept)')) return;
  const res=await fetch('/api/config_reset',{method:'POST'});const data=await res.json();
  showToast(data.status==='success'?'Configuration reset':'Reset failed');fetchStatus();
}
// graduate
async function loadGraduatePreview(){
  const tb=$('grad-tbody');tb.innerHTML='<tr><td class="empty" colspan="6">Loading…</td></tr>';
  try{
    const res=await fetch('/api/graduate_preview');const d=await res.json();
    if(d.error){tb.innerHTML='<tr><td class="empty" colspan="6">'+esc(d.error)+'</td></tr>';return}
    $('g-total').textContent=d.total_agent_notes??0;
    $('g-ready').textContent=d.review_ready??0;
    $('g-graduable').textContent=d.graduable??0;
    $('g-private').textContent=d.agent_private??0;
    $('b-grad').textContent=d.graduable??0;
    const rows=d.graduable_preview||[];
    if(!rows.length){tb.innerHTML='<tr><td class="empty" colspan="6">No graduable notes — nothing to graduate</td></tr>';return}
    tb.innerHTML=rows.map(r=>{
      const c=esc((r.content||'').slice(0,100));const att=esc(r.attention||'');
      return '<tr><td>'+esc(r.label)+'</td><td title="'+esc(r.content||'')+'">'+c+'</td><td><span class="chip">'+att+'</span></td><td>'+(r.trust_level!=null?Number(r.trust_level).toFixed(2):'-')+'</td><td>'+(r.importance!=null?Number(r.importance).toFixed(2):'-')+'</td><td>'+ago(r.updated_at)+'</td></tr>';
    }).join('');
  }catch(e){tb.innerHTML='<tr><td class="empty" colspan="6">Failed: '+esc(e.message)+'</td></tr>'}
}
async function doGraduate(){
  if(!confirm('Graduate all eligible notes to core memory?')) return;
  showToast('Graduating notes…');
  const res=await fetch('/api/graduate',{method:'POST'});const d=await res.json();
  if(d.status==='success') showToast('Graduated '+d.graduated+' notes to core memory');
  else showToast('Graduation failed: '+(d.message||'error'));
  loadGraduatePreview();fetchStatus();
}
function renderEphemeralAllowlist(cfg){
  const list=(cfg && cfg.ephemeral_labels) ? cfg.ephemeral_labels : [];
  const el=$('eph-list');if(el) el.textContent=list.length?list.join(', '):'—';
  const wrap=$('eph-current');if(!wrap) return;
  wrap.innerHTML='';
  if(!list.length){wrap.innerHTML='<span style="color:#555;font-size:11px">Allowlist empty</span>';return}
  list.forEach(l=>{
    const tag=document.createElement('span');
    tag.className='chip';
    tag.style.cursor='pointer';
    tag.title='Click to remove';
    tag.textContent=l+' ×';
    tag.onclick=()=>removeEphemeralLabel(l);
    wrap.appendChild(tag);
  });
}
async function loadEphemeralCandidates(){
  const tb=$('eph-candidates');if(!tb) return;
  tb.innerHTML='<tr><td class="empty" colspan="6">Scanning…</td></tr>';
  try{
    const res=await fetch('/api/ephemeral_candidates');const d=await res.json();
    if(d.error){tb.innerHTML='<tr><td class="empty" colspan="6">'+esc(d.error)+'</td></tr>';return}
    const cand=d.candidates||[];
    if(!cand.length){tb.innerHTML='<tr><td class="empty" colspan="6">No candidates — no high-frequency telemetry-like labels found</td></tr>';return}
    tb.innerHTML=cand.map(c=>{
      return '<tr><td class="mono">'+esc(c.label)+'</td><td>'+c.count+'</td><td>'+(c.json_ratio*100).toFixed(0)+'%</td><td>'+c.score+'</td><td style="font-size:11px;max-width:200px;white-space:normal">'+esc(c.reasons)+'</td><td><button class="btn small primary" onclick="addEphemeralLabel(\\''+esc(c.label)+'\\')">Add</button></td></tr>';
    }).join('');
  }catch(e){tb.innerHTML='<tr><td class="empty" colspan="6">Failed: '+esc(e.message)+'</td></tr>'}
}
async function addEphemeralLabel(label){
  const l=label || $('eph-custom').value.trim();
  if(!l){showToast('Enter a label');return}
  const res=await fetch('/api/ephemeral_allowlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'add',label:l})});
  const d=await res.json();
  if(d.status==='success'){showToast('Added '+l+' to allowlist');$('eph-custom').value='';fetchStatus();renderEphemeralAllowlist({ephemeral_labels:d.ephemeral_labels});notifyManagerConfig(d.ephemeral_labels);}
  else showToast(d.message||'Add failed');
}
async function removeEphemeralLabel(label){
  if(!confirm('Remove '+label+' from allowlist? It will no longer be compacted.')) return;
  const res=await fetch('/api/ephemeral_allowlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'remove',label})});
  const d=await res.json();
  if(d.status==='success'){showToast('Removed '+label);fetchStatus();renderEphemeralAllowlist({ephemeral_labels:d.ephemeral_labels});notifyManagerConfig(d.ephemeral_labels);}
  else showToast(d.message||'Remove failed');
}
function notifyManagerConfig(labels){
  try{
    const msg={type:'asha-config-update', ephemeral_labels:labels};
    const gf=$('graph-frame'); if(gf && gf.contentWindow) gf.contentWindow.postMessage(msg,'*');
    const mf=$('manager-frame'); if(mf && mf.contentWindow) mf.contentWindow.postMessage(msg,'*');
  }catch(e){}
}
async function loadStatistics(){
  const setHtml=(id,html)=>{const el=$(id);if(el) el.innerHTML=html;};
  setHtml('stats-overview','Loading…');setHtml('stats-bloat','Loading…');
  try{
    const res=await fetch('/api/statistics');const s=await res.json();
    if(s.error){setHtml('stats-overview',esc(s.error));return}
    setHtml('stats-overview','Nodes: <b>'+s.total_nodes+'</b> (core '+s.core_nodes+' / agent '+s.agent_note_nodes+')<br>Edges: <b>'+s.total_edges+'</b><br>DB size: <b>'+s.db_size_mb+' MB</b><br>Trust avg: '+s.trust_avg+' · Imp avg: '+s.importance_avg);
    const b=s.bloat||{};
    setHtml('stats-bloat','Page: '+b.page_count+' × '+b.page_size+'<br>Total '+b.total_mb+' MB — free '+b.free_mb+' MB — used '+b.used_mb+' MB<br>Freelist '+b.freelist_count+' pages ('+b.freelist_pct+'%) — threshold '+b.vacuum_threshold_pct+'% / '+b.vacuum_min_pages+' pages'+(b.needs_vacuum?' <b style="color:#ea6">— VACUUM needed</b>':' — ok')+'<br>Contradicts: '+b.contradicts_total+' (ephemeral '+b.contradicts_ephemeral+')');
    const nt=s.node_types||{};setHtml('stats-nodetypes',Object.keys(nt).length?Object.entries(nt).map(([k,v])=>'<span class="chip">'+esc(k)+': '+v+'</span>').join(' '):'<span style="color:#555">none</span>');
    const et=s.edge_types||{};setHtml('stats-edgetypes',Object.keys(et).length?Object.entries(et).map(([k,v])=>'<span class="chip">'+esc(k)+': '+v+'</span>').join(' '):'<span style="color:#555">none</span>');
    const ly=s.layers||{};setHtml('stats-layers',Object.keys(ly).length?Object.entries(ly).map(([k,v])=>esc(k)+': <b>'+v+'</b>').join('<br>'):'<span style="color:#555">no layers</span>');
    const so=s.sources||{};setHtml('stats-sources',Object.keys(so).length?Object.entries(so).map(([k,v])=>esc(k||'(null)')+': <b>'+v+'</b>').join('<br>'):'<span style="color:#555">none</span>');
    const tb=$('stats-toplabels');if(tb){
      if(s.top_labels && s.top_labels.length){
        tb.innerHTML=s.top_labels.map(r=>'<tr><td class="mono">'+esc(r.label)+'</td><td>'+r.cnt+'</td></tr>').join('');
      } else tb.innerHTML='<tr><td class="empty" colspan="2">No labels</td></tr>';
    }
  }catch(e){setHtml('stats-overview','Failed: '+esc(e.message))}
}
async function loadContradictions(){
  const status=$('contra-status') ? $('contra-status').value : 'pending';
  const tb=$('contra-tbody');if(!tb) return;
  tb.innerHTML='<tr><td class="empty" colspan="6">Loading…</td></tr>';
  try{
    const res=await fetch('/api/contradictions?status='+encodeURIComponent(status)+'&limit=50');
    const d=await res.json();
    if(d.error){tb.innerHTML='<tr><td class="empty" colspan="6">'+esc(d.error)+'</td></tr>';return}
    const rows=d.contradictions||[];
    const counts=d.counts||{};
    $('contra-counts').textContent='pending '+ (counts.pending||0)+' · confirmed '+(counts.confirmed||0)+' · ignored '+(counts.ignored||0)+' · total '+(d.total||0);
    $('b-contra').textContent=counts.pending||0;
    if(!rows.length){tb.innerHTML='<tr><td class="empty" colspan="6">No '+esc(status||'all')+' contradictions</td></tr>';return}
    tb.innerHTML=rows.map(r=>{
      const conf = r.metadata && r.metadata.confidence!=null ? r.metadata.confidence.toFixed(2) : '-';
      const overlap = r.metadata && r.metadata.overlap_words ? r.metadata.overlap_words.join(', ') : '';
      const stat = r.metadata && r.metadata.status ? r.metadata.status : 'pending';
      const sug = r.suggested_action;
      const fromTrust = r.from_trust!=null ? r.from_trust.toFixed(2) : '-';
      const toTrust = r.to_trust!=null ? r.to_trust.toFixed(2) : '-';
      const sugChip = sug ? '<span class="chip" style="border-color:#2a5a3a;color:#5d8" title="Auto suggestion: trust gap '+fromTrust+' vs '+toTrust+'">suggest '+esc(sug.replace('keep_','keep '))+'</span>' : '';
      const keepFromCls = sug==='keep_from' ? 'btn small primary' : 'btn small';
      const keepToCls = sug==='keep_to' ? 'btn small primary' : 'btn small';
      return '<tr'+(sug?' style="background:rgba(52,211,153,0.06)"':'')+'><td><span class="chip">'+conf+'</span> '+sugChip+'</td>'
        +'<td title="'+esc(r.from_content)+'"><b>'+esc(r.from_label)+'</b> <span style="color:#666">('+esc(r.from_type)+' · trust '+fromTrust+')</span><br><span style="color:#888;font-size:11px">'+esc(r.from_content.slice(0,60))+'</span></td>'
        +'<td title="'+esc(r.to_content)+'"><b>'+esc(r.to_label)+'</b> <span style="color:#666">('+esc(r.to_type)+' · trust '+toTrust+')</span><br><span style="color:#888;font-size:11px">'+esc(r.to_content.slice(0,60))+'</span></td>'
        +'<td style="font-size:11px;max-width:120px;white-space:normal">'+esc(overlap)+'</td>'
        +'<td><span class="chip">'+esc(stat)+'</span></td>'
        +'<td style="white-space:nowrap"><button class="btn small" onclick="contraAction(\\''+r.edge_id+'\\',\\'confirm\\')">Confirm</button> <button class="btn small" onclick="contraAction(\\''+r.edge_id+'\\',\\'ignore\\')">Ignore</button> <button class="btn small danger" onclick="contraAction(\\''+r.edge_id+'\\',\\'delete\\')">Delete</button><br><button class="'+keepFromCls+'" style="margin-top:3px" onclick="contraAction(\\''+r.edge_id+'\\',\\'keep_from\\')">Keep From</button> <button class="'+keepToCls+'" style="margin-top:3px" onclick="contraAction(\\''+r.edge_id+'\\',\\'keep_to\\')">Keep To</button> <button class="btn small" style="margin-top:3px" onclick="contraAction(\\''+r.edge_id+'\\',\\'merge\\')">Merge</button></td></tr>';
    }).join('');
  }catch(e){tb.innerHTML='<tr><td class="empty" colspan="6">Failed: '+esc(e.message)+'</td></tr>'}
}
async function autoResolveDry(){
  const res=await fetch('/api/contradiction_auto_resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dry_run:true})});
  const d=await res.json();
  if(d.status==='success' && d.dry_run){
    const cand=d.candidates||[];
    showToast('Preview: '+cand.length+' auto-resolvable (trust gap)');
    if(cand.length) loadContradictions(); // highlight suggestions
  } else showToast(d.message||d.error||'Preview failed');
}
async function autoResolveExec(){
  if(!confirm('Auto-resolve all pending contradictions where trust gap <0.3 vs >0.8 ? Keeps high-trust node, deletes low-trust.')) return;
  const res=await fetch('/api/contradiction_auto_resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dry_run:false})});
  const d=await res.json();
  if(d.status==='success'){showToast('Auto-resolved '+ (d.resolved||0)+' / '+(d.candidates_total||0)); loadContradictions(); fetchStatus();}
  else showToast(d.message||d.error||'Auto-resolve failed');
}
async function contraAction(edge_id, action){
  if(action==='delete' && !confirm('Delete this contradiction edge?')) return;
  if((action==='keep_from'||action==='keep_to'||action==='merge') && !confirm('This will delete a node/merge content and lock DB briefly. Confirm '+action+'?')) return;
  const res=await fetch('/api/contradiction_action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({edge_id, action})});
  const d=await res.json();
  if(d.status==='success'){showToast(action+' ok');loadContradictions();fetchStatus();}
  else showToast(d.message||'Action failed');
}
async function fetchContradictionBadge(){
  try{const r=await fetch('/api/contradictions?status=pending&limit=1');const d=await r.json();$('b-contra').textContent=d.counts?d.counts.pending||0:(d.contradictions?d.contradictions.length:0);}catch(e){}
}
if($('contra-status')) $('contra-status').onchange=()=>loadContradictions();
// embedded graph/manager
async function loadGraphDB(){
  const f=$('graph-frame');if(!f) return;
  $('graph-status').textContent='Loading DB into Graph…';
  try{
    const res=await fetch('/api/db_bytes');if(!res.ok) throw new Error('DB fetch failed '+res.status);
    const buf=await res.arrayBuffer();
    // try postMessage to iframe (asha_graph listens for embedded load)
    let tries=0;const send=()=>{
      try{f.contentWindow.postMessage({type:'asha-load-db', buffer:buf}, '*');}catch(e){}
    };
    send();
    // Also store in sessionStorage via local hook: reload frame with autoload
    $('graph-status').textContent='DB sent ('+(buf.byteLength/1024).toFixed(1)+' KB) — rendering…';
    setTimeout(()=>{$('graph-status').textContent='DB loaded — if no graph appears click Reload View';},1800);
  }catch(e){$('graph-status').textContent='Failed: '+e.message;showToast('Graph load failed: '+e.message)}
}
async function loadManagerDB(){
  const f=$('manager-frame');if(!f) return;
  $('manager-status').textContent='Loading DB into Manager…';
  try{
    const res=await fetch('/api/db_bytes');if(!res.ok) throw new Error('DB fetch failed '+res.status);
    const buf=await res.arrayBuffer();
    f.contentWindow.postMessage({type:'asha-load-db', buffer:buf}, '*');
    $('manager-status').textContent='DB sent ('+(buf.byteLength/1024).toFixed(1)+' KB) — rendering…';
    setTimeout(()=>{$('manager-status').textContent='DB loaded — if no data appears click Reload View';},1800);
  }catch(e){$('manager-status').textContent='Failed: '+e.message;showToast('Manager load failed: '+e.message)}
}
function reloadGraphFrame(){$('graph-frame').src='/humantools/asha_graph.html?embedded=1';$('graph-status').textContent='Frame reloaded — click Load Active DB';}
function reloadManagerFrame(){$('manager-frame').src='/humantools/asha_manager.html?embedded=1';$('manager-status').textContent='Frame reloaded — click Load Active DB';}
function showToast(msg){
  const t=$('toast');t.textContent=msg;t.className='show';clearTimeout(t._t);t._t=setTimeout(()=>t.className='',3200);
}
fetchStatus();setInterval(fetchStatus,12000);
</script>
</body>
</html>
"""


def start_dashboard(port: int = 8500, brain_dir: Optional[str] = None):
    """Starts the Brain HTTP dashboard server on the given port."""
    global GLOBAL_SCHEDULER
    GLOBAL_SCHEDULER = BrainScheduler(brain_dir=brain_dir)

    server_address = ("", port)
    try:
        httpd = HTTPServer(server_address, BrainDashboardHandler)
        print(f"\n==================================================")
        print(f"[Brain] Asha Memory Brain Dashboard Running!")
        print(f"-> Access UI at: http://localhost:{port}")
        print(f"-> Target DB: {GLOBAL_SCHEDULER.engine.db_path}")
        print(f"==================================================\n")
        httpd.serve_forever()
    except OSError:
        # If port 8500 is in use, try port 8501
        start_dashboard(port=port + 1, brain_dir=brain_dir)


if __name__ == "__main__":
    start_dashboard(port=8500)
