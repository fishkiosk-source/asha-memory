# Operations (runbook)

All commands run from the v3 root (`asha_memory` unless noted). The engine uses two DBs (`memory/core.db` + `memory/agents.db`) and one live config (`brain/config.json`). See [ARCHITECTURE](ARCHITECTURE.md) for layout.

## Start / stop

```powershell
# Dashboard (human UI) — binds 127.0.0.1:8500, converges both schemas on start.
# First run creates memory/*.db + schemas automatically.
python -m dashboard.server --port 8500
# with explicit dirs (all optional, defaults shown):
python -m dashboard.server --port 8500 --memory-path ./memory --brain-dir ./brain --host 127.0.0.1

# MCP server (AI clients, stdio JSON-RPC) — defaults to ./memory (live data).
python -m src.mcp.server --memory-path ./memory

# Brain runs headless (no dashboard) — via dashboard Maintenance tab is preferred, or:
python -c "from brain.engine import BrainEngine; from brain.scheduler import BrainScheduler; e=BrainEngine(); BrainScheduler(engine=e).run_job_now()"
python -c "from brain.engine import BrainEngine; e=BrainEngine(); print(e.health('all'))"
```

Keep exactly one writer stack at a time per DB — MCP + dashboard + brain coordinate via `memory/.lock` (file lock, `timeout 120s`, `busy_timeout 8000ms` `src/core/store.py:19`). Do not run two dashboards or two MCPs against the same `memory/` concurrently.

## How to run the dashboard

1. **Prerequisites** — Python 3.10+, stdlib only (no `pip install` needed). The repo root must contain `brain/`, `dashboard/`, `memory/` (created if missing), `src/`.
2. **Launch** — from the v3 root:
   
   ```powershell
   python -m dashboard.server --port 8500
   # custom:
   python -m dashboard.server --port 8500 --memory-path ./memory --brain-dir ./brain --host 127.0.0.1
   ```
   
   Flags (`dashboard/server.py:2134-2139`):

    | Flag            | Default                                                     | Notes                                                                                |
    | --------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------ |
    | `--port`        | `8500`                                                      | TCP port                                                                             |
    | `--host`        | `127.0.0.1`                                                 | Bind address — `127.0.0.1` is local-only; set `0.0.0.0` only behind a reverse proxy. |
    | `--memory-path` | `<v3root>/memory` (`src.core.store.core_db_path().parent`)  | Directory holding `core.db`, `agents.db`, `.lock`                                    |
    | `--brain-dir`   | `<v3root>/brain` (`dashboard/server.py:_default_brain_dir`) | Directory holding `config.json`, `snapshots/`, `history/`, `logs/`                   |
3. **What happens on start** — `start_dashboard()` in `server.py:2106` opens a `BrainEngine(core_path=memory/core.db, agents_path=memory/agents.db, brain_dir=brain)`, then converges schemas: `migrate(core_conn)` + `agent_store.ensure_schema(agents_conn)` (idempotent; first run creates the DBs). Then `configure(engine, BrainScheduler(engine))` — which auto-restores the scheduler if `brain/config.json:cron_enabled==true` (`server.py:45 configure()` + `dashboard/static/modules/config.js:124 toggle`) — and `ThreadingHTTPServer((host,port), DashboardHandler).serve_forever()`.
4. **Open** — `http://127.0.0.1:8500/` (or `http://<host>:<port>/`). `/`, `/static/*`, `/wiki/*`, `/api/health`, `/api/ping` are public; everything else needs the token header (see Auth below).
5. **Stop** — `Ctrl-C` in the terminal (handler is `KeyboardInterrupt` → exit `0`). Or close the terminal. No daemon — use a process manager if you need persistence.

## Auth — token setup

The single credential is `brain/config.json → dashboard_token` (`server.py:243-254` `_check_auth` + `45 configure()` scheduler auto-restore).

```powershell
# 1. Generate / set a token
# Option A — Config tab (while dashboard is running and you have the old token):
#   Config tab → dashboard_token field → new value → Save

# Option B — edit the file directly (no restart needed when using reload):
#   PowerShell:
notepad brain\config.json
#   set "dashboard_token": "asha-sam-<random>"
#   then POST reload (or restart dashboard):
#   curl -X POST http://127.0.0.1:8500/api/config -H "Content-Type: application/json" -H "X-Api-Token: <old-token>" -d '{"reload": true}'

# Option C — one-liner (PowerShell):
python -c "import json, pathlib; p=pathlib.Path('brain/config.json'); j=json.loads(p.read_text()); j['dashboard_token']='asha-sam-19222'; p.write_text(json.dumps(j, indent=2))"
```

Protect the file:

