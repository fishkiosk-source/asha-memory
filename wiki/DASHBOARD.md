# Dashboard

Human UI for Asha Memory v3. Stdlib-only `http.server` (`ThreadingHTTPServer`), vanilla JS, zero dependencies — no CDN, no iframe, no `sql.js` (enforced by tests that assert no `cdn`, `fetch("http`, `sql.js`, or `iframe` strings in `dashboard/`).

- **Server:** `dashboard/server.py` — binds `127.0.0.1:8500` by default.
- **Start:** `python -m dashboard.server --port 8500` (see [OPERATIONS](OPERATIONS.md)).
- **Convergence on start:** `start_dashboard()` converges both schemas — `migrate(core_conn)` + `agent_store.ensure_schema(agents_conn)` — first run creates `memory/core.db` + `memory/agents.db` automatically.
- **Stack guard:** `memory/.lock` — dashboard, MCP, and brain coordinate via the file lock; run only one writer stack at a time.

## Static shell

The browser shell is three layers, all same-origin:

| File | Role |
|---|---|
| `dashboard/static/dashboard.html` | Single-page shell — header (core/agents pills, scheduler dot, Login/Logout), `#status`, `#statsbar`, `#tabs` nav, `#panel` container, `#toast`, `#modal`, `#tokenbar`. Loads `app.js` + 12 tab modules via `<script src="/static/modules/*.js">`. Inline `<style>` only. |
| `dashboard/static/app.js` | Shell controller — `window.Tabs = {}`, `window.App` singleton (`tab`, `token()`, `login()`, `doLogin()`, `logout()`, `api()`, `show(key)`, `refresh()`, `toast()`, `modal()`, `esc()`/`ago()`/`nodeDetail()` helpers, `setBadge()`). Fetches `/api/status?db=all` every 30s, renders health/bloat pills, drives badge polling. No external fetch, no CDN. |
| `dashboard/static/modules/*.js` (12 files) | Tab plugins — each file assigns `window.Tabs.<name> = { render(el), ... }`. Loaded synchronously after `app.js`; `App.show(key)` calls `Tabs[key].render(panel)` if present. |

Modules loaded in `dashboard.html:90-101`:

```
app.js → overview.js → maintenance.js → graduate.js → observer.js
→ contradicts.js → ephemeral.js → graph.js → manager.js
→ system.js → statistics.js → config.js → mail.js
```

## Wiki (public, same style as dashboard)

`GET /wiki`, `/wiki/<file>` renders `wiki/*.md` with dashboard dark theme (`#0d1117` bg, `#161b22` cards, `#30363d` borders, `.pill/.tag/.btn` same as dashboard). Sidebar is sticky `top:70px`, shows `Pages · N` + active `background:#1f6feb`. Header shows `ASHA Wiki v3` + `public`/`AI ?raw=1` pills. Content is `<div class="card">` with TOC (`.toc`).

**Search** (`dashboard/server.py:_serve_wiki`): input `#wiki-search` (`/ to focus`) filters sidebar live (name + content via `fetch(...?raw=1)` index), `hits` shows `N pages matched`, `results` panel lists up to 5 snippets from other pages, `Enter` highlights `q` in current page via `TreeWalker` → `<mark>` (`#e3b341` on `#0d1117`), `Esc` clears. `?raw=1` returns `text/markdown` for AI.

## 13 tabs

