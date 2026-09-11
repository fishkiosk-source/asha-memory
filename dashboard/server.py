"""dashboard.server — human UI (http.server, stdlib only).

Rework of v2 brain/brain_dashboard.py for the dual-DB engine. 12 tabs.
Contract (Idea.md rev8):
- Deleted (C4): /humantools/*, POST /api/switch_db, GET /api/db_bytes,
  binary POST /api/manager_commit, per-file agent_*.db discovery.
- Every remaining endpoint takes ?db=core|agents|all (defaults §13.9 matrix);
  Graph/Manager/nodes are single-DB + selector by design.
- Auth (C8): bind 127.0.0.1 default, CORS off, X-Api-Token header only
  (no ?token=, no localhost bypass when set); health/ping stay open.
- Paths jailed (safe_join + resolve + is_relative_to, reject ..).
- New: /api/graph, /api/nodes, /api/node_update|node_delete, /api/agents,
  /api/mailbox*, all dual-DB aware. Row writes commit under the call;
  snapshot only per §2.12 policy (no auto discover/rebuild per Apply, C15).

Run from the v3 root:  python -m dashboard.server --port 8500
"""

import argparse
import html as _html
import json
import re as _re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.engine import DEFAULTS as BRAIN_DEFAULTS, BrainEngine
from brain.scheduler import BrainScheduler
from src.agents import bridge as agent_bridge
from src.agents import mailbox as agent_mailbox
from src.agents import store as agent_store
from src.core import edges as core_edges
from src.core import nodes as core_nodes

HOST = "127.0.0.1"
PORT = 8500
STATIC_DIR = Path(__file__).resolve().parent / "static"
WIKI_DIR = Path(__file__).resolve().parent.parent / "wiki"

_ENGINE: Optional[BrainEngine] = None
_SCHEDULER: Optional[BrainScheduler] = None


def configure(engine: BrainEngine, scheduler: Optional[BrainScheduler] = None) -> None:
    global _ENGINE, _SCHEDULER
    _ENGINE = engine
    _SCHEDULER = scheduler or BrainScheduler(engine=engine)
    # Auto-restore scheduler if it was left enabled (cron_enabled persisted in brain/config.json).
    # This makes dashboard restarts remember the toggle — no manual re-enable needed.
    try:
        if _ENGINE.config.get("cron_enabled"):
            if not _SCHEDULER.running:
                _SCHEDULER.start()
    except Exception:
        pass


def safe_join(root, *parts: str) -> Path:
    """Join untrusted path parts under root; raise ValueError on escape.

    REAL (Phase 1). Used by every file-serving endpoint (C8).
    """
    root = Path(root).resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes jail: {parts!r}")
    return target


def _wiki_md_to_html(md: str, rel: str) -> str:
    """Tiny markdown → HTML (stdlib only, for wiki). Covers headers, tables,
    lists, code fences, blockquote, hr, and inline **bold/*italic/`code`/[link](url)."""
    def esc(s: str) -> str:
        return _html.escape(s)
    def inline(s: str) -> str:
        # code first (protect)
        parts = []
        def save(m):
            parts.append(m.group(1))
            return f"\x00{len(parts)-1}\x00"
        s = _re.sub(r"`([^`]+)`", save, s)
        s = esc(s)
        # restore code
        for i, p in enumerate(parts):
            s = s.replace(f"\x00{i}\x00", f"<code>{esc(p)}</code>")
        s = _re.sub(r"\*\*([^\*]+)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"\*([^\*]+)\*", r"<em>\1</em>", s)
        s = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: f'<a href="{esc(m.group(2))}">{m.group(1)}</a>', s)
        return s
    lines = md.split("\n")
    out: List[str] = []
    i = 0
    # collect headers for toc
    headers: List[tuple] = []
    in_code = False
    code_lang = ""
    code_buf: List[str] = []
    in_table = False
    table_head = False
    list_stack: List[str] = []
    def close_lists():
        while list_stack:
            out.append(f"</{list_stack.pop()}>")
    while i < len(lines):
        line = lines[i]
        # code fence
        if line.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = line[3:].strip()
                code_buf = []
            else:
                in_code = False
                out.append(f'<pre><code class="language-{esc(code_lang)}">{esc(chr(10).join(code_buf))}</code></pre>')
                code_buf = []
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        stripped = line.strip()
        if not stripped:
            close_lists()
            if in_table:
                out.append("</table>")
                in_table = False
                table_head = False
            i += 1
            continue
        # hr
        if _re.match(r"^-{3,}$", stripped) or _re.match(r"^\*{3,}$", stripped):
            close_lists()
            out.append("<hr>")
            i += 1
            continue
        # header
        m = _re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            close_lists()
            lvl = len(m.group(1))
            text = m.group(2).strip()
            hid = _re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            headers.append((lvl, text, hid))
            out.append(f"<h{lvl} id=\"{esc(hid)}\">{inline(text)}</h{lvl}>")
            i += 1
            continue
        # blockquote
        if line.startswith(">"):
            close_lists()
            out.append(f"<blockquote>{inline(line[1:].strip())}</blockquote>")
            i += 1
            continue
        # table: | ... | line and next line is |---|
        if "|" in line and i + 1 < len(lines) and _re.match(r"^\s*\|?[\s\-|:]*\|", lines[i + 1]):
            if not in_table:
                out.append("<table>")
                in_table = True
                table_head = True
            cells = [inline(c.strip()) for c in line.strip().strip("|").split("|")]
            tag = "th" if table_head else "td"
            out.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
            if table_head:
                table_head = False
            i += 1
            # skip the separator line on next iter if it's the --- line
            if i < len(lines) and _re.match(r"^\s*\|?[\s\-|:]*\|", lines[i]):
                i += 1
            continue
        if in_table:
            # non-table line ends table
            out.append("</table>")
            in_table = False
            table_head = False
            continue
        # lists
        ml = _re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)", line)
        if ml:
            indent = len(ml.group(1))
            marker = ml.group(2)
            content = ml.group(3)
            want = "ol" if _re.match(r"\d+\.", marker) else "ul"
            if not list_stack or list_stack[-1] != want:
                close_lists()
                out.append(f"<{want}>")
                list_stack.append(want)
            out.append(f"<li>{inline(content)}</li>")
            i += 1
            continue
        # paragraph
        close_lists()
        # collect consecutive non-blank non-special lines as one paragraph
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith("#") and not lines[i].startswith("```") and not lines[i].strip().startswith(">") and "|" not in lines[i] and not _re.match(r"^(\s*)([-*]|\d+\.)\s+", lines[i]) and not _re.match(r"^-{3,}$", lines[i].strip()):
            para.append(lines[i])
            i += 1
        out.append(f"<p>{inline(' '.join(p.strip() for p in para))}</p>")
    close_lists()
    if in_table:
        out.append("</table>")
    if in_code:
        out.append(f'<pre><code>{esc(chr(10).join(code_buf))}</code></pre>')
    body = "\n".join(out)
    # prepend toc if headers present
    if headers:
        toc = '<div class="toc"><b>On this page</b>' + "".join(
            f'<a href="#{_html.escape(hid)}" style="margin-left:{(lvl-1)*12}px">{_html.escape(text)}</a>' for lvl, text, hid in headers
        ) + "</div>"
        body = toc + body
    return body


def _engine() -> BrainEngine:
    assert _ENGINE is not None, "dashboard not configured"
    return _ENGINE


def _default_brain_dir() -> Path:
    """Repo brain/ dir — the SINGLE home of config/logs/snapshots/history.

    (Was Path(__file__).parent i.e. dashboard/ until 2026-09-05: every launch
    then rooted its engine at dashboard/, creating dashboard/config.json with
    defaults and splitting logs/snapshots/history away from brain/.)
    """
    return Path(__file__).resolve().parent.parent / "brain"


