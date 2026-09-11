# Dashboard REST API — Asha Memory v3

Standalone reference for the dashboard HTTP server (`dashboard/server.py`, stdlib `http.server`, `ThreadingHTTPServer`). Vanilla JS frontend, zero deps, binds `127.0.0.1` by default. This page mirrors `dashboard/server.py:_POST_ROUTES` + `do_GET` handlers.

Run from the v3 root:

```bash
python -m dashboard.server --port 8500 --memory-path ./memory --brain-dir ./brain
```

Wiki docs are public under `/wiki/*.md` (no token). Dashboard API below is token-gated when `dashboard_token` is set.

## Auth

- Header: `X-Api-Token: <token>` (browser keeps it in `sessionStorage`, prompts on 401).
- No `?token=` and no localhost bypass when a token is set.
- Open without token: `GET /api/health`, `GET /api/ping` (+ `HEAD`), `GET /`, `/static/*`, `/wiki/*`.

## Common query param `?db=`

Many endpoints take `?db=core|agents|all` (or `both` as alias for `all` on POST). Defaults:

| Default `all` | Default `core` (+toggle) | Default `agents` |
|---|---|---|
| Overview, Maintenance, System, Statistics, Config | Contradicts, Ephemeral, Graph, Manager | Graduate, Observer |

Single-DB endpoints (`Graph`, `Manager`, `nodes`, `edges`, `vectors`, `layers`, `path`, `schema`, `manager_health`) accept `core|agents` only (default `core`).

---

## GET routes

### `GET /` , `GET /static/*`, `GET /wiki/*`

Dashboard shell, static assets, and wiki markdown (rendered HTML or `?raw=1` for raw markdown).

### `GET /api/health` and `GET /api/ping` (+ `HEAD`)

Liveness. `ping` returns `{"pong": true}`. `health` returns:

```json
{"running":true,"status":"ok","timestamp":1720000000,"core_db":".../core.db","agents_db":".../agents.db","scheduler_running":false,"port":8500}
```

No auth required.

### `GET /api/status?db=`

Composite health+bloat+scheduler snapshot.

```json
{"health":{...},"bloat":{...},"scheduler":{"running":false,"interval_minutes":60,"cron_enabled":false},"snapshots":[...],"history":[...],"logs":[...],"auto_snapshot":false}
```

### `GET /api/config`

```json
{"config":{... brain/config.json ...},"defaults":{... brain/engine.py:DEFAULTS ...}}
```

### `GET /api/snapshots?db=`

`?db=core|agents|all` (all = both). Sorted newest first.

```json
{"snapshots":[{"filename":"snapshot_core_20260905_120000.db","size_bytes":123456,"created_at":"2026-09-05 12:00:00","mtime":1720000000}]}
```

### `GET /api/history?limit=`

`?limit=20` (default). Job history from `brain/job_history.json` via `BrainScheduler.get_history`.

### `GET /api/logs` and `GET /api/log_content?file=`

`logs` lists `brain/logs/*.md`; `log_content` returns the audit markdown for one file (404 if missing).

### `GET /api/bloat?db=`

Freelist / size metrics for the dashboard Config `Check & Auto-VACUUM` button.

### `GET /api/ephemeral_candidates?db=&min_count=`

`?min_count=3`. Labels in `ephemeral_events` not on `ephemeral_labels` allowlist (dashboard suggest). Filtered to `?db=` when not `all`.

### `GET /api/ephemeral_stats?db=`

```json
{"core":{"total":42,"labels":{"my_label":{"count":10,"newest":1720000000}}},"agents":{...}}
```

### `GET /api/statistics?db=`

Full stats via `BrainEngine.get_full_statistics` (nodes/edges/types/layers/sources/averages/top-labels, per-DB + combined).

### `GET /api/contradictions?status=&limit=&db=`

`?status=pending|confirmed|ignored|all` (default all), `?limit=50`, `?db=core|agents` (default `core`).

### `GET /api/graduate_preview?limit=`

`?limit=100`. Agents notes awaiting review.

```json
{"total_agent_notes":48,"review_ready":5,"agent_private":43,"graduable":7,"total":5,"notes":[{"node_id":"...","agent_id":"...","label":"...","content":"... truncated 200","trust_level":0.5,"importance":0.5,"attention":"review_ready","updated_at":1720000000}]}
```

### `GET /api/agent_working_preview`

Agents-only WORKING janitor preview. See `BrainEngine.get_agent_working_preview`:

```json
{"enabled":true,"high_water":12,"max_age_hours":48,"weights":{"wa":1.5,"wi":4.0,"wd":0.15},"demote_batch":5,"agent_working_count":8,"preview":[{"node_id":"...","agent_id":"...","score":0.42,"age_hours":30.1,"days_left":0.74,"action":"demote_next|stale_soon|keep|protected (review_ready)"}]}
```

`Score = access_count*Wa + importance*Wi - age_hours*Wd`; `days_left = (max_age_hours - age_hours)/24`; `review_ready` protected.

### `GET /api/graph?db=&limit=`

`?db=core|agents` (default `core`), `?limit=300` (clamped 10–2000). Returns `{"db":"core","limit":300,"truncated":false,"nodes":[{"node_id","node_type","label","trust_level","importance","layer"}],"edges":[{"edge_id","from_node","to_node","edge_type","weight"}]}` (edges capped `limit*3`).

### `GET /api/nodes`

Single-DB, paginated, filterable. This is the primary Manager data source.

**Query params**

| Param | Type | Description |
|---|---|---|
| `db` | `core\|agents` | Default `core` |
| `q` | string | Substring search on `label`, `content`, or exact `node_id` |
| `type` | string | `node_type` filter (`PERSON\|TOPIC\|EVENT\|FACT\|...`) |
| `scope` | string | `CORE\|AGENT` (derived from `metadata.attention_state` / `node_type` / `agent_scoped`) |
| `attention` | string | `agent_private\|review_ready\|core_verified` or `__none` (null/empty) |
| `layer` | string | `working\|short_term\|long_term\|archive` |
| `source` | string | Exact `source` match |
| `agent` | string | Owner agent; on `agents.db` also matches `agent_id` column |
| `sort` | string | `node_id\|label\|content\|node_type\|trust_level\|importance\|access_count\|created_at\|updated_at\|source\|layer` (default `created_at`, C22, newest creation first) |
| `dir` | string | `asc\|desc` or `1\|-1` (default `desc`) |
| `limit` | int | 1–200, default 50 |
| `offset` | int | Default 0 |

Response:

```json
{"db":"core","total":123,"offset":0,"limit":50,"nodes":[{"node_id":"...","label":"...","content":"...","node_type":"FACT","trust_level":0.8,"importance":0.7,"layer":"working","last_access":1720000000,"_scope":"CORE","_attention":"review_ready","_agent":"agent_x", ...}]}
```

### `GET /api/edges?db=&type=&node=&limit=&offset=`

`?db=core|agents` (default `core`), `?type=RELATES_TO|CONTRADICTS|...`, `?node=<node_id>` (either endpoint), `?limit=50` (1–200), `?offset=0`.

### `GET /api/agents`

No params. `{"agents":[{"agent_id","slug","job_hint","created_at"}]}`.

### `GET /api/empty_agents` (new)

Preview ghosts (agents with 0 nodes), age-gated via `prune_empty_agents_min_age_hours` (24).

```json
{"total_empty":6,"eligible":2,"min_age_hours":24,"cutoff_ts":1720000000,"agents":[{"agent_id":"agent_foo_ab12","slug":"foo","job_hint":"Foo","created_at":1720000000}],"all":[...]}
```

### `GET /api/mailbox?scope=&state=&limit=&offset=`

`?scope=core|agent:<id>|user|all` (default `all`), `?state=pending|noted|all` (default `pending`), `?limit=50`, `?offset=0`. Admin view (`all`) does not mark `read_at`. Returns `{"messages":[...],"limit":50,"offset":0}` where each message is `{"msg_id","from_scope","to_scope","body","created_at","metadata","receipt_scope","noted_at","read_at","acked_at"}`.

### `GET /api/manager_health?db=`

Per-DB graph health: `{"db":"core","total_nodes":...,"total_edges":...,"orphans":[...],"orphans_count":0,"dupes":[...],"dupes_count":0,"isolated":["node_..."],"isolated_count":3,"ephemeral_events":42}`.

### `GET /api/vectors?db=&limit=&offset=&q=`

`?db=core|agents` (default `core`), `?limit=50` (1–200), `?offset=0`, `?q=` label/id substring. `{"db":"core","total":...,"vectors":[{"node_id","label","magnitude","top_terms":[["term",0.42],...],"term_count":12}]}` (top 10 terms).

### `GET /api/layers?db=`

Groups `memory_layers` per layer: `{"db":"core","layers":{"working":[...],"short_term":[...],...}}` each entry has `layer`, `node_id`, `label`, `node_type`, `promoted_at`, `att`, `metadata`.

### `GET /api/path?from=&to=&db=`