```powershell
# Unix/macOS:
chmod 600 brain/config.json

# Windows — remove inherited access, grant only current user (PowerShell as admin):
icacls brain\config.json /inheritance:r
icacls brain\config.json /grant:r "$env:USERNAME:(R,W)"
```

Behavior:

- When `dashboard_token` is non-empty, every API route except `/api/health` and `/api/ping` requires `X-Api-Token: <token>` header — including `127.0.0.1`. `?token=` is never read (rejected by design). Missing/wrong → `401 {"error":"unauthorized: missing X-Api-Token"}`.
- When `dashboard_token` is `""` (empty), auth is disabled — all routes are open (only do this on `127.0.0.1` binds).
- **To rotate:** set the new value via Config tab or file + `POST /api/config {"reload":true}`. Clients must re-login.
- **To disable:** set `dashboard_token` to `""` and Save (or `POST /api/config {"dashboard_token":""}`).
- **Browser UX:** 401 shows `#tokenbar` and auto-opens the login modal (`app.js:57-73`). The modal stores the token in `sessionStorage` by default, or `localStorage` when `Remember me` is checked (see [DASHBOARD](DASHBOARD.md) — Auth).
- **`brain/config.json` is the only live config** — `memory/config.json` (if present) is informational and never read (`brain/config.json:_note`).

## How to connect to MCP

The MCP server is a **stdio JSON-RPC 2.0** process — no HTTP, no token, no port. It speaks the same protocol as `v2 asha_mcp.py` minus the skills-registry bloat.

### Run

```powershell
# From the v3 root — memory dir is the single arg:
python -m src.mcp.server --memory-path ./memory
# defaults to <v3root>/memory if omitted (src/mcp/server.py:154-159)
```

What it does (`src/mcp/server.py:154-173`):

- `tools.open_context(memory_dir)` — opens both DBs, converges schemas, primes the file lock.
- `MCPServer(ctx)` — handles lines on `stdin`, replies on `stdout`, diagnostics on `stderr`.
- Lifecycle: `initialize` → `notifications/initialized` → `tools/list` / `tools/call` / `resources/list|read` / `ping`. Unknown tool/bad args → `ValueError` → `TOOL_EXECUTION_ERROR (-32002)`.

### Protocol

- **Transport:** stdio (one JSON object per line, `Content-Type: application/json`).
- **Methods:** `initialize`, `notifications/initialized`, `notifications/cancelled`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `ping`. Unknown method → `METHOD_NOT_FOUND (-32601)`.
- **Resources:** `asha://memory/stats`, `asha://memory/health`, `asha://memory/profile` (see [MCP_TOOLS](MCP_TOOLS.md)).
- **Errors:** `PARSE_ERROR -32700`, `INVALID_REQUEST -32600`, `METHOD_NOT_FOUND -32601`, `INVALID_PARAMS -32602`, `INTERNAL_ERROR -32603`, `TOOL_NOT_FOUND -32001`, `TOOL_EXECUTION_ERROR -32002`, `RESOURCE_NOT_FOUND -32003`.

### Client config example

Add one entry to your MCP client's config file (Claude Desktop, VS Code, Cursor, etc.). Paths must be absolute.

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json` on Windows, `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "asha-memory": {
      "command": "python",
      "args": ["-m", "src.mcp.server", "--memory-path", "D:\\opencode\\asha_memory\\asha_memory v3\\memory"],
      "cwd": "D:\\opencode\\asha_memory\\asha_memory v3"
    }
  }
}
```

**Generic MCP client** (`mcp.json` / `settings.json`):

```json
{
  "mcpServers": {
    "asha-memory": {
      "command": "python",
      "args": ["-m", "src.mcp.server", "--memory-path", "./memory"],
      "cwd": "/absolute/path/to/asha_memory v3",
      "env": {}
    }
  }
}
```

Notes:

- `command` must resolve to the Python that owns the repo's dependencies (use `python` or an absolute `venv/Scripts/python.exe`).
- `cwd` **must** be the v3 root so `src.mcp.server` resolves; `memory/` is then relative to `cwd` if not absolute.
- No token/env needed — MCP is direct DB access via the file lock (same lock as the dashboard, so the client and dashboard can run together, but not two MCPs against the same `memory/`).
- Success `tools/call` results are `{content:[{type:"text", text: JSON.stringify(result, null, 2)}]}` and may include `mailbox_notice` (see [MAIL](MAIL.md) and [MCP_TOOLS](MCP_TOOLS.md)).

### MCP troubleshooting

| Symptom                                           | Check                                                                                                              |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Client shows `spawn ENOENT` / `command not found` | `python` not on PATH for the client process — use absolute `C:\Python312\python.exe` or `venv\Scripts\python.exe`. |
| `ModuleNotFoundError: No module named 'src'`      | `cwd` not set to the v3 root — the `src/` package must be importable.                                              |
| `memory/.lock` timeout on start                   | Another dashboard/MCP/brain holds the lock — stop the other process or point `--memory-path` at a copy.            |
| `initialize` never returns                        | Write a single valid JSON line to stdin per request and flush — bare `echo` without newline will hang.             |
| `TOOL_EXECUTION_ERROR` on `agent_*`               | Unknown `agent_id` → `hint: call agent_ensure first` — call `agent_ensure` before `agent_remember/recall`.         |

## Backup / restore

### Snapshots (per-DB, `brain/snapshots/`)

- **Auto:** per `auto_snapshot_before_jobs` + `snapshot_cooldown_s` policy (coalesced; `snapshot_skipped: fresh_exists` is normal, not an error).
- **Manual:** System tab → `Snapshot Now` or `POST /api/create_snapshot {"db":"core|agents|both|all"}`.
- **Retention:** `keep_last_snapshots` (default `10`) per DB — rotation is automatic.
- **Format:** SQLite file copy + sidecar metadata (created_at, sha256).

### Restore

1. **Via System tab (preferred):** System tab → Snapshots table → `Restore` on the chosen row. The server automatically creates a pre-rollback backup, restores the file, verifies health (`engine.health(db)`), and reports the result.
2. **Via API:**
   
   ```powershell
   curl -X POST http://127.0.0.1:8500/api/restore_snapshot `
     -H "Content-Type: application/json" -H "X-Api-Token: <token>" `
     -d '{"db":"core","filename":"core-2026-09-04T12-00-00Z.db"}'
   ```
