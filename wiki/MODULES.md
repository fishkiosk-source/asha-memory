# Modules

Code map. Every file <400 lines target (`src/core/*` strict, `brain/engine.py` exempt as orchestrator), no circular imports. Import rules are load-bearing — violations regress recall latency or break the two-DB guarantee. See bottom for the full rule table.

## `src/core/` — `core.db` graph store

Canonical graph primitives. The only package that knows `core.db` DDL. No import of `brain/` or `src/agents/*` (except `agents/store.py` reusing `core` primitives via explicit relative import).

| File | Purpose (one line) | Key public API |
|---|---|---|
| `src/core/store.py:1` | Paths + the **only** `connect()` for `core.db` (PRAGMA invariant) | `connect(db_path, cache_size?, timeout_s?) -> sqlite3.Connection` (with `check_same_thread=False` for Hermes multi-thread, `BUSY_TIMEOUT_MS=8000` `src/core/store.py:19`, `timeout_s=30.0` `src/core/store.py:37`), `core_db_path(base?)`, `memory_dir(base?)`, `USER_VERSION_V3 = 4`, `DEFAULT_CACHE_SIZE = -64000` |
| `src/core/schema.json:1` | DDL single source (tables/triggers/indexes, deviations C19/C20 noted) | JSON with `tables{}`, `triggers[]`, `indexes[]`, `user_version`, `lexicon_version`. Executed only by `migrate.py`; no inline SQL elsewhere. |
| `src/core/migrate.py:1` | Idempotent `v1->v4` chain + read-only `check()` for `health()` | `migrate(conn) -> {from,to,ran,report}`, `check(conn) -> {ok,user_version,missing_*,orphans,schema_meta,ndocs}`, `get_user_version(conn)`, CLI `--check --memory DIR` |
| `src/core/nodes.py:1` | CRUD: `remember` (delegates to bulk), `get`/`update`/`delete`, keyword index, auto-link, FACT contradiction check | `remember(conn, content, node_type, label?, source?, trust?, importance?, metadata?, config?) -> node_id`, `remember_many(conn, items, config?, extra_cols?) -> [node_id]`, `get_node(conn, node_id, with_layer?)`, `update_node(conn, node_id, **fields)`, `delete_node(conn, node_id)`, `bump_access(conn, node_id)`, `build_index(conn, node_id, label, content)`, `clear_index`, `row_to_dict`, `resolve_ref(conn, ref, agent_id?)`, `NODE_TYPES`, `DEFAULTS`, `UPDATABLE_FIELDS` |
| `src/core/edges.py:1` | `relate` + batched `neighbors` + edge queries | `relate(conn, from_id, to_id, edge_type?, weight?, metadata?, agent_id?) -> edge_id` (weight clamped to `-1..1`, C16), `neighbors(conn, node_id, agent_id?) -> [(neighbor_id, weight, edge_type, direction)]` (single batched query), `edges_between(conn, a, b)`, `delete_edge(conn, edge_id)`, `EDGE_TYPES` (+`PROMOTED_FROM`) |
| `src/core/recall.py:1` | 9 workers + DSL auto-detect + `LRUCache` + `QueryLogger` | `recall(conn, query, mode?, bound?, offset?, include_agent_notes?, agent_id?, config?, clock?, cache?, logger?) -> {query,mode,nodes,total_found,bound_applied}`, `parse_query(str) -> ParsedQuery`, `is_core_visible(node)`, `LRUCache(capacity=50)`, `QueryLogger(conn, buffer_limit?, retain?)`, `SEMANTIC_CANDIDATE_CAP=2000`, `QUERY_LOG_RETAIN=5000`, `RECALL_MODES`, `WORKER_MODES` |
| `src/core/vectors.py:1` | Incremental TF-IDF (v2-exact math, no per-insert full fit) | `update_on_insert(conn, node_id, label, content)`, `update_on_delete(conn, node_id, label, content)`, `refresh_on_content_change(conn, node_id, old_label, old_content, new_label, new_content)`, `rebuild_all(conn) -> {nodes, terms}`, `get_vector(conn, node_id) -> (vec, magnitude)`, `get_ndocs(conn)`, `get_df(conn, term)`, `idf(ndocs, df)`, `encode(vec)`, `decode(s)`, `cosine(a,b, mag_a?, mag_b?)`, `magnitude(vec)` |
| `src/core/layers.py:1` | Layer reads + init/set (writes owned by brain tick, C-write-amp) | `get_layer(conn, node_id) -> layer`, `init_layer(conn, node_id, now?) -> 'working'`, `set_layer(conn, node_id, layer, now?)`, `LAYERS`, `LAYER_ORDER` |
| `src/core/lexicon.py:1` | Tokenizer / stopwords / sentiment / ephemeral heuristic (verbatim v2 carry) | `_tokenize(text, min_len?)`, `_extract_keywords(text, max_words?)`, `_sentiment_score(text)`, `_looks_like_json_log(content)`, `_sanitize_fts_query(query)`, `_jaccard_similarity(a,b)`, `STOPWORDS`, `POSITIVE_WORDS`, `NEGATIVE_WORDS`, `DEFAULT_EPHEMERAL_LABELS`, `LEXICON_VERSION=3` |
| `src/core/clock.py:1` | Temporal context (verbatim v2 carry; `TODAY` writes only to `core.db`) | `InternalClock(enabled?, stale_after_days?)` with `now()`, `humanize(epoch, now_epoch?)`, `summarize_node(node, last_accessed?, access_count?, now_epoch?)`, `last_accessed_before(conn, node_id, before_epoch)`, `today_summary(conn)`, `graph_activity(conn, hours?)`, `build_tick_content(summary)` |
| `src/core/__init__.py` | Package marker (no runtime logic) | — |
| `src/logic/engine.py:1` | Logic Engine — agent resolve/heal/route (`resolve_agent`, `heal`, `resolve_ref_exact/fuzzy`) | `LogicEngine(agents_conn, core_conn?)` with `resolve_agent(ref, auto_create?)`, `heal(tool, args, auto_create?)`, `resolve_ref_exact/fuzzy` |
| `src/logic/api_client.py:1` | Dashboard HTTP client — token auto-load | `load_dashboard_token(brain_dir)`, `DashboardClient(base_url, brain_dir?)` |