| # | Key (`Tabs.<key>`) | Label | Module | Purpose | Default `?db` |
|---|---|---|---|---|---|
| 1 | `overview` | Overview | `overview.js` | Per-DB health + bloat cards (notes/links/size/waste/telemetry/clashes/backups) + last-5 execution history. Read-only; details live in Statistics. | `all` (fetches `/api/status?db=all`) |
| 2 | `maintenance` | Maintenance | `maintenance.js` | All 11 brain jobs in canonical order (`dedup`, `compact`, `agent_working`, `core_helper`, `age_prune`, `tiers`, `contradictions`, `graduation`, `discover`, `prune_empty_agents`, `vacuum`) + `Run FULL` (defaults) + `Snapshot Now`. Target selector `both/core/agents`. Result JSON rendered inline, coalesced snapshots per policy. | `all` (bloat via `/api/bloat?db=all`, jobs POST `/api/run_job`) |
| 3 | `graduate` | Graduate | `graduate.js` | Review queue — stats (`total`, `review_ready`, `private`, `graduable`) + per-note table with checkboxes + per-row/bulk/all-graduable promotion. Move semantics: source row deleted from `agents.db`, provenance kept in `core.db` (see `brain/bridge.py`). | `agents` (implicit — `GET /api/graduate_preview`, `POST /api/graduate`) |
| 4 | `observer` | Observer | `observer.js` | `agents.db` WORKING-layer regulator — per-agent scores, `days_left`, `demote_next` / `stale_soon` / `keep` / `protected`. Dry-run preview + `Regulate Now` (`POST /api/regulate_agent_working`). Core WORKING is never shown. | `agents` (implicit — `GET /api/agent_working_preview`) |
| 5 | `core_helper` | Core Helper | `core_helper.js` | `core.db` WORKING regulator — age ≥`min_age_hours` + `trust`×`importance`×`access` rules (keep hot / →`short_term` / →`long_term`, archive manual-only). Preview `GET /api/core_helper_preview` + `Regulate Now` (`POST /api/regulate_core_helper`). Mirrors Observer for Core. | `core` (implicit — `GET /api/core_helper_preview`) |
| 6 | `contradicts` | Contradicts | `contradicts.js` | CONTRADICTS edges — filters `pending/confirmed/ignored/all` + overlap chips + trust-gap highlight. Per-row `Confirm/Ignore/Delete/Keep-From/Keep-To/Merge` + bulk bar + opt-in auto-resolve (`POST /api/contradiction_action`, `/api/contradiction_auto_resolve`). DB selector core/agents. | `core` (selector; default `core`) |
| 7 | `ephemeral` | Ephemeral | `ephemeral.js` | `ephemeral_events` telemetry table — allowlist chips (add/remove via `POST /api/ephemeral_allowlist`), auto-detect candidates (never auto-deleted, `GET /api/ephemeral_candidates`), `Compact Now` (`POST /api/compact_ephemeral` + optional vacuum/rebuild). Operates on the table, not the graph. | `core` (allowlist global; stats `?db=all`, candidates per-DB) |
| 8 | `graph` | Graph | `graph.js` | Native Canvas renderer + hand-rolled force layout, fed by `GET /api/graph?db=&limit=` (server-capped, `truncated` flagged). Type colors, CONTRADICTS in red, click-for-details, `core|agents` selector. | `core` single-DB |
| 9 | `manager` | Manager | `manager.js` | Data browser — paginated `GET /api/nodes` search + row table (layer shown, **Date** `created_at` + `Updated` `updated_at`, default sort `created_at DESC` newest first) + edit modal (`POST /api/node_update` commits server-side with incremental vectors; snapshot only per `auto_snapshot_before_jobs` policy) + single/bulk delete (`/api/node_delete`, `/api/bulk_nodes`). Extra subtabs: Edges (`/api/edges` + `/api/edge_add|delete`), Vectors (`/api/vectors`), Layers (`/api/layers`), Path BFS (`/api/path`), Schema (`/api/schema`), Statistics (`/api/statistics`), raw SQL (`POST /api/sql`). Health header `Orphans/Dup labels/Isolated/Ephemeral` pills via `/api/manager_health` — click to filter (Dup shows dup-label nodes, Isolated/Orphans shows isolated nodes, Ephemeral shows `ephemeral_events` table), click again to clear. | `core` single-DB |
| 10 | `system` | System | `system.js` | Snapshots table (per-DB, `GET /api/snapshots?db=`; Restore with pre-rollback backup + health verification + Delete via `POST /api/restore_snapshot|delete_snapshot`), audit-log viewer (`/api/logs`, `/api/log_content`) with styled cards (`fmtLog` parses `#`/`##`/`-` into system colours, tags, `pre` blocks) + 3-column journals (Run/Core/Agents) + 100-deep execution history (`/api/history`). | `all` (selector) |
| 11 | `statistics` | Statistics | `statistics.js` | Per-DB + combined stats — `GET /api/statistics?db=` → nodes/edges/types/layers/sources/averages/top-labels. DB selector. | `all` |
| 12 | `config` | Config | `config.js` | Every live interval/threshold/weight (`GET /api/config`, `POST /api/config` whitelist + typed coercion, `prune_importance_floor`/`prune_threshold` alias synced). Includes `core_helper_*` toggle+thresholds (Master `core_helper_enabled` + 5 fields) + `agent_working_*` + `prune_empty_agents*`. Token setter, scheduler toggle (`POST /api/scheduler`, `cfg-sched-toggle` + `schedToggle()`, persists `cron_enabled` + auto-restores on `dashboard/server.py:45 configure()`), `Check & Auto-VACUUM` (`POST /api/check_vacuum`), `Reset Defaults` (`POST /api/config_reset`). | `all` |
| 13 | `mail` | Mail | `mail.js` | Operator mailbox console — user inbox (backoff-poll + badge via `GET /api/mailbox?scope=user&state=pending`), all-traffic newest 5 + mini graphs (by sender bar, by state donut), Log drawer (`openLog` → `GET /api/mailbox?scope=all` with search `mail-log-q` + state filter `pending/noted/acked`, per-row Open/Del, Reply via `mail-reply-area` + `sendReply(toScope)`), compose-as-`user` to `core|agent:<id>` (`POST /api/mailbox/send`), ack/delete/wipe (`/api/mailbox/ack|delete|wipe`). Sticky `[REVIEW READY]/[REVIEW QUEUE]` mails until ack/promote. Endpoints are scope-based, not `?db=`. | scopes, not `?db` |

