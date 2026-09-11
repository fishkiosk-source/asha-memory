# Brain Engine — Asha Memory v3

`brain/engine.py` (`BrainEngine`) + `brain/scheduler.py` (`BrainScheduler`). Dual-DB throughout: every job runs per-DB and stores one connection per DB from `src.core.store` / `src.agents.store` (PRAGMA invariant). No code in `src/core` or `src/agents` imports the brain.

---

## 1. Canonical job order

Locked in `brain/scheduler.py:22` `JOB_ORDER`. Inputs are **sorted** to this order regardless of caller order. Discovery is last, mutations first.

| # | Job key | Engine method | What it does | Scope |
|---|---|---|---|---|
| 1 | `dedup` | `deduplicate()` | Exact merge (checksum + normalized label + normalized content) + TF-IDF near-duplicate merge at `dedup_similarity_threshold` (0.85). Relinks edges, deletes via `src.core.vectors.update_on_delete` df-exact cleanup + `DELETE FROM nodes`. Capped scans report `truncated:true` past `dedup_scan_cap` (2000). One automatic retry on `FOREIGN KEY` clash (concurrent delete). | whole `core` / per-`agent_id` |
| 2 | `compact` | `compact_ephemeral()` | Unified TTL sweep over `ephemeral_events` + `nodes` where `label IN ephemeral_labels` (C13 unified, fix 2026-09-11). TTL + `keep_last` (3) per label to both tables, preserving newest `keep` even if older than TTL; orphan purge via `vectors.update_on_delete`. | per DB (`both`) |
| 3 | `agent_working` | `regulate_agent_working_memory()` | WORKING janitor for `agents.db`. Score `= acc * Wa + imp * Wi - ageH * Wd`. Over high-water (12) or any entry older than `max_age_hours` (48) demotes lowest-score entries `working -> short_term` in batches of `demote_batch` (5). `review_ready` is protected (never demoted). | `agents` only |
| 4 | `core_helper` | `regulate_core_helper()` | **Core WORKING regulator** (Observer twin). Age≥`core_helper_min_age_hours` (72) + `trust`×`importance`×`access` rules: `trust≥0.8+imp≥0.6 → keep hot`, `trust≤0.5 → short_term`, `trusted but not important → short_term`, `mid trust + imp≥0.6 → acc≥4 long_term else short_term`. Never touches `short_term/long_term/archive`; archive manual-only. | `core` only |
| 5 | `age_prune` | `prune_stale_unused()` | Triple-gate prune: `updated_at <= now - max_unused_days` (4) AND `access_count <= 2` AND `importance < prune_importance_floor` (0.05) AND edgeless AND not protected type (`PERSON, SKILL, BOUNDARY, FACT, CORE_REF`) AND not `review_ready`. Deletes via `src.core.nodes.delete_node` (FK cascade, df-exact). `agents` and `core` use identical gates (C-fix). | per DB |
| 6 | `tiers` | `manage_tiers()` | **Decay-only** — no promotions (now owned by `agent_working` + `core_helper`). Decay `short_term * 0.97^days`, `long_term * 0.995^days`; prune stale+quiet (`updated_at ≤ max_unused_days`, `access<3`) below `prune_importance_floor` when edgeless and not `review_ready`/protected. Deletes via `delete_node`. | per DB |
| 7 | `contradictions` | `detect_contradictions()` | Sentiment-clash scan: 2+ shared meaningful words (`len>3`, not in STOPWORDS) AND `overlap/min_len >= contradiction_min_overlap_ratio` (0.25) AND opposite sentiment (`POSITIVE_WORDS` vs `NEGATIVE_WORDS`) -> `CONTRADICTS` edge (`weight -0.8`). Confidence `min(0.98, 0.25 + overlap*0.15 + gap*0.12 + imp*0.1)`, `status pending` if `confidence>=0.55` or `high_value` (avg importance >=0.6 or type `PERSON/PREFERENCE`) else `ignored`. Capped scans report `truncated:true`. | `core` whole-DB; `agents` per-`agent_id` (never cross-agent), edges stamped `agent_id` |
| 8 | `graduation` | `graduate_agent_notes()` | Manual-only promotion via `src.agents.bridge.promote` (MOVE, not copy). `node_ids=None` graduates ALL graduable (`review_ready` OR `trust>=0.7` AND `importance>=0.6`); explicit `node_ids` bypasses the gate. Graduated nodes become `core_verified` in `core.db` (`trust = max(orig, 0.8)`). | bridge `agents -> core` |
| 9 | `discover` | `discover_links()` | Semantic link discovery per scope (whole `core` / per-`agent_id`). Similarity band `[discover_link_floor, discover_link_ceil)` = `[0.50, 0.85)`: create `RELATES_TO`, don't merge. Paginated (`offset/limit`), no silent `[:100]`; capped groups report `truncated:true`. `agents` edges stamped via `agent_store.agent_relate`. Skips ephemeral labels + `_looks_like_json_log`. | whole `core` / per-`agent_id` |
| 10 | `prune_empty_agents` | `prune_empty_agents()` | Deletes agents with 0 nodes (ghosts) when `prune_empty_agents:true`, age-gated by `prune_empty_agents_min_age_hours` (24). Cleans `mailbox_receipts` for `agent:<id>` (aliases cascade). Dry-run via `get_empty_agents()`. | `agents` only |
| 11 | `vacuum` | `vacuum_db()` | `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM` on an **isolated** `sqlite3.connect` per DB, under the inter-process lock. Reports `before_bytes/after_bytes/saved_bytes` + MB. | per DB |

