# Architecture

> 60-second summary: v3 keeps every v2 capability but splits one contended `core.db` into two isolated WAL databases — `core.db` (curated knowledge) and `agents.db` (all agents sharded by `agent_id`). Each has its own single-writer gate and WAL. Incremental TF-IDF replaces per-insert full-corpus fits. Telemetry moves out of the graph into `ephemeral_events` but `nodes` legacy telemetry (label in `ephemeral_labels`) is still cleaned unified via `compact_ephemeral` + `get_ephemeral_stats` (both tables, fix 2026-09-11). Mailbox and promotion cross the split via explicit, auditable bridges. The dashboard and brain are dual-DB aware. Nothing breaks recall semantics.

## Why v2 peaked

`asha_memory v2` (~2600 lines, single `core.db`) hit a ceiling. Every new capability added scan / sync / lock cost on one WAL file and on one writer set.

| Bottleneck | Evidence in v2 | Effect |
|---|---|---|
| **Single-DB contention** | `asha_memory_v2.py:644` / `brain_engine.py:143` — core recall, agent notes, vectors, FTS, `query_log`, `access_log` all contend on one WAL | Further features regress recall latency and MCP throughput |
| **Scope-leakage cost** | `_is_core_visible:1364` + `fetch_bound = bound*5` — agent notes live in `core.db` and are filtered post-retrieval | Correctness paid at query time for every core recall |
| **Brain as second writer** | `rebuild_vector_index:801` with `busy_timeout=5000` plus dedup / tiers / vacuum / orphan sweeps competing with live MCP writes | Write/write races, `busy` retries, freelist bloat |
| **Registry / ceremony bloat** | `ASHA_SKILLS_REGISTRY.txt` (53 skills) + `spawn_agent` | Inflated `tools/list`, client confusion, free-form ID fragmentation |
| **Ephemeral as graph nodes** | Telemetry (`FEED_SNAPSHOT`, `RUNTIME_SAMPLE`, ...) stored as `nodes`/`edges` | Pollutes `node_index`/`node_vectors`, requires compaction scans — fixed unified: `compact_ephemeral`/`get_ephemeral_stats` now scan `nodes` + `ephemeral_events` where label in list (2026-09-11) |

`RULINGS.md` C1–C20 diagnose the single-WAL collapse (see `DATABASE.md` for the FTS/vector fixes). V3 is therefore a **modularity + separation release**: same capabilities, isolated hot paths.

## Principles (non-negotiable)

1. **Pure Python, local-only, zero external dependencies.** Stdlib only (`sqlite3, json, math, re, pathlib, http.server`). No pip, no network, no AI calls in core. `src/core/lexicon.py:1` and `src/core/clock.py:1` are verbatim v2 copies; `dashboard/static/modules/graph.js` is a Canvas/SVG rewrite with no D3 CDN (C9).
2. **No recall-semantics break.** Mode names (`RELATED, SEMANTIC, TIMELINE, PATH, CLUSTER, WHO_IS, WHAT_ABOUT, RECENT, PRUNE, DSL`), DSL strings (`FIND ...`), and result shape (`{query, mode, nodes, total_found, bound_applied}`) stay v2-compatible. `src/core/recall.py:44` preserves all 9 workers.
3. **Single-writer + WAL per DB.** Two WALs total, never one shared contention point. One `connect()` per store.
4. **Correctness before cleverness.** Every deviation from v2 is documented where it lives with its ruling number (C1–C20). See `RULINGS.md` and `src/core/schema.json:2`.

## Layout

```
asha_memory v3/
  src/            # importable modules — the product
    core/         #   core.db owner: store, schema+migrate, nodes, edges, recall, vectors, layers, lexicon, clock
    agents/       #   agents.db owner: store, bridge, mailbox
    mcp/          #   stdio JSON-RPC 2.0 server + 23 tool definitions/dispatch
    direct/       #   in-process provider (no MCP): DirectMemory re-exports dispatch via RLock for Hermes/OpenClaw
    logic/        #   Logic Engine: resolve/heal/route + DashboardClient token auto-load
    brain/          # maintenance backend — never imported by src/core
    engine.py     #   BrainEngine(core, agents): 11 per-DB jobs (incl. core_helper + prune_empty_agents), snapshots+coalescing, .lock, contradictions, history
    scheduler.py  #   canonical order, DEFAULT/MUTATING sets, single-flight, daemon loop
    config.json   #   brain-only config (intervals, thresholds, weights, token)
    logs/ snapshots/ job_history.json contradiction_resolutions.jsonl
  dashboard/      # human UI — http.server, stdlib only
    server.py     #   threaded HTTP + header auth + path jail (see API.md for route count)
    static/
      dashboard.html  # shell (header, pills, stats bar, 12 tabs, toast/modal)
      app.js          # tab loader, api() (token+401), status/badges
      modules/*.js    # 12 tab modules (zero deps)
  memory/         # DATA ONLY — gitignored, never hand-edited
    core.db  agents.db  .lock  backups/  (deleted: no humantools/, no per-file agent_*.db — C4)
  bench/          # perf harness (stdlib)
  tools/          # one-shot v2→v3 splitter (read-only on v2)
  tests/          # 134 tests
  wiki/           # this documentation (19 files + logo)
```

