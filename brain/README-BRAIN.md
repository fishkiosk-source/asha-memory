# ASHA Brain — Maintenance & Management Module

The **Brain** is an independent, autonomous maintenance supervisor for AshaMemory
databases. It operates decoupled from the AI runtime recall loop to keep the
memory graph healthy: deduplication, tier lifecycles, contradiction curation,
stale-node pruning, ephemeral log compaction, and semantic link discovery — with safety snapshots before
every mutation and dated audit logs after every run.

All code is pure Python standard library (sqlite3, http.server). No external
dependencies, no AI calls.

```
┌──────────────────────────────────────────────────────────┐
│                  HUMAN DASHBOARD                         │
│  brain_dashboard.py — dark manager-style web UI          │
│  Tabs: Overview | Maintenance | Graduate | Observer |     │
│        Contradicts | Ephemeral | Graph | Manager |       │
│        System | Statistics | Config                      │
│  - target DB switcher, live DB push to Graph/Manager     │
│  - scheduler control, health + bloat, full statistics    │
└───────────────────────────┬──────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────┐
│                  BRAIN SCHEDULER                         │
│  scheduler.py — background interval runner,             │
│  canonical order, orphan sweep, vacuum, vector rebuild  │
│  job history in job_history.json                        │
└───────────────────────────┬──────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────┐
│                   BRAIN ENGINE                           │
│  brain_engine.py                                        │
│  - DB resolution & safety snapshots                     │
│  - deduplication (core & agent scopes)                  │
│  - tier lifecycle + age-based pruning                   │
│  - contradiction detection (scored pending curation)    │
│  - agent-note graduation (MANUAL only)                  │
│  - ephemeral auto-detect & compaction (allowlist)       │
│  - auto VACUUM by freelist % threshold + vector rebuild │
│  - full statistics + bloat metrics                      │
│  - health metrics + markdown audit reports              │
└───────────────────────────┬──────────────────────────────┘
                            │ direct SQLite operation
┌───────────────────────────▼──────────────────────────────┐
│              Target AshaMemory core.db                   │
│  auto-discovered or configured (brain_config.json)      │
└──────────────────────────────────────────────────────────┘
```

---

## Core / Agent-Note Separation

AshaMemory v2 stores raw agent work as `AGENT_NOTE` nodes inside the same
`core.db` — **but agent notes are NOT core memory**. They are attention-scoped
(`agent_private` / `review_ready` / `core_verified`) and excluded from the main
AI's normal recall.

The Brain mirrors the memory system's own visibility rule
(`AshaMemory._is_core_visible`) in `BrainEngine.is_agent_note()`:

| Node                                           | Scope     |
| ---------------------------------------------- | --------- |
| `node_type != 'AGENT_NOTE'`, no agent metadata | **core**  |
| `node_type == 'AGENT_NOTE'`                    | **agent** |
| metadata `agent_scoped: true`                  | **agent** |
| metadata `attention_state == 'core_verified'`  | **core**  |

Every maintenance operation respects this boundary:

- **Deduplication** runs per scope — identical content in core and agent scope
  is never merged, and agent metadata (agent ids, attention state) is preserved
  when agent notes merge.
- **Contradictions, link discovery** never cross the
  boundary; agent notes can never receive `CONTRADICTS` edges.
- **Pruning / decay** applies to both scopes, but notes with
  `attention_state == 'review_ready'` are **never pruned** — they are waiting
  for a human/core decision.
- **Graduation** (agent note → core) is the only sanctioned crossing, and it is
  intentionally **manual-only** (see below).

---

## Maintenance Jobs

### `deduplicate()` — Deduplication

- **Exact:** same checksum + label + content → merge into one node, re-point all
  edges to the survivor.
- **Near-duplicate:** TF-IDF cosine similarity ≥ `dedup_similarity_threshold`
  (default 0.85) → merge content and re-point edges.
- Runs separately for core nodes and agent notes; agent merges preserve
  `agent_id` / `agent_ids` and the highest attention state.
- Edge re-pointing is conflict-safe (`UNIQUE(from_node, to_node, edge_type)`),
  self-loops and conflicting duplicates are dropped, and orphaned rows
  (`node_vectors`, `memory_layers`, `access_log`) are cleaned up.
- Result keys: `exact_merged`, `semantic_merged`, `total_merged`,
  `exact_merged_agent`, `semantic_merged_agent`, `total_merged_agent`,
  `edges_relinked`.