### Job set constants — `brain/scheduler.py:27`

```python
DEFAULT_JOB_TYPES = ["dedup", "compact", "agent_working", "core_helper", "age_prune", "tiers",
                     "contradictions", "discover", "prune_empty_agents"]  # excludes graduation, vacuum (11 jobs total)

MUTATING_JOBS = {"dedup", "compact", "age_prune", "tiers", "graduation", "agent_working", "core_helper", "prune_empty_agents"}
# contradictions / discover / vacuum never trigger vector rebuilds by design

JOB_RESULT_KEYS = {
    "dedup": "deduplicate", "compact": "compact_ephemeral",
    "agent_working": "regulate_agent_working", "core_helper": "regulate_core_helper",
    "age_prune": "age_prune", "tiers": "manage_tiers", "contradictions": "detect_contradictions",
    "graduation": "graduate_agent_notes", "discover": "discover_links", "prune_empty_agents": "prune_empty_agents",
    "vacuum": "vacuum",
}
```

`Run FULL` = `DEFAULT_JOB_TYPES`. `graduation` is never in a default run; `vacuum` only runs explicitly or when auto-vacuum fires.

---

## 2. Scheduler — `brain/scheduler.py`

### Single-flight

`BrainScheduler._run_lock` is a `threading.Lock`. `run_job_now` tries `acquire(blocking=False)`:

* Acquired -> holds `engine.locked()` for the whole run (inter-process lock, see §3), calls `_run_locked`, records `job_history.json` + markdown logs.
* Not acquired -> refuses. Writes entry `{"status": "skipped_busy", "timestamp": ..., "jobs": ..., "target": ...}` to `job_history.json` via `engine.record_history` and returns it. Dashboard button-mashing queues, never stampedes; daemon + manual runs cannot overlap.

```python
def _run_locked(self, jobs, target):
    ordered = sorted(jobs, key=lambda j: JOB_ORDER.index(j) if j in JOB_ORDER else 99)
    touched = sorted({db for j in ordered for db in self._job_dbs(j, target)})
    snapshots = self.engine.ensure_pre_run_snapshot(touched)  # coalesced
    # ... run each job in order via _run()
    # auto-vacuum when bloated, auto-rebuild vectors when ran mutating
```

* `target` = `core` | `agents` | `both` (alias `all`). `agent_working` is filtered to `agents` even when `both` (see `scheduler.py:102` `_job_dbs`).
* Per-mutating-job orphan sweep is re-applied at the scheduler layer (`_purge_db`) after each mutating job whose DB reported `status: success` (belt-and-suspenders over engine-internal sweeps).