Rule of thumb: `src/` = code you import. `brain/` = code that maintains. `dashboard/` = code that visualizes. `memory/` = data you never hand-edit.

## Data flow

### Writer map — the only writers (enforced by code review, C-concurrency)

| Database | Writers | Notes |
|---|---|---|
| `core.db` | `src/core/*` (`src/core/store.py:37`), `src/agents/bridge.py:promote` (insert), `brain` (under `memory/.lock`) | Core recall + curated writes |
| `agents.db` | `src/agents/*` (`src/agents/store.py:131`), `src/agents/mailbox.py:send` / `check_inbox` / `read` / `ack`, `src/agents/bridge.py:promote` (delete), `brain` (under lock) | Agent notes + mailbox |

No other module opens either DB. `brain` is the only writer that touches both.

### Connection invariant

Every connection comes from exactly one `connect()` per store:

```python
# src/core/store.py:37 and src/agents/store.py:131
conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")   # 8000 — not 0, v2 lacked this on hot path
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA foreign_keys=ON")
conn.execute(f"PRAGMA cache_size={int(cache_size)}")  # -64000 from v2 DEFAULT_CONFIG
```

No raw `sqlite3.connect` elsewhere (lint: grep). `check_same_thread=False` is required because Hermes reuses the same provider `ctx` from gateway `turn` vs `sync_turn` daemon threads (otherwise `ProgrammingError: SQLite objects created in thread A and this is thread B`). Callers own the transaction (`with connect(...) as conn:` commits on clean exit).

### Inter-process lock

`VACUUM` / snapshot create / restore / full scheduler runs serialize on `memory/.lock`:

* Reentrant within one thread (depth-counted `threading.local` in `brain/engine.py:219`) — a scheduler run holds the lock while its steps re-enter it for snapshots.
* Cross-platform: `msvcrt.locking` on Windows, `fcntl.flock` on POSIX (`brain/engine.py:240`).
* Timeout 120s; `run_job_now` single-flight via `threading.Lock` records `skipped_busy` in `job_history.json` (`brain/scheduler.py:112`).

Snapshot / restore always use the `backup()` API, never `shutil.copy2` on a live WAL DB (which orphans `-wal`/`-shm`).

### Read paths

* **Core recall** reads `core.db` only. It never sees `agents.db` by accident — cross-agent search is an explicit bridge call (`src/agents/bridge.py:58 search_all_agents`). DSL folding (`FIND ...` auto-detected by `src/core/recall.py:68 parse_query`) overrides `mode` without a separate tool (C1).
* **Agent recall** is the same engine with an `agent_id` predicate on every node access (`src/core/recall.py:191 _agent_pred` + `src/agents/store.py:312 agent_recall`). Isolation is SQL-level (`WHERE agent_id = ?`), not post-filtering.
* **Promotion** is a **move** (`agents.db -> core.db`, source clean-removed, provenance in `metadata.promoted_from`), idempotent on retry — there is no cross-DB transaction in stdlib `sqlite3`, so the protocol is at-least-once + converge (C7, `src/agents/bridge.py:101`):
  1. Verify source exists + belongs to `agent_id` (fail closed).
  2. Idempotency check: `SELECT ... WHERE json_extract(metadata,'$.promoted_from.node_id')=?` — if hit, skip insert, delete source, return existing `core_node_id`.
  3. Insert into `core.db` (reuse `node_id` when free else `node_<hex>`, `trust = max(orig, 0.8)`, `attention_state=core_verified`, `memory_layers` row, incremental vector, re-create co-resident edges).
  4. Delete source (`vector_df` decrement first, then `DELETE FROM nodes` with `FK CASCADE`).
  5. Commit order: `core` first, then `agents`. Retry converges via step 2.

### Write amplification budget