`?from=` and `?to=` are label or node_id (resolved like `nodes.resolve_ref`), `?db=core|agents`. BFS undirected. Returns `{"found":true,"hops":3,"steps":[{"from":"...","from_label":"..."}, {"edge_type":"RELATES_TO","to":"...","to_label":"..."}], "from_resolved":"node_...","to_resolved":"node_..."}` or `{"found":false,"message":"No path found"}`.

### `GET /api/recall?q=&agent=&mode=&bound=&offset=&include_agent_notes=`

Unified recall (LogicEngine): `?q` required, `?agent` hint scopes to `agents.db` (`auto_create=False`), `?mode` heals to `RELATED`, `?bound` 1..200 default 10, `?offset` default 0, `?include_agent_notes` explicit bridge merge. Returns `{"mode","total_found","bound_applied","nodes":[...],"healed":{}}` (+ `agent_id` when scoped).

### `GET /api/schema?db=`

Tables + `PRAGMA table_info` + `PRAGMA index_list` per table plus `schema_meta`.

---

## POST routes

All POST bodies are JSON. Auth required. `400` on `ValueError` (unknown `db`, missing field, bad value), `500` on unhandled errors.

### `POST /api/run_job`

```json
{"jobs":["dedup","compact","agent_working"],"target":"both"}
```

- `jobs`: array or omitted (default = `DEFAULT_JOB_TYPES`, excludes `graduation,vacuum`).
- `target`: `core|agents|both|all` (`all` normalized to `both`).
- Canonical order enforced (`JOB_ORDER`: `dedup→compact→agent_working→age_prune→tiers→contradictions→graduation→discover→prune_empty_agents→vacuum`, 10 jobs).
- Returns `{"status":"success","run":{... history + markdown log ...}}`.

### `POST /api/scheduler`

```json
{"enabled":true,"interval_minutes":60,"max_unused_days":4}
```

Starts/stops the in-process scheduler; saves `interval_minutes` + `max_unused_days` to `brain/config.json`.

### `POST /api/config`

```json
{"interval_minutes":30,"prune_importance_floor":0.1,"dashboard_token":"s3cr3t","reload":false}
```

- `reload:true` re-reads `brain/config.json` from disk (no restart).
- Otherwise every key in the whitelist (`interval_minutes`, `max_unused_days`, `dedup_similarity_threshold`, `dedup_scan_cap`, `discover_scan_cap`, `discover_link_floor`, `discover_link_ceil`, `prune_importance_floor` / `prune_threshold` alias-synced, `auto_snapshot_before_jobs`, `snapshot_cooldown_s`, `auto_rebuild_vectors`, `ephemeral_keep_last`, `ephemeral_max_age_days`, `vacuum_after_prune`, `vacuum_freelist_threshold_pct`, `vacuum_freelist_min_pages`, `contradiction_auto_resolve`, `contradiction_low_trust`, `contradiction_high_trust`, `contradiction_scan_cap`, `contradiction_min_overlap_ratio`, `dashboard_token`, `keep_last_snapshots`, `keep_last_logs`, `agent_working_*`) is coerced to its type and saved atomically.

### `POST /api/config_reset`

```json
{}
```

Resets to `brain/engine.py:DEFAULTS`.

### `POST /api/create_snapshot`

```json
{"db":"core"}
```

`db`: `core|agents|both|all` (both = both DBs). Returns `{"core": {"status":"success","filename":"snapshot_core_...db", ...}}`.

### `POST /api/rebuild_vectors`

```json
{"db":"all"}
```

`db`: `core|agents|both|all`. Returns `{"core": {"status":"success",...}}` (full TF-IDF `rebuild_all` per DB). This is the only vector-rebuild entry point (not an MCP tool).

### `POST /api/restore_snapshot`

```json
{"db":"core","filename":"snapshot_core_20260905_120000.db"}
```

`db`: `core|agents` (default `core`). Jailed to `brain/snapshots/`, pre-rollback backup via `backup()` API, then health verify. Returns `{"status":"success","restored":"...","pre_rollback_backup":"prerollback_core_...db","health_ok":true}`.

### `POST /api/delete_snapshot`

```json
{"filename":"snapshot_core_...db"}
```

### `POST /api/vacuum`

```json
{"db":"all"}
```

`WAL checkpoint + VACUUM` on an isolated conn under `memory/.lock`. Returns per-DB `{"core":{"status":"success","before_bytes":...,"after_bytes":...,"saved_bytes":...,"before_mb":1.2,"after_mb":1.1}}`.

### `POST /api/compact_ephemeral`

```json
{"db":"all","keep_last":3,"max_age_days":7,"vacuum":true}
```