3. **Offline disaster case:**
   
   ```powershell
   # Stop everything (dashboard + MCP) first — otherwise .lock / WAL will conflict.
   # Copy aside:
   Copy-Item -Recurse memory memory.bak
   Copy-Item -Recurse brain\snapshots brain\snapshots.bak
   # Restore a snapshot file directly (stop the server first):
    Copy-Item brain\snapshots\snapshot_core_20260904_120000.db memory\core.db -Force
    Copy-Item brain\snapshots\snapshot_agents_20260904_120000.db memory\agents.db -Force
   # Or re-run migration from v2:
   python tools\migrate_v2_to_v3.py --force
   # Verify:
   python -c "from brain.engine import BrainEngine; e=BrainEngine(); print(e.health('all'))"
   ```

### Full export (MCP)

```json
// tools/call export
{"name":"export","arguments":{"path":"D:/backups/asha-2026-09-05.tar.gz"}}
```

- Produces a `.tar.gz` + sibling `.manifest.json` (versions, `sha256`, per-DB counts). Verify by restoring the tarball to a scratch `memory/` and running `health` on both DBs.

## The lock — `memory/.lock`

- Created on first engine open (`BrainEngine` / `tools.open_context`); held via `fcntl`/`msvcrt` file lock while the process is alive.
- `POST /api/run_job` and MCP `compact`/`vacuum`/`export` hold it for the duration of the job; concurrent `run_job_now` returns `skipped_busy` (not an error — wait and retry).
- **Rule:** one writer stack at a time per `memory/` directory — one dashboard, one MCP, or one `BrainScheduler` — not two. Readers via `GET` are concurrent.
- If you see persistent lock timeouts: `lsof memory/.lock` (Unix) or close the stray dashboard/MCP process consuming it. Do not delete `.lock` while a process is running; deleting it while stopped is harmless (recreated on next open).

## Files you never touch by hand

