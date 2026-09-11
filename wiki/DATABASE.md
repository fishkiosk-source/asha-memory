# Database

Two SQLite files in `memory/` — `core.db` (curated knowledge) and `agents.db` (all agents, sharded by `agent_id`). Both are WAL (`PRAGMA journal_mode=WAL, synchronous=NORMAL, foreign_keys=ON, busy_timeout=8000, cache_size=-64000, check_same_thread=False` for Hermes multi-thread `turn`/`sync` reuse `src/core/store.py:37` + `src/agents/store.py:131`), both carry `PRAGMA user_version = 4`, both are created only via their store's `connect()` and `ensure_schema`/`migrate`. Never hand-edit the files.

Source of truth for DDL:

* `core.db` — `src/core/schema.json` (tables/triggers/indexes with C19/C20 notes) executed by `src/core/migrate.py:183 migrate`.
* `agents.db` — `src/agents/store.py:52 NODES_DDL` / `EDGES_DDL` / `AUX_DDL` + `src/agents/mailbox.py:32 MAILBOX_DDL`, created by `src/agents/store.py:146 ensure_schema`.

## `core.db` — curated knowledge

Idempotent migration chain `src/core/migrate.py:171 MIGRATIONS`: v1 base + triggers + 5 indexes, v2 `node_vectors`/`memory_layers`/`query_log` + layer backfill, v3 `ephemeral_events`/`vector_df`/`vector_meta` + 5 new indexes + seeds/backfills/orphan purge, v4 trigger rescoping (C20). `PRAGMA user_version` = **4** (`src/core/store.py:19 USER_VERSION_V3`). `schema_meta` holds `version=3.0` and `lexicon_version=3`.

### Tables