### `manage_tiers()` — Tier Lifecycle

Memory layers: `working → short_term → long_term → archive`.

- **Promotion:** `short_term → long_term` when `access_count >= 3` or
  `importance >= 0.8`.
- **Decay:** exponential importance decay per layer (`decay_factor ^ days`).
- **Pruning:** candidates below `importance < 0.05` with `access_count < 3` in
  `short_term` and no edges are removed — `review_ready` agent notes and
  protected core types (`PERSON`, `SKILL`, `BOUNDARY`, `FACT`, `CORE_REF`)
  are never pruned here.
- Results reported per scope: `promoted_core`, `promoted_agent`,
  `decayed_core`, `decayed_agent`, `pruned_core`, `pruned_agent`,
  `skipped_review_ready`, `skipped_protected` (legacy aggregate keys kept for
  compatibility).

### `prune_stale_unused_nodes()` — Age-Based Pruning

- Removes nodes not updated in `max_unused_days` (default 4) with
  `access_count <= 2`.
- **Agent notes** follow the staleness rule directly (they are cron/worker
  garbage) except `review_ready` notes, which are never pruned.
- **Core nodes** are only pruned when **all** of these hold:
  1. node type is not protected — `PERSON`, `SKILL`, `BOUNDARY`, `FACT`,
     `CORE_REF` are never age-pruned;
  2. `importance < prune_importance_floor` (config, default `0.05`);
  3. the node has **zero edges** (still referenced = still alive).
- Result keys: `removed_core`, `removed_agent`, `skipped_review_ready`,
  `skipped_protected`, `skipped_important`, `skipped_connected`,
  `pruned_count`, `removed_nodes` (each entry tagged with `scope`).

### `detect_contradictions()` — Conflict Detection (Scored Curation)

- Scans core `FACT` / `PREFERENCE` / `TOPIC` / `AFFECT` nodes for opposing
  sentiment on shared subjects (`meaningful overlap ≥2` words, `len>3` and not in `STOPWORDS` `shared_lexicon.py:56` — filters `it,is,and` noise) and creates weighted `CONTRADICTS` edges (weight −0.8). Also checked in `asha_memory_v2._detect_contradiction_v2:354` (same filter, `shared` filtered to `len>3` + not stopword, `≥2` required).
- Scoring: `confidence = 0.25 + overlap*0.15 + sentiment_gap*0.12 + importance_avg*0.1` (capped 0.98). Stored in `edges.metadata` as `{confidence, overlap_words, overlap_count, pos_a/neg_a/pos_b/neg_b, importance_avg, status}`.
- **Curation, not bulk delete:** `status` is `pending` (default, high-value or `confidence≥0.55`) or `ignored` (low-confidence + low importance). All edges are `pending` by default; legacy edges without status count as `pending`. Dashboard `Contradicts` tab curates via `get_contradictions()` / `update_contradiction_status()` / `resolve_contradiction()` (`delete`, `keep_from`, `keep_to`, `merge`).
- **Opt-in auto-resolve:** `auto_resolve_low_trust()` — if `contradiction_auto_resolve=true`, pending edges where one side `trust < low (0.3)` and other `trust > high (0.8)` are suggested as `keep high-trust` one-click; `GET /api/contradictions` returns `suggested_action` / `auto_resolvable`. Trigger via `POST /api/contradiction_auto_resolve {dry_run:false}` (caps 20/ run, rebuilds vectors if needed).
- Agent notes and ephemeral telemetry logs are excluded entirely.

### `graduate_agent_notes()` — Agent → Core (MANUAL ONLY)

- Converts agent notes to core memory: sets `node_type = 'FACT'`,
  `source = 'CORE'`, `trust_level = 0.95`, `attention_state = 'core_verified'`
  (node id and graph links are preserved).
- Applies to `review_ready` notes, or notes with `trust >= 0.7` and
  `importance >= 0.6`. **Explicit per-node:** `POST /api/graduate {node_ids:[...]}` / `graduate_agent_notes(node_ids=[...])` graduates exactly those IDs (bypasses trust/importance, still respects `is_agent_note` and `review_ready` protection) — dashboard `Graduate` tab supports tick-one / `Graduate Selected` / `Graduate` per row + bulk `Graduate All Graduable`. `graduate_single_note(node_id)` convenience.