| Path                                                                 | Why not                                                               | Use instead                                                                           |
| -------------------------------------------------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `memory/core.db`                                                     | Live DB — WAL mode, FK, cache; direct edits risk integrity / lost WAL | Manager tab / MCP `update_node` / `src.core.nodes` helpers                            |
| `memory/agents.db`                                                   | Same                                                                  | Manager tab (agents selector) / MCP `agent_*` tools                                   |
| `memory/.lock`                                                       | File lock — deleting while held causes split-brain writes             | Let the engine manage it; only safe to delete while everything is stopped             |
| `memory/*.db-wal`, `memory/*.db-shm`                                 | WAL sidecars — part of the DB state                                   | Never copy `.db` without its `-wal`/`-shm` while running; use snapshots or stop first |
| `brain/snapshots/*`                                                  | Snapshot set — naming/rotation/health-coupled                         | System tab (Restore/Delete) or `POST /api/create_snapshot                             |
| `brain/job_history.json` (+ `brain/contradiction_resolutions.jsonl`) | Execution history — append-only                                       | System tab / `GET /api/history`                                                       |
| `brain/config.json` (bulk)                                           | Live config — typed coercion + alias sync (`prune_*`)                 | Config tab / `POST /api/config` / `POST /api/config {"reload":true}` after hand-edit  |

Safe to read (and, with care, edit while stopped): everything in `wiki/`, `ideas/`, `brain/logs/`, `tools/migrate_v2_to_v3.py`.

## Troubleshooting

| Symptom                                                              | Check                                                                                                                                                                                                                    |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `401` from dashboard / `unauthorized: missing X-Api-Token`           | Set the token: login modal → `X-Api-Token` header. `?token=` never works by design. `curl` needs `-H "X-Api-Token: <brain/config.json → dashboard_token>"`.                                                              |
| `403` or CORS error when exposing `--host 0.0.0.0`                   | Dashboard has no CORS — put a reverse proxy (nginx) in front and forward the `X-Api-Token` header.                                                                                                                       |
| `skipped_busy` in history                                            | Overlapping run refused — normal under rapid clicks or concurrent MCP+dashboard jobs; wait a few seconds, retry.                                                                                                         |
| `snapshot_skipped: fresh_exists`                                     | Snapshot cooldown (`snapshot_cooldown_s`, default `300s`) working — expected, not an error.                                                                                                                              |
| Graduate queue empty                                                 | No `review_ready` notes — agents must `set_attention(agent_id, node_id, "review_ready")` first; bulk graduate threshold is `review_ready` OR (`trust≥0.7` AND `importance≥0.6`).                                         |
| Recall slow on huge graphs                                           | `SEMANTIC` cap is `2000` node-vectors; run brain `discover` + `rebuild_vectors` from Maintenance, or lower `discover_scan_cap` / raise `discover_link_floor`.                                                            |
| `needs_vacuum` true (bloat)                                          | Config tab → `Check & Auto-VACUUM` (`POST /api/check_vacuum`) or Maintenance → Vacuum. Threshold is `vacuum_freelist_threshold_pct` (default `15%`) and `vacuum_freelist_min_pages` (`50`).                              |
| `freelist` keeps growing after `compact`                             | `compact` evicts `ephemeral_events` rows but frees pages only after `vacuum`; Maintenance `Compact Now` auto-vacuums when `removed_total>0` and `auto_rebuild_vectors` is on.                                            |
| Lock timeouts / `busy_timeout`                                       | Another brain/MCP holds `memory/.lock` — one stack at a time. Check `Get-Process python` / `lsof`.                                                                                                                       |
| `history` shows `integrity_check` failure                            | Stop everything, run `PRAGMA integrity_check` offline: `sqlite3 memory/core.db "PRAGMA integrity_check;"` and `memory/agents.db`; restore last healthy snapshot if `ok` is not returned.                                 |
| Dashboard shows `Scheduler ?` / `off`                                | Scheduler is on-demand unless `cron_enabled` + `interval_minutes` (default `60`) — Config tab → toggle `cfg-sched-toggle` on (persists `cron_enabled`, `dashboard/server.py:45` auto-restores on next launch), or `POST /api/scheduler {"enabled":true,"interval_minutes":60}`. `GET /api/status` reports `scheduler.running`. |
| `POST /api/config` returns `400 Invalid value`                       | Config whitelist + typed coercion (`_CONFIG_TYPES` in `server.py:1043`); check type (e.g. `interval_minutes` is `int`, `dashboard_token` is `str`).                                                                      |
| `POST /api/sql` returns `400 Only SELECT / PRAGMA / EXPLAIN allowed` | SQL endpoint is read-only by design — prefix must be `SELECT`, `PRAGMA`, or `EXPLAIN`. Use Manager tab edits for writes.                                                                                                 |
| Integrity doubt but no failure                                       | `POST /api/check_vacuum` (bloat), `GET /api/manager_health?db=core                                                                                                                                                       |

## See also

- [DASHBOARD](DASHBOARD.md) — 12 tabs, auth matrix, `?db` defaults, tab plugin contract, full REST reference, jailed paths, deleted routes, caps.
- [MCP_TOOLS](MCP_TOOLS.md) — 23 tools + resources — the AI surface (dashboard is the human surface).
- [MAIL](MAIL.md) — mailbox scoping and `mailbox_notice` flow.
- [ARCHITECTURE](ARCHITECTURE.md) — DB layout, brain phases, invariants.


### Direct provider (Hermes/OpenClaw, no MCP)

```python
from src.direct import DirectMemory
m = DirectMemory("./memory")  # or DirectMemory() -> <v3root>/memory
m.remember("hello", label="hi")
m.recall("hello")  # -> {clock, nodes:[{label_display, age}]}
m.agent_ensure("web scout")
m.mailbox_send("core","agent:xxx","hi")
m.close()
```
Same 23 tools as MCP `src/mcp/tools.py:138` dispatch, same `LogicEngine` heal, same `RLock` + `check_same_thread=False` for `turn`/`sync` threads. Use when co-located; keep `python -m src.mcp.server` for external stdio clients.