* Recall writes **only** `access_count` + `access_log` (`src/core/nodes.py:201 bump_access`, called by recall workers). Layer moves happen on the brain tick, never in the read path (`src/core/layers.py:1` — recall is read-heavy, `brain/engine.py:688 manage_tiers` owns promotions/demotions/decay/prune).
* `query_log` is buffered (`src/core/recall.py:143 QueryLogger`, flush every N, cap 5000 rows). `access_log` is TTL-pruned to 90 days by the brain (`brain/engine.py:892 prune_stale_unused`).
* `remember` delegates to `remember_many` bulk path (`src/core/nodes.py:111`); single inserts do not trigger divergent code.

## Key mechanisms

### Incremental TF-IDF (`src/core/vectors.py:1`)

V2 rebuilt the full `TfidfVectorizer` on every `remember` (`_load_vectorizer` full `SELECT label,content + fit`). V3 maintains `vector_df(term -> df)` and `vector_meta(ndocs)` incrementally:

* Insert: `df[term]+=1` per distinct term, `ndocs+=1`, store compact `term:weight` text + `magnitude` (`src/core/vectors.py:112 update_on_insert`).
* Delete: `df[term]-=1` (floor 0, rows deleted at 0), `ndocs-=1` (`src/core/vectors.py:125 update_on_delete`).
* Content edit: decrement old term set, increment new term set, restow (`src/core/vectors.py:133 refresh_on_content_change`).
* `idf = log((ndocs+1)/(df+1)) + 1` identical to v2 (`src/core/vectors.py:28`).
* Stored format: space-joined `term:weight` (6dp, sorted, ~3x smaller than v2 JSON dicts) + `magnitude REAL`. `decode()` tolerates legacy JSON (`src/core/vectors.py:43`).
* Drift: older vectors go slightly stale as `df/ndocs` move; next `rebuild_all()` (migration, brain tick when `auto_rebuild_vectors`, or manual) recomputes exactly (`src/core/vectors.py:158 rebuild_all`). `SEMANTIC` caps `node_index` pre-filter to `SEMANTIC_CANDIDATE_CAP=2000` (`src/core/recall.py:48`) and vocab-gates unknown terms.

### Mailbox auto-inject (`src/agents/mailbox.py:1`, `src/mcp/tools.py`)

* Tables live in `agents.db` (`mailbox` + `mailbox_receipts` + `idx_receipts_scope_noted`) so both sides reach them without agent writes to `core.db`.
* `mailbox.send(from_scope, to_scope, body)` fans out: unicast creates 1 receipt, `broadcast` (core-only, `agent -> broadcast` rejected with `hint: send to core`) fans out to one receipt per registered agent + `core` (`src/agents/mailbox.py:113`). Unknown `to` agent raises `unknown_agent, hint: call agent_ensure first`.
* **Only** the MCP dispatch wrapper (`src/mcp/tools.py` — the `with_inbox_notice` envelope) calls `mailbox.check_inbox(scope)` (`src/agents/mailbox.py:122`). It `SELECT`s un-noted receipts, marks `noted_at`, and **prepends** `Hey Core/Agent <id> — you have N message(s) from <from> — use mailbox.read / mailbox.ack to handle.` plus per-message date/preview (`src/agents/mailbox.py:240 format_injection`). Empty inbox: response is byte-identical (no noise, no write). Internal `store.py`/`recall.py` never touch the mailbox (C5).
 * Explicit handling: `mailbox.read(scope, state)` / `mailbox.ack(scope, msg_ids?)` update `mailbox_receipts` and mirror `read_at`/`acked_at` onto unicast `mailbox` rows for cheap dashboard counts (`src/agents/mailbox.py:196 _mirror_unicast`).
 * Dashboard Mail tab polls `GET /api/mailbox?scope=user|all` with backoff; human composes as `user` to `core`/`agent:<id>` via `POST /api/mailbox/send`.
 * **Review reminders** (`src/agents/mailbox.py:260 _sync_review_reminders` + `format_injection`): when any `review_ready` node exists, a sticky `kind=review_ready` mail is coalesced for `core` — single `[REVIEW READY] NODE X …` when 1, aggregated `[REVIEW QUEUE] You have N nodes…` when >1. It is injected on **every** core `tools/call` until `mailbox.ack` or `promote_to_core`, rendered distinct from normal mail and grouped before it.

### Snapshot coalescing (`brain/engine.py:278`, `brain/scheduler.py:129`)