- **Design decision:** graduation is deliberately **NOT part of routine
  maintenance**. Promoting agent work into core memory is a decision for the
  human (or the main core) — it never runs in scheduled/full maintenance, only
  when triggered explicitly (dashboard `Graduate` tab or `job_types=["graduation"]`).

### `regulate_agent_working_memory()` — Agent WORKING Janitor (agent-only, core untouched)

- Deterministic `Score = acc*Wa + imp*Wi - ageH*Wd` (`Wa:1.5 Wi:4.0 Wd:0.15` tunable `brain_config.json`, `config tab`) demotes low-score agent `WORKING` notes to `short_term` (preserves `node_id`/edges). Triggered when `agent_working >= high_water:12/20` or any `ageH >= max_age_hours:48`. Batch `demote_batch:5`, `review_ready` never touched. Core `WORKING` untouched (`is_agent_note` guard, scope-aware `AshaMemory._update_layer_on_access:971` evicts per-scope). Result `demoted/demoted_ids`. Preview via `get_agent_working_preview()` (score/days_left/hours_left/action `demote_next/stale_soon/keep/protected`) shown in dashboard `Observer` tab (`GET /api/agent_working_preview`, `POST /api/regulate_agent_working {dry_run}`).

### `discover_links()` — Semantic Link Discovery ("Serendipity")

- Finds latent semantic associations between unlinked nodes (TF-IDF cosine
  similarity 0.50–0.85) and creates `RELATES_TO` candidate edges.
- Runs within each scope only (core↔core, agent↔agent), never across the
  boundary. Result keys: `links_created_core`, `links_created_agent`.

### `compact_ephemeral_logs()` — Telemetry Compaction

- Caps append-only logs for `ephemeral_labels` (default **10** labels `shared_lexicon.py:86`: `FEED_SNAPSHOT`, `RUNTIME_SAMPLE`, `TIME_ENTRY`, `DAILY_STATE`, `CRON_SUPERVISOR_REPORT`, `BRAIN_MAINTENANCE_REPORT`, `BRAIN_HISTORY`, `SCOUT_WRAPPER_TOP_STORIES`, `HN_SCOUT_TOP3`, `HN_SCOUT`): keeps last `ephemeral_keep_last` (3) per label and `ephemeral_max_age_days` (7) TTL, removes stale `CONTRADICTS` between ephemerals. Edges removed with nodes, orphans purged. Allowlist unified with core `config.json` `ephemeral_labels` (P0-3) — edits sync to both.
- Allowlist is editable live via `add_ephemeral_label()` / `remove_ephemeral_label()` or dashboard `Ephemeral` tab. Auto-detect suggests candidates via `discover_ephemeral_candidates(min_count=3)` (heuristics: high freq, JSON shape via unified `_looks_like_json_log` `shared_lexicon.py:128` (`[:400]` + `timestamp && (post_count|load1m|status)`), low importance, few edges, `UPPER_SNAKE`).

### `vacuum_db()` — Freelist Reclaim

- `VACUUM` with `PRAGMA wal_checkpoint(TRUNCATE)` beforehand. Triggered automatically when `freelist > vacuum_freelist_min_pages (50)` and `freelist_pct > vacuum_freelist_threshold_pct (15)` — both configurable; also via `POST /api/check_vacuum` or `POST /api/vacuum`.

### ~~`summarize_clusters()`~~ — REMOVED

Cluster auto-summarization was removed: it generated `SUMMARY:` TOPIC
meta-nodes that polluted the graph. No longer part of brain maintenance.
The `SUMMARIZES` edge type remains available in the memory core for manual use.

---

## Ephemeral Auto-Detect (Safe Suggest-Only)

`discover_ephemeral_candidates()` scans labels not yet in allowlist:

- `COUNT(*) >= min_count`, `json_ratio` via `_looks_like_json_log()`, `avg_importance <0.5`, `edge_ratio <0.2`, `UPPER_SNAKE`, burst freq.
- Scores `≥3` and `(json_ratio≥0.4 or cnt≥10)` → candidate `{label, count, json_ratio, avg_importance, edge_ratio, score, reasons}`.
- Dashboard `Ephemeral` tab: left current allowlist chips (× to remove) + manual add; right candidate table (label/count/JSON%/score/reasons) → `Add` approves to `brain_config.json`. Never auto-deletes.