Badges (polled via `App.asyncBadges()` after each `refresh()`):

- `graduate` — `GET /api/graduate_preview?limit=1` → `graduable` count.
- `contradicts` — `GET /api/contradictions?db=core|agents&status=pending&limit=1` → `counts.pending` (both DBs summed).
- `mail` — `GET /api/mailbox?scope=user&state=pending&limit=100` → `messages.length`.

## Auth (C8)

`brain/config.json → dashboard_token` is the single credential. Server: `server.py:243-254` (`DashboardHandler._check_auth`, `_unauthorized`) + `server.py:45 configure()` auto-starts scheduler when `cron_enabled` true.

| Surface | Token required? | Notes |
|---|---|---|
| `GET /`, `/index.html` | No | Shell HTML — public so the login form itself is reachable. |
| `GET /static/*` | No | JS/CSS assets — public. |
| `GET /wiki`, `/wiki/*` | No | Rendered wiki + `?raw=1` markdown — public for humans and AI. See `server.py:294-295`. |
| `GET /api/health`, `GET /api/ping` (+ `HEAD`) | No | Liveness probes — intentionally open (`_check_auth` early-return for these two paths). |
| Everything else (`/api/status`, `/api/config`, `/api/nodes`, all `POST /api/*`, etc.) | **Yes** when `dashboard_token` is set | Requires `X-Api-Token: <token>` header; missing/wrong → `401 {"error":"unauthorized: missing X-Api-Token"}`. |

Rules:

- **Header only** — `X-Api-Token`. `?token=` query param is not read and is rejected by design.
- **No localhost bypass** — when a token is set, `127.0.0.1` is not exempt; health/ping are the only bypass.
- **No CORS** — dashboard binds `127.0.0.1` and does not set CORS headers (local-only by default; use `--host` explicitly to expose).
- **401 UX** — `app.js:57-73` (`App.api`): on `401` the `#tokenbar` is shown, `Login` button label updated, and if no token is stored `App.login()` opens the modal automatically.
- **Login modal** — `app.js:14-44` (`App.login`, `App.doLogin`): password input + `Remember me` checkbox. Validation: `fetch("/api/status?db=all", {headers: {"X-Api-Token": t}})` — `401` → "Wrong token", otherwise stored and `App.refresh()` re-tried.
- **Remember me** — `sessionStorage` by default (cleared when tab closes); when checked, `localStorage` (persists across restarts). `App.token()` checks `sessionStorage` first, then `localStorage`. `App.logout()` clears both and re-shows `#tokenbar`. `App.remembered()` reports the `localStorage` state for the checkbox default.
- **Rotation** — change `dashboard_token` in `brain/config.json` (or Config tab → set `dashboard_token` → Save). `POST /api/config` with `{"dashboard_token": "..."}` or `{"reload": true}` re-reads the file without restart.

## `?db=` defaults matrix

Every remaining endpoint takes `?db=` (`core|agents|all`, case-insensitive); invalid value falls back to the default. Manager/Graph/nodes-style endpoints are **single-DB** by design (selector in the tab itself).

| `?db` default | Endpoints |
|---|---|
| `all` | `GET /api/status`, `GET /api/snapshots`, `GET /api/bloat`, `GET /api/ephemeral_candidates`, `GET /api/ephemeral_stats`, `GET /api/statistics` (plus `POST /api/create_snapshot`, `/api/vacuum`, `/api/compact_ephemeral`, `/api/check_vacuum`, `/api/rebuild_vectors` where `db`/`target` in body defaults to `both`/`all`) |
| `core` (single-DB) | `GET /api/contradictions` (defaults `core`; `all` is invalid and falls back to `core`), `GET /api/graph`, `GET /api/nodes`, `GET /api/edges`, `GET /api/manager_health`, `GET /api/vectors`, `GET /api/layers`, `GET /api/path`, `GET /api/schema` (+ `POST /api/node_update|delete|add`, `/api/bulk_nodes`, `/api/edge_add|delete`, `/api/sql` — `db` in body defaults `core`) |
| `agents`-biased (implicit) | `GET /api/graduate_preview` (always `agents.db` via `agent_bridge`), `GET /api/agent_working_preview` (always `agents.db` regulator) |
| scopes (not `?db`) | `GET /api/mailbox?scope=&state=` (`scope=user|agent:<id>|broadcast|all`), `GET /api/agents` (list), `GET /api/history`, `GET /api/logs`, `GET /api/log_content`, `GET /api/config` (global) |