### Daemon

```python
sched = BrainScheduler(engine=BrainEngine())  # reads brain/config.json
sched.start(interval_minutes=60)  # sets cron_enabled=True, spawns daemon thread
sched.stop()                      # clears flag, joins thread, sets cron_enabled=False
```

Loop in `scheduler.py:89` `_loop`: `wait(timeout=interval_minutes*60)`, then `run_job_now()` (defaults to `DEFAULT_JOB_TYPES`, `target=both`). `interval_minutes` is re-read from `engine.config` each iteration. `cron_enabled` is persisted.

### How to run

**Manually (dashboard / API / Python):**

```python
from brain.engine import BrainEngine
from brain.scheduler import BrainScheduler

engine = BrainEngine()  # brain_dir defaults to brain/, core/agents from memory/
sched = BrainScheduler(engine=engine)

# One full DEFAULT run against both DBs (coalesced snapshot, bloat check, vector rebuild)
sched.run_job_now()

# Selected jobs, single DB, explicit order ignored (sorted to canonical order)
sched.run_job_now(jobs=["discover", "dedup", "tiers"], target="core")

# Manual graduation (explicit nodes, or all graduable when node_ids=None)
engine.graduate_agent_notes(node_ids=["node_abc123"], agent_id="agent_foo_ab12")

# Manual vacuum or vector rebuild (standalone, not via scheduler)
engine.vacuum_db("both")
engine.rebuild_vectors("agents")
```

Dashboard equivalents: `POST /api/run_job {"jobs": [...], "db": "core|agents|both"}` -> `scheduler.run_job_now`; `POST /api/scheduler {"enabled": true, "interval_minutes": 60}` toggles daemon; `POST /api/vacuum`, `POST /api/rebuild_vectors`, `POST /api/compact_ephemeral`, `POST /api/graduate`, `POST /api/contradiction_action` call engine directly.

**As daemon (persistent):**

```python
engine = BrainEngine(config={"interval_minutes": 60, "cron_enabled": True})
sched = BrainScheduler(engine=engine)
sched.start()  # interval comes from config
# ... later
sched.stop()
```

On launch the dashboard server (`dashboard/server.py:45 configure()` + `2106 start_dashboard()`) creates `BrainEngine(brain_dir=brain/)` + `BrainScheduler`; if `brain/config.json:cron_enabled==true` it auto-calls `scheduler.start()` (persisted toggle, survives restarts — see `dashboard/static/modules/config.js:124 cfg-sched-toggle`). `GET /api/status` reports `scheduler.running` and `interval_minutes`.

---

## 3. Inter-process lock — `memory/.lock`

`BrainEngine.locked(timeout_s=120.0)` (`brain/engine.py:219`) is a re-entrant, inter-process exclusive lock:

* Path: `engine.lock_path` -> first writable of `core_path.parent / ".lock"` and `agents_path.parent / ".lock"`, else `./.lock`. In practice `memory/.lock` (both DBs live in `memory/`).
* Re-entrancy: thread-local `depth` counter. Same thread may nest `locked()` (scheduler holds it for the whole run while `create_snapshot` / `vacuum_db` / `restore_snapshot` take it again) — depth-counted, single underlying OS acquire.
* Windows: `msvcrt.locking(fh.fileno(), LK_NBLCK, 1)` in a poll loop (`sleep 0.05`) with wall-clock timeout. Release via `LK_UNLCK`.
* POSIX: `fcntl.flock(fh, LOCK_EX)` with `SIGALRM` timeout guard. Release via `LOCK_UN`.
* Held by: the whole `scheduler._run_locked` run, plus each snapshot, VACUUM, and restore individually. Serializes `backup()` API storms.

---

## 4. Snapshots

Files live in `brain/snapshots/`. Naming encodes the DB (`engine.py:280`):