* Manual snapshots (`POST /api/create_snapshot`, `Snapshot Now` button) always run, serialized under `memory/.lock`, rotated to `keep_last_snapshots` (default 10).
* Auto snapshots: toggle `auto_snapshot_before_jobs` (default OFF). When ON, `ensure_pre_run_snapshot` (`brain/engine.py:334`) checks the freshest `snapshot_<db>_*.db` mtime; if younger than `snapshot_cooldown_s` (default 300), it returns `snapshot_skipped: fresh_exists` instead of backing up. `run_job_now` coalesces: one pre-run snapshot per **touched** DB per run, not per job — `run_job_now(jobs=[dedup, compact, tiers, ...])` takes at most 1 snapshot for `core` and 1 for `agents` (`brain/scheduler.py:129`).
* `VACUUM` / `restore` never auto-snapshot (they already run under the lock; `restore` takes a pre-rollback `prerollback_*` backup instead).

### FTS — internal-content + scoped trigger (C19, C20)

* `node_fts` is an **internal-content** `fts5(label, content, node_id UNINDEXED)` (`src/core/schema.json:8`), not v2's `content='nodes'` external-content. Reason (C19, proven on `sqlite 3.50.4`): external-content `DELETE` via the documented `delete` command corrupts (`database disk image is malformed`) and plain `DELETE` silently no-ops, while internal-content `DELETE FROM node_fts WHERE node_id=?` works and `integrity_check` holds. Cost: `label`+`content` duplicated in the index; correctness first.
* Triggers (`src/core/schema.json:20`):
  ```sql
  CREATE TRIGGER nodes_ai AFTER INSERT ON nodes
    BEGIN INSERT INTO node_fts(label, content, node_id) VALUES (new.label, new.content, new.node_id); END;
  CREATE TRIGGER nodes_ad AFTER DELETE ON nodes
    BEGIN DELETE FROM node_fts WHERE node_id = old.node_id; END;
  CREATE TRIGGER nodes_au AFTER UPDATE OF label, content ON nodes
    BEGIN DELETE FROM node_fts WHERE node_id = old.node_id;
          INSERT INTO node_fts(label, content, node_id) VALUES (new.label, new.content, new.node_id); END;
  ```
  `nodes_au` is scoped to `UPDATE OF label, content` (C20). Access bumps (`access_count`/`updated_at` on every recall) never reindex FTS: 153ms per 175 bumps unscoped vs 1.2ms scoped (measured on `sqlite 3.50.4`, internal-content FTS). V2's trigger looked cheap only because its delete half silently no-oped.
* Query path: `SELECT n.* FROM nodes n JOIN node_fts f ON f.node_id=n.node_id WHERE node_fts MATCH ?` (`src/core/recall.py:297`), with sanitization (`src/core/lexicon.py:143 _sanitize_fts_query`) stripping `"` `*` `:` `-` to avoid `OperationalError`. `LIKE` fallback when FTS query is empty or throws.

### Layers

`memory_layers(node_id PK, layer, promoted_at, layer_order)` (`src/core/layers.py:14`). New nodes start `working` (`init_layer`). `get_layer` reads, `set_layer` writes (brain tick + `bridge.promote` only). The brain's `manage_tiers` (`brain/engine.py:688`) promotes `working -> short_term` after `short_term_promote_after` accesses, `short_term -> long_term` on hot/important, decays `importance` by `MEMORY_DECAY` per day, and prunes `prune_importance_floor` + stale + edgeless + not `review_ready` + not `PROTECTED_TYPES`. Agent `working` memory has its own regulator (`brain/engine.py:828 regulate_agent_working_memory`) scoring `acc*Wa + imp*Wi - ageH*Wd`, never touching core `WORKING`.

## What v3 deliberately does NOT do

* No vector DB / embeddings — TF-IDF + `node_index` pre-filter is the semantic layer.
* No HTTP API beyond the dashboard (`dashboard/server.py` is the only HTTP surface; `src/mcp/server.py` is stdio JSON-RPC 2.0).
* No multi-process clustering — still single-writer + WAL per DB (two WALs total, not one). No cross-DB transaction.
* No new features on the v2 monolith — the `asha_memory v2/` folder is look-up only and was never modified.
* No `humantools/` compat shim, no `/api/switch_db`, no `GET /api/db_bytes` whole-DB download, no binary `POST /api/manager_commit` whole-file replace, no CDN. Graph and Manager are full rewrites as native dashboard modules (`dashboard/static/modules/graph.js`, `manager.js`) talking `GET /api/graph?db=` / `GET /api/nodes?db=` + row endpoints under lock.

Further reading: `DATABASE.md` (schemas, indexes, triggers, conventions), `MODULES.md` (code map and import rules), `BRAIN.md` / `DASHBOARD.md` / `MCP_TOOLS.md` for subsystem detail.