`POST` body `db`/`target` aliases: `_target_of(payload)` reads `payload.db` then `payload.target`, lower-cased, `all` → `both` for job routes. See `server.py:1008-1017`.

## Tab plugin contract — how to add a new tab module

Add a file `dashboard/static/modules/<name>.js` (no CDN, same-origin only):

```js
// dashboard/static/modules/my_tab.js
"use strict";
window.Tabs.my_tab = {
  // Called by App.show("my_tab") — render into the supplied #panel element.
  async render(el) {
    el.innerHTML = '<div class="card">Loading…</div>';
    try {
      const data = await App.api("/api/nodes?db=core&limit=10");
      el.innerHTML = '<div class="card"><h3>My Tab</h3>'
        + data.nodes.map(n => `<div>${App.esc(n.label)}</div>`).join("")
        + '</div>';
    } catch (e) {
      el.innerHTML = '<div class="card">Error: ' + App.esc(e.message) + '</div>';
    }
  },
};
```

Then register it in two places:

1. **`dashboard/static/dashboard.html`** — add `<script src="/static/modules/my_tab.js"></script>` after the other module tags (order does not matter, but keep it with the group).
2. **`dashboard/static/app.js:159`** — add `["my_tab", "My Tab"]` to `App.tabs` (controls nav button order, `data-tab`, and badge span `b-my_tab`).

Available shell API inside `render()`:

| API | Signature | Notes |
|---|---|---|
| `App.api(url, opts?)` | `async (url, {method, headers, body}) → JSON` | Injects `X-Api-Token` from `App.token()`, throws on `401` (auto-opens login modal) or non-2xx. Always use this — never raw `fetch`. |
| `App.show(key)` | `(key: string) → void` | Switch active tab and re-render. |
| `App.refresh(quiet?)` | `async (quiet?: bool) → void` | Re-fetches `/api/status?db=all`, updates `#statsbar`/pills/scheduler dot, then `asyncBadges()`; `quiet=false` toasts errors. |
| `App.toast(msg)` | `(msg: string) → void` | Bottom-right ephemeral banner. |
| `App.modal(html)` / `App.closeModal()` | `(html: string)` | Center overlay (`#modal`); click outside to close; `closeModal()` resets width and `App._loginOpen`. |
| `App.esc(s)` | `(any) → string` | HTML-escape. |
| `App.ago(ts)` / `App.fdate(ts)` / `App.when(ts)` | `(unix seconds)` | Relative/absolute time helpers. |
| `App.attTag(a)` / `App.metaTable(meta)` / `App.nodeDetail(n)` | — | Attention badge, metadata table, full node card (used by Manager/Graph detail drawers). |
| `App.setBadge(key, n)` | `(tabKey, count)` | Show/hide `#b-<key>` badge (capped at `99+`). |
| `App.token()` / `App.remembered()` | `() → string / bool` | Current token / whether `localStorage` holds it. |

Constraints: **no CDN, no `import`, no `fetch` outside `App.api`**. The shell never dynamically imports; keep modules as IIFEs assigning to `window.Tabs`.

## REST reference

Base: `http://127.0.0.1:8500` (or `--host`/`--port` override). All `POST` bodies are JSON; row writes (`node_update`, `node_add`, `bulk_nodes`, `graduation`, etc.) `commit()` under the call — snapshots only per `auto_snapshot_before_jobs` policy (no auto discover/rebuild per `Apply`, C15).

### GET (+ HEAD)