* Regular: `snapshot_core_YYYYMMDD_HHMMSS.db`, `snapshot_agents_YYYYMMDD_HHMMSS.db`
* Pre-rollback: `prerollback_core_YYYYMMDD_HHMMSS.db` / `prerollback_agents_...` (written before a restore)

### Policy

| Trigger | When it runs |
|---|---|
| **Manual** `POST /api/create_snapshot` / `Snapshot Now` -> `engine.create_snapshot(db)` | **Always.** Takes a `backup()`-API copy under `locked()`, rotates, returns `{status, db, filename, size_bytes, created_at}`. |
| **Auto** at run start `engine.ensure_pre_run_snapshot(touched)` (`brain/engine.py:334`) | **Coalesced, at most one per DB per window.** Only if `auto_snapshot_before_jobs == true` AND no snapshot for that DB is younger than `snapshot_cooldown_s` (default 300). Otherwise returns `{snapshot_skipped: "toggle_off"}` or `{snapshot_skipped: "fresh_exists", fresh_age_s: ...}`. The `run_job_now(jobs=[...])` call takes **one** snapshot per touched DB, not one per job. |

Creation uses the SQLite `backup()` API on staging connections (sanctioned raw-connect exception) — never `shutil.copy2` on a live WAL DB (would orphan `-wal/-shm`) — still under `locked()` and serialized.

### Rotation & jailing

* `_rotate_snapshots()` keeps `keep_last_snapshots` (10) newest `snapshot_*.db` files by `mtime`; older are deleted. Applies after every manual snapshot.
* `_jailed_snapshot(filename)` rejects `..`, `/`, `\` and verifies `snapshots_dir.resolve() in p.resolve().parents` and existence. Used by `delete_snapshot` and `restore_snapshot`.
* `list_snapshots(db?)` returns `[{filename, size_bytes, created_at, mtime}]` sorted newest-first. `?db=core|agents|all` on `GET /api/snapshots`.
* Restore (`engine.py:377` `restore_snapshot`): `locked()` -> pre-rollback `backup()` of the live DB -> `backup()` from the jailed snapshot file into the live DB path -> `health(target=db)` verify -> `{status, db, restored, pre_rollback_backup, health_ok}`.

---

## 5. Per-DB results, `purge_orphans`, vector rebuild

### Per-DB results

Every engine job returns `Dict[str, Any]` keyed by DB:

```python
engine.deduplicate("both")
# {"core": {"status": "success", "exact_merged": 2, ...}, "agents": {"status": "success", ...}}
engine.regulate_agent_working_memory()
# {"agents": {"status": "success", "demoted": 3, ...}}  # no "core" key

sched.run_job_now(jobs=["dedup","tiers"], target="both")
# {"status": "success", "target": "both", "jobs": ["dedup","tiers"],
#  "duration_s": 1.234, "snapshots": {"core": {"snapshot_taken": "..."}, "agents": {...}},
#  "results": {"deduplicate": {"core": {...}, "agents": {...}}, "manage_tiers": {...}},
#  "health_after": {"core": {...}, "agents": {...}, "combined": {...}},
#  "markdown_log": "brain_run_...md", "markdown_logs": ["brain_run_...md", "brain_run_..._core.md", ...]}
```

Single-scope jobs report the other side as skipped via caller filtering (`agent_working` only populates `agents`). `health()`, `get_bloat_metrics()`, and `get_full_statistics()` add a `combined` key when `target in ("both","all")`:

* `health: {core: {...}, agents: {...}, combined: {total_nodes, total_edges, ok}}`
* `bloat: {core: {...}, agents: {...}, combined: {total_mb, needs_vacuum}}`
* `statistics: {core: {...}, agents: {...}, combined: {total_nodes, total_edges}}`

### Reports & history

`generate_markdown_report(entry)` (`engine.py:1726`) writes `brain/logs/brain_run_<ts>.md` (combined audit) plus `brain_run_<ts>_core.md` / `_agents.md` per touched DB (per-DB journals). A run's `health_after` is included. Rotation keeps `keep_last_logs` (30) newest logs by `mtime`. `job_history.json` (100 entries, `get_history`/`record_history`) stores every run including `skipped_busy`.

### Purge orphans

`engine._purge_orphans(conn)` (`engine.py:430`) sweeps after every mutating job, per DB:

```sql
DELETE FROM edges          WHERE from_node NOT IN (SELECT node_id FROM nodes)
                            OR to_node   NOT IN (SELECT node_id FROM nodes);