| Table | Columns (PK / FK / CHECK / DEFAULT) | Purpose |
|---|---|---|
| `nodes` | `node_id TEXT PRIMARY KEY`, `node_type TEXT NOT NULL CHECK IN ('PERSON','TOPIC','EVENT','FACT','PREFERENCE','BOUNDARY','AFFECT','AGENT_NOTE','CORE_REF','SKILL')`, `label TEXT NOT NULL`, `content TEXT NOT NULL`, `source TEXT NOT NULL DEFAULT 'CORE'`, `trust_level REAL NOT NULL DEFAULT 0.5 CHECK (0..1)`, `created_at INTEGER NOT NULL`, `updated_at INTEGER NOT NULL`, `access_count INTEGER NOT NULL DEFAULT 0`, `importance REAL NOT NULL DEFAULT 0.5 CHECK (0..1)`, `checksum TEXT NOT NULL` (sha256[:16] of content), `metadata TEXT NOT NULL DEFAULT '{}'` | Graph nodes. v2's 10-type CHECK preserved; `trust`/`importance` clamped to [0,1] at the boundary or `CHECK` would raise. |
| `edges` | `edge_id TEXT PRIMARY KEY`, `from_node TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE`, `to_node TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE`, `edge_type TEXT NOT NULL CHECK IN ('RELATES_TO','CONTRADICTS','SUPPORTS','CAUSED_BY','PART_OF','TRUSTS','DISTRUSTS','REMEMBERS','HAS_PREFERENCE','HAS_BOUNDARY','HAS_AFFECT','HAS_SKILL','REFERS_TO','SUMMARIZES','PROMOTED_FROM')`, `weight REAL NOT NULL DEFAULT 1.0 CHECK (-1..1)`, `created_at INTEGER NOT NULL`, `metadata TEXT NOT NULL DEFAULT '{}'`, `UNIQUE(from_node, to_node, edge_type)` | Directed edges. 15-type CHECK = v2's 14 + `PROMOTED_FROM` (bridge provenance, `src/core/edges.py:18`). `from_node`/`to_node` both `CASCADE` — deleting a node deletes its incident edges, vectors, layers, index, access rows. `INSERT OR REPLACE` semantics in `src/core/edges.py:52 relate`. |
| `node_fts` | `CREATE VIRTUAL TABLE node_fts USING fts5(label, content, node_id UNINDEXED)` | **Internal-content FTS** (C19). `node_id` is `UNINDEXED` stored-only, joined as `JOIN node_fts f ON f.node_id=n.node_id WHERE node_fts MATCH ?` (`src/core/recall.py:297`). Duplicates `label`+`content` in the index; v2's `content='nodes'` external-content is rejected because its `DELETE` corrupts / silently no-ops on `sqlite 3.50.4`. |
| `node_index` | `word TEXT NOT NULL`, `node_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE`, `field TEXT NOT NULL DEFAULT 'content'`, `weight REAL NOT NULL DEFAULT 1.0`, `PRIMARY KEY (word, node_id, field)` | Keyword -> node (top-20 `field=content` + `label` keywords/doc via `src/core/lexicon.py:103 _extract_keywords`, weights = `count/total`). Powers `RELATED` ranking and `SEMANTIC` pre-filter. Maintained explicitly in `src/core/nodes.py:86 build_index` / `97 clear_index`, not by trigger. |
| `node_vectors` | `node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE`, `vector TEXT NOT NULL` (compact `term:weight` sorted, 6dp), `magnitude REAL NOT NULL DEFAULT 0.0` | Per-node TF-IDF vector + magnitude. Replaces v2 JSON dicts. Written by `src/core/vectors.py:112 update_on_insert` / `125 update_on_delete` / `133 refresh_on_content_change`; full rebuild by `158 rebuild_all` only on migration / brain tick. |
| `vector_df` | `term TEXT PRIMARY KEY`, `df INTEGER NOT NULL DEFAULT 0` | Incremental document frequency per term. `INSERT ... ON CONFLICT DO UPDATE SET df=df+1` / `df=df-1` + `DELETE WHERE df<=0` (`src/core/vectors.py:96 _bump_df`). |
| `vector_meta` | `key TEXT PRIMARY KEY`, `value TEXT NOT NULL` | Single row `key='ndocs'` -> total doc count (`src/core/vectors.py:77 get_ndocs` / `83 _set_ndocs`). `idf = log((ndocs+1)/(df+1))+1` (`src/core/vectors.py:28`). |
| `memory_layers` | `node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE`, `layer TEXT NOT NULL DEFAULT 'working' CHECK IN ('working','short_term','long_term','archive')`, `promoted_at INTEGER`, `layer_order INTEGER NOT NULL DEFAULT 1` | Layer state machine. Writes owned by brain tick (`src/core/layers.py:37 set_layer`) and `src/agents/bridge.py:161 promote`; recall never moves layers. Default `working` fixes v2 divergence where DDL said `short_term` but `_init_node_layer` wrote `working` (`src/core/schema.json:2` note 2). |
| `ephemeral_events` | `id INTEGER PRIMARY KEY AUTOINCREMENT`, `label TEXT NOT NULL`, `body TEXT NOT NULL DEFAULT ''`, `created_at INTEGER NOT NULL`, `metadata TEXT NOT NULL DEFAULT '{}'` | Telemetry table (C13). No FTS/vectors/edges/index — telemetry never enters the graph. Unified with `nodes` where `label IN ephemeral_labels`: `brain/engine.py:942 compact_ephemeral` applies TTL + `keep_last` (preserving newest `keep` even if older than TTL) per label to **both** `ephemeral_events` and `nodes` (legacy `AGENT_NOTE` telemetry); `get_ephemeral_stats` `brain/engine.py:1404` and `get_bloat_metrics` `brain/engine.py:2173` sum both tables for `ephemeral in list` counts. |
| `access_log` | `log_id INTEGER PRIMARY KEY AUTOINCREMENT`, `node_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE`, `accessed_at INTEGER NOT NULL` | Recall touches (90-day TTL, brain prunes). |
| `query_log` | `log_id INTEGER PRIMARY KEY AUTOINCREMENT`, `query_text TEXT NOT NULL`, `mode TEXT NOT NULL`, `result_count INTEGER NOT NULL DEFAULT 0`, `duration_ms REAL NOT NULL DEFAULT 0`, `cache_hit INTEGER NOT NULL DEFAULT 0`, `queried_at INTEGER NOT NULL` | Recall log (5000-row cap, buffered `src/core/recall.py:143 QueryLogger`). |
| `schema_meta` | `key TEXT PRIMARY KEY`, `value TEXT NOT NULL` | `version=3.0`, `lexicon_version=3` (`src/core/schema.json:4`). Verified by `src/core/migrate.py:216 check`. |

### Triggers (C20 — scoped to `UPDATE OF label, content`)