def _scheduler() -> BrainScheduler:
    assert _SCHEDULER is not None, "dashboard not configured"
    return _SCHEDULER


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AshaMemoryV3/3.0"

    def log_message(self, format, *args):
        pass

    # ── Auth (C8) ──

    def _check_auth(self) -> bool:
        try:
            token = _engine().config.get("dashboard_token") or ""
        except AssertionError:
            return True
        if not token:
            return True
        if self.headers.get("X-Api-Token") == token:
            return True
        if self.path.startswith("/api/health") or self.path.startswith("/api/ping"):
            return True
        return False

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized: missing X-Api-Token"}')

    # ── IO helpers ──

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> Dict[str, List[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _q(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self._query().get(name, [default])[0]

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _db(self, default: str = "all") -> str:
        db = (self._q("db", default) or default).lower()
        return db if db in ("core", "agents", "all") else default

    def _single_db(self, default: str = "core") -> str:
        db = (self._q("db", default) or default).lower()
        return db if db in ("core", "agents") else default

    # ── GET ──

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html") or path.startswith("/static/") or path.startswith("/wiki"):
            pass  # public: shell, assets, and wiki docs (no token, for AI and humans)
        elif not self._check_auth():
            self._unauthorized()
            return
        try:
            if path in ("/", "/index.html"):
                self._serve_file(STATIC_DIR / "dashboard.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/wiki" or path == "/wiki/":
                self._serve_wiki("README.md")
            elif path.startswith("/wiki/"):
                self._serve_wiki(path[len("/wiki/"):])
            elif path in ("/api/health", "/api/ping"):
                self._api_health(path == "/api/ping")
            elif path == "/api/status":
                self._api_status()
            elif path == "/api/config":
                self._send_json({"config": _engine().config, "defaults": BRAIN_DEFAULTS})
            elif path == "/api/snapshots":
                self._send_json({"snapshots": _engine().list_snapshots(
                    None if self._db() == "all" else self._db())})
            elif path == "/api/history":
                try:
                    limit = int(self._q("limit", "20") or 20)
                except ValueError:
                    limit = 20
                self._send_json({"history": _scheduler().get_history(limit)})
            elif path == "/api/logs":
                self._send_json({"logs": _engine().list_logs()})
            elif path == "/api/log_content":
                content = _engine().log_content(self._q("file", "") or "")
                if content is None:
                    self._send_json({"error": "File not found"}, status=404)
                else:
                    self._send_json({"filename": self._q("file"), "content": content})
            elif path == "/api/bloat":
                self._send_json(_engine().get_bloat_metrics(self._db()))
            elif path == "/api/ephemeral_candidates":
                try:
                    mc = int(self._q("min_count", "3") or 3)
                except ValueError:
                    mc = 3
                res = _engine().discover_ephemeral_candidates(min_count=mc)
                db = self._db()
                if db != "all":
                    res = {"candidates": [c for c in res["candidates"] if c["db"] == db],
                           "allowlist": res.get("allowlist", res.get("ephemeral_labels", [])),
                           "ephemeral_labels": res.get("ephemeral_labels", res.get("allowlist", [])),
                           "ignored_labels": res.get("ignored_labels", [])}
                self._send_json(res)
            elif path == "/api/ephemeral_stats":
                self._send_json(_engine().get_ephemeral_stats(self._db()))
            elif path == "/api/statistics":
                self._send_json(_engine().get_full_statistics(self._db()))
            elif path == "/api/contradictions":
                try:
                    limit = int(self._q("limit", "50") or 50)
                except ValueError:
                    limit = 50
                db = (self._q("db", "core") or "core").lower()
                self._send_json(_engine().get_contradictions(
                    status=self._q("status"), limit=limit,
                    db=db if db in ("core", "agents") else "core"))
            elif path == "/api/graduate_preview":
                self._api_graduate_preview()
            elif path == "/api/agent_working_preview":
                self._send_json(_engine().get_agent_working_preview())
            elif path == "/api/core_helper_preview":
                self._send_json(_engine().get_core_helper_preview())
            elif path == "/api/graph":
                self._api_graph()
            elif path == "/api/nodes":
                self._api_nodes()
            elif path == "/api/edges":
                self._api_edges()
            elif path == "/api/agents":
                conn = _engine().agents_conn()
                try:
                    self._send_json({"agents": agent_store.agent_list(conn)})
                finally:
                    conn.close()
            elif path == "/api/empty_agents":
                min_age = int(_engine().config.get("prune_empty_agents_min_age_hours", 24))
                self._send_json(_engine().get_empty_agents(min_age_hours=min_age))
            elif path == "/api/mailbox":
                self._api_mailbox_read()
            elif path == "/api/manager_health":
                self._api_manager_health()
            elif path == "/api/vectors":
                self._api_vectors()
            elif path == "/api/layers":
                self._api_layers()
            elif path == "/api/path":
                self._api_path()
            elif path == "/api/schema":
                self._api_schema()
            elif path == "/api/recall":
                self._api_recall()
            else:
                self.send_error(404, "Not Found")
        except AssertionError as e:
            self._send_json({"error": str(e)}, status=500)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def do_HEAD(self):
        if not self._check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/health", "/api/ping"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
        else:
            self.send_error(404, "Not Found")

    def _serve_file(self, path: Path, ctype: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel: str) -> None:
        try:
            target = safe_join(STATIC_DIR, *rel.split("/"))
        except ValueError:
            self.send_error(404, "Not Found")
            return
        if not target.is_file() or target.suffix not in (
                ".html", ".js", ".css", ".json", ".png", ".svg"):
            self.send_error(404, "Not Found")
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                 ".css": "text/css", ".json": "application/json",
                 ".png": "image/png", ".svg": "image/svg+xml"}[target.suffix]
        self._serve_file(target, ctype)

    def _serve_wiki(self, rel: str) -> None:
        # public wiki: md files from WIKI_DIR rendered with dashboard style
        rel = (rel or "").strip().lstrip("/")
        if not rel:
            rel = "README.md"
        # allow /wiki/ARCHITECTURE without .md
        if "/" not in rel and "." not in rel:
            rel = rel + ".md"
        if rel.endswith("/"):
            rel = rel + "README.md"
        try:
            target = safe_join(WIKI_DIR, *rel.split("/"))
        except ValueError:
            self.send_error(404, "Not Found")
            return
        if target.is_dir():
            target = target / "README.md"
        if not target.is_file():
            # try adding .md
            alt = WIKI_DIR / (rel + ".md")
            try:
                alt = safe_join(WIKI_DIR, *(rel + ".md").split("/"))
            except ValueError:
                alt = None
            if alt and alt.is_file():
                target = alt
            else:
                self.send_error(404, "Not Found")
                return
        if target.suffix.lower() != ".md":
            # serve non-md assets from wiki/ (e.g. wiki/logo/*.png) with correct type
            ext = target.suffix.lower()
            ctype = {".png": "image/png", ".svg": "image/svg+xml",
                     ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".css": "text/css", ".js": "application/javascript",
                     ".json": "application/json", ".html": "text/html; charset=utf-8",
                     ".txt": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
            # allow subfolders like wiki/logo/*.png (safe_join already jailed to WIKI_DIR)
            self._serve_file(target, ctype)
            return
        try:
            md = target.read_text(encoding="utf-8")
        except OSError:
            self.send_error(404, "Not Found")
            return
        # list wiki files for sidebar
        try:
            files = sorted([p.name for p in WIKI_DIR.glob("*.md")])
        except OSError:
            files = ["README.md"]
        html = _wiki_md_to_html(md, rel)
        sidebar = "\n".join(
            f'<a href="/wiki/{_html.escape(f)}" class="{"active" if f==target.name else ""}">{_html.escape(f.replace(".md",""))}</a>'
            for f in files
        )
        # also add raw link for AI
        raw_link = f"/wiki/{_html.escape(target.name)}"
        page = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wiki — {_html.escape(target.name)}</title>
<style>
:root{{color-scheme:dark}}
body{{background:#0d1117;color:#c9d1d9;font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:0 16px 40px;max-width:1440px;margin-inline:auto}}
header{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:14px 0 12px;border-bottom:1px solid #30363d;position:sticky;top:0;z-index:20;background:#0d1117}}
h1{{letter-spacing:-.02em}}
.pill{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:3px 10px;font-size:12px;display:inline-flex;align-items:center;gap:6px}}
#wiki-search{{width:100%;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:8px 10px;font-size:13px;transition:.15s}}
#wiki-search:focus{{outline:none;border-color:#58a6ff;box-shadow:0 0 0 3px rgba(88,166,255,.15)}}
a{{color:#58a6ff;text-decoration:none}}a:hover{{text-decoration:underline}}
.layout{{display:grid;grid-template-columns:260px 1fr;gap:16px;min-height:calc(100vh - 80px);margin-top:16px}}
@media(max-width:900px){{.layout{{grid-template-columns:1fr}}.sidebar{{display:none}}}}
.sidebar{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;overflow:auto;position:sticky;top:70px;max-height:calc(100vh - 80px);box-shadow:0 1px 0 rgba(0,0,0,.2)}}
.sidebar a{{display:block;padding:6px 8px;border-radius:6px;color:#8b949e;font-size:13px;margin:1px 0;border:1px solid transparent}}
.sidebar a:hover{{background:#21262d;color:#e6edf3;text-decoration:none;border-color:#30363d}}
.sidebar a.active{{background:#1f6feb;color:#fff;border-color:#1f6feb;box-shadow:0 1px 8px rgba(31,111,235,.35)}}
.sidebar a.hl{{background:#3b2e12;color:#ffce4d;border-color:#5a3d0a}}
.content{{padding:0;max-width:860px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;margin:0 0 14px;box-shadow:0 1px 0 rgba(0,0,0,.2)}}
.content .card{{padding:20px 24px}}
.content h1{{font-size:28px;border-bottom:1px solid #21262d;padding-bottom:8px;margin-top:0;color:#e6edf3}}
.content h2{{font-size:20px;margin-top:24px;border-bottom:1px solid #21262d;padding-bottom:6px;color:#e6edf3}}
.content h3{{font-size:16px;margin-top:18px;color:#e6edf3}}
.content pre{{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px;overflow:auto;font-size:13px;line-height:1.5}}
.content code{{background:#21262d;padding:1px 4px;border-radius:4px;font-size:13px}}
.content pre code{{background:transparent;padding:0}}
.content table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}}
.content th,.content td{{border:1px solid #30363d;padding:6px 9px;text-align:left;vertical-align:top}}
.content th{{background:#1c2128;font-weight:600;letter-spacing:.01em}}
.content blockquote{{border-left:3px solid #58a6ff;margin:12px 0;padding:8px 12px;color:#8b949e;background:#161b22;border-radius:4px}}
.content hr{{border:none;border-top:1px solid #21262d;margin:16px 0}}
.muted{{color:#8b949e;font-size:12px}}
.toc{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 12px;margin:12px 0;font-size:13px}}
.toc a{{display:block;padding:4px 8px;border-radius:4px}} .toc a:hover{{background:#21262d}}
mark{{background:#e3b341;color:#0d1117;padding:1px 3px;border-radius:3px;font-weight:600}}
.btn{{background:#238636;color:#fff;border:none;border-radius:6px;padding:7px 14px;cursor:pointer;margin:2px;font-weight:500;transition:.15s}}
.btn:hover{{filter:brightness(1.08)}} .btn.ghost{{background:#21262d;border:1px solid #30363d}} .btn.ghost:hover{{background:#30363d}}
.tag{{display:inline-block;border-radius:9px;font-size:11px;padding:2px 9px;margin:1px;font-weight:600;letter-spacing:.01em}}
.tag.green{{background:#12361f;color:#7ee787;border:1px solid #1f4a2a}} .tag.blue{{background:#0f2a4d;color:#79c0ff;border:1px solid #1a3d6b}} .tag.amber{{background:#3b2300;color:#ffce4d;border:1px solid #5a3d0a}} .tag.grey{{background:#21262d;color:#c9d1d9;border:1px solid #30363d}}
</style>
</head><body>
<header>
<h1 style="margin:0;font-size:20px;display:flex;align-items:center;gap:10px"><img src="/wiki/logo/AshaMemorySMALL.png" style="height:24px"><a href="/wiki/README.md" style="color:#e6edf3;text-decoration:none">ASHA Wiki</a> <span class="tag grey" style="font-size:11px">v3</span></h1>
<span class="pill">public <span class="muted" style="margin-left:6px">no token</span></span>
<span class="pill">AI: <code style="background:transparent;padding:0">?raw=1</code></span>
<span style="margin-left:auto;display:flex;gap:8px"><a class="btn ghost" href="/">Dashboard</a><a class="btn ghost" href="{raw_link}?raw=1">Raw md</a></span>
</header>
<div class="layout">
<nav class="sidebar">
<div style="margin-bottom:10px"><input id="wiki-search" placeholder="Search wiki… ( / to focus )" autocomplete="off"><div id="wiki-search-hits" class="muted" style="font-size:11px;margin-top:6px"></div><div id="wiki-search-results" style="margin-top:8px;display:none"></div></div>
<div class="muted" style="margin:10px 0 6px;font-size:11px;letter-spacing:.04em;text-transform:uppercase">Pages · {len(files)}</div>{sidebar}
<div style="margin-top:14px;padding-top:10px;border-top:1px solid #21262d" class="muted">Served from <code>wiki/</code> · <span style="color:#79c0ff">{len(files)} files</span></div>
</nav>
<main class="content"><div class="card">{html}</div>
</div>
<script>
(function(){{
  const input=document.getElementById('wiki-search');
  const hits=document.getElementById('wiki-search-hits');
  const results=document.getElementById('wiki-search-results');
  const sidebarLinks=[...document.querySelectorAll('.sidebar a')];
  const contentEl=document.querySelector('.content .card');
  const origHTML=contentEl?contentEl.innerHTML:"";
  let index=[]; let built=false;
  async function build(){{ if(built) return; built=true;
    const files=sidebarLinks.map(a=>a.getAttribute('href').replace('/wiki/',''));
    for(let f of files){{
      try{{
        const r=await fetch('/wiki/'+encodeURIComponent(f)+'?raw=1');
        if(!r.ok) continue;
        const txt=await r.text();
        index.push({{file:f, name:f.replace('.md',''), content:txt, lower:txt.toLowerCase()}});
      }}catch(e){{}}
    }}
  }}
  build();
  function clearMarks(){{ if(!contentEl) return; contentEl.innerHTML=origHTML; }}
  function highlight(q){{ if(!contentEl||!q) return;
    const walker=document.createTreeWalker(contentEl, NodeFilter.SHOW_TEXT, null);
    const toMark=[]; let node; const re=new RegExp('('+q.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\$&')+')','gi');
    while(node=walker.nextNode()){{
      if(node.parentElement.closest('pre,code')) continue;
      re.lastIndex=0; if(re.test(node.nodeValue)) toMark.push(node);
    }}
    toMark.forEach(n=>{{
      const re2=new RegExp('('+q.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\$&')+')','gi');
      const span=document.createElement('span');
      span.innerHTML=n.nodeValue.replace(re2,'<mark>$1</mark>');
      n.parentNode.replaceChild(span,n);
    }});
  }}
  input.addEventListener('input', async ()=>{{
    const q=input.value.trim(); const ql=q.toLowerCase();
    if(!q){{ sidebarLinks.forEach(a=>{{a.style.display=''; a.classList.remove('hl');}}); hits.textContent=''; results.style.display='none'; results.innerHTML=''; clearMarks(); return; }}
    await build();
    let matched=0;
    sidebarLinks.forEach(a=>{{
      const href=a.getAttribute('href').replace('/wiki/','');
      const name=a.textContent.toLowerCase();
      const entry=index.find(e=>e.file===href);
      const inContent=entry&&entry.lower.includes(ql);
      const inName=name.includes(ql)||href.toLowerCase().includes(ql);
      const show=inName||inContent;
      a.style.display=show?'':'none';
      a.classList.toggle('hl', !!(show&&inContent&&!inName));
      if(show) matched++;
    }});
    hits.textContent=matched? matched+' pages matched — Enter to highlight / Esc to clear' : 'No pages matched';
    let snippets=[];
    if(ql.length>=2){{
      index.forEach(e=>{{
        const pos=e.lower.indexOf(ql);
        if(pos!==-1){{ 
          if(e.file===location.pathname.split('/').pop()) return;
          const start=Math.max(0,pos-40);
          const snip=e.content.slice(start,pos+ql.length+60).replace(/\\n/g,' ').slice(0,140);
          const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
          snippets.push(`<a href="/wiki/${{e.file}}" style="display:block;padding:6px 8px;margin:4px 0;background:#0d1117;border:1px solid #21262d;border-radius:6px;text-decoration:none"><div style="font-weight:600;color:#58a6ff;font-size:12px">${{esc(e.name)}}</div><div class="muted" style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${{esc(snip)}}</div></a>`);
        }}
      }});
      if(snippets.length){{ results.innerHTML=`<div style="margin-top:6px"><div class="muted" style="font-size:11px;margin-bottom:4px">Matches in other pages</div>${{snippets.slice(0,5).join('')}}${{snippets.length>5?'<div class="muted" style="font-size:11px">+'+(snippets.length-5)+' more</div>':''}}</div>`; results.style.display='block'; }} else {{ results.style.display='none'; }}
    }} else {{ results.style.display='none'; }}
    clearMarks(); if(ql.length>=2) highlight(q);
  }});
  input.addEventListener('keydown', e=>{{
    if(e.key==='Enter'){{ e.preventDefault(); const m=document.querySelector('mark'); if(m) m.scrollIntoView({{behavior:'smooth',block:'center'}}); }}
    if(e.key==='Escape'){{ input.value=''; input.dispatchEvent(new Event('input')); }}
  }});
  document.addEventListener('keydown', e=>{{
    if(e.key==='/' && document.activeElement!==input && !e.ctrlKey && !e.metaKey){{ e.preventDefault(); input.focus(); }}
  }});
}})();
</script>
</body></html>"""
        # support ?raw=1 for AI raw fetch
        if "raw=1" in (self.headers.get("Referer") or "") or "raw=1" in self.path:
            # also check query
            qs = urllib.parse.urlparse(self.path).query
            if "raw=1" in qs:
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                data = md.encode("utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _api_health(self, is_ping: bool) -> None:
        import time as _t
        if is_ping:
            self._send_json({"pong": True})
            return
        eng = _engine()
        self._send_json({"running": True, "status": "ok",
                         "timestamp": int(_t.time()),
                         "core_db": str(eng.core_path),
                         "agents_db": str(eng.agents_path),
                         "scheduler_running": _SCHEDULER.running if _SCHEDULER else False,
                         "port": self.server.server_address[1]})

    def _api_status(self) -> None:
        eng, sched = _engine(), _scheduler()
        db = self._db()
        health = eng.health(db)
        bloat = eng.get_bloat_metrics(db)
        self._send_json({
            "health": health, "bloat": bloat,
            "scheduler": {"running": sched.running,
                          "interval_minutes": eng.config.get("interval_minutes", 60),
                          "cron_enabled": eng.config.get("cron_enabled", False)},
            "snapshots": eng.list_snapshots(None if db == "all" else db)[:10],
            "history": sched.get_history(5),
            "logs": eng.list_logs(5),
            "auto_snapshot": bool(eng.config.get("auto_snapshot_before_jobs", False)),
        })

    def _api_graduate_preview(self) -> None:
        try:
            limit = int(self._q("limit", "100") or 100)
        except ValueError:
            limit = 100
        conn = _engine().agents_conn()
        try:
            stats = agent_bridge.review_stats(conn)
            notes = agent_bridge.list_review_queue(conn, limit)
            preview = [{"node_id": n["node_id"], "agent_id": n.get("agent_id"),
                        "label": n.get("label"), "content": (n.get("content") or "")[:200],
                        "trust_level": n.get("trust_level"),
                        "importance": n.get("importance"),
                        "attention": (n.get("metadata") or {}).get("attention_state"),
                        "updated_at": n.get("updated_at")} for n in notes]
            # graduable == review_ready only (importance ignored per 2026-09-07)
            grad_preview = preview[:50]
            self._send_json({"total_agent_notes": stats["total"],
                             "review_ready": stats["review_ready"],
                             "agent_private": stats["private"],
                             "graduable": stats["graduable"],
                             "total": len(preview), "notes": preview,
                             "graduable_preview": grad_preview[:50]})
        finally:
            conn.close()

    def _api_graph(self) -> None:
        db = self._single_db()
        try:
            limit = int(self._q("limit", "300") or 300)
        except ValueError:
            limit = 300
        limit = max(10, min(limit, 2000))
        eng = _engine()
        conn = eng.core_conn() if db == "core" else eng.agents_conn()
        try:
            nodes = conn.execute(
                "SELECT n.node_id, n.node_type, n.label, n.trust_level, n.importance,"
                " COALESCE(l.layer, 'working') AS layer FROM nodes n"
                " LEFT JOIN memory_layers l ON n.node_id = l.node_id"
                " ORDER BY n.updated_at DESC LIMIT ?", (limit,)).fetchall()
            ids = [r["node_id"] for r in nodes]
            edges: List[Dict[str, Any]] = []
            truncated = False
            if ids:
                ph = ",".join("?" * len(ids))
                erows = conn.execute(
                    f"SELECT edge_id, from_node, to_node, edge_type, weight FROM edges"
                    f" WHERE from_node IN ({ph}) AND to_node IN ({ph}) LIMIT ?",
                    (*ids, *ids, limit * 3)).fetchall()
                edges = [dict(r) for r in erows]
                truncated = len(erows) >= limit * 3 or len(nodes) >= limit
            self._send_json({"db": db, "limit": limit, "truncated": truncated,
                             "nodes": [dict(r) for r in nodes], "edges": edges})
        finally:
            conn.close()

    def _api_nodes(self) -> None:
        raw_agent = (self._q("agent", "") or "").strip() or (self._q("agent_id", "") or "").strip()
        # LogicEngine infer: ?agent=AshaWeb without ?db= -> agents.db (auto_create=False read)
        inferred = _agent_db_infer(self, raw_agent, self._single_db())
        db = inferred
        # canonicalize agent hint for filtering (exact)
        agent = _resolve_agent_param(raw_agent, auto_create=False) or raw_agent
        try:
            limit = int(self._q("limit", "50") or 50)
        except ValueError:
            limit = 50
        try:
            offset = int(self._q("offset", "0") or 0)
        except ValueError:
            offset = 0
        limit = max(1, min(limit, 200))
        q = (self._q("q", "") or "").strip()
        ntype = (self._q("type", "") or "").strip().upper()
        scope = (self._q("scope", "") or "").strip().upper()
        attention = (self._q("attention", "") or "").strip()
        layer = (self._q("layer", "") or "").strip().lower()
        source = (self._q("source", "") or "").strip()
        sort_key = (self._q("sort", "created_at") or "created_at").strip()
        sort_dir = (self._q("dir", "desc") or "desc").strip().lower()
        # allowlist sort keys to prevent injection
        sort_map = {
            "node_id": "n.node_id", "label": "n.label", "content": "n.content",
            "node_type": "n.node_type", "trust_level": "n.trust_level",
            "importance": "n.importance", "access_count": "n.access_count",
            "created_at": "n.created_at", "updated_at": "n.updated_at",
            "source": "n.source", "layer": "COALESCE(l.layer,'working')",
        }
        order_col = sort_map.get(sort_key, "n.created_at")
        order_dir = "DESC" if sort_dir in ("desc", "-1", "1") and sort_key != "node_id" else "ASC"
        # legacy: dir=-1 means desc for numeric; we normalize to ASC/DESC via param
        # support both ?dir=desc/asc and ?dir=-1/1
        if sort_dir in ("-1", "desc"):
            order_dir = "DESC"
        elif sort_dir in ("1", "asc"):
            order_dir = "ASC"
        eng = _engine()
        conn = eng.core_conn() if db == "core" else eng.agents_conn()
        try:
            clauses, params = [], []
            joins = " LEFT JOIN memory_layers l ON n.node_id = l.node_id"
            if q:
                clauses.append("(n.label LIKE ? OR n.content LIKE ? OR n.node_id = ?)")
                params += [f"%{q}%", f"%{q}%", q]
            if ntype:
                clauses.append("n.node_type = ?")
                params.append(ntype)
            if source:
                clauses.append("n.source = ?")
                params.append(source)
            if layer:
                clauses.append("COALESCE(l.layer,'working') = ?")
                params.append(layer)
            if attention:
                if attention == "__none":
                    clauses.append("(json_extract(n.metadata,'$.attention_state') IS NULL OR json_extract(n.metadata,'$.attention_state')='')")
                else:
                    clauses.append("json_extract(n.metadata,'$.attention_state') = ?")
                    params.append(attention)
            if agent:
                if db == "agents":
                    clauses.append("(n.agent_id = ? OR json_extract(n.metadata,'$.agent_id') = ? OR json_extract(n.metadata,'$.agent_ids') LIKE ?)")
                    params += [agent, agent, f"%{agent}%"]
                else:
                    clauses.append("(json_extract(n.metadata,'$.agent_id') = ? OR json_extract(n.metadata,'$.agent_ids') LIKE ?)")
                    params += [agent, f"%{agent}%"]
            if scope:
                if scope == "CORE":
                    clauses.append("((json_extract(n.metadata,'$.attention_state') = 'core_verified') OR (n.node_type != 'AGENT_NOTE' AND (json_extract(n.metadata,'$.agent_scoped') IS NULL OR json_extract(n.metadata,'$.agent_scoped') != 1)))")
                elif scope == "AGENT":
                    clauses.append("((json_extract(n.metadata,'$.attention_state') IS NULL OR json_extract(n.metadata,'$.attention_state') != 'core_verified') AND (n.node_type = 'AGENT_NOTE' OR json_extract(n.metadata,'$.agent_scoped') = 1 OR json_extract(n.metadata,'$.agent_id') IS NOT NULL))")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            total = conn.execute(f"SELECT COUNT(*) FROM nodes n{joins} {where}",
                                 tuple(params)).fetchone()[0]
            rows = conn.execute(
                f"SELECT n.* FROM nodes n{joins} {where}"
                f" ORDER BY {order_col} {order_dir}, n.node_id ASC LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
            out = []
            for r in rows:
                d = core_nodes.row_to_dict(r)
                d["layer"] = conn.execute(
                    "SELECT layer FROM memory_layers WHERE node_id = ?",
                    (r["node_id"],)).fetchone()
                d["layer"] = d["layer"]["layer"] if d["layer"] else "working"
                d["last_access"] = conn.execute(
                    "SELECT MAX(accessed_at) FROM access_log WHERE node_id = ?",
                    (r["node_id"],)).fetchone()[0]
                # enrich for table: scope, attention, agent
                try:
                    m = json.loads(r["metadata"] or "{}")
                except Exception:
                    m = {}
                att = m.get("attention_state") or ""
                # scope logic mirrors v2
                if m.get("attention_state") == "core_verified":
                    sc = "CORE"
                elif r["node_type"] == "AGENT_NOTE" or m.get("agent_scoped"):
                    sc = "AGENT"
                else:
                    # agents.db fallback: if has agent_id column, it's AGENT
                    sc = "AGENT" if (db == "agents" and r["agent_id"] if "agent_id" in r.keys() else None) else "CORE"
                d["_scope"] = sc
                d["_attention"] = att
                agent_id_val = m.get("agent_id") or ""
                if not agent_id_val and m.get("agent_ids"):
                    agent_id_val = "+".join(m["agent_ids"])
                elif db == "agents" and "agent_id" in r.keys() and r["agent_id"]:
                    agent_id_val = r["agent_id"]
                d["_agent"] = agent_id_val
                out.append(d)
            # distinct values for filters (for manager dropdowns)
            # only when no pagination offset? compute always but cap
            self._send_json({"db": db, "total": total, "offset": offset,
                             "limit": limit, "nodes": out})
        finally:
            conn.close()

    def _api_manager_health(self) -> None:
        db = self._single_db()
        eng = _engine()
        conn = eng.core_conn() if db == "core" else eng.agents_conn()
        try:
            total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            ids = {r[0] for r in conn.execute("SELECT node_id FROM nodes")}
            orphans = []
            for r in conn.execute("SELECT edge_id, from_node, to_node, edge_type FROM edges"):
                if r["from_node"] not in ids or r["to_node"] not in ids:
                    orphans.append(dict(r))
            # dup labels
            dupes = []
            for r in conn.execute("SELECT label, COUNT(*) as c FROM nodes GROUP BY label HAVING c > 1"):
                for rr in conn.execute("SELECT node_id, label FROM nodes WHERE label = ?", (r["label"],)):
                    dupes.append(dict(rr))
            # isolated = degree 0
            deg = {nid: 0 for nid in ids}
            for r in conn.execute("SELECT from_node, to_node FROM edges"):
                if r["from_node"] in deg:
                    deg[r["from_node"]] += 1
                if r["to_node"] in deg:
                    deg[r["to_node"]] += 1
            isolated = [nid for nid, d in deg.items() if d == 0]
            # filtered ephemeral: only labels in Ephemeral list (both tables, unified)
            try:
                allow = set(_engine().config.get("ephemeral_labels", []))
                if allow:
                    ph = ",".join("?" * len(allow))
                    eph_e = conn.execute(f"SELECT COUNT(*) FROM ephemeral_events WHERE label IN ({ph})", (*allow,)).fetchone()[0] if self._has_table(conn, "ephemeral_events") else 0
                    eph_n = conn.execute(f"SELECT COUNT(*) FROM nodes WHERE label IN ({ph})", (*allow,)).fetchone()[0]
                    eph = eph_e + eph_n
                elif self._has_table(conn, "ephemeral_events"):
                    eph = 0
                else:
                    eph = conn.execute("SELECT COUNT(*) FROM nodes WHERE 0").fetchone()[0]  # 0
            except Exception:
                eph = 0
            self._send_json({"db": db, "total_nodes": total_nodes, "total_edges": total_edges,
                             "orphans": orphans, "orphans_count": len(orphans),
                             "dupes": dupes, "dupes_count": len(dupes),
                             "isolated": isolated, "isolated_count": len(isolated),
                             "ephemeral_events": eph})
        finally:
            conn.close()

    def _has_table(self, conn, name: str) -> bool:
        return bool(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    def _api_vectors(self) -> None:
        db = self._single_db()
        try:
            limit = int(self._q("limit", "50") or 50)
        except ValueError:
            limit = 50
        try:
            offset = int(self._q("offset", "0") or 0)
        except ValueError:
            offset = 0
        limit = max(1, min(limit, 200))
        q = (self._q("q", "") or "").strip().lower()
        eng = _engine()
        conn = eng.core_conn() if db == "core" else eng.agents_conn()
        try:
            if not self._has_table(conn, "node_vectors"):
                self._send_json({"db": db, "total": 0, "vectors": []})
                return
            where = ""
            params: List[Any] = []
            if q:
                where = "WHERE n.label LIKE ? OR n.node_id = ?"
                params = [f"%{q}%", q]
            total = conn.execute(f"SELECT COUNT(*) FROM node_vectors nv JOIN nodes n ON nv.node_id=n.node_id {where}", tuple(params)).fetchone()[0]
            rows = conn.execute(f"SELECT nv.node_id, n.label, nv.vector, nv.magnitude FROM node_vectors nv JOIN nodes n ON nv.node_id=n.node_id {where} ORDER BY nv.magnitude DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            out = []
            for r in rows:
                vec = {}
                try:
                    # vector stored as term:weight text
                    raw = r["vector"] or ""
                    for part in raw.split():
                        if ":" in part:
                            k, v = part.split(":", 1)
                            vec[k] = float(v)
                except Exception:
                    vec = {}
                top = sorted(vec.items(), key=lambda x: x[1], reverse=True)[:10]
                out.append({"node_id": r["node_id"], "label": r["label"] or "", "magnitude": r["magnitude"], "top_terms": top, "term_count": len(vec)})
            self._send_json({"db": db, "total": total, "offset": offset, "limit": limit, "vectors": out})
        finally:
            conn.close()

    def _api_layers(self) -> None:
        db = self._single_db()
        eng = _engine()
        conn = eng.core_conn() if db == "core" else eng.agents_conn()
        try:
            if not self._has_table(conn, "memory_layers"):
                self._send_json({"db": db, "layers": {}})
                return
            rows = conn.execute("SELECT l.layer, l.node_id, n.label, n.node_type, l.promoted_at, json_extract(n.metadata,'$.attention_state') as att, n.metadata FROM memory_layers l LEFT JOIN nodes n ON l.node_id=n.node_id ORDER BY l.layer, l.promoted_at").fetchall()
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                grouped.setdefault(r["layer"], []).append(dict(r))
            self._send_json({"db": db, "layers": grouped})
        finally:
            conn.close()

    def _api_path(self) -> None:
        db = self._single_db()
        fro = (self._q("from", "") or "").strip()
        to = (self._q("to", "") or "").strip()
        if not fro or not to:
            raise ValueError("from and to required (label or node_id)")
        eng = _engine()
        conn = eng.core_conn() if db == "core" else eng.agents_conn()
        try:
            # resolve label -> node_id (like v2 resolveNodeRef)
            def resolve(ref: str) -> Optional[str]:
                r = conn.execute("SELECT node_id FROM nodes WHERE node_id=?", (ref,)).fetchone()
                if r:
                    return ref
                r = conn.execute("SELECT node_id FROM nodes WHERE label=?", (ref,)).fetchone()
                return r["node_id"] if r else None
            a = resolve(fro)
            b = resolve(to)
            if not a or not b:
                self._send_json({"db": db, "found": False, "message": "One or both nodes not found", "from_resolved": a, "to_resolved": b})
                return
            if a == b:
                self._send_json({"db": db, "found": True, "hops": 0, "path": [{"node_id": a, "label": fro}]})
                return
            # BFS undirected
            adj: Dict[str, List[Dict[str, Any]]] = {}
            for r in conn.execute("SELECT from_node, to_node, edge_type, weight FROM edges"):
                adj.setdefault(r["from_node"], []).append({"to": r["to_node"], "type": r["edge_type"], "w": r["weight"]})
                adj.setdefault(r["to_node"], []).append({"to": r["from_node"], "type": r["edge_type"], "w": r["weight"]})
            from collections import deque
            prev: Dict[str, Dict[str, Any]] = {}
            visited = {a: True}
            qd = deque([a])
            found = False
            while qd:
                cur = qd.popleft()
                if cur == b:
                    found = True
                    break
                for nb in adj.get(cur, []):
                    nxt = nb["to"]
                    if nxt not in visited:
                        visited[nxt] = True
                        prev[nxt] = {"from": cur, "type": nb["type"]}
                        qd.append(nxt)
            if not found:
                self._send_json({"db": db, "found": False, "message": "No path found", "from_resolved": a, "to_resolved": b})
                return
            # reconstruct
            path = []
            cur = b
            while cur != a:
                p = prev[cur]
                path.append(p)
                cur = p["from"]
            path.reverse()
            # enrich labels
            labels = {r["node_id"]: r["label"] for r in conn.execute("SELECT node_id, label FROM nodes")}
            steps = [{"from": a, "from_label": labels.get(a, a)}]
            cur = a
            for s in path:
                cur = s["to"] if s["from"] == cur else s["from"]
                steps.append({"edge_type": s["type"], "to": cur, "to_label": labels.get(cur, cur)})
            self._send_json({"db": db, "found": True, "hops": len(path), "path": path, "steps": steps, "from_resolved": a, "to_resolved": b})
        finally:
            conn.close()

    def _api_schema(self) -> None:
        db = self._single_db()
        eng = _engine()
        conn = eng.core_conn() if db == "core" else eng.agents_conn()
        try:
            tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            out = []
            for t in tables:
                cols = conn.execute(f'PRAGMA table_info("{t["name"]}")').fetchall()
                idxs = conn.execute(f'PRAGMA index_list("{t["name"]}")').fetchall()
                out.append({"name": t["name"], "sql": t["sql"], "columns": [dict(c) for c in cols], "indexes": [dict(i) for i in idxs]})
            meta = []
            if self._has_table(conn, "schema_meta"):
                meta = [dict(r) for r in conn.execute("SELECT key, value FROM schema_meta")]
            self._send_json({"db": db, "tables": out, "schema_meta": meta})
        finally:
            conn.close()

    def _api_recall(self) -> None:
        """Unified recall GET: ?q=&agent=&mode=&bound=&include_agent_notes= . LogicEngine aware."""
        q = (self._q("q", "") or self._q("query", "") or "").strip()
        if not q:
            raise ValueError("q or query required")
        raw_agent = (self._q("agent", "") or self._q("agent_id", "") or "").strip()
        mode = (self._q("mode", "RELATED") or "RELATED").strip().upper()
        try:
            bound = int(self._q("bound", self._q("limit", "10")) or 10)
        except ValueError:
            bound = 10
        try:
            offset = int(self._q("offset", "0") or 0)
        except ValueError:
            offset = 0
        inc = (self._q("include_agent_notes", "false") or "false").lower() in ("1", "true", "yes")
        eng = _engine()
        # LogicEngine heal mode + agent resolve
        from src.logic.engine import LogicEngine as _LE
        conn_a = eng.agents_conn()
        try:
            le = _LE(conn_a, eng.core_conn())
            # heal mode via engine (reuse heal for mode clamp)
            healed_args, healed = le.heal("recall", {"query": q, "mode": mode, "bound": bound, "agent": raw_agent}, auto_create=False)
            mode = healed_args.get("mode", mode)
            bound = healed_args.get("bound", bound)
            raw_agent = healed_args.get("agent", raw_agent)
        finally:
            conn_a.close()
        aid = _resolve_agent_param(raw_agent, auto_create=False) if raw_agent else None
        if aid:
            conn = eng.agents_conn()
            try:
                from src.agents import store as _as
                res = _as.agent_recall(conn, aid, q, mode=mode, bound=bound, offset=offset)
                self._send_json({"agent_id": aid, "mode": res["mode"], "total_found": res["total_found"],
                                "bound_applied": res["bound_applied"], "nodes": res["nodes"], "healed": healed if 'healed' in locals() and healed else {}})
                return
            finally:
                conn.close()
        else:
            conn = eng.core_conn()
            try:
                from src.core import recall as _cr
                res = _cr.recall(conn, q, mode=mode, bound=bound, offset=offset, include_agent_notes=inc)
                # explicit merge when inc True (guardrail #3)
                if inc:
                    try:
                        from src.agents import bridge as _br
                        x = _br.search_all_agents(eng.agents_conn(), q, bound=bound)
                        seen = {n.get("node_id") for n in res["nodes"]}
                        for r in x:
                            if r["node_id"] not in seen:
                                res["nodes"].append(r)
                        res["nodes"] = res["nodes"][:bound]
                    except Exception:
                        pass
                self._send_json({"mode": res["mode"], "total_found": res["total_found"],
                                "bound_applied": res["bound_applied"], "nodes": res["nodes"], "healed": healed if 'healed' in locals() and healed else {}})
            finally:
                conn.close()

    def _api_edges(self) -> None:
        db = self._single_db()
        try:
            limit = int(self._q("limit", "50") or 50)
        except ValueError:
            limit = 50
        try:
            offset = int(self._q("offset", "0") or 0)
        except ValueError:
            offset = 0
        limit = max(1, min(limit, 200))
        etype = (self._q("type", "") or "").strip().upper()
        node = (self._q("node", "") or "").strip()
        eng = _engine()
        conn = eng.core_conn() if db == "core" else eng.agents_conn()
        try:
            clauses, params = [], []
            if etype:
                clauses.append("e.edge_type = ?")
                params.append(etype)
            if node:
                clauses.append("(e.from_node = ? OR e.to_node = ?)")
                params += [node, node]
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            total = conn.execute(f"SELECT COUNT(*) FROM edges e {where}",
                                 tuple(params)).fetchone()[0]
            rows = conn.execute(
                f"SELECT e.* FROM edges e {where}"
                " ORDER BY e.created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
            labels = {r["node_id"]: (r["label"] or "") for r in conn.execute(
                "SELECT node_id, label FROM nodes")}
            out = [dict(r, from_label=labels.get(r["from_node"], "?"),
                        to_label=labels.get(r["to_node"], "?")) for r in rows]
            self._send_json({"db": db, "total": total, "offset": offset,
                             "limit": limit, "edges": out})
        finally:
            conn.close()

    def _api_mailbox_read(self) -> None:
        try:
            limit = int(self._q("limit", "50") or 50)
        except ValueError:
            limit = 50
        try:
            offset = int(self._q("offset", "0") or 0)
        except ValueError:
            offset = 0
        conn = _engine().agents_conn()
        try:
            msgs = agent_mailbox.read(conn, self._q("scope", "all") or "all",
                                      self._q("state", "pending") or "pending",
                                      limit, offset, mark_read=False)
            conn.commit()
            self._send_json({"messages": msgs, "limit": limit, "offset": offset})
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
        finally:
            conn.close()

    # ── POST ──

    def do_POST(self):
        if not self._check_auth():
            self._unauthorized()
            return
        path = urllib.parse.urlparse(self.path).path
        payload = self._body()
        try:
            handler = _POST_ROUTES.get(path)
            if handler is None:
                self.send_error(404, "Not Found")
                return
            self._send_json(handler(self, payload))
        except ValueError as e:
            self._send_json({"status": "error", "message": str(e)}, status=400)
        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, status=500)


def _target_of(payload: Dict[str, Any], default: str = "both") -> str:
    t = str(payload.get("db", payload.get("target", default))).lower()
    return t if t in ("core", "agents", "both", "all") else default


def _post_run_job(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    jobs = payload.get("jobs")
    target = _target_of(payload)
    if target == "all":
        target = "both"
    res = _scheduler().run_job_now(jobs=jobs, target=target)
    return {"status": "success", "run": res}


def _post_scheduler(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    eng, sched = _engine(), _scheduler()
    try:
        eng.config["max_unused_days"] = int(payload.get("max_unused_days",
                                                        eng.config.get("max_unused_days", 4)))
    except (ValueError, TypeError):
        pass
    try:
        interval = int(payload.get("interval_minutes",
                                   eng.config.get("interval_minutes", 60)))
    except (ValueError, TypeError):
        interval = eng.config.get("interval_minutes", 60)
    eng._save_config()
    if payload.get("enabled", False):
        sched.start(interval_minutes=interval)
    else:
        sched.stop()
    return {"status": "success", "config": sched.engine.config,
            "is_running": sched.running}


_CONFIG_TYPES = {
    "interval_minutes": int, "max_unused_days": int,
    "dedup_similarity_threshold": float, "dedup_scan_cap": int,
    "discover_scan_cap": int, "discover_link_floor": float, "discover_link_ceil": float,
    "prune_importance_floor": float, "prune_threshold": float,
    "auto_snapshot_before_jobs": bool, "snapshot_cooldown_s": int,
    "auto_rebuild_vectors": bool, "ephemeral_keep_last": int, "ephemeral_max_age_days": int, "ephemeral_max_age_hours": int,
    "vacuum_after_prune": bool, "vacuum_freelist_threshold_pct": float,
    "vacuum_freelist_min_pages": int, "contradiction_auto_resolve": bool,
    "contradiction_low_trust": float, "contradiction_high_trust": float,
    "contradiction_scan_cap": int, "contradiction_min_overlap_ratio": float, "dashboard_token": str,
    "keep_last_snapshots": int, "keep_last_logs": int,
    "agent_working_regulator_enabled": bool, "agent_working_high_water": int,
    "agent_working_demote_batch": int, "agent_working_max_age_hours": int,
    "agent_working_weight_access": float, "agent_working_weight_importance": float,
    "agent_working_weight_age": float, "short_term_promote_after": int,
    "core_helper_enabled": bool, "core_helper_min_age_hours": int,
    "core_helper_imp_threshold": float, "core_helper_trust_low": float,
    "core_helper_trust_high": float, "core_helper_access_threshold": int,
    "prune_empty_agents": bool, "prune_empty_agents_min_age_hours": int,
}


def _coerce(key: str, val: Any) -> Any:
    want = _CONFIG_TYPES[key]
    if want is bool:
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes", "on")
        return bool(val)
    return want(val)


def _post_config(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    eng = _engine()
    if payload.get("reload"):
        # Re-read brain/config.json from disk (hand-edits go live without restart).
        return {"status": "success", "config": eng.reload_config(), "reloaded": True}
    for key, val in payload.items():
        if key in _CONFIG_TYPES:
            try:
                eng.config[key] = _coerce(key, val)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid value for {key}: {val!r}")
    if "prune_importance_floor" in payload or "prune_threshold" in payload:
        v = eng.config.get("prune_importance_floor",
                           eng.config.get("prune_threshold", 0.05))
        eng.config["prune_importance_floor"] = eng.config["prune_threshold"] = float(v)
    eng._save_config()  # brain/config.json only — the single config file
    return {"status": "success", "config": eng.config}


def _post_config_reset(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": "success", "config": _engine().reset_config()}


def _post_create_snapshot(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    target = _target_of(payload, "both")
    dbs = ["core", "agents"] if target in ("both", "all") else [target]
    return {db: _engine().create_snapshot(db) for db in dbs}


def _post_rebuild_vectors(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    return _engine().rebuild_vectors(_target_of(payload))


def _post_restore_snapshot(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    filename = payload.get("filename", "")
    if not filename:
        raise ValueError("filename required")
    return _engine().restore_snapshot(_target_of(payload, "core"), filename)


def _post_delete_snapshot(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    filename = payload.get("filename", "")
    if not filename:
        raise ValueError("filename required")
    return _engine().delete_snapshot(filename)


def _post_vacuum(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    return _engine().vacuum_db(_target_of(payload))


def _post_compact_ephemeral(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    eng = _engine()
    keep = payload.get("keep_last")
    age_d = payload.get("max_age_days")
    age_h = payload.get("max_age_hours")
    # hours canonical: explicit hours wins, else days*24, else config
    if age_h is not None:
        age_h = int(age_h)
        age_d = None
    elif age_d is not None:
        age_d = int(age_d)
        age_h = None
    res = eng.compact_ephemeral(_target_of(payload),
                                int(keep) if keep is not None else None,
                                int(age_d) if age_d is not None else None,
                                int(age_h) if age_h is not None else None)
    removed = sum(v.get("removed_total", 0) for v in res.values()
                  if isinstance(v, dict))
    if payload.get("vacuum", True) and removed > 0:
        res["vacuum"] = eng.vacuum_db(_target_of(payload))
        if eng.config.get("auto_rebuild_vectors", True):
            res["vector_rebuild"] = eng.rebuild_vectors(_target_of(payload))
    return res


def _post_ephemeral_sync_nodes(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    dry = payload.get("dry_run", False)
    if isinstance(dry, str):
        dry = dry.lower() in ("1", "true", "yes")
    eng = _engine()
    target = _target_of(payload)
    res = eng.sync_ephemeral_nodes(target, dry_run=bool(dry))
    if not dry:
        removed = sum(v.get("removed_total", 0) for v in res.values() if isinstance(v, dict))
        if removed > 0:
            # vacuum + rebuild if needed (same as compact)
            res["vacuum"] = eng.vacuum_db(target)
            if eng.config.get("auto_rebuild_vectors", True):
                res["vector_rebuild"] = eng.rebuild_vectors(target)
    return res


def _post_check_vacuum(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    eng = _engine()
    target = _target_of(payload)
    bloat = eng.get_bloat_metrics(target)
    dbs = ["core", "agents"] if target in ("both", "all") else [target]
    if any(bloat.get(db, {}).get("needs_vacuum") for db in dbs):
        vac = eng.vacuum_db(target)
        res: Dict[str, Any] = {"triggered": True, "bloat": bloat, "vacuum": vac}
        if eng.config.get("auto_rebuild_vectors", True):
            res["vector_rebuild"] = eng.rebuild_vectors(target)
        res["bloat_after"] = eng.get_bloat_metrics(target)
        return res
    return {"triggered": False, "bloat": bloat}


def _post_graduate(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    node_ids = None
    if isinstance(payload.get("node_ids"), list):
        node_ids = [str(x).strip() for x in payload["node_ids"] if str(x).strip()]
    elif isinstance(payload.get("node_ids"), str) and payload["node_ids"].strip():
        node_ids = [payload["node_ids"].strip()]
    elif payload.get("node_id"):
        node_ids = [str(payload["node_id"]).strip()]
    # LogicEngine polish: accept agent hint (AshaWeb, web scout) + canonical
    raw_agent = str(payload.get("agent", "") or payload.get("agent_id", "") or "").strip()
    if raw_agent:
        canon = _resolve_agent_param(raw_agent, auto_create=False)
        agent_id = canon if canon else raw_agent
    else:
        agent_id = None
    healed = {}
    if raw_agent and agent_id != raw_agent:
        healed = {"agent": f"{raw_agent!r} -> {agent_id}"}
    res = _engine().graduate_agent_notes(node_ids=node_ids, agent_id=agent_id)
    if healed:
        # attach for API learning, keep engine result intact
        if isinstance(res, dict):
            res = dict(res)
            res["healed"] = healed
    return res


def _post_demote(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    """Demote review_ready → agent_private (undo graduate queue)."""
    node_ids = None
    if isinstance(payload.get("node_ids"), list):
        node_ids = [str(x).strip() for x in payload["node_ids"] if str(x).strip()]
    elif isinstance(payload.get("node_ids"), str) and payload["node_ids"].strip():
        node_ids = [payload["node_ids"].strip()]
    elif payload.get("node_id"):
        node_ids = [str(payload["node_id"]).strip()]
    if not node_ids:
        raise ValueError("node_ids[] required")
    eng = _engine()
    conn = eng.agents_conn()
    try:
        ok = 0
        errors: List[str] = []
        for nid in node_ids:
            try:
                # find owner agent
                row = conn.execute("SELECT agent_id, metadata FROM nodes WHERE node_id=?", (nid,)).fetchone()
                if not row:
                    errors.append(f"{nid}: not found")
                    continue
                aid = row["agent_id"]
                if not aid:
                    try:
                        m = json.loads(row["metadata"] or "{}")
                        aid = m.get("agent_id") or ""
                    except Exception:
                        aid = ""
                if not aid:
                    errors.append(f"{nid}: no owner agent")
                    continue
                if not agent_store.agent_set_attention(conn, aid, nid, "agent_private"):
                    errors.append(f"{nid}: not review_ready or not agent note")
                    continue
                ok += 1
            except Exception as e:
                errors.append(f"{nid}: {e}")
        conn.commit()
        return {"status": "demoted", "demoted": ok, "total": len(node_ids), "errors": errors}
    finally:
        conn.close()


def _post_contradictions_clear(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    db = str(payload.get("db", "both")).lower()
    dbs = ["core", "agents"] if db in ("both", "all") else ([db] if db in ("core", "agents") else ["both"])
    # normalize: if both, clear both
    if db in ("both", "all"):
        dbs = ["core", "agents"]
    out = {}
    for d in dbs:
        conn = _engine().core_conn() if d == "core" else _engine().agents_conn()
        try:
            cur = conn.execute("DELETE FROM edges WHERE edge_type='CONTRADICTS'")
            conn.commit()
            out[d] = {"deleted": cur.rowcount if cur.rowcount is not None and cur.rowcount >=0 else 0}
        finally:
            conn.close()
    return {"status": "cleared", **out}


def _post_regulate(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    dry = payload.get("dry_run", False)
    if isinstance(dry, str):
        dry = dry.lower() in ("1", "true", "yes")
    return _engine().regulate_agent_working_memory(dry_run=bool(dry))


def _post_regulate_core(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    dry = payload.get("dry_run", False)
    if isinstance(dry, str):
        dry = dry.lower() in ("1", "true", "yes")
    return _engine().regulate_core_helper(dry_run=bool(dry))


def _post_contradiction_action(handler: "DashboardHandler",
                               payload: Dict[str, Any]) -> Dict[str, Any]:
    edge_id = payload.get("edge_id", "")
    action = payload.get("action", "")
    alias_map = {"confirm": "confirmed", "ignore": "ignored"}
    action_norm = alias_map.get(action, action)
    if not edge_id or not action:
        raise ValueError("edge_id and action required")
    eng = _engine()
    db = str(payload.get("db", "core")).lower()
    db = db if db in ("core", "agents") else "core"
    if action_norm in ("confirmed", "ignored", "pending", "resolved"):
        return eng.update_contradiction_status(edge_id, action_norm, db=db)
    if action in ("delete", "keep_from", "keep_to", "merge"):
        # engine.resolve rebuilds internally when configured (no double rebuild here)
        return eng.resolve_contradiction(edge_id, action, db=db)
    raise ValueError(f"Unknown action {action}")


def _post_contradiction_auto_resolve(handler: "DashboardHandler",
                                     payload: Dict[str, Any]) -> Dict[str, Any]:
    dry = payload.get("dry_run", True)
    if isinstance(dry, str):
        dry = dry.lower() in ("true", "1", "yes")
    db = str(payload.get("db", "core")).lower()
    return _engine().auto_resolve_low_trust(dry_run=bool(dry),
                                            db=db if db in ("core", "agents") else "core")


def _post_ephemeral_allowlist(handler: "DashboardHandler",
                              payload: Dict[str, Any]) -> Dict[str, Any]:
    eng = _engine()
    if isinstance(payload.get("labels"), list):
        return eng.set_ephemeral_allowlist([str(x) for x in payload["labels"]])
    action = payload.get("action", "")
    label = str(payload.get("label", "")).strip()
    current = list(eng.config.get("ephemeral_labels", []))
    if action == "add" and label:
        if label not in current:
            current.append(label)
        res = eng.set_ephemeral_allowlist(current)
        # mutual exclusion: remove from ignored if present
        ign = eng.config.get("ephemeral_ignored", [])
        if label in ign:
            eng.set_ephemeral_ignored([x for x in ign if x != label])
            res["ephemeral_ignored"] = eng.config["ephemeral_ignored"]
        return res
    if action == "remove" and label:
        return eng.set_ephemeral_allowlist([x for x in current if x != label])
    raise ValueError("provide {labels:[...]} or {action:add|remove, label}")


def _post_ephemeral_ignored(handler: "DashboardHandler",
                            payload: Dict[str, Any]) -> Dict[str, Any]:
    eng = _engine()
    if isinstance(payload.get("labels"), list):
        return eng.set_ephemeral_ignored([str(x) for x in payload["labels"]])
    action = payload.get("action", "")
    label = str(payload.get("label", "")).strip()
    current = list(eng.config.get("ephemeral_ignored", []))
    if action == "add" and label:
        if label not in current:
            current.append(label)
        res = eng.set_ephemeral_ignored(current)
        # mutual exclusion: remove from ephemeral if present
        eph = eng.config.get("ephemeral_labels", [])
        if label in eph:
            eng.set_ephemeral_allowlist([x for x in eph if x != label])
            res["ephemeral_labels"] = eng.config["ephemeral_labels"]
        return res
    if action == "remove" and label:
        return eng.set_ephemeral_ignored([x for x in current if x != label])
    raise ValueError("provide {labels:[...]} or {action:add|remove, label}")


def _conn_for_db(db: str):
    eng = _engine()
    return eng.core_conn() if db == "core" else eng.agents_conn()


def _resolve_agent_param(raw: Optional[str], *, auto_create: bool) -> Optional[str]:
    """LogicEngine agent hint -> canonical agent_id. Reads token-less, same FS."""
    if not raw or not str(raw).strip():
        return None
    raw = str(raw).strip()
    try:
        eng = _engine()
        conn = eng.agents_conn()
    except Exception:
        return None
    try:
        from src.logic.engine import LogicEngine
        le = LogicEngine(conn, None)
        aid = le.resolve_agent(raw, auto_create=auto_create)
        if auto_create and aid:
            try:
                conn.commit()
            except Exception:
                pass
        return aid
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _agent_db_infer(handler: "DashboardHandler", raw_agent: Optional[str], default_db: str) -> str:
    """If ?agent= is present and resolves, infer agents.db when caller omitted ?db=."""
    if raw_agent and not handler._query().get("db"):
        # auto_create=False for reads, caller can hint agent without creating ghost
        cand = _resolve_agent_param(raw_agent, auto_create=False)
        if cand:
            return "agents"
    return default_db


def _post_node_update(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    db = str(payload.get("db", "core")).lower()
    if db not in ("core", "agents"):
        raise ValueError("db must be core|agents")
    node_id = payload.get("node_id", "")
    fields = payload.get("fields", {})
    if not node_id or not isinstance(fields, dict):
        raise ValueError("node_id and fields{} required")
    allowed = {"label", "content", "trust_level", "importance", "source", "metadata"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Invalid fields: {sorted(unknown)}")
    if isinstance(fields.get("metadata"), str):
        try:
            fields = dict(fields)
            fields["metadata"] = json.loads(fields["metadata"])
        except ValueError:
            raise ValueError("metadata must be an object or JSON string")
    conn = _conn_for_db(db)
    try:
        node = core_nodes.update_node(conn, node_id, **{k: v for k, v in fields.items()
                                                         if k in allowed})
        conn.commit()
        if not node:
            return {"status": "error", "message": "not found"}
        return {"status": "updated", "node": node}
    finally:
        conn.close()


def _post_node_delete(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    db = str(payload.get("db", "core")).lower()
    if db not in ("core", "agents"):
        raise ValueError("db must be core|agents")
    node_id = payload.get("node_id", "")
    if not node_id:
        raise ValueError("node_id required")
    conn = _conn_for_db(db)
    try:
        ok = core_nodes.delete_node(conn, node_id)
        conn.commit()
        return {"status": "deleted" if ok else "not found"}
    finally:
        conn.close()


def _post_node_add(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create one node server-side (vectors/layers/index in the same commit). LogicEngine unified."""
    # unified agent param: payload.agent or agent_id, auto-resolve + auto-create, infer db
    raw_agent = str(payload.get("agent", "") or payload.get("agent_id", "") or "").strip()
    if raw_agent:
        # reuse LogicEngine: write path auto_create=True
        canon = _resolve_agent_param(raw_agent, auto_create=True)
        if canon:
            payload["agent_id"] = canon
            payload["agent"] = canon
            db = "agents"
        else:
            db = str(payload.get("db", "core")).lower()
    else:
        db = str(payload.get("db", "core")).lower()
    if db not in ("core", "agents"):
        raise ValueError("db must be core|agents")
    node_type = str(payload.get("node_type", "FACT")).upper()
    label = str(payload.get("label", "") or "")
    content = str(payload.get("content", "") or "")
    if not content:
        raise ValueError("content required")
    item = {"node_type": node_type, "label": label or content[:30],
            "content": content}
    for key in ("trust", "importance"):
        if payload.get(key) not in (None, ""):
            try:
                item[key] = float(payload[key])
            except (ValueError, TypeError):
                raise ValueError(f"Invalid {key}: {payload[key]!r}")
    # also handle source + healed_fields via LogicEngine metadata if present
    if isinstance(payload.get("metadata"), dict):
        item["metadata"] = payload["metadata"]
    elif isinstance(payload.get("metadata"), str) and payload["metadata"].strip():
        try:
            item["metadata"] = json.loads(payload["metadata"])
        except ValueError:
            item["metadata"] = {}
    conn = _conn_for_db(db)
    try:
        if db == "agents":
            agent_id = str(payload.get("agent_id", "") or "")
            if not agent_id:
                raise ValueError("agent_id required for agents.db")
            # canonical already validated via _resolve_agent_param; still verify
            agent_store.require_agent(conn, agent_id)
            ids = core_nodes.remember_many(
                conn, [{**item, "source": f"AGENT_{agent_id}"}],
                extra_cols={"agent_id": agent_id})
        else:
            ids = core_nodes.remember_many(conn, [item])
        conn.commit()
        node = core_nodes.get_node(conn, ids[0])
        return {"status": "created", "node_id": ids[0], "node": node}
    finally:
        conn.close()


def _post_remember(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    """Unified remember POST (agent auto-resolve + healing). Delegates to _post_node_add."""
    # LogicEngine heal for unified Direct API (trust clamp, node_type default, healed_fields)
    healed = {}
    try:
        from src.logic.engine import LogicEngine as _LE
        eng = _engine()
        conn_a = eng.agents_conn()
        core_c = eng.core_conn()
        try:
            le = _LE(conn_a, core_c)
            healed_payload, healed = le.heal("remember", payload, auto_create=True)
            # commit auto-create from heal (agent_ensure)
            try:
                conn_a.commit()
            except Exception:
                pass
            payload = healed_payload
        finally:
            try:
                conn_a.close()
            except Exception:
                pass
            try:
                core_c.close()
            except Exception:
                pass
    except Exception:
        healed = {}
    res = _post_node_add(handler, payload)
    # attach healed map if present
    try:
        if 'healed' in locals() and healed:
            res["healed"] = healed
    except Exception:
        pass
    return res


def _post_recall(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    """Unified recall POST: payload {query, agent?, mode?, bound?}."""
    q = str(payload.get("query", "") or payload.get("q", "") or "").strip()
    if not q:
        raise ValueError("query required")
    raw_agent = str(payload.get("agent", "") or payload.get("agent_id", "") or "").strip()
    mode = str(payload.get("mode", "RELATED") or "RELATED").strip().upper()
    try:
        bound = int(payload.get("bound", payload.get("limit", 10)))
    except Exception:
        bound = 10
    try:
        offset = int(payload.get("offset", 0))
    except Exception:
        offset = 0
    eng = _engine()
    aid = _resolve_agent_param(raw_agent, auto_create=False) if raw_agent else None
    if aid:
        conn = eng.agents_conn()
        try:
            from src.agents import store as _as
            res = _as.agent_recall(conn, aid, q, mode=mode, bound=bound, offset=offset)
            return {"agent_id": aid, "mode": res["mode"], "total_found": res["total_found"], "bound_applied": res["bound_applied"], "nodes": res["nodes"]}
        finally:
            conn.close()
    else:
        conn = eng.core_conn()
        try:
            from src.core import recall as _cr
            inc = bool(payload.get("include_agent_notes", False))
            res = _cr.recall(conn, q, mode=mode, bound=bound, offset=offset, include_agent_notes=inc)
            if inc:
                try:
                    from src.agents import bridge as _br
                    x = _br.search_all_agents(eng.agents_conn(), q, bound=bound)
                    seen = {n.get("node_id") for n in res["nodes"]}
                    for r in x:
                        if r["node_id"] not in seen:
                            res["nodes"].append(r)
                    res["nodes"] = res["nodes"][:bound]
                except Exception:
                    pass
            return {"mode": res["mode"], "total_found": res["total_found"], "bound_applied": res["bound_applied"], "nodes": res["nodes"]}
        finally:
            conn.close()


def _post_edge_add(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_agent = str(payload.get("agent", "") or payload.get("agent_id", "") or "").strip()
    if raw_agent:
        canon = _resolve_agent_param(raw_agent, auto_create=False)
        if canon:
            payload["agent_id"] = canon
            db = "agents"
        else:
            db = str(payload.get("db", "core")).lower()
    else:
        db = str(payload.get("db", "core")).lower()
    if db not in ("core", "agents"):
        raise ValueError("db must be core|agents")
    fro = payload.get("from_node", "")
    to = payload.get("to_node", "")
    if not fro or not to:
        raise ValueError("from_node and to_node required")
    edge_type = str(payload.get("edge_type", "RELATES_TO")).upper()
    try:
        weight = float(payload.get("weight", 1.0))
    except (ValueError, TypeError):
        raise ValueError(f"Invalid weight: {payload.get('weight')!r}")
    # support exact label -> node_id (guardrail #1) + fuzzy LIKE when requested
    fuzzy = bool(payload.get("fuzzy", False))
    try:
        from src.logic.engine import LogicEngine as _LE
        conn_tmp = _conn_for_db(db)
        try:
            le = _LE(conn_tmp, None)
            ag = payload.get("agent_id")
            # exact first (never LIKE)
            ef = le.resolve_ref_exact(conn_tmp, str(fro), ag) if ag else le.resolve_ref_exact(conn_tmp, str(fro))
            et = le.resolve_ref_exact(conn_tmp, str(to), ag) if ag else le.resolve_ref_exact(conn_tmp, str(to))
            if ef:
                fro = ef
            if et:
                to = et
            # fuzzy fallback only when requested and exact miss
            if fuzzy:
                if not ef:
                    f = le.resolve_ref_fuzzy(conn_tmp, str(payload.get("from_node","")), ag) if ag else le.resolve_ref_fuzzy(conn_tmp, str(payload.get("from_node","")))
                    if f:
                        fro = f
                if not et:
                    t = le.resolve_ref_fuzzy(conn_tmp, str(payload.get("to","") or payload.get("to_node","")), ag) if ag else le.resolve_ref_fuzzy(conn_tmp, str(payload.get("to","") or payload.get("to_node","")))
                    if t:
                        to = t
        finally:
            conn_tmp.close()
    except Exception:
        pass
    conn = _conn_for_db(db)
    try:
        if db == "agents":
            agent_id = str(payload.get("agent_id", "") or "")
            if not agent_id:
                raise ValueError("agent_id required for agents.db")
            edge_id = agent_store.agent_relate(conn, agent_id, fro, to,
                                               edge_type, weight)
        else:
            edge_id = core_edges.relate(conn, fro, to, edge_type, weight)
        conn.commit()
        return {"status": "created", "edge_id": edge_id}
    finally:
        conn.close()


def _post_edge_delete(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    db = str(payload.get("db", "core")).lower()
    if db not in ("core", "agents"):
        raise ValueError("db must be core|agents")
    edge_id = payload.get("edge_id", "")
    if not edge_id:
        raise ValueError("edge_id required")
    conn = _conn_for_db(db)
    try:
        cur = conn.execute("DELETE FROM edges WHERE edge_id = ?", (edge_id,))
        conn.commit()
        return {"status": "deleted" if cur.rowcount else "not found"}
    finally:
        conn.close()


def _post_bulk_nodes(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    db = str(payload.get("db", "core")).lower()
    if db not in ("core", "agents"):
        raise ValueError("db must be core|agents")
    ids = payload.get("node_ids") or payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise ValueError("node_ids[] required")
    action = str(payload.get("action", "")).strip()
    if not action:
        raise ValueError("action required")
    conn = _conn_for_db(db)
    try:
        ok = 0
        errors: List[str] = []
        if action == "delete":
            for nid in ids:
                try:
                    if core_nodes.delete_node(conn, str(nid)):
                        ok += 1
                except Exception as e:
                    errors.append(f"{nid}: {e}")
            conn.commit()
            return {"status": "deleted", "deleted": ok, "total": len(ids), "errors": errors}
        elif action == "set_attention":
            val = str(payload.get("value", "")).strip()
            # allow empty to clear
            for nid in ids:
                try:
                    row = conn.execute("SELECT metadata FROM nodes WHERE node_id=?", (str(nid),)).fetchone()
                    if not row:
                        errors.append(f"{nid}: not found")
                        continue
                    try:
                        meta = json.loads(row["metadata"] or "{}")
                    except Exception:
                        meta = {}
                    if val == "__clear" or val == "":
                        meta.pop("attention_state", None)
                    else:
                        if val not in ("agent_private", "review_ready", "core_verified"):
                            raise ValueError(f"Invalid attention {val}")
                        meta["attention_state"] = val
                    conn.execute("UPDATE nodes SET metadata=?, updated_at=? WHERE node_id=?", (json.dumps(meta), int(__import__("time").time()), str(nid)))
                    ok += 1
                except Exception as e:
                    errors.append(f"{nid}: {e}")
            conn.commit()
            return {"status": "updated", "updated": ok, "total": len(ids), "errors": errors}
        elif action == "set_layer":
            val = str(payload.get("value", "")).strip()
            if val not in ("working", "short_term", "long_term", "archive"):
                raise ValueError(f"Invalid layer {val}")
            for nid in ids:
                try:
                    # check node exists
                    if not conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (str(nid),)).fetchone():
                        errors.append(f"{nid}: not found")
                        continue
                    conn.execute("INSERT OR REPLACE INTO memory_layers (node_id, layer, promoted_at, layer_order) VALUES (?,?,?,?)",
                                 (str(nid), val, int(__import__("time").time()) if val == "working" else None, {"working": 1, "short_term": 2, "long_term": 3, "archive": 4}[val]))
                    ok += 1
                except Exception as e:
                    errors.append(f"{nid}: {e}")
            conn.commit()
            return {"status": "updated", "updated": ok, "total": len(ids), "errors": errors}
        elif action == "relabel":
            val = str(payload.get("value", "")).strip()
            if not val:
                raise ValueError("value (new label) required")
            for nid in ids:
                try:
                    if not conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (str(nid),)).fetchone():
                        errors.append(f"{nid}: not found")
                        continue
                    conn.execute("UPDATE nodes SET label=?, updated_at=? WHERE node_id=?", (val, int(__import__("time").time()), str(nid)))
                    # keep index in sync
                    try:
                        from src.core.nodes import build_index
                        row2 = conn.execute("SELECT label, content FROM nodes WHERE node_id=?", (str(nid),)).fetchone()
                        build_index(conn, str(nid), row2["label"] or "", row2["content"] or "")
                    except Exception:
                        pass
                    ok += 1
                except Exception as e:
                    errors.append(f"{nid}: {e}")
            conn.commit()
            return {"status": "updated", "updated": ok, "total": len(ids), "errors": errors}
        else:
            raise ValueError(f"Unknown bulk action {action}")
    finally:
        conn.close()


def _post_sql(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    db = str(payload.get("db", "core")).lower()
    if db not in ("core", "agents"):
        raise ValueError("db must be core|agents")
    sql = (payload.get("sql") or "").strip()
    if not sql:
        raise ValueError("sql required")
    if not sql.lstrip().lower().startswith(("select", "pragma", "explain")):
        raise ValueError("Only SELECT / PRAGMA / EXPLAIN allowed")
    eng = _engine()
    conn = eng.core_conn() if db == "core" else eng.agents_conn()
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        truncated = len(rows) > 200
        rows = rows[:200]
        data = []
        for r in rows:
            if hasattr(r, "keys"):
                data.append([r[k] for k in cols])
            else:
                data.append(list(r))
        return {"db": db, "columns": cols, "rows": data, "row_count": len(data), "truncated": truncated}
    finally:
        conn.close()

def _post_mailbox_send(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("from", "to", "body"):
        if not payload.get(key):
            raise ValueError(f"{key} required")
    # LogicEngine polish: heal from/to scopes (AshaWeb -> agent:agent_ashaweb_4fee, fuzzy, broadcast)
    healed = {}
    try:
        from src.logic.engine import LogicEngine as _LE
        eng = _engine()
        conn_tmp = eng.agents_conn()
        try:
            le = _LE(conn_tmp, None)
            # use heal helper (auto_create=False for scopes, but allow from hint resolution)
            tmp_payload = {"from": payload["from"], "to": payload["to"], "body": payload["body"]}
            healed_payload, healed = le.heal("mailbox.send", tmp_payload, auto_create=False)
            # heal does agent hint -> agent:canonical for from/to
            payload = {**payload, **{k: v for k, v in healed_payload.items() if k in ("from", "to")}}
            if healed_payload.get("metadata"):
                payload["metadata"] = healed_payload["metadata"]
        finally:
            conn_tmp.close()
    except Exception:
        healed = {}
    meta = payload.get("metadata")
    conn = _engine().agents_conn()
    try:
        if isinstance(meta, dict):
            res = agent_mailbox.send(conn, payload["from"], payload["to"], payload["body"], metadata=meta)
        else:
            res = agent_mailbox.send(conn, payload["from"], payload["to"], payload["body"])
        conn.commit()
        if healed:
            res = dict(res)
            res["healed"] = healed
        return res
    finally:
        conn.close()


def _post_mailbox_ack(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    msg_ids = payload.get("msg_ids")
    if isinstance(msg_ids, str):
        try:
            msg_ids = json.loads(msg_ids)
        except ValueError:
            raise ValueError("msg_ids must be a list or JSON string")
    conn = _engine().agents_conn()
    try:
        res = agent_mailbox.ack(conn, payload.get("scope", "user"), msg_ids)
        conn.commit()
        return res
    finally:
        conn.close()


def _post_mailbox_delete(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    msg_id = payload.get("msg_id") or (payload.get("msg_ids") or [None])[0]
    if isinstance(payload.get("msg_ids"), list) and len(payload["msg_ids"]) > 1:
        # bulk via wipe-like loop
        ids = payload["msg_ids"]
        conn = _engine().agents_conn()
        try:
            total = 0
            for mid in ids:
                cur = conn.execute("DELETE FROM mailbox WHERE msg_id=?", (str(mid),))
                total += cur.rowcount
            conn.commit()
            return {"deleted": total}
        finally:
            conn.close()
    if not msg_id:
        raise ValueError("msg_id required")
    conn = _engine().agents_conn()
    try:
        cur = conn.execute("DELETE FROM mailbox WHERE msg_id=?", (str(msg_id),))
        conn.commit()
        return {"deleted": cur.rowcount, "msg_id": str(msg_id)}
    finally:
        conn.close()


def _post_mailbox_wipe(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    conn = _engine().agents_conn()
    try:
        cur = conn.execute("DELETE FROM mailbox")
        conn.commit()
        return {"wiped": cur.rowcount}
    finally:
        conn.close()


def _post_prune_empty_agents(handler: "DashboardHandler", payload: Dict[str, Any]) -> Dict[str, Any]:
    dry = bool(payload.get("dry_run", False))
    # allow override min_age
    min_age = payload.get("min_age_hours")
    if min_age is not None:
        min_age = int(min_age)
    else:
        min_age = None
    res = _engine().prune_empty_agents(target="agents", dry_run=dry, min_age_hours=min_age)
    # unwrap agents key for dashboard convenience
    if isinstance(res, dict) and "agents" in res and isinstance(res["agents"], dict) and "status" not in res:
        # old shape when toggle check, but prune returns direct
        return res
    return res


_POST_ROUTES = {
    "/api/run_job": _post_run_job,
    "/api/scheduler": _post_scheduler,
    "/api/config": _post_config,
    "/api/config_reset": _post_config_reset,
    "/api/create_snapshot": _post_create_snapshot,
    "/api/rebuild_vectors": _post_rebuild_vectors,
    "/api/restore_snapshot": _post_restore_snapshot,
    "/api/delete_snapshot": _post_delete_snapshot,
    "/api/vacuum": _post_vacuum,
    "/api/compact_ephemeral": _post_compact_ephemeral,
    "/api/ephemeral_sync_nodes": _post_ephemeral_sync_nodes,
    "/api/check_vacuum": _post_check_vacuum,
    "/api/graduate": _post_graduate,
    "/api/regulate_agent_working": _post_regulate,
    "/api/regulate_core_helper": _post_regulate_core,
    "/api/contradiction_action": _post_contradiction_action,
    "/api/contradiction_auto_resolve": _post_contradiction_auto_resolve,
    "/api/ephemeral_allowlist": _post_ephemeral_allowlist,
    "/api/ephemeral_ignored": _post_ephemeral_ignored,
    "/api/node_update": _post_node_update,
    "/api/node_delete": _post_node_delete,
    "/api/node_add": _post_node_add,
    "/api/remember": _post_remember,
    "/api/recall": _post_recall,
    "/api/bulk_nodes": _post_bulk_nodes,
    "/api/sql": _post_sql,
    "/api/demote": _post_demote,
    "/api/contradictions/clear": _post_contradictions_clear,
    "/api/edge_add": _post_edge_add,
    "/api/edge_delete": _post_edge_delete,
    "/api/mailbox/send": _post_mailbox_send,
    "/api/mailbox/ack": _post_mailbox_ack,
    "/api/mailbox/delete": _post_mailbox_delete,
    "/api/mailbox/wipe": _post_mailbox_wipe,
    "/api/prune_empty_agents": _post_prune_empty_agents,
}


def start_dashboard(memory_dir: Optional[str] = None,
                    brain_dir: Optional[str] = None,
                    host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    """Create + configure a dashboard server (caller serves it)."""
    from src.core.store import core_db_path
    mem = Path(memory_dir) if memory_dir else core_db_path().parent
    brain = Path(brain_dir) if brain_dir else _default_brain_dir()
    engine = BrainEngine(core_path=str(mem / "core.db"),
                         agents_path=str(mem / "agents.db"),
                         brain_dir=str(brain))
    # converge both schemas on start (first run creates the DBs)
    core_conn = engine.core_conn()
    try:
        from src.core.migrate import migrate
        migrate(core_conn)
        core_conn.commit()
    finally:
        core_conn.close()
    agents_conn = engine.agents_conn()
    try:
        agent_store.ensure_schema(agents_conn)
        agents_conn.commit()
    finally:
        agents_conn.close()
    configure(engine, BrainScheduler(engine=engine))
    return ThreadingHTTPServer((host, port), DashboardHandler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ASHA Memory v3 dashboard")
    ap.add_argument("--memory-path", default=None)
    ap.add_argument("--brain-dir", default=None)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    server = start_dashboard(args.memory_path, args.brain_dir, args.host, args.port)
    print(f"ASHA v3 dashboard on http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