DELETE FROM node_vectors   WHERE node_id  NOT IN (SELECT node_id FROM nodes);
DELETE FROM memory_layers  WHERE node_id  NOT IN (SELECT node_id FROM nodes);
DELETE FROM access_log     WHERE node_id  NOT IN (SELECT node_id FROM nodes);
DELETE FROM node_index     WHERE node_id  NOT IN (SELECT node_id FROM nodes);
```

Returned as `{edges, node_vectors, memory_layers, access_log, node_index}` counts under `orphans_purged` in each DB's job result. The scheduler re-applies it once more after each mutating job that reported success (belt-and-suspenders over engine sweeps). `engine.purge_orphans(target="both")` exposes it standalone.

### Vector rebuild rules

`engine.rebuild_vectors(target)` does a full TF-IDF rebuild via `src.core.vectors.rebuild_all(conn)` per DB.

Auto-rebuild in `scheduler._run_locked` (`scheduler.py:165`):

```python
ran_mutating = any(j in ordered for j in MUTATING_JOBS)
# explicit vacuum job
if "vacuum" in ordered:
    results["vacuum"] = engine.vacuum_db(target)
# auto-vacuum when bloated (only if a mutating job ran and vacuum_after_prune)
elif ran_mutating and cfg.get("vacuum_after_prune"):
    bloated = [db for db in touched if engine.get_bloat_metrics(db)[db]["needs_vacuum"]]
    if bloated: results["vacuum"] = engine.vacuum_db(...)
# rebuild vectors (skipped for contradictions/discover/vacuum runs)
if ran_mutating and cfg.get("auto_rebuild_vectors"):
    results["vector_index_rebuild"] = engine.rebuild_vectors(target)