| Trigger | When | Body |
|---|---|---|
| `nodes_ai` | `AFTER INSERT ON nodes` | `INSERT INTO node_fts(label, content, node_id) VALUES (new.label, new.content, new.node_id)` |
| `nodes_ad` | `AFTER DELETE ON nodes` | `DELETE FROM node_fts WHERE node_id = old.node_id` |
| `nodes_au` | `AFTER UPDATE OF label, content ON nodes` | `DELETE + INSERT` for the new `label`/`content` |

Access bumps (`access_count`/`updated_at` on every recall via `src/core/nodes.py:201 bump_access`) do **not** reindex FTS. Unscoped `AFTER UPDATE ON nodes` cost 153ms per 175 bumps; scoped costs 1.2ms (C20, `src/core/schema.json:19`). V4 rescopes all three triggers (`src/core/migrate.py:161 _apply_v4`).

### Indexes (12 = v2's 6 + 6 new, `src/core/schema.json:25`)

| Index | On | Origin |
|---|---|---|
| `idx_nodes_label_type` | `nodes(label, node_type)` | v1 base |
| `idx_nodes_updated_access` | `nodes(updated_at, access_count)` | v1 base |
| `idx_nodes_source` | `nodes(source)` | v1 base |
| `idx_edges_from` | `edges(from_node)` | v1 base |
| `idx_edges_to` | `edges(to_node)` | v1 base |
| `idx_memory_layers_layer` | `memory_layers(layer)` | v2 |
| `idx_nodes_type` | `nodes(node_type)` | **v3 new** |
| `idx_node_index_word` | `node_index(word)` | **v3 new** |
| `idx_access_log_node_time` | `access_log(node_id, accessed_at)` | **v3 new** |
| `idx_node_vectors_mag` | `node_vectors(magnitude)` | **v3 new** |
| `idx_query_log_time_mode` | `query_log(queried_at, mode)` | **v3 new** |
| `idx_ephemeral_label_time` | `ephemeral_events(label, created_at)` | **v3 new** |

All created `IF NOT EXISTS` from `schema.json`; `src/core/migrate.py:216 check` verifies missing tables / indexes / triggers / orphans / `user_version` / `lexicon_version`.

## `agents.db` — single file, all agents

Mirrors `core.db` **plus** `agent_id TEXT NOT NULL` on `nodes` and `edges`. Edges carry `agent_id` denormalized so per-agent walks/deletes never scan (`src/agents/store.py:69 EDGES_DDL`). One `agents.db` for every agent, sharded by `agent_id` column — no per-file `agent_*.db`.

DDL lives in `src/agents/store.py:52 NODES_DDL` (+ `agent_id`), `69 EDGES_DDL` (+ `agent_id`), `85 AUX_DDL` (mirrors core auxiliaries + `agents`/`agent_aliases`), `100 AUX_TRIGGERS` (same C20 scope), `107 AUX_INDEXES`, plus `src/agents/mailbox.py:32 MAILBOX_DDL`.

### Delta tables (only in `agents.db`)

| Table | Columns (PK / FK / CHECK) | Purpose |
|---|---|---|
| `agents` | `agent_id TEXT PRIMARY KEY`, `slug TEXT UNIQUE NOT NULL`, `job_hint TEXT NOT NULL DEFAULT ''`, `created_at INTEGER NOT NULL`, `metadata TEXT NOT NULL DEFAULT '{}'` | Identity registry. `slug` is `lowercase, [a-z0-9]+, dash-joined` from `job_hint` (`src/agents/store.py:179 _slugify`). Canonical IDs are `agent_<slug>_<hex4>` (`src/agents/store.py:196 agent_ensure`), idempotent on `slug` hit. |
| `agent_aliases` | `alias TEXT PRIMARY KEY`, `agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE` | v2 free-form ID migration map. `src/agents/store.py:244 add_alias` / `222 resolve_agent` checks `agents` then `agent_aliases`, so old references still resolve. |
| `mailbox` | `msg_id TEXT PRIMARY KEY`, `from_scope TEXT NOT NULL` (`core` / `agent:<id>` / `user`), `to_scope TEXT NOT NULL` (`core` / `agent:<id>` / `broadcast` / `user`), `body TEXT NOT NULL`, `created_at INTEGER NOT NULL`, `read_at INTEGER`, `acked_at INTEGER`, `metadata TEXT NOT NULL DEFAULT '{}'` | Messages. `src/agents/mailbox.py:32 MAILBOX_DDL`. Unicast rows get 1 receipt; `broadcast` (core-only) fans out at send time. `read_at`/`acked_at` mirrored from receipts for cheap dashboard counts. |
| `mailbox_receipts` | `msg_id TEXT NOT NULL REFERENCES mailbox(msg_id) ON DELETE CASCADE`, `scope TEXT NOT NULL` (`core` / `agent:<id>` / `user`), `noted_at INTEGER`, `read_at INTEGER`, `acked_at INTEGER`, `PRIMARY KEY (msg_id, scope)` | Per-recipient state. `src/agents/mailbox.py:43`. `noted_at` = first tool-call observation (auto-inject) or first Mail-tab poll. `scope='all'` is not stored — it is the admin view (`JOIN` with no scope filter). |