| Method | Path | `?query` | Default `?db` | Caps / notes |
|---|---|---|---|---|
| `GET` | `/`, `/index.html` | — | — | Serves `dashboard/static/dashboard.html` (`text/html`). Public. |
| `GET` | `/static/<rel>` | — | — | Jailed to `dashboard/static` via `safe_join`; allowed suffixes `.html .js .css .json .png .svg` else `404`. Public. |
| `GET` | `/wiki`, `/wiki/`, `/wiki/<rel>` | `?raw=1` for raw markdown | — | Jailed to `wiki/` via `safe_join`; renders `.md` → HTML with TOC/sidebar. `?raw=1` returns `text/markdown`. Public. |
| `GET`/`HEAD` | `/api/health` | — | — | `{"running":true,"status":"ok","timestamp":…,"core_db":…,"agents_db":…,"scheduler_running":bool,"port":…}`. Public, no token. `HEAD` returns `200` with no body. |
| `GET`/`HEAD` | `/api/ping` | — | — | `{"pong":true}`. Public, no token. |
| `GET` | `/api/status` | `?db=core\|agents\|all` | `all` | `{health, bloat, scheduler{running, interval_minutes, cron_enabled}, snapshots[10], history[5], logs[5], auto_snapshot}`. Token required. |
| `GET` | `/api/config` | — | — | `{"config": brain/config.json, "defaults": BRAIN_DEFAULTS}`. Token required. |
| `GET` | `/api/snapshots` | `?db=core\|agents\|all` | `all` | `{"snapshots": [{filename, db, created_at, size_mb, ...}]}`. `all` → both DBs; else filtered. |
| `GET` | `/api/history` | `?limit=` | — | `{"history": [{timestamp, jobs, target, status, duration_s}]}`. Default `20`, `limit` parsed as int. |
| `GET` | `/api/logs` | — | — | `{"logs": […]}` — `engine.list_logs()` (default 30 per config). |
| `GET` | `/api/log_content` | `?file=` | — | `{"filename":…, "content":…}` or `404 {"error":"File not found"}`. |
| `GET` | `/api/bloat` | `?db=core\|agents\|all` | `all` | `engine.get_bloat_metrics(db)` → `{freelist_pct, needs_vacuum, freelist_pages, ...}` per-DB or combined. |
| `GET` | `/api/ephemeral_candidates` | `?db=core\|agents\|all&min_count=3` | `all` | `{"candidates": [{label, count, db}], "allowlist": […]}`. `min_count` default `3`. Filtered by `db` when not `all`. |
| `GET` | `/api/ephemeral_stats` | `?db=core\|agents\|all` | `all` | `engine.get_ephemeral_stats(db)`. |
| `GET` | `/api/statistics` | `?db=core\|agents\|all` | `all` | `engine.get_full_statistics(db)` → nodes/edges/types/layers/sources/averages/top-labels. |
| `GET` | `/api/contradictions` | `?db=core\|agents&status=pending\|confirmed\|ignored\|all&limit=` | `core` | `engine.get_contradictions(status, limit, db)` → `{contradictions, counts}`. Default `limit 50`. Single-DB. |
| `GET` | `/api/graduate_preview` | `?limit=` | — | `{"total_agent_notes","review_ready","agent_private","graduable","total","notes":[…],"graduable_preview":[…]}`. Default `100`. Always `agents.db`. |
| `GET` | `/api/agent_working_preview` | — | — | `engine.get_agent_working_preview()` — per-agent WORKING scores. Always `agents.db`. |
| `GET` | `/api/graph` | `?db=core\|agents&limit=` | `core` | `{"db", "limit", "truncated", "nodes":[…],"edges":[…]}`. Clamped `10 ≤ limit ≤ 2000` (default `300`); `edges` capped `limit*3`; only edges where both endpoints in sampled `nodes` are returned; `truncated=true` when caps hit. |
| `GET` | `/api/nodes` | `?db=core\|agents&q=&type=&scope=CORE\|AGENT&attention=&layer=&source=&agent=&sort=&dir=&offset=&limit=` | `core` | `{"db","total","offset","limit","nodes":[…]}`. `limit` `1..200` default `50`; `offset` default `0`; `q` matches `label LIKE %q%` OR `content LIKE` OR `node_id=`; `type` exact UPPER; `scope`/`attention`/`layer`/`agent` filters via `json_extract(metadata…)`; `sort` allowlist (`node_id label content node_type trust_level importance access_count created_at updated_at source layer`) else `created_at` (default `created_at DESC` newest creation first, C22); `dir` `asc/desc` (also `1/-1`); nodes enriched with `layer`, `last_access`, `_scope`, `_attention`, `_agent`. |
| `GET` | `/api/edges` | `?db=core\|agents&type=&node=&offset=&limit=` | `core` | `{"db","total","offset","limit","edges":[…]}`. `limit` `1..200` default `50`; `type` UPPER exact; `node` matches `from_node` OR `to_node`; edges include `from_label`/`to_label`. |
| `GET` | `/api/agents` | — | — | `{"agents": agent_store.agent_list(conn)}` — from `agents.db` (idempotent). |
| `GET` | `/api/mailbox` | `?scope=all\|user\|agent:<id>\|broadcast&state=pending\|noted\|all&limit=&offset=` | — | `{"messages": […], "limit", "offset"}` via `agent_mailbox.read(mark_read=False)` then `commit`. Backoff-poll friendly. |
| `GET` | `/api/manager_health` | `?db=core\|agents` | `core` | `{"db","total_nodes","total_edges","orphans":[],"orphans_count","dupes":[],"dupes_count","isolated":[],"isolated_count","ephemeral_events"}`. Orphan edges, dup labels, degree-0 isolates. |
| `GET` | `/api/vectors` | `?db=core\|agents&q=&offset=&limit=` | `core` | `{"db","total","offset","limit","vectors":[{node_id,label,magnitude,top_terms[10],term_count}]}`. `limit` `1..200` default `50`; `q` filters `n.label LIKE %q%` OR `node_id=`; empty when `node_vectors` table missing. |
| `GET` | `/api/layers` | `?db=core\|agents` | `core` | `{"db","layers": {working:[…], short_term:[…], long_term:[…], archive:[…]}}` grouped by `memory_layers.layer`. Empty when table missing. |
| `GET` | `/api/path` | `?db=core\|agents&from=&to=` (each label or node_id) | `core` | BFS undirected over `edges`; resolves `label→node_id` first; `{"db","found":bool,"hops","path","steps":[{from,from_label,edge_type,to,to_label}],"from_resolved","to_resolved"}`. `400` if `from`/`to` missing. |
| `GET` | `/api/recall` | `?q=&agent=&mode=&bound=&offset=&include_agent_notes=` | `core` | LogicEngine-healed unified recall `dashboard/server.py:1053 _api_recall`; `agent` scopes via `_resolve_agent_param`, `FIND` DSL auto-detect, returns `healed` map. |
| `GET` | `/api/empty_agents` | `?` | — | `{"total_empty","eligible","min_age_hours","cutoff_ts","agents":[…]}` via `engine.get_empty_agents()`; `age-gated` preview for ghost cleanup. |
| `GET` | `/api/schema` | `?db=core\|agents` | `core` | `{"db","tables": [{name,sql,columns:[PRAGMA table_info], indexes:[PRAGMA index_list]}], "schema_meta": [{key,value}]}` from `schema_meta` table. |

