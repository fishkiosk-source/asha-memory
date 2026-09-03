# Asha Brain Dashboard — Documentation

**What it is:** Pure Python `http.server` web console at `brain/brain_dashboard.py` — zero dependencies, dark manager-style UI (`#0d0d0d` / `#1a1a1a`) for humans to operate the Brain without CLI. Binds `0.0.0.0:8500` (auto-increments to 8501… if busy). Token-gated (`dashboard_token` `brain_config.json`, `X-Api-Token` / `?token=` `brain_dashboard.py:35`; `127.0.0.1` bypasses, use `--bind 127.0.0.1` for local-only P2-4). Serves `brain/static/dashboard.html` at `/_static/` with fallback inline `HTML_DASHBOARD` `brain_dashboard.py:553` (`brain_dashboard.py:79`).

**What it does:** Live view + control for the target `core.db` (auto-discovered via `BrainEngine.resolve_db_path()` or `brain_config.json: last_db_path`). No AI loop — direct SQLite ops with safety snapshots and audit logs.

## Quick Start

```bash
python brain/brain_dashboard.py            # http://localhost:8500
python brain/brain_dashboard.py --help     # via BrainScheduler
# health check (for AI/human before assuming down):
curl http://localhost:8500/api/health
curl http://localhost:8500/api/ping   # -> {"pong":true}
curl -I http://localhost:8500/api/health  # HEAD also 200
```

## Tabs

| Tab | Functionality |
|-----|-----|
| **Overview** | Target DB switcher (auto-discovers `**/*.db` + manual path, Copy/Download), Health & Bloat (ephemeral/contradicts/freelist), Recent history (5) |
| **Maintenance** | `Deduplicate` / `Prune Stale` / `Manage Tiers` / `Contradictions` / `Discover Links` / `Run FULL` + `Snapshot Now` + `Compact Ephemeral` / `VACUUM` (freelist % hint) + result JSON |
| **Graduate** | Per-node + bulk curation: `total/review_ready/graduable/private`, checkboxes `Graduate Selected`/`Graduate` per row, `Graduate All Graduable` (`POST /api/graduate {node_ids}` bypasses trust/imp) |
| **Observer** | Agent-only `WORKING` janitor: `Score=acc*Wa+imp*Wi-ageH*Wd`, `Days left` `(max_age-ageH)/24`, `demote_next/stale_soon/keep/protected`, `Preview`/`Regulate Now` (`POST /api/regulate_agent_working {dry_run}`), `GET /api/agent_working_preview` |
| **Contradicts** | Curation queue `pending/confirmed/ignored/all` with `confidence`, `overlap_words` (filtered `len>3` + stopword `shared_lexicon.py:56`), trust gap `0.3 vs 0.8`. Actions `Confirm`/`Ignore`/`Delete`/`Keep From`/`Keep To`/`Merge`. `Preview Auto-resolve` (dry) / `Auto-resolve low-trust` (opt-in) |
| **Ephemeral** | Allowlist chips (× to remove, unified 10 labels `shared_lexicon.py:86` + `config.json` sync) + `Scan Candidates` (freq/JSON% via unified `_looks_like_json_log` `[:400]`/score/reasons, never auto-deletes) → `Add` to `brain_config.json` + core `config.json` |
| **Graph** | Embedded `humantools/asha_graph.html?embedded=1` — `Load Active DB` via `postMessage({type:'asha-load-db', buffer})` + `/api/db_bytes`, respects `ephemeral_labels` from `/api/config` |
| **Manager** | Embedded `humantools/asha_manager.html?embedded=1` — `Load Active DB` + `Apply to DB` (dashboard button or Manager's `Apply to DB` when embedded). Apply does `POST /api/manager_commit` (binary SQLite), then auto `discover_links` + `rebuild_vectors` + snapshot. Direct add/edit/delete inside Manager are now live, not download-only. |
| **System** | `Snapshots & Rollback` (Restore + **Delete** per row via `POST /api/delete_snapshot`), `Audit Logs` viewer, `Execution History` |
| **Statistics** | Full `get_full_statistics()`: nodes/edges, core/agent, trust/imp avg, bloat, node/edge/layer/source breakdowns, top labels |
| **Config** | `Interval` / `Unused Age` / `Dedup Similarity` / `Prune Floor` / `Keep Last` / `Max Age` / `Vacuum Freelist %/Min Pages` / `Contra Low/High` / `Agent High-water/Demote batch/Max age/Wa/Wi/Wd` + toggles `auto_snapshot`/`auto_rebuild`/`vacuum_after_prune`/`contradiction_auto_resolve`/`agent_working_regulator_enabled` + `Save`/`Reset` + `Check & Auto-VACUUM` |

Stats bar always visible: `Nodes | Core/Agent | Edges | DB Size | Snapshots | Bloat`.

## REST API

```
GET  /api/health          # lightweight {running,status,timestamp,iso,db_path,pid,port} — no DB scan
GET  /api/ping            # {pong:true}
HEAD /api/health / /      # 200 if up
GET  /api/status          # {health,bloat,scheduler,databases,snapshots,history,logs} (heavy)
GET  /api/config / POST /api/config / POST /api/config_reset
GET  /api/bloat, /api/statistics, /api/ephemeral_candidates, POST /api/ephemeral_allowlist
GET  /api/contradictions?status=&limit=  POST /api/contradiction_action  POST /api/contradiction_auto_resolve
POST /api/check_vacuum, /api/vacuum, /api/compact_ephemeral, /api/rebuild_vectors
POST /api/create_snapshot /api/restore_snapshot /api/delete_snapshot
GET  /api/db_bytes  POST /api/manager_commit (application/octet-stream or JSON {data:base64})
GET  /api/snapshots /api/logs /api/log_content?file=  /api/history /api/databases /api/graduate_preview  POST /api/graduate {node_ids?}  GET /api/agent_working_preview  POST /api/regulate_agent_working {dry_run}
POST /api/switch_db /api/run_job /api/scheduler
GET  /humantools/*
GET  /_static/dashboard.html  # static split (fallback inline HTML_DASHBOARD brain_dashboard.py:553)
```

All JSON endpoints send `Access-Control-Allow-Origin: *`.

## Manager Live Flow

1. Manager tab → `Load Active DB` → dashboard fetches `/api/db_bytes` → `postMessage` to iframe → manager `loadFromBytes()` + `/api/config` for ephemeral list.
2. Edit nodes/edges inside Manager (dirty flag).
3. Dashboard `Apply to DB` (or Manager's `Apply to DB` when embedded) → manager `db.export()` → `postMessage({type:'asha-manager-commit', buffer})` → dashboard `POST /api/manager_commit` → `BrainEngine.commit_manager_db()` (header check, `integrity_check`, snapshot, atomic replace) → `discover_links` → `rebuild_vectors` → toast + `fetchStatus()`.

## Health Check for AI

Before probing `8500`, use cheap endpoint:

```bash
curl -s http://localhost:8500/api/health | jq
# {"running":true,"status":"ok","timestamp":..., "db_path":".../core.db","pid":1234,"port":8500}
```

If `running` false or timeout → dashboard down. No DB scan, no lock contention.

## File Layout

`brain_dashboard.py` (handler + `HTML_DASHBOARD:553` + `/_static/` serve `:79`), `static/dashboard.html` (extracted UI), `brain_engine.py` (engine), `scheduler.py`, `brain_config.json` (10-label allowlist + `sqlite_cache_size` + `dashboard_token`), `DASHBOARD.md` (this file).