### Mirrored tables (same as core plus `agent_id` where noted)

`nodes` (+ `agent_id TEXT NOT NULL` after `node_id`), `edges` (+ `agent_id TEXT NOT NULL`), `node_fts` (same internal-content FTS, C19), `node_index`, `access_log`, `schema_meta`, `node_vectors`, `memory_layers`, `query_log`, `ephemeral_events`, `vector_df`, `vector_meta` — all `IF NOT EXISTS` in `src/agents/store.py:85 AUX_DDL`. Triggers identical and C20-scoped (`src/agents/store.py:100 AUX_TRIGGERS`).

### Indexes in `agents.db` (11 total)

| Index | On | Purpose |
|---|---|---|
| `idx_agents_agent_id` | `nodes(agent_id)` | **v3 sharding** — every agent query is `WHERE agent_id = ?` |
| `idx_agents_agent_updated` | `nodes(agent_id, updated_at)` | **v3 sharding** — `agent_digest` newest-first, `review_queue` ordering |
| `idx_agents_edges_agent` | `edges(agent_id)` | **v3 sharding** — per-agent walks/deletes without scan |
| `idx_nodes_type` | `nodes(node_type)` | v3 new (mirrors core) |
| `idx_node_index_word` | `node_index(word)` | v3 new |
| `idx_access_log_node_time` | `access_log(node_id, accessed_at)` | v3 new |
| `idx_node_vectors_mag` | `node_vectors(magnitude)` | v3 new |
| `idx_memory_layers_layer` | `memory_layers(layer)` | v2 |
| `idx_edges_from` | `edges(from_node)` | base |
| `idx_edges_to` | `edges(to_node)` | base |
| `idx_receipts_scope_noted` | `mailbox_receipts(scope, noted_at)` | **mailbox** — `check_inbox` hot path (`src/agents/mailbox.py:51`) |

`src/agents/store.py:120 REQUIRED_TABLES` (16 entries) and `161 check` verify tables + the 8 critical indexes.

### Identity

`src/agents/store.py:184 agent_ensure(job_hint, preferred_id?) -> {agent_id, created}`:

* Normalizes `job_hint` to `slug` (`re.sub(r'[^a-z0-9]+','-', lower).strip('-') or 'agent'`).
* `slug` hit -> return existing `{agent_id, created:false}`.
* New slug -> issue `agent_<slug>_<hex4>` (`uuid4().hex[:4]`), loop on `agent_id` collision, insert `agents` row.
* `preferred_id` honored only if free (not in `agents` nor `agent_aliases`) **and** `^[a-z0-9][a-z0-9_-]{1,40}$` (`src/agents/store.py:41 PREFERRED_ID_RE`), else `ValueError` naming the canonical ID as hint.
* Every agent write gates through `src/agents/store.py:236 require_agent` (`unknown_agent, hint: call agent_ensure first`).

## Cross-cutting conventions

### Timestamps

Integer epoch seconds (`int(time.time())`) everywhere. `created_at` = insertion time; `updated_at` = last modification. Recall bumps also touch `updated_at` (v2 parity for `RECENT` ordering, `src/core/nodes.py:201 bump_access` does `SET access_count=access_count+1, updated_at=?`) — FTS is unaffected thanks to C20 `UPDATE OF label, content` scoping.

### `metadata` JSON

Always `TEXT DEFAULT '{}'`, parsed with `json.loads` on read. Known keys:

| Key | Where | Meaning |
|---|---|---|
| `attention_state` | agent `nodes` | `agent_private` / `review_ready` / `core_verified` (`src/agents/store.py:44 ATTENTION_STATES`). `agent_private -> review_ready` via `src/agents/store.py:363 agent_set_attention`; `core_verified` only via `src/agents/bridge.py:101 promote`. |
| `agent_id` / `agent_scoped` | any `nodes` | Denormalized owner + scoping flag (`src/agents/store.py:251 _agent_metadata`). |
| `promoted_from` `{agent_id, node_id}` + `promoted_at` + `promoted_from_agent` + `original_agents_node_id` / `original_node_type` / `original_trust` | `core.db` promoted copies | Provenance for idempotency (`src/agents/bridge.py:128` checks `json_extract(metadata,'$.promoted_from.node_id')`). No tombstone in `agents.db` — source is clean-removed. |
| `contradiction_flag` / `contradiction_pair` / `contradictions_detected` | `FACT` nodes | Set by `src/core/nodes.py:415 _check_contradictions` (FACT-only, `PROMOTED_FROM`/`CONTRADICTS` edges weight `-conf`). |
| `_similarity` / `_clock` | recall responses | Transient, attached by `src/core/recall.py:543` (`SEMANTIC`) and `208 _attach_clock` — not persisted. |
| `clock_node` | `TODAY` nodes | `clock_tick` writes `TODAY` only to `core.db` (`src/core/clock.py:214 build_tick_content`). |
| `agent_ids` | merged dedup primaries | `src/brain/engine.py:532 _merge_agent_metadata` when two notes from different agents are the same content. |
| `kind` / `count` / `node_id` / `node_ids` | `mailbox.metadata` | `kind=review_ready` sticky review mails — `node_id`+`agent_id` when `count==1`, `count`+`node_ids[]` when aggregated (`src/agents/mailbox.py:260 _sync_review_reminders`, `src/agents/store.py:363`). |

### IDs

`node_<hex>` everywhere (`node_` + `uuid4().hex[:16]`, `src/core/nodes.py:52 _uuid`; `edge_` + same, `msg_` + same). C11 keeps `node_<hex>` for both DBs — `core_<hex>`/`agnt_<hex>` prefixes are rejected to avoid breaking clients holding stored IDs. Pre-insert collision check in both stores; bridge reuses the agent `node_id` in `core.db` when free else mints (`src/agents/bridge.py:93 _core_id_for`).

### Caps and retention

| Cap | Value | Where |
|---|---|---|
| `content` length | 500 chars core, 800 chars agent (truncate `...`) | `src/core/nodes.py:42 DEFAULTS`, `src/agents/store.py:47 AGENT_DEFAULTS`, `src/core/nodes.py:157` |
| `agent_max_notes` | 100 per agent (oldest `agent_private` evicted first, `review_ready` never auto-evicted) | `src/agents/store.py:262 _enforce_cap` |
| `query_log` | 5000 rows, buffered, oldest pruned on flush | `src/core/recall.py:49 QUERY_LOG_RETAIN`, `143 QueryLogger:164` |
| `access_log` | 90-day TTL pruned by brain | `brain/engine.py:892 prune_stale_unused` (cutoff `max_unused_days`) + `ARCHITECTURE.md` write-amp rule |
| `recall fetch_bound` | `bound` (with `include_agent_notes`) else `max(bound*5, bound+25)` | `src/core/recall.py:263` (core visibility: `src/core/recall.py:183 is_core_visible`) |
| `SEMANTIC_CANDIDATE_CAP` | 2000 | `src/core/recall.py:48` |
| `node_index` | top-20 keywords/doc | `src/core/lexicon.py:103 _extract_keywords` |
| `LEXICON_VERSION` | 3 | `src/core/lexicon.py:65`, stored in `schema_meta` |

### Health

`src/core/migrate.py:216 check(conn)` and `src/agents/store.py:161 check(conn)` are the read-only verifiers wired into `brain/engine.py` `health()` and `dashboard/server.py` `GET /api/health`. They report `ok`, `user_version`/`target`, `missing_tables`, `missing_indexes`, `missing_triggers`, `orphans` (`edges` pointing at missing nodes, `layers_missing`, `fts_missing`), `schema_meta`, and `ndocs`. `PRAGMA integrity_check` is called by brain health as well.