## Safety Snapshots & Rollback

- `create_snapshot()` — SQLite online backup of the target DB before any
  mutation (`brain/snapshots/snapshot_<db>_<timestamp>.db`).
- `list_snapshots()` / `restore_snapshot(filename)` — 1-click rollback
  (a `prerollback_*` backup is saved before restoring).
- Auto-snapshot before jobs is controlled by `auto_snapshot_before_jobs`
  in `brain_config.json`.

## Audit Reports

Every scheduler run produces a dated markdown audit report in `brain/logs/`
(e.g. `2026-08-12_151934_maintenance_report.md`) with the core/agent memory
composition and per-job results, plus a JSON history entry in
`job_history.json`.

---

## Dashboard

```bash
python brain_dashboard.py            # http://localhost:8500  (auto-increments if busy)
# health check (AI/human):
curl http://localhost:8500/api/health  # lightweight, no DB scan
```

Full docs: [`brain/DASHBOARD.md`](DASHBOARD.md).

Clean manager-style UI (`#0d0d0d`/`#1a1a1a`, no emojis):

| Tab             | Purpose                                                                                                                                                                                                                                            |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Overview**    | `Target DB` switcher + `Health & Bloat` + recent history                                                                                                                                                                                           |
| **Maintenance** | dedup / prune / tiers / contradictions / discover / `Run FULL` + `Compact Ephemeral` / `VACUUM` (freelist % hint)                                                                                                                                  |
| **Graduate**    | per-node + bulk: stats `total/review_ready/graduable/private`, checkboxes `Graduate Selected`/`Graduate` per row, `Graduate All Graduable` (explicit `node_ids` bypasses trust/imp)                                                       |
| **Observer**    | agent-only `WORKING` janitor: `Score=acc*Wa+imp*Wi-ageH*Wd`, `Days left` `(max_age-ageH)/24`, `demote_next/stale_soon/keep/protected (review_ready)`, `Preview`/`Regulate Now` (`dry_run`), `agent_working/high_water`                       |
| **Contradicts** | pending/confirmed/ignored filter, confidence/overlap (filtered `len>3` + stopword) + trust, `Confirm`/`Ignore`/`Delete`/`Keep From`/`Keep To`/`Merge`, `Preview Auto-resolve` + `Auto-resolve low-trust` (opt-in trust gap)                            |
| **Ephemeral**   | allowlist chips + `Scan Candidates` (score/reason) → `Add`                                                                                                                                                                                         |
| **Graph**       | embedded `humantools/asha_graph.html?embedded=1`, `Load Active DB` via `postMessage` + `/api/db_bytes`, respects `ephemeral_labels` from `/api/config`                                                                                             |
| **Manager**     | embedded `humantools/asha_manager.html?embedded=1`, same live DB push, ephemeral bloat uses config                                                                                                                                                 |
| **System**      | snapshots & rollback + audit logs (viewer) + execution history                                                                                                                                                                                     |
| **Statistics**  | full `get_full_statistics()`: nodes/edges, core/agent, trust/imp avg, bloat freelist, node/edge/layer/source breakdowns, top labels                                                                                                                |
| **Config**      | interval/max_days/similarity/prune floor/keep_last/max_age + `Vacuum Freelist %/Min Pages` + `Contra Low/High` + `Agent High-water/Demote batch/Max age/Wa/Wi/Wd` + toggles `auto_snapshot`/`auto_rebuild`/`vacuum_after_prune`/`contradiction_auto_resolve`/`agent_working_regulator_enabled` + `Check & Auto-VACUUM` |

Stats bar always visible: `Nodes | Core/Agent | Edges | DB Size | Snapshots | Bloat`.

Manager/Graph embedded via `iframe` + `window.postMessage({type:'asha-load-db', buffer})` and `asha-config-update` for live allowlist sync — no download needed, respects server allowlist.

> Note: the dashboard binds to all interfaces (`0.0.0.0`) per PLAN.md, so it is
> reachable on the local network — keep it on a trusted network. `dashboard_token` (`brain_config.json`, `brain_engine.py:48`) gates non-local binds via `X-Api-Token` header / `?token=` query (`brain_dashboard.py:35` `_check_auth`); `127.0.0.1` bypasses token. Use `--bind 127.0.0.1` for local-only (P2-4).

---

## Scheduler