Conventions: callers own transactions (`with connect(path) as conn:` commits on clean exit); `trust`/`importance` clamped to `[0,1]` (`src/core/nodes.py:68 _clamp01`); `update_node` shallow-merges `metadata`, refreshes `node_index` + vectors on text change; `get_node` is pure read (`bump_access` is recall's job); `remember` single-row is a thin delegate to `remember_many` bulk path.

## `src/agents/` — `agents.db` + bridge + mailbox

Single `agents.db` for all agents, sharded by `agent_id` column. Schemas mirror `core` plus sharding. Identity is registry-owned.

| File | Purpose (one line) | Key public API |
|---|---|---|
| `src/agents/store.py:1` | The **only** `connect()` for `agents.db`; full DDL; identity registry; isolation gates; agent CRUD | `connect(db_path, cache_size?, timeout_s?)` (with `check_same_thread=False`), `ensure_schema(conn)`, `check(conn)`, `agents_db_path(base?)`, `agent_ensure(conn, job_hint, preferred_id?) -> {agent_id, created}`, `agent_list(conn)`, `resolve_agent(conn, ref)`, `require_agent(conn, agent_id)`, `add_alias(conn, alias, agent_id)`, `agent_remember(conn, agent_id, content, label?, metadata?, attention_state?, config?) -> node_id`, `agent_recall(conn, agent_id, query, mode?, bound?, offset?, config?, clock?)`, `agent_get_node(conn, agent_id, node_id)`, `agent_delete_node(conn, agent_id, node_id)`, `agent_relate(conn, agent_id, from_id, to_id, edge_type?, weight?, metadata?)`, `agent_set_attention(conn, agent_id, node_id, attention_state)`, `agent_digest(conn, agent_id, limit?)`, `PREFERRED_ID_RE`, `UNKNOWN_AGENT_HINT`, `ATTENTION_STATES`, `AGENT_DEFAULTS` (`agent_max_notes=100`, `agent_max_content_length=800`) |
| `src/agents/bridge.py:1` | **Only dual-DB module**: review queue, cross-agent search, idempotent promote (MOVE) | `list_review_queue(agents_conn, limit?)`, `review_stats(agents_conn)`, `search_all_agents(agents_conn, query, min_confidence?, bound?) -> [{_agent_id, _similarity}]`, `get_agent_note(agents_conn, agent_id, node_id)`, `promote(core_conn, agents_conn, agent_id, node_id, new_type?, new_label?) -> core_node_id` (6 steps, C7), `graduate_many(core_conn, agents_conn, agent_id, node_ids, new_type?)` |
| `src/agents/mailbox.py:1` | Mail tables (DDL owned here) + `send`/`read`/`ack`/`check_inbox` + injection format | `MAILBOX_DDL`, `send(conn, from_scope, to_scope, body, metadata?) -> {msg_id}`, `check_inbox(conn, scope) -> [msg]` (MCP wrapper only, marks `noted_at`), `read(conn, scope, state?, limit?, offset?, mark_read?)`, `ack(conn, scope, msg_ids?) -> {acked:n}`, `format_injection(messages, scope) -> str`, `AGENT_BROADCAST_HINT`, `RECEIPT_STATES` |
| `src/agents/__init__.py` | Package marker | — |

Identity: `agent_ensure` normalizes `job_hint` to `slug` (`re.sub(r'[^a-z0-9]+','-', lower).strip('-') or 'agent'`), returns existing on `slug` hit else issues `agent_<slug>_<hex4>`, honors `preferred_id` only if free and `^[a-z0-9][a-z0-9_-]{1,40}$`. Unknown IDs are rejected everywhere with `unknown_agent, hint: call agent_ensure first`. Agent notes are always `AGENT_NOTE` (richer types only via promotion, v2 parity). Caps: 800 chars, 100 notes/agent (oldest `agent_private` evicted first, `review_ready` never auto-evicted, `src/agents/store.py:262 _enforce_cap`).

## `src/mcp/` — Model Context Protocol surface

Stdio JSON-RPC 2.0 server, 23 tools, scope-aware dispatch, mailbox auto-inject wrapper.

| File | Purpose (one line) | Key public API |
|---|---|---|
| `src/mcp/server.py` | stdio JSON-RPC 2.0 loop (v2 protocol parity), resources, `open_context` that converges schema on start | `main()`, `open_context(memory_path?)`, stdio `initialize`/`tools/list`/`tools/call`/`resources/list` handlers |
| `src/mcp/tools.py` | 23 tool definitions + handlers + scope derivation + injection wrapper + dual-DB transaction control + duplicate label versioning | `TOOL_DEFINITIONS: [...]` (23 entries), `dispatch(tool_name, args, core_conn?, agents_conn?, scope?)`, `with_inbox_notice(scope, result)` (the only caller of `mailbox.check_inbox`), `derive_scope(auth?)`, `_collect_label_versions(conn,nodes,agent_id?)`, `_annotate_nodes_with_versions(conn,nodes)`, `_shape_node` now returns `{label_display, label_version, node_id_short}` + `age` + `clock` + `mailbox_notice` |

Tool surface (23, C1): `remember, recall, relate, get_node, update_node, delete_node, agent_ensure, agent_list, agent_remember, agent_recall, find_across_agents, promote_to_core, review_queue, set_attention, mailbox.send, mailbox.read, mailbox.ack, profile, health, stats, export, vacuum, compact`. No `spawn_agent` (replaced by `agent_ensure`), no `query_dsl` (folded into `recall` auto-detect), no `rebuild_vector_index` (internal-only). `register_skill` survives only as a thin alias to `remember(node_type=SKILL)`. See `MCP_TOOLS.md` for the full surface.

Run: `python -m src.mcp.server --memory-path ./memory` (from v3 root).


## `src/direct/` — Direct in-process provider (no MCP)

Hermes/OpenClaw direct integration without stdio. Reuses the same 23-tool surface via `src/mcp/tools.py:dispatch` (single source C1), but as Python methods with a re-entrant `RLock` and `check_same_thread=False` connections for gateway `turn` vs `sync_turn` daemon thread reuse.

| File | Purpose (one line) | Key public API |
|---|---|---|
| `src/direct/provider.py:1` | Thread-safe `DirectMemory` wrapper: `RLock` + `open_context` + `dispatch` per method | `DirectMemory(memory_dir?)` with `remember/recall/relate/get_node/update_node/delete_node/agent_ensure/agent_list/agent_remember/agent_recall/find_across_agents/promote_to_core/review_queue/set_attention/mailbox_send/mailbox_read/mailbox_ack/profile/health/stats/export/vacuum/compact/register_skill`, `call(tool,args)`, `definitions()`, `close()`, context-manager |
| `src/direct/__init__.py` | Re-export | `from src.direct import DirectMemory` |

Parity: `len(DirectMemory.definitions()) == len(tools.definitions()) == 24` (23+deprecated). Import `from src.direct.provider import DirectMemory; m=DirectMemory("./memory"); m.recall("Sam")`; MCP stays the stdio wrapper for external clients.

## `brain/` — maintenance backend (never imported by `src/core`)

Dual-DB engine that owns every mutating background job. Never imported by `src/core` (import rule enforced).

| File | Purpose (one line) | Key public API |
|---|---|---|
| `brain/engine.py:1` | `BrainEngine(core_path, agents_path)`: 9 per-DB jobs, snapshots+coalescing, `.lock`, contradictions, discovery, bloat/health/stats, markdown reports, history | `BrainEngine(core_path?, agents_path?, brain_dir?, config?)` with `core_conn()`, `agents_conn()`, `locked(timeout_s?)` (reentrant `msvcrt`/`fcntl`), `create_snapshot(db)`, `ensure_pre_run_snapshot(dbs)`, `list_snapshots(db?)`, `restore_snapshot(db, filename)`, `delete_snapshot(filename)`, `deduplicate(target?, similarity_threshold?)`, `manage_tiers(target?)`, `regulate_agent_working_memory(dry_run?)`, `get_agent_working_preview()`, `prune_stale_unused(target?, max_unused_days?)`, `compact_ephemeral(target?, keep_last?, max_age_days?)`, `get_ephemeral_stats(target?)`, `discover_ephemeral_candidates(min_count?)`, `set_ephemeral_allowlist(labels)`, `vacuum_db(target?)`, `rebuild_vectors(target?)`, `detect_contradictions(target?)`, `discover_links(target?)`, `graduate_agent_notes()`, `health(target?)`, `get_bloat_metrics(db)`, `get_full_statistics(target?)`, `generate_markdown_report(entry)`, `record_history(entry)`, `get_history(limit?)`, `reload_config()`, `reset_config()` |
| `brain/scheduler.py:1` | Canonical order, `DEFAULT`/`MUTATING` sets, single-flight runs, daemon loop, history | `JOB_ORDER` (10 jobs sorted regardless of input, includes `prune_empty_agents`), `DEFAULT_JOB_TYPES` (excludes `graduation,vacuum`, includes `prune_empty_agents`), `MUTATING_JOBS` (`dedup,compact,age_prune,tiers,graduation,agent_working,prune_empty_agents`), `JOB_RESULT_KEYS`, `BrainScheduler(engine?, brain_dir?, interval_minutes?)` with `start(interval_minutes?)`, `stop()`, `run_job_now(jobs?, target?) -> {status, snapshots, results, health_after, markdown_log}` (coalesced snapshots + single-flight `skipped_busy`), `get_history(limit?)` |
| `brain/config.json` | Brain-only config (intervals, thresholds, weights, token) | JSON: `interval_minutes=60`, `max_unused_days=4`, `dedup_similarity_threshold=0.85`, `ephemeral_labels[]`, `ephemeral_ignored[]`, `prune_empty_agents` + `prune_empty_agents_min_age_hours`, `ephemeral_keep_last=3`, `ephemeral_max_age_days=7`, `vacuum_freelist_threshold_pct=15`, `contradiction_low/high_trust`, `agent_working_*` weights/high-water/batch/max_age, `auto_snapshot_before_jobs=false`, `snapshot_cooldown_s=300`, `keep_last_snapshots=10`, `dashboard_token`, `sqlite_cache_size=-64000`. See `BRAIN.md`. |
| `brain/logs/` | Per-run markdown audit logs | `brain_run_YYYYMMDD_HHMMSS.md` (+ `_core`/`_agents` variants) via `engine.generate_markdown_report` |
| `brain/snapshots/` | Per-DB snapshots (serialized under `.lock`) | `snapshot_core_*.db` / `snapshot_agents_*.db` + `prerollback_*` on restore |
| `brain/job_history.json` | Last ~100 runs (single-flight records `skipped_busy`) | `[{timestamp, jobs, target, duration_s, snapshots, results, health_after, markdown_log}]` |
| `brain/contradiction_resolutions.jsonl` | Append-only contradiction decisions (confirm/ignore/merge) | JSONL `{"node_id_a","node_id_b","resolution","ts"}` |

## `dashboard/` — human control plane (`http.server`, stdlib only)

Threaded HTTP + REST API + header auth + path jail. No `humantools/` shim, no CDN, no `sql.js`, no iframe.

| File | Purpose (one line) | Key public API / Notes |
|---|---|---|
| `dashboard/server.py` | Threaded HTTP + `X-Api-Token` auth + path jail (`is_relative_to`) | `python -m dashboard.server --port 8500 --memory-path ./memory` (bind `127.0.0.1` default, CORS off). Routes: `GET /`, `/static/*`, `/wiki`, `/api/health|ping|status|bloat|statistics|config|contradictions|ephemeral_candidates|snapshots|logs|log_content|history|databases|graduate_preview|agent_working_preview|graph?db=&limit=|nodes?db=&q=&offset=&limit=|agents|mailbox?scope=&state=` + `POST /api/run_job|scheduler|config|config_reset|check_vacuum|vacuum|compact_ephemeral|rebuild_vectors|create_snapshot|restore_snapshot|delete_snapshot|contradiction_action|contradiction_auto_resolve|graduate|regulate_agent_working|node_update|node_delete|mailbox/send|mailbox/ack|ephemeral_allowlist|mailbox/delete|wipe` — see `API.md` for full list. Auth: `X-Api-Token` header only; no `?token=`; no localhost bypass when token is set (C8). All paths jailed to `memory/` + `brain/snapshots/` (`resolve()` + `is_relative_to`, reject `..`). |
| `dashboard/static/dashboard.html` | Shell: header, DB pills, scheduler pill, stats bar, 12 tabs, toast/modal | Loads `app.js` + `modules/*.js` via `import` (no bundler) |
| `dashboard/static/app.js` | Tab loader, `api()` (token+401), status/badges, helpers | `api(path, opts?)`, `loadTab(name)`, `refreshStatus()` |
| `dashboard/static/modules/overview.js` | Overview tab: per-DB health+bloat cards + combined history (5 per DB) | `GET /api/health?db=all`, `GET /api/history` |
| `dashboard/static/modules/maintenance.js` | Instant job runner: 9 canonical jobs + `Snapshot Now` + `Run FULL` per-DB | `POST /api/run_job {jobs, target}` |
| `dashboard/static/modules/graduate.js` | Manual curation: `review_ready` table + bulk `Graduate Selected` | `GET /api/graduate_preview?limit=` (via `bridge.list_review_queue`), `POST /api/graduate {node_ids}` (via `bridge.promote`) |
| `dashboard/static/modules/observer.js` | Agent-only `WORKING` janitor: `Score = acc*Wa + imp*Wi - ageH*Wd` | `GET /api/agent_working_preview`, `POST /api/regulate_agent_working {dry_run}` |
| `dashboard/static/modules/contradicts.js` | Contradiction queue: pending/confirmed/ignored + bulk actions + auto-resolve preview | `GET /api/contradictions?status=&limit=`, `POST /api/contradiction_action`, `POST /api/contradiction_auto_resolve` |
| `dashboard/static/modules/ephemeral.js` | Allowlist chips + candidate scan + `Compact Now` | `GET /api/ephemeral_candidates?min_count=`, `POST /api/ephemeral_allowlist`, `POST /api/compact_ephemeral {keep_last,max_age_days,vacuum}` |
| `dashboard/static/modules/graph.js` | Native Canvas/SVG graph (no D3, no CDN): `core|agents` selector | `GET /api/graph?db=&limit=` (server-capped `{nodes[], edges[]}`) |
| `dashboard/static/modules/manager.js` | Native CRUD (no `sql.js`): `core|agents` selector + paginated table + row updates/deletes | `GET /api/nodes?db=&q=&offset=&limit=`, `POST /api/node_update|node_delete` |
| `dashboard/static/modules/system.js` | Snapshots+rollback + audit logs + execution history | `GET /api/snapshots?db=`, `POST /api/restore_snapshot|delete_snapshot`, `GET /api/logs`, `GET /api/log_content?file=`, `GET /api/history` |
| `dashboard/static/modules/statistics.js` | Full `get_full_statistics()` grid (nodes/edges/size/trust/importance/types/layers/sources/labels) | `GET /api/statistics?db=all` |
| `dashboard/static/modules/config.js` | Intervals/thresholds/weights/toggles + `Scheduler toggle (cfg-sched-toggle, schedToggle(), persists cron_enabled, auto-restores on dashboard restart)` + `Save/Reload/Reset` + `Check & Auto-VACUUM` | `GET /api/config`, `GET /api/status (for running)`, `POST /api/config|scheduler|config_reset|check_vacuum` |
| `dashboard/static/modules/mail.js` | Operator console: user inbox + all-traffic admin view + compose-as-`user` | `GET /api/mailbox?scope=user|all&state=`, `POST /api/mailbox/send {from:'user',to,body}` + `agent_list` dropdown |

See `DASHBOARD.md` for the tab matrix and `?db=core|agents|all` defaults (`all` for Overview/Maintenance/System/Statistics/Config, `agents` for Graduate/Observer, `core` with toggle for Contradicts/Ephemeral/Graph/Manager; `idea: 13.9`).

## `bench/`, `tools/`, `tests/`

| File | Purpose (one line) | Key public API / Notes |
|---|---|---|
| `bench/bench_recall.py` | Deterministic perf harness (stdlib, 10k default, `--nodes 100000` capable) | `python -m bench.bench_recall --nodes 10000 --concurrent` reports p50/p95 per mode per-DB + writer-vs-dedup gate; gate fails the build on regression |
| `tools/migrate_v2_to_v3.py` | One-shot splitter (v2 read-only): classify -> registry/`agent_aliases` -> bulk load -> index/vectors -> health verify -> configs -> report | `python -m tools.migrate_v2_to_v3 --v2-path asha_memory\ v2/asha_memory/core.db --dry-run` / `--force` (rebuild); never writes v2 |
| `tests/test_store_migrate.py` | `user_version` chain, FTS backfill, `lexicon_version`, orphan purge | `check()` shape, `migrate()` idempotence, FTS/index coverage |
| `tests/test_nodes_edges.py` | Nodes/edges CRUD, clamp, FK cascade, auto-link, contradiction flag | `remember`/`remember_many` caps, `relate` clamp `-1..1`, `delete_node` vector decrement |
| `tests/test_recall_modes.py` | 9-mode parity vs v2 fixtures + FTS fallbacks + offset/bound | All workers + DSL auto-detect + `WHO_IS`/`WHAT_ABOUT` type-check |
| `tests/test_agents_store.py` | Identity + isolation + caps + attention lifecycle | `agent_ensure` idempotence/slug/`preferred_id` regex, `resolve_agent` alias, `require_agent` gate, `agent_remember` 800/100 caps |
| `tests/test_bridge_promote.py` | Idempotent promote, collision, provenance, isolation (MOVE semantics) | 6-step move, `promoted_from` idempotency, `trust=max(orig,0.8)`, source clean-removed |
| `tests/test_mailbox.py` | Unicast + broadcast receipts, `no-inject-when-empty`, scope rejects | `send` fan-out, `check_inbox` noted_at, `read`/`ack` receipt mirroring, `agent->broadcast` reject |
| `tests/test_mcp.py` | MCP dispatch + scope derivation + injection wrapper | 23 tools, `with_inbox_notice` envelope, commit/rollback both DBs |
| `tests/test_concurrency.py` | MCP writer + brain job overlap, `busy_timeout`, single-flight | `memory/.lock` reentrancy, `PRAGMA busy_timeout=8000` (code `src/core/store.py:19`), `skipped_busy` |
| `tests/test_brain.py` | Brain jobs per-DB, `health`/`bloat`/`stats`, snapshot coalescing, bloat thresholds | `BrainEngine` per-DB results `{core, agents}`, `ensure_pre_run_snapshot` 300s coalesce |
| `tests/test_dashboard.py` | Live HTTP: header auth, path jail, `?db=` param + wiki search + manager pills | `ThreadingHTTPServer` + `X-Api-Token` + `/static/*` jail + `is_relative_to` |
| `tests/test_export_import.py` | Manifest, `safe_extract`, per-DB `health` after restore | `manifest.json {v3_version,lexicon_version,user_version,sha256}`, `backup()` API, jail `..`/absolute |

Total 135 tests. Run: `python -m pytest tests/` from v3 root.

## Import rules (non-negotiable)

| Rule | Statement | Why | Enforced by |
|---|---|---|---|
| **Brain never imported by core** | `src/core/*` never imports `brain/*`. `brain/engine.py:3` header: `NEVER imported by src/core`. | Core is the product; brain is a plugin. Imports would couple recall to maintenance and widen the writer set. | Code review + grep: `rg "from brain\|import brain" src/core`. |
| **Bridge is the only dual-DB module** | Only `src/agents/bridge.py` opens both `core.db` and `agents.db` in one operation. | Single place to audit the `core-first-then-agents` commit order and idempotency (C7). No other module should reason about cross-DB provenance. | Grep: two `connect(` calls in one file = only `bridge.py`. |
| **Mailbox wrapper only** | Only `src/mcp/tools.py` (the MCP dispatch wrapper `with_inbox_notice`) calls `mailbox.check_inbox`. Internal `store.py`/`recall.py` never touch `mailbox` (C5). | Keeps hot read paths read-only; avoids `SELECT + UPDATE noted_at` write amplification on every internal call — same bug `layers` fixed (§9.2). | `rg check_inbox src/` should hit only `src/mcp/tools.py` + `src/agents/mailbox.py` def. |
| **One `connect()` per DB** | `src/core/store.py:37 connect` is the only opener for `core.db`; `src/agents/store.py:131 connect` the only for `agents.db`. No raw `sqlite3.connect` elsewhere except three sanctioned exceptions: snapshot `backup()` staging (2 sites) and `VACUUM` isolated conn (commented `Sanctioned raw-connect exception` in `brain/engine.py:293` + `389`) | Single place for `WAL + NORMAL + FK ON + busy_timeout + cache_size` invariant; grep-lintable. | `rg "sqlite3\.connect" --hidden` + allowlist comments. |
| **Single-writer per DB** | `core.db` writers = `src/core/*` + `bridge.promote` insert + `brain` (under `.lock`); `agents.db` writers = `src/agents/*` + `mailbox` + `bridge.promote` delete + `brain` (under `.lock`). No other writers exist. | Two WALs, two contention domains. Adding a writer without documenting it regresses recall. | `ARCHITECTURE.md` writer map + `RULINGS.md`. |
| **`memory/` is data-only** | `memory/*.db`, `memory/.lock`, `brain/snapshots/*.db`, `brain/logs/*.md` are gitignored and never hand-edited. DDL lives in `src/core/schema.json` + `src/agents/store.py` + `src/agents/mailbox.py`. | Hand-edits bypass `migrate`/`ensure_schema` and break `user_version` convergence. | `.gitignore` + `tools/migrate_v2_to_v3.py` read-only v2. |
| **No `humantools/` compat** | `humantools/` is deleted with no shim (`dashboard/server.py` does not serve `/humantools/*`, C4). Graph/Manager are full rewrites against `GET /api/graph?db=` / `GET /api/nodes?db=` + row endpoints. | Patching the old `asha_graph.html` (D3 CDN, `postMessage`, whole-DB `db_bytes` download) across the two-DB split would leave it permanently broken (§2.11). | Delete + no route. |

Further reading: `ARCHITECTURE.md` (data flow, writer map, `.lock`, FTS C19/C20, snapshot coalescing), `DATABASE.md` (full schemas, indexes, triggers, conventions), `MCP_TOOLS.md` (23-tool surface), `BRAIN.md` / `DASHBOARD.md` (subsystem detail).