### POST (`Content-Type: application/json`)

| Path | Body (`db`/`target` alias) | Effect | Caps / notes |
|---|---|---|---|
| `POST /api/run_job` | `{"jobs": ["dedup","compact",…] \| null, "target": "core\|agents\|both\|all"}` | `scheduler.run_job_now(jobs, target)` (canonical order). `jobs=null/omitted` → FULL defaults. `all`→`both`. | `{status:"success", run:{jobs,target,results,health_after,duration_s}}` |
| `POST /api/scheduler` | `{"enabled": bool, "interval_minutes"?: int, "max_unused_days"?: int}` | Writes `engine.config` + `engine._save_config()`, then `scheduler.start(interval)` or `scheduler.stop()`. | `{status:"success", config, is_running}` |
| `POST /api/config` | `{"reload": true}` **or** `{"<key>": value, …}` — whitelist see `server.py:1043` (`_CONFIG_TYPES`) | `reload` → re-read `brain/config.json` from disk (hand-edits go live). Else typed coercion per `_coerce` (`bool` accepts `1/true/yes/on`), `prune_importance_floor`/`prune_threshold` alias synced, then `_save_config()`. Unknown keys ignored. | `{status:"success", config, reloaded?:true}`; invalid value → `400` |
| `POST /api/config_reset` | `{}` | `engine.reset_config()` (defaults → save). | `{status:"success", config}` |
| `POST /api/create_snapshot` | `{"db": "core\|agents\|both\|all"}` | `engine.create_snapshot(db)` per requested DBs. | `{core: {...}, agents: {...}}` or single-DB dict |
| `POST /api/rebuild_vectors` | `{"db": "core\|agents\|both\|all"}` | `engine.rebuild_vectors(target)` | Per-DB result |
| `POST /api/restore_snapshot` | `{"db": "core\|agents", "filename": "…"}` | `engine.restore_snapshot(db, filename)` (pre-rollback backup + health verify). | `db` defaults `core`; `filename` required |
| `POST /api/delete_snapshot` | `{"filename": "…"}` | `engine.delete_snapshot(filename)` | `filename` required |
| `POST /api/vacuum` | `{"db": "core\|agents\|both\|all"}` | `engine.vacuum_db(target)` | Per-DB `{before_mb, after_mb}` |
| `POST /api/compact_ephemeral` | `{"db": "core\|agents\|both\|all", "keep_last"?: int, "max_age_days"?: int, "vacuum"?: bool (default true)}` | `engine.compact_ephemeral(target, keep_last, max_age_days)`; if `removed_total>0` and `vacuum` true → `vacuum_db` + optional `rebuild_vectors` when `auto_rebuild_vectors`. | `{per-db removed_*, vacuum?, vector_rebuild?}` |
| `POST /api/check_vacuum` | `{"db": "core\|agents\|both\|all"}` | `get_bloat_metrics` → if any `needs_vacuum` → `vacuum_db` + optional `rebuild_vectors` → `bloat_after`. | `{"triggered": bool, "bloat", "vacuum"?, "vector_rebuild"?, "bloat_after"?}` |
| `POST /api/graduate` | `{"node_ids": ["…"] \| "…", "node_id": "…", "agent_id"?: "…"}` | `engine.graduate_agent_notes(node_ids, agent_id)` — move semantics + provenance. | Only `agents.db`; promotion maintains vectors incrementally (no rebuild) |
| `POST /api/demote` | `{"node_ids": ["…"] \| "…", "node_id": "…"}` | `agent_store.agent_set_attention(conn, agent_id, node_id, "agent_private")` per id (owner `agent_id` resolved from `nodes.metadata`). | `{"status":"demoted","demoted":n,"total":m,"errors":[…]}`; `node_ids[]` required |
| `POST /api/regulate_agent_working` | `{"dry_run": bool\|"1"/"true"/"yes"}` | `engine.regulate_agent_working_memory(dry_run)` | `dry_run` default `false` |
| `POST /api/contradiction_action` | `{"edge_id": "…", "action": "confirmed\|ignored\|pending\|resolved\|confirm\|ignore\|delete\|keep_from\|keep_to\|merge", "db": "core\|agents"}` | `confirm`/`ignore` aliased to `confirmed`/`ignored` → `update_contradiction_status`; `delete`/`keep_from`/`keep_to`/`merge` → `resolve_contradiction` (with internal rebuild when configured). | `db` defaults `core`; `edge_id`+`action` required |
| `POST /api/contradiction_auto_resolve` | `{"dry_run"?: bool, "db"?: "core\|agents"}` | `engine.auto_resolve_low_trust(dry_run, db)` | `dry_run` default `true`; `db` defaults `core` |
| `POST /api/contradictions/clear` | `{"db": "core\|agents\|both\|all"}` | `DELETE FROM edges WHERE edge_type='CONTRADICTS'` per DB. | `{"status":"cleared", core:{deleted:n}, agents:{…}}` |
| `POST /api/ephemeral_allowlist` | `{"labels": ["…"]}` **or** `{"action":"add\|remove","label":"…"}` | `engine.set_ephemeral_allowlist(labels)` (append/remove on global `engine.config.ephemeral_labels`). | One of the two shapes required |
| `POST /api/ephemeral_ignored` | `{"labels": ["…"]}` **or** `{"action":"add\|remove","label":"…"}` | `engine.set_ephemeral_ignored(labels)` — labels excluded from `ephemeral_candidates` (kept, not ephemeral). | Undocumented before; mirrors allowlist |
| `POST /api/prune_empty_agents` | `{"dry_run"?: bool, "min_age_hours"?: int}` | `engine.prune_empty_agents(target="agents", dry_run, min_age_hours)` — deletes ghosts, cleans `mailbox_receipts`. | `dry_run` default false |
| `POST /api/remember` | `{"content": "…", "node_type"?: "FACT", "label"?: "…", "trust"?: float, "importance"?: float, "agent"?: "hint", "metadata"?: obj}` | LogicEngine-healed `dashboard/server.py:1665 _post_remember` → `POST /api/node_add` style via `remember_many`. Returns `healed` map. | Unified Direct API |
| `POST /api/recall` | `{"query": "…", "agent"?: "hint", "mode"?: "RELATED", "bound"?: int, "offset"?: int, "include_agent_notes"?: bool}` | Unified `dashboard/server.py:1704 _post_recall` — LogicEngine heal + scoped `agent_recall` or `recall` + optional bridge merge. | Unified Direct API |
| `POST /api/node_update` | `{"db":"core\|agents","node_id":"…","fields":{"label"?:…,"content"?:…,"trust_level"?:…,"importance"?:…,"source"?:…,"metadata"?: object\|JSON string}}` | `core_nodes.update_node(conn, node_id, **fields)` then `commit` (vectors/index maintained incrementally). | `db` must be `core|agents`; `fields` allowlist only; invalid fields → `400`; `metadata` string is JSON-parsed |
| `POST /api/node_delete` | `{"db":"core\|agents","node_id":"…"}` | `core_nodes.delete_node(conn, node_id)` + `commit`. | `{status:"deleted"\|"not found"}` |
| `POST /api/node_add` | `{"db":"core\|agents","node_type"?: "FACT" (UPPER), "label"?: "…", "content": "…", "trust"?: float, "importance"?: float, "agent_id"?: "…" (required for agents.db)}` | `core_nodes.remember_many([item], extra_cols={"agent_id":…})` / `agent_store.require_agent` when `agents.db`; `commit`; returns created node. | `content` required; `agent_id` required + validated for `agents.db` |
| `POST /api/bulk_nodes` | `{"db":"core\|agents","node_ids": ["…"], "action":"delete\|set_attention\|set_layer\|relabel", "value"?: string}` | Loops `node_ids[]`: `delete`→`delete_node`; `set_attention`→`metadata.attention_state` (allow `"__clear"`/empty to clear, else `agent_private/review_ready/core_verified`); `set_layer`→`INSERT OR REPLACE memory_layers` (`working/short_term/long_term/archive`); `relabel`→`label=` + `build_index`. Then `commit`. | `node_ids[]` + `action` required; invalid combos → `400`; returns `{status, deleted|updated, total, errors}` |
| `POST /api/edge_add` | `{"db":"core\|agents","from_node":"…","to_node":"…","edge_type"?: "RELATES_TO" (UPPER), "weight"?: 1.0, "agent_id"?: "…" (required for agents.db)}` | `core_edges.relate` / `agent_store.agent_relate` (isolation-checked) + `commit`. | `from_node`+`to_node` required; `weight` clamped by callee |
| `POST /api/edge_delete` | `{"db":"core\|agents","edge_id":"…"}` | `DELETE FROM edges WHERE edge_id=?` + `commit`. | `{status:"deleted"\|"not found"}` |
| `POST /api/sql` | `{"db":"core\|agents","sql":"SELECT …\|PRAGMA …\|EXPLAIN …"}` | Read-only `conn.execute(sql)`; `cols` from `cursor.description`; `rows` up to 200. | Only `SELECT/PRAGMA/EXPLAIN` (case-insensitive prefix check); otherwise `400`; truncated flag when >200 |
| `POST /api/mailbox/send` | `{"from":"…","to":"…","body":"…"}` | `agent_mailbox.send(conn, from, to, body)` + `commit`. | All three required; `from/to` scopes `core|agent:<id>|broadcast|user` (broadcast only from core) |
| `POST /api/mailbox/ack` | `{"scope"?: "…", "msg_ids"?: ["…"] \| JSON-string}` | `agent_mailbox.ack(conn, scope, msg_ids)` + `commit`. | `msg_ids` may be stringified JSON |
| `POST /api/mailbox/delete` | `{"msg_id":"…"} \| {"msg_ids":["…"]}` | `DELETE FROM mailbox WHERE msg_id=?` loop for bulk (when `msg_ids` length>1). | `{deleted, msg_id?}` |
| `POST /api/mailbox/wipe` | `{}` | `DELETE FROM mailbox` + `commit`. | `{"wiped": n}` |