TTL sweep over `ephemeral_events`. TTL **always** applies; `keep_last` caps survivors. When `vacuum` true and rows removed, also vacuums and (if `auto_rebuild_vectors`) rebuilds vectors. Returns `{"core":{"status":"success","removed_ttl":5,"removed_cap":2,...},"vacuum":{...}}`.

### `POST /api/check_vacuum`

```json
{"db":"both"}
```

Returns `{"triggered":true,"bloat":...,"vacuum":{...},"bloat_after":...}` or `{"triggered":false,"bloat":...}` depending on freelist thresholds.

### `POST /api/graduate`

```json
{"node_ids":["node_..."],"agent_id":"agent_x"}
```

or `{"node_id":"..."}` / `{"node_ids":"..."}` (string normalized to array). Moves notes `agents.db → core.db` via `bridge.promote` (no vector rebuild here — incremental).

### `POST /api/regulate_agent_working`

```json
{"dry_run":true}
```

Agents-only WORKING regulator. `dry_run:true` returns `would_demote`; `false` demotes.

### `POST /api/contradiction_action`

```json
{"edge_id":"edge_...","action":"confirm","db":"core"}
```

`action`: `confirm`/`confirmed`, `ignore`/`ignored`, `pending`, `resolved` (status update) or `delete`, `keep_from`, `keep_to`, `merge` (resolve). `db`: `core|agents` (default `core`).

### `POST /api/contradiction_auto_resolve`

```json
{"dry_run":true,"db":"core"}
```

### `POST /api/ephemeral_allowlist`

```json
{"labels":["my_label","other"]}
```

or `{"action":"add","label":"my_label"}` / `{"action":"remove","label":"my_label"}`.

### `POST /api/ephemeral_ignored`

```json
{"labels":["my_label","other"]}
```

or `{"action":"add","label":"my_label"}` / `{"action":"remove","label":"my_label"}`. Mirrors allowlist but marks labels as explicitly kept (excluded from `GET /api/ephemeral_candidates`).

### `POST /api/prune_empty_agents` (new)

Deletes ghosts (`agents` with 0 nodes). Body `{"dry_run":true|false,"min_age_hours":24}` (omit to use config). Cleans `mailbox_receipts` for `agent:<id>` + aliases cascade.

```json
{"status":"success","deleted":3,"total_empty":6,"eligible":3,"min_age_hours":24,"agents":["agent_foo_ab12"]}
```
Dry-run: `{"would_delete":3, ...}`.

### `POST /api/remember`

Unified Direct: `{"content":"hello","node_type":"FACT","label":"hello","trust":0.8,"importance":0.7,"agent":"AshaWeb"}` → LogicEngine `heal` → `POST /api/node_add` style via `remember_many`. Returns `{"status":"created","node_id":"...","healed":{...}}`.

### `POST /api/recall`

Unified Direct: `{"query":"budget","agent":"AshaWeb","mode":"RELATED","bound":10,"offset":0}` → LogicEngine heal + scoped `agent_recall` or core `recall`. Returns `{"mode","total_found","bound_applied","nodes":[...],"healed":{}}`.

### `POST /api/node_update`

```json
{"db":"core","node_id":"node_...","fields":{"label":"new label","importance":0.9,"metadata":{"note":"x"}}}
```

`db`: `core|agents`, `fields`: subset of `label, content, trust_level, importance, source, metadata` (`metadata` merges via JSON, string accepted). Commits server-side; incremental vectors on label/content change. Returns `{"status":"updated","node":{...}}` or `{"status":"error","message":"not found"}`.

### `POST /api/node_delete`

```json
{"db":"core","node_id":"node_..."}
```

Returns `{"status":"deleted"}` / `{"status":"not found"}`.

### `POST /api/node_add`

```json
{"db":"core","node_type":"FACT","label":"hello","content":"hello world","trust":0.8,"importance":0.7}
```

`content` required. `db` `agents` requires `agent_id`. Returns `{"status":"created","node_id":"node_...","node":{...}}`.

### `POST /api/bulk_nodes`

```json
{"db":"core","node_ids":["node_a","node_b"],"action":"delete"}
{"db":"core","node_ids":["node_a"],"action":"set_attention","value":"review_ready"}
{"db":"core","node_ids":["node_a"],"action":"set_layer","value":"short_term"}
{"db":"core","node_ids":["node_a","node_b"],"action":"relabel","value":"new label"}
```

`action`: `delete | set_attention | set_layer | relabel`. `value`: `agent_private|review_ready|core_verified|__clear|""` (attention), `working|short_term|long_term|archive` (layer), new label (relabel).