```

* Rebuild runs **after** auto-vacuum in the same `locked()` run.
* `MUTATING_JOBS` = `dedup, compact, age_prune, tiers, graduation, agent_working`. `contradictions` / `discover` / `vacuum` **never** trigger a rebuild by design — their `merge`/`keep`/`graduate`/`compact` paths call `rebuild_vectors` manually where needed.
* Individual jobs that delete via `nodes.delete_node` maintain vectors incrementally (`df` exact via `vectors.update_on_delete`); the full rebuild is the periodic converge.

---

## 6. Curation queues

### Contradictions — `CONTRADICTS` edges

* Detection: `detect_contradictions(target)` -> `{core: {...}, agents: {...}}` with `{status, contradictions_found, edges_created, edges_ignored, pending, truncated, scan_cap}` (+ `agents_scanned`, `per_agent` on agents side). Inserts `CONTRADICTS` edges `weight -0.8` with metadata `{detected_by, method: sentiment_clash, overlap_words, overlap_count, overlap_ratio, confidence, status: pending|ignored, importance_avg}`.
* Queue: `get_contradictions(status?, limit=50, db="core")` -> `{contradictions: [{edge_id, from_node, to_node, weight, created_at, metadata, db, agent_id, from_label, from_content, from_type, from_trust, to_label, to_content, to_type, to_trust, suggested_action, auto_resolvable}], counts: {pending, confirmed, ignored, resolved}, total}`. `resolved` comes from the append-only ledger `brain/contradiction_resolutions.jsonl` (fixes v2's never-incrementing counter). `suggested_action` is `keep_from`/`keep_to` when `from_trust < 0.3` and `to_trust > 0.8` (or vice versa).
* Triage: `update_contradiction_status(edge_id, "pending|confirmed|ignored|resolved", db)` flips `metadata.status`.
* Resolve: `resolve_contradiction(edge_id, "delete|keep_from|keep_to|merge", db)` -> `delete` removes edge only; `keep_from`/`keep_to` deletes the loser via `nodes.delete_node` (df-exact); `merge` concatenates `content[:800]` into `from`, refreshes vectors, deletes `to`. Each successful resolve appends to the ledger and (when `auto_rebuild_vectors`) rebuilds vectors. Returns `{status, action, ...}`.
* Auto: `auto_resolve_low_trust(dry_run=True, db="core")` -> dry run lists `candidates` (auto_resolvable pending). With `dry_run=False` and `contradiction_auto_resolve` ON, resolves up to 20 per run via `suggested_action`.

Dashboard: Contradicts tab `GET /api/contradictions?db=core|agents&status=&limit=` + `POST /api/contradiction_action`, `POST /api/contradiction_auto_resolve`.

### Graduate — `agents.db -> core.db`

* Stats: `bridge.review_stats` / `review_queue` -> `{total, review_ready, private, graduable, notes}`. `graduable = review_ready OR (trust>=0.7 and importance>=0.6)`. Explicit per-note selection bypasses the gate.
* `graduate_agent_notes(node_ids?, agent_id?)` -> `{core: {status, graduated, graduated_ids: {node_id: core_node_id}}, agents: {status, moved}}`. When `node_ids` is None, graduates all graduable. Each note moved via `bridge.promote` (6-step idempotent MOVE: verify + idempotency check on `promoted_from` provenance + core INSERT with `attention_state=core_verified` + source delete).
* Dashboard: Graduate tab `GET /api/graduate_preview` + `POST /api/graduate {node_ids}`.

### Observer — agent working regulator

* Preview: `get_agent_working_preview()` -> `{enabled, high_water, max_age_hours, weights: {wa, wi, wd}, demote_batch, agent_working_count, preview: [{node_id, agent_id, label, content:160, importance, trust_level, access_count, attention_state, is_review_ready, promoted_at, last_access, age_hours, score, days_left, hours_left, action}]}` where `action` in `protected (review_ready) | demote_next | stale_soon | keep`.
* Regulate: `regulate_agent_working_memory(dry_run=False)` -> `{agents: {status, demoted, demoted_ids, total_agent_working, high_water, max_age_hours, dry_run}}` (dry run adds `would_demote`).

---

## 7. Health, bloat, statistics

### `health(target="both")` — `engine.py:1676`

Per-DB via `_health_one`:

```python
{
  "db": "core",
  "path": "memory/core.db",
  "check": {"ok": bool, "errors": [...]},  # from src.core.migrate.check / src.agents.store.check
  "total_nodes": int,
  "total_edges": int,
  "node_types": {"FACT": 12, ...},
  "layers": {"working": 5, "short_term": 20, ...},
  "db_size_bytes": int,
  "db_size_mb": float,
  "snapshots": int,          # count of snapshot_<db>_*.db
  "status": "healthy" | "empty"
}
```

Plus `combined: {total_nodes, total_edges, ok}` when `target` is `both`/`all`. The dashboard `GET /api/status` surfaces this with `snapshots[:10]` and `scheduler.running`.

### `get_bloat_metrics(target="both")` — `engine.py:1620`

```python
{
  "page_count": int,
  "page_size": int,
  "freelist_count": int,
  "total_mb": float,
  "free_mb": float,
  "used_mb": float,
  "freelist_pct": float,            # freelist / page_count * 100
  "ephemeral_events": int,          # SELECT COUNT(*) FROM ephemeral_events WHERE label IN (allow)
                                    # + SELECT COUNT(*) FROM nodes WHERE label IN (allow) — unified
  "contradicts_total": int,         # SELECT COUNT(*) FROM edges WHERE edge_type='CONTRADICTS'
  "needs_vacuum": bool,             # freelist > vacuum_freelist_min_pages (50)
                                    #   AND freelist_pct > vacuum_freelist_threshold_pct (15)
  "vacuum_threshold_pct": float,
  "vacuum_min_pages": int
}
# combined when target both/all: {total_mb, needs_vacuum}
```

### `get_full_statistics(target="both")` — `engine.py:1687`

Per-DB: `{total_nodes, total_edges, node_types, edge_types, layers, sources, trust_avg, importance_avg, top_labels: [{label, cnt}] (10), db_size_mb}` plus `combined: {total_nodes, total_edges}`.

### Related helpers

* `get_ephemeral_stats(target)` — `{<db>: {total, labels: {label: {count, nodes, ephemeral_events, oldest, newest, samples}}}}` over `ephemeral_events` + `nodes` unified (`brain/engine.py:1404`).
* `discover_ephemeral_candidates(min_count=3)` — labels in `ephemeral_events` + `nodes` not on `ephemeral_labels`/`ephemeral_ignored` (`brain/engine.py:1433`).
* `set_ephemeral_allowlist(labels)` — replaces `ephemeral_labels` (sorted, deduped) and saves `brain/config.json`.

---

## 8. Config reference — `brain/config.json`

Single file. `brain/config.json` is the **only** config file (Idea.md rev8 C17 superseded: no `memory/config.json` overlay, no shared-section sync). It is created with `DEFAULTS` on first run and auto-migrates `prune_threshold` <-> `prune_importance_floor` (synced). Hand-edits go live via `engine.reload_config()` / dashboard `POST /api/config {"reload": true}` without restart. `POST /api/config_reset` restores `DEFAULTS`.

Types in the dashboard whitelist are `dashboard/server.py:1043` `_CONFIG_TYPES`; `engine.py:51` `DEFAULTS` is the source of truth.

| Key | Type | Default | Notes |
|---|---|---|---|
| `cron_enabled` | `bool` | `False` | Daemon flag; set by `scheduler.start/stop`. |
| `interval_minutes` | `int` | **60** (migrated live **2880**) | Daemon tick. |
| `auto_snapshot_before_jobs` | `bool` | `False` | Master toggle for coalesced pre-run snapshots (`brain/engine.py:334 ensure_pre_run_snapshot`). Manual snapshots ignore this. |
| `snapshot_cooldown_s` | `int` | `300` | Auto-snapshot window; at most one per DB per this many seconds. |
| `dedup_similarity_threshold` | `float` | `0.85` | TF-IDF cosine threshold for near-duplicate merge. |
| `short_term_promote_after` | `int` | `3` | `working -> short_term` when `access_count >=` this (tick-owned). |
| `dedup_scan_cap` | `int` | `2000` | Max nodes per scope in dedup semantic pass; beyond -> `truncated:true`. |
| `discover_scan_cap` | `int` | `2000` | Max nodes per scope in discovery. |
| `discover_link_floor` | `float` | `0.50` | Lower bound of link band (inclusive). |
| `discover_link_ceil` | `float` | `0.85` | Upper bound of link band (exclusive); above = merge, not link. |
| `prune_importance_floor` | `float` | `0.05` | Floor for `tiers` decay-prune and `age_prune` importance gate. Alias `prune_threshold` kept in sync. |
| `prune_threshold` | `float` | `0.05` | Alias of `prune_importance_floor`; the two are synced on load and save. |
| `auto_rebuild_vectors` | `bool` | `True` | Rebuild full TF-IDF index after any mutating job run. |
| `max_unused_days` | `int` | **4** (migrated live **5**) | Age gate for `age_prune`. |
| `ephemeral_labels` | `list[str]` | `["BRAIN_HISTORY","BRAIN_MAINTENANCE_REPORT","CRON_SUPERVISOR_REPORT","DAILY_STATE","FEED_SNAPSHOT","HN_SCOUT","HN_SCOUT_TOP3","RUNTIME_SAMPLE","SCOUT_WRAPPER_TOP_STORIES","TIME_ENTRY"]` (sorted, 10) | Allowlist for `ephemeral_events` telemetry labels. |
| `ephemeral_ignored` | `list[str]` | `[]` | Labels explicitly kept (excluded from `discover_ephemeral_candidates`). Mutual exclusion with `ephemeral_labels` via `POST /api/ephemeral_ignored`. |
| `prune_empty_agents` | `bool` | `False` (migrated live **True**) | Ghost auto-delete toggle — when true `prune_empty_agents:prune_empty_agents` runs in `Run FULL`/`DEFAULT_JOB_TYPES`. |
| `prune_empty_agents_min_age_hours` | `int` | `24` (migrated live **48**) | Age gate (hours since `agents.created_at`) before a 0-note agent is eligible for purge. |
| `ephemeral_keep_last` | `int` | `3` | Cap survivors per label after TTL sweep. |
| `ephemeral_max_age_days` | `int` | `7` | TTL for `ephemeral_events`. |
| `vacuum_after_prune` | `bool` | `True` | Auto-vacuum when bloated after a mutating run. |
| `vacuum_freelist_threshold_pct` | `float` | `15` | `needs_vacuum` freelist % threshold. |
| `vacuum_freelist_min_pages` | `int` | `50` | `needs_vacuum` freelist page-count threshold. |
| `contradiction_auto_resolve` | `bool` | **False** (migrated live **True**) | Opt-in auto-resolve of low-vs-high-trust contradictions (20/run cap). Dashboard badge reflects live. |
| `contradiction_low_trust` | `float` | `0.3` | Low-trust side threshold for `suggested_action`. |
| `contradiction_high_trust` | `float` | `0.8` | High-trust side threshold. |
| `contradiction_scan_cap` | `int` | `2000` | Max candidates in contradiction scan. |
| `contradiction_min_overlap_ratio` | `float` | `0.25` | Probability gate `overlap/min_len >=` this. |
| `keep_last_snapshots` | `int` | `10` | Snapshot rotation ceiling. |
| `keep_last_logs` | `int` | `30` | Markdown log rotation ceiling (`brain/logs/brain_run_*.md`). |
| `sqlite_cache_size` | `int` | `-64000` | SQLite `cache_size` pragma (negative = KB). Applied on every `core_conn` / `agents_conn`. |
| `agent_working_regulator_enabled` | `bool` | `True` | Master toggle for the WORKING janitor. |
| `agent_working_high_water` | `int` | `12` | Demote when `total >=` this. |
| `agent_working_demote_batch` | `int` | **5** (migrated live **3**) | Max entries demoted per run (stale-first, then lowest score). |
| `agent_working_max_age_hours` | `int` | `48` | Stale threshold (hours since `max(promoted_at, last_access)`). |
| `agent_working_weight_access` | `float` | `1.5` | `Wa` in `Score = acc*Wa + imp*Wi - ageH*Wd`. |
| `agent_working_weight_importance` | `float` | `4.0` | `Wi`. |
| `agent_working_weight_age` | `float` | `0.15` | `Wd`. |
| `dashboard_token` | `str` | **""** (migrated live **set**, e.g. `asha-sam-*`) | `X-Api-Token` header required when non-empty (no `?token=`, no localhost bypass, CORS off). Restrict file perms (`chmod 600`) after setting. |

Bolded defaults are those whose live `brain/config.json` (migrated from v2) differs from the code default — check the live file before the first `Run FULL`. All keys are live-tunable via the Config tab `POST /api/config` without restart.

Related files: `brain/job_history.json` (last 100 runs, appended by `record_history`), `brain/contradiction_resolutions.jsonl` (append-only ledger for `resolved` counts), `brain/logs/`, `brain/snapshots/`.

---

## 9. Related docs

* [MAIL](MAIL.md) — mailbox + receipts + injection + operator Mail tab (also dual-DB aware).
* [AGENTS](AGENTS.md) — `agents.db` sharding + bridge `promote`.
* [MCP_TOOLS](MCP_TOOLS.md) — 23-tool surface; mailbox tools + `with_inbox_notice` decoration.
* `ideas/Idea.md` §2 locks + §5 mailbox DDL + §7 brain/dashboard + §13.1 concurrency + §9.3 ephemeral table (normative).
* `ideas/V2_TO_V3_MIGRATION_PLAN.md` — phases 0–9 build order.