```bash
python scheduler.py                  # manual test run (dedup + tiers)
```

- `run_job_now()` — runs the default job set:
  `dedup, compact, agent_working, age_prune, tiers, contradictions, discover`
  (`graduation` is intentionally excluded).
- **Canonical order is enforced** regardless of input order:
  `dedup → compact → agent_working → age_prune → tiers → contradictions → graduation → discover`.
  Mutating jobs run first; **link discovery runs LAST** so it never creates
  edges towards nodes that pruning would delete. `agent_working` (`brain_engine.py:1117`) demotes low-score agent `WORKING→short_term` (core untouched) — see above.
- **End-of-job orphan sweep** — after every job, `purge_orphans()` removes
  dangling edges and orphaned `node_vectors` / `memory_layers` / `access_log` /
  `node_index` rows left by that job's mutations. `PRAGMA foreign_keys=ON` is now enforced (`AshaMemory._core_conn:647`, `BrainEngine._connect_db:140`), so this is a safety net (previously required because FK cascades were not enforced).
- **Vector index rebuild** — after any mutating run (dedup / compact / age prune /
  tiers / graduation / resolve merge) the TF-IDF vectors are rebuilt via
  `AshaMemory.rebuild_vector_index_for_path(db_path)` static helper (`asha_memory_v2.py:822`, P2-6, no config side-effect, `busy_timeout=5000`) or `BrainEngine.rebuild_vectors()` (`brain/brain_engine.py:1094`, schedules outside jobs to avoid lock contention). Controlled by `auto_rebuild_vectors`
  (default `true`); can be triggered manually from the dashboard.
- **Auto VACUUM** — after mutating jobs, if `freelist > vacuum_freelist_min_pages` and `freelist_pct > vacuum_freelist_threshold_pct`, `vacuum_db()` runs automatically.
- `start(interval_minutes=N)` — background daemon thread running the default
  job set every N minutes.
- `update_config({...})` / `get_config()` — persist settings in
  `brain_config.json`.
- History: `job_history.json` (last 100 entries).

### brain_config.json

```json
{
  "last_db_path": ".../core.db",
  "cron_enabled": false,
  "interval_minutes": 60,
  "auto_snapshot_before_jobs": true,
  "dedup_similarity_threshold": 0.85,
  "prune_importance_floor": 0.05, // alias of core prune_threshold (P2-2, kept in sync)
  "auto_rebuild_vectors": true,
  "max_unused_days": 4,
  "sqlite_cache_size": -64000, // P1-1 configurable
  "ephemeral_labels": ["BRAIN_HISTORY","BRAIN_MAINTENANCE_REPORT","CRON_SUPERVISOR_REPORT","DAILY_STATE","FEED_SNAPSHOT","HN_SCOUT","HN_SCOUT_TOP3","RUNTIME_SAMPLE","SCOUT_WRAPPER_TOP_STORIES","TIME_ENTRY"],
  "ephemeral_keep_last": 3,
  "ephemeral_max_age_days": 7,
  "ephemeral_min_importance": 0.6,
  "vacuum_after_prune": true,
  "vacuum_freelist_threshold_pct": 15,
  "vacuum_freelist_min_pages": 50,
  "contradiction_auto_resolve": false,
  "contradiction_low_trust": 0.3,
  "contradiction_high_trust": 0.8,
  "agent_working_regulator_enabled": true,
  "agent_working_high_water": 12,
  "agent_working_demote_batch": 5,
  "agent_working_max_age_hours": 48,
  "agent_working_weight_access": 1.5,
  "agent_working_weight_importance": 4.0,
  "agent_working_weight_age": 0.15
}
```

---

## For AI Agents — Trust / Importance / Access / Ephemeral (read this)

*This section is for LLM agents using `asha_mcp.py` / `AshaMemory.remember()` — humans can skip.*

- **`trust 0.0–1.0` = provenance reliability** (`asha_memory_v2.py:remember(trust)`, `brain/brain_engine.py:1427` graduation). Set from source, not value. `0.2` hearsay / untrusted agent, `0.5` default, `0.8` verified tool, `0.95` human-confirmed (`core_verified`). Used by contradiction auto-resolve: `trust <0.3` vs `>0.8` → `keep high-trust` suggestion (`brain/brain_engine.py:1475`). Do **not** raise trust to avoid pruning — use `importance`.