### `POST /api/sql`

```json
{"db":"core","sql":"SELECT label, node_type FROM nodes LIMIT 10"}
```

Only `SELECT / PRAGMA / EXPLAIN` allowed. Capped at 200 rows, returns `{"db":"core","columns":[...],"rows":[[...]],"row_count":10,"truncated":false}`.

### `POST /api/demote`

```json
{"node_ids":["node_..."]}
```

Undo `review_ready → agent_private` on agents notes. Returns `{"status":"demoted","demoted":1,"total":1,"errors":[]}`.

### `POST /api/contradictions/clear`

```json
{"db":"both"}
```

Deletes all `CONTRADICTS` edges per DB (`db`: `core|agents|both|all`). Returns `{"status":"cleared","core":{"deleted":3},"agents":{"deleted":0}}`.

### `POST /api/edge_add`

```json
{"db":"core","from_node":"node_a","to_node":"node_b","edge_type":"RELATES_TO","weight":0.8}
```

`db` `agents` requires `agent_id` (ownership-gated). Returns `{"status":"created","edge_id":"edge_..."}`.

### `POST /api/edge_delete`

```json
{"db":"core","edge_id":"edge_..."}
```

### `POST /api/mailbox/send`

```json
{"from":"core","to":"agent:agent_x","body":"please promote ..."}
```

### `POST /api/mailbox/ack`

```json
{"scope":"agent:agent_x","msg_ids":["msg_..."]}
```

`msg_ids` optional (string JSON list accepted); default = all unacked. Returns `{"acked":1}`.

### `POST /api/mailbox/delete`

```json
{"msg_id":"msg_..."}
```

or `{"msg_ids":["msg_..."]}` (bulk). Deletes `mailbox` row.

### `POST /api/mailbox/wipe`

```json
{}
```

Deletes all mailbox rows. Returns `{"wiped": n}`.

### `POST /api/prune_empty_agents` (new)

Deletes ghosts (`agents` with 0 nodes). Body `{"dry_run":true|false,"min_age_hours":24}` (omit to use config). Cleans `mailbox_receipts` for `agent:<id>` + aliases cascade.

```json
{"status":"success","deleted":3,"total_empty":6,"eligible":3,"min_age_hours":24,"agents":["agent_foo_ab12"]}
```
Dry-run: `{"would_delete":3, ...}`.

## Error shapes

- Success (POST): `{"status":"success",...}` or per-DB map `{"core":{...},"agents":{...}}`.
- Client error: `{"status":"error","message":"..."}` with HTTP 400 (`ValueError`) or 500 (unhandled).
- Auth failure: `{"error":"unauthorized: missing X-Api-Token"}` with HTTP 401.

## cURL examples

```bash
# Health (no auth)
curl http://127.0.0.1:8500/api/health

# Nodes — search + paginate
curl -H "X-Api-Token: $TOKEN" \
  "http://127.0.0.1:8500/api/nodes?db=core&q=budget&type=FACT&sort=updated_at&dir=desc&limit=20&offset=0"

# Bulk delete
curl -X POST -H "Content-Type: application/json" -H "X-Api-Token: $TOKEN" \
  -d '{"db":"core","node_ids":["node_a","node_b"],"action":"delete"}' \
  http://127.0.0.1:8500/api/bulk_nodes

# Read-only SQL
curl -X POST -H "Content-Type: application/json" -H "X-Api-Token: $TOKEN" \
  -d '{"db":"core","sql":"SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type"}' \
  http://127.0.0.1:8500/api/sql

# Demote
curl -X POST -H "Content-Type: application/json" -H "X-Api-Token: $TOKEN" \
  -d '{"node_ids":["node_..."]}' \
  http://127.0.0.1:8500/api/demote

# Clear contradictions
curl -X POST -H "Content-Type: application/json" -H "X-Api-Token: $TOKEN" \
  -d '{"db":"core"}' \
  http://127.0.0.1:8500/api/contradictions/clear

# Mailbox
curl -H "X-Api-Token: $TOKEN" "http://127.0.0.1:8500/api/mailbox?scope=core&state=pending&limit=20"
curl -X POST -H "Content-Type: application/json" -H "X-Api-Token: $TOKEN" \
  -d '{"from":"core","to":"user","body":"hello"}' http://127.0.0.1:8500/api/mailbox/send
curl -X POST -H "Content-Type: application/json" -H "X-Api-Token: $TOKEN" \
  -d '{"scope":"core","msg_ids":["msg_..."]}' http://127.0.0.1:8500/api/mailbox/ack
```