Errors: handler not found → `404`; `ValueError` from validation → `400 {"status":"error","message":…}`; unhandled exception → `500 {"status":"error","message":…}`. Non-JSON bodies are treated as `{}`.

### Jailed paths

`server.py:59-68` `safe_join(root, *parts)`:

```python
root = Path(root).resolve()
target = root.joinpath(*parts).resolve()
if target != root and root not in target.parents:
    raise ValueError(f"path escapes jail: {parts!r}")
```

- Applied to every file-serving endpoint (`/static/*` jailed to `dashboard/static`, `/wiki/*` jailed to `wiki/`). `..` or absolute escapes → `ValueError` → `404`.
- `/static/*` also enforces allowlist suffixes `.html .js .css .json .png .svg` and `is_file()` check before serve.
- All API routes that accept `db`/`scope`/`file` parameters use lower-cased allowlists, never path-joined raw.

### Caps summary

| Resource | Default | Min | Max | Truncation |
|---|---|---|---|---|
| `/api/graph?limit=` | 300 | 10 | 2000 | `truncated=true` when `nodes>=limit` or `edges>=limit*3`; edges capped `limit*3` |
| `/api/nodes?limit=` | 50 | 1 | 200 | `total` returned; standard `offset/limit` pagination |
| `/api/edges?limit=` | 50 | 1 | 200 | same |
| `/api/vectors?limit=` | 50 | 1 | 200 | same |
| `POST /api/sql` | — | — | 200 rows | `truncated=true` when `>200` |
| `/api/history?limit=` | 20 | — | unbounded (int) | — |
| `/api/contradictions?limit=` | 50 | — | unbounded | — |
| `/api/graduate_preview?limit=` | 100 | — | unbounded | — |

## Deleted routes (all `404`)

Removed in v3 (Idea.md rev8 C4); any request to these returns `404 Not Found` (no `_POST_ROUTES`/`do_GET` entry):

| Deleted | Notes |
|---|---|
| `GET /humantools/*`, `POST /humantools/*` | v2 slot-connector routes — removed. |
| `POST /api/switch_db` | Per-request `?db=` model replaces active-DB switching. |
| `GET /api/db_bytes` | Binary bytes endpoint removed — DBs are dual-DB only. |
| `POST /api/manager_commit` | Binary commit endpoint removed — Manager edits now commit via `POST /api/node_update` (incremental vectors, snapshot per policy). |
| Per-file `agent_*.db` discovery | Single `agents.db` replaces per-agent files — no per-file scan; `GET /api/agents` lists identities. |

## See also

- [OPERATIONS](OPERATIONS.md) — how to run the dashboard and connect the MCP server.
- [MCP_TOOLS](MCP_TOOLS.md) — 23 MCP tools (stdio JSON-RPC) — the AI surface (dashboard is the human surface).
- `dashboard/server.py:1-18` docstring — contract header (deleted endpoints, `?db=` matrix, auth, jailed paths, convergence).