- **`importance 0.0–1.0` = retention value** (`brain/brain_engine.py:105` tiers). `0.05` is prune floor, `0.6` review-ready threshold (`brain/brain_engine.py:1440`), `0.8` promotes `short_term→long_term`. Low importance + low access → decay/prune. Telemetry must stay `<0.5`.

- **`access_count` = usage frequency**. Incremented on recall. Together with `importance` decides promotion (`≥3` or `≥0.8`) and prune (`<3` and `<0.05` and `edges==0` in `short_term`).

- **`ephemeral`** = high-freq telemetry, not knowledge (`shared_lexicon.py:86` canonical 10 labels, `brain/brain_engine.py:27` + `config.json` `ephemeral_labels`). Unified `_looks_like_json_log` `shared_lexicon.py:128` (`[:400]` + `timestamp && (post_count|load1m|status)`). Content is JSON (`{"timestamp":..., "post_count":...}`) and `label` is `UPPER_SNAKE`. Keep `importance <0.5`, `edges=0` — brain caps to `keep_last`/`max_age_days` and never links it (auto-link / contradictions excluded).

- **Contradictions:** `confidence 0.0–1.0` in `edges.metadata` (`overlap*0.15 + sentiment_gap*0.12 + importance*0.1`). `pending` = needs review, `ignored` = low confidence auto-filtered. Confirm/ignore via dashboard; `merge`/`keep_*` deletes loser node.

**Rule of thumb for `remember()`:**

- User fact → `trust 0.9, importance 0.7` · Agent scouting → `trust 0.5, importance 0.4, AGENT_NOTE` · Sensor/cron log → `trust 0.6, importance 0.2, label EPHEMERAL` · Verified promotion → `trust 0.95`.

---

## Target DB Resolution

Priority order in `BrainEngine.resolve_db_path()`:

1. Explicit `db_path` argument (file or dir containing `core.db`)
2. `ASHA_MEMORY_DB_PATH` environment variable
3. Saved `last_db_path` in `brain_config.json`
4. Auto-discovery: `../asha_memory/core.db`, `../core.db`,
   `./asha_memory/core.db`, `./core.db`, `brain/core.db`
5. Fallback: creates `../asha_memory/core.db` (fresh schema)

`find_available_databases()` scans the workspace for all AshaMemory `.db`
files (skips snapshot backups) and flags the active one.
NOTE: If used through MCP .db folder will be asha_mcp_data near asha_memory

---

## REST API (dashboard)

`GET /api/status` → `{health,bloat,scheduler,databases,snapshots,history,logs}`
`GET /api/config` + `POST /api/config` / `POST /api/config_reset`
`GET /api/bloat`, `GET /api/statistics` (`get_full_statistics`), `GET /api/ephemeral_candidates`, `POST /api/ephemeral_allowlist {action:add|remove,label}`, `GET /api/contradictions?status=&limit=`, `POST /api/contradiction_action {edge_id,action:confirm|ignore|delete|keep_from|keep_to|merge}`, `POST /api/contradiction_auto_resolve {dry_run}`, `POST /api/check_vacuum`, `GET /api/graduate_preview`, `POST /api/graduate {node_ids?}`, `GET /api/agent_working_preview`, `POST /api/regulate_agent_working {dry_run}`, `POST /api/compact_ephemeral`, `POST /api/vacuum`, `POST /api/manager_commit` (binary SQLite upload), `GET /api/db_bytes`, `GET /api/health` (+ `/_static/` for `brain/static/dashboard.html` fallback inline `brain_dashboard.py:79`), `GET /humantools/*`

---



## File Layout

| File                 | Purpose                                         |
| -------------------- | ----------------------------------------------- |
| `brain_engine.py`    | Maintenance engine (all jobs, metrics, reports) |
| `scheduler.py`       | Background interval runner + job history        |
| `brain_dashboard.py` | Human web dashboard (pure http.server, `/_static/` serve, token auth) |
| `static/dashboard.html` | Dashboard UI (extracted from `brain_dashboard.py:553`, fallback inline) |
| `brain_config.json`  | Persistent configuration (`sqlite_cache_size`, `dashboard_token`, 10-label allowlist) |
| `job_history.json`   | Run history (last 100 entries)                  |
| `logs/`              | Dated markdown audit reports                    |
| `snapshots/`         | Safety backups + rollback source                |
