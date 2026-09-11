# Rulings (decision log C1–C20)

Locked calls from the v2→v3 migration. "Locked" = not re-litigated without a new ruling entry.

## Surface & structure

- **C1** — 23 restructured tools (not ~14): +`update_node`/`delete_node`/`agent_ensure`/`agent_list`/`agent_recall`/mailbox trio; folded `query_dsl→recall`, `rebuild→internal`, skill trio→recall/relate, `get_bloat_metrics→health`; `register_skill` thin alias, then deleted.
- **C2** — Maintenance exposes all 10 canonical jobs (incl. `prune_empty_agents`); `Run FULL` = DEFAULT (no graduation/vacuum); `MUTATING_JOBS` includes `prune_empty_agents` (no rebuild after contradictions/discover by design).
- **C3** — `Idea.md` is the sole spec source (no `Ideas.md` resurrection).
- **C4** — Deleted: `switch_db`, `db_bytes`, binary `manager_commit`, `/humantools/*` shim, per-file shard discovery. `?db=` everywhere instead. Before: per-file `agent_*.db` + whole-DB binary download/upload. After: `?db=core|agents|all` per endpoint, row-level `node_update`/`node_delete` under lock.
- **C9** — Graph is dependency-free Canvas (no D3 CDN, no vendor blob). Before: `humantools/asha_graph.html` via D3 CDN + iframe + `postMessage`. After: `dashboard/static/modules/graph.js` Canvas, `GET /api/graph?db=` JSON.
- **C10** — 12 tabs everywhere (Mail counts).
- **C14** — `tests/` scaffolded from day one (v2 shipped zero).
- **C15** — Manager row writes: commit + incremental vectors only; snapshot per policy; discover/rebuild deferred. Before: manager export/commit-back widened the lock. After: row ops + incremental index.
- **C16** — Weights canonical −1..1 in stores; MCP clamps 0..1 inputs.
- **C17** — `memory/config.json` wins conflicts; brain mirrors ephemeral + `prune_*`. **Superseded by operator directive 2026-09-05**: `brain/config.json` is now the **only** live config (no overlay from `memory/config.json`, no write-through, no `shared.*` section). `memory/config.json` is informational only (never read). `WIKI_DIR` / `_default_brain_dir()` note: dashboard resolves brain as `<v3root>/brain` (not `dashboard/`), fixing the `dashboard/config.json` split. `WIKI_DIR = Path(__file__).parent.parent / "wiki"` and `brain_dir = Path(__file__).parent.parent / "brain"` — single source, no duplication.

## Data & identity

- **C5** — Mailbox checks happen in the MCP dispatch wrapper ONLY (never in store/recall hot paths). Before: every `store`/`recall` call would pay `SELECT + UPDATE noted_at` on `agents.db`. After: `src/mcp/tools.py:dispatch` decorates the tool result once (empty inbox → no injection).
- **C6** — Broadcast uses per-recipient receipts (shared `noted_at` would re-notify forever). Before: single `mailbox.noted_at` row — one agent's `noted_at` silenced all, or never silenced. After: `mailbox_receipts(msg_id, scope)` fan-out on send, `WHERE scope=? AND noted_at IS NULL`.
- **C7** — Promote is at-least-once + idempotent converge (no cross-DB transactions in stdlib). `core` commit first, then `agents` delete; provenance `promoted_from={agent_id,node_id}` + unique lookup before insert; retry converges via existing `core_verified` hit.
- **C11** — Keep `node_<hex>` (no `core_/agnt_` prefixes — breaks stored IDs); collision checks instead. Before: proposal was `core_<hex>`/`agnt_<hex>` prefixes. After: keep hex, entropy + pre-insert check, provenance on `promoted_from`.
- **C12** — `?db=` default matrix: all→Overview/Maintenance/System/Statistics/Config; agents→Graduate/Observer; core+toggle→Contradicts/Ephemeral/Graph/Manager; mailbox is scope-based.
- **C13** — Telemetry lives in `ephemeral_events` table (never graph nodes); TTL always applies, `keep_last` only caps survivors; labels never auto-deleted. Migration moves `label IN allowlist OR _looks_like_json_log` rows out of `nodes`.
- **C18** — Review queue filters in SQL (`LIMIT` honored — v2 scanned-then-filtered in Python).

## Found by building (new rulings, proven by test)

- **C19** — `node_fts` is internal-content. Before: v2 external-content FTS (`content="nodes", content_rowid="rowid"`). `DELETE` on it either corrupts (`database disk image is malformed` on SQLite 3.50.4) or silently no-ops (delete half inherited by triggers). After: `CREATE VIRTUAL TABLE node_fts USING fts5(label, content, tokenize="unicode61")` with `AFTER INSERT/UPDATE/DELETE` triggers that `INSERT/DELETE ... node_fts(node_fts, rowid, label, content)` — internal content, explicit triggers. Proven by `tests/test_store_migrate.py` + Phase 9 bench gate (pre-fix v3 sat ~800ms/mode).
- **C20** — `nodes_au` scoped to `UPDATE OF label, content`. Before: `AFTER UPDATE ON nodes` fired on every `SET updated_at/access_count` bump from recall (153ms amortized over 175 bumps). After: `CREATE TRIGGER nodes_au AFTER UPDATE OF label, content ON nodes ...` — recall bumps never reindex FTS (1.2ms). v2 only looked cheap because its external-content delete half no-ops. Proven by bench `bench/bench_recall.py` warm-vs-cold delta + `tests/test_recall_modes.py`.

## Process

- **C8** (auth/path hardening — bind `127.0.0.1` default, CORS off, `X-Api-Token` header only; `safe_join` jail via `resolve() + is_relative_to`), plus concurrency rules (single `connect()` per store, `.lock` reentrant lock, scheduler single-flight) and the bench gate (fail on >20% regression).
- Open questions from §14 all resolved: agent→agent allowed (broadcast not), promote = clean move (no tombstone), core-assigned IDs (`spawn_agent` deleted), Canvas graph, deferred discover/rebuild on Manager edits.

> Directive note 2026-09-05: security/concurrency + single-config (`brain/config.json` only) are operator overrides that supersede C8/C17 where noted; wiki reflects the live behavior.

## New rulings (2026-09-09)

- **C25** — Scheduler toggle persistence. `dashboard/server.py:45 configure()` auto-calls `scheduler.start()` if `brain/config.json:cron_enabled==true` (set by `start()`/cleared by `stop()`). `dashboard/static/modules/config.js:124` replaces `Enable/Stop` buttons with single `cfg-sched-toggle` switch `schedToggle()` that persists `cron_enabled`+`interval_minutes` and survives dashboard restarts. `GET /api/status` still reports `scheduler.running`.

- **C26** — Direct skills doc. `wiki/DIRECTSKILLS.md` mirrors `wiki/SKILLS.md` but for in-process `src/direct/provider.py:1 DirectMemory` (Hermes/OpenClaw) — same `LogicEngine` heal, no MCP stdio, `RLock` + `check_same_thread=False`, `call(tool,args)` escape hatch.

## New rulings (2026-09-08)

- **C21** — Duplicate label versioning. `recall`/`agent_recall`/`find_across_agents`/`get_node` annotate globally-ranked `label_version: v1/N..vN/N` (`v1` oldest by `created_at ASC`, `vN` newest) plus `label_display: "label v1/N (short)"` and `node_id_short` (`src/mcp/tools.py:158` `_collect_label_versions`). Only when global `total>1`; brain janitor still owns dedup (`dedup` job). Keeps semantic search unchanged.
- **C22** — Manager default sort `created_at DESC`. `GET /api/nodes` default `?sort=created_at` (was `updated_at`) and fallback `n.created_at` (`dashboard/server.py:720`), `manager.js` default `sort: "created_at"` with new sortable `Date` (`created_at`) column keeping `Updated` (`updated_at`), both `App.ago`/`App.fdate` (`dashboard/static/modules/manager.js:14`). Preserves all prior columns.
- **C23** — `check_same_thread=False` on both stores. `src/core/store.py:37` and `src/agents/store.py:131` open with `sqlite3.connect(..., timeout=30.0, check_same_thread=False)` because Hermes reuses the same `open_context` `ctx` from gateway `turn` thread vs `sync_turn` daemon vs MCP stdio (`ProgrammingError` otherwise). `BUSY_TIMEOUT_MS=8000` + WAL + `RLock` in `src/direct/provider.py:17` serialize contending writes.
- **C24** — Direct provider. `src/direct/provider.py:1` `DirectMemory` re-exports the 23-tool surface via `src/mcp/tools.py:138` `dispatch` with a `RLock` and shared `InternalClock`/`LRUCache`, no stdio. Single source for tools (MCP is the stdio wrapper). For in-process Hermes/OpenClaw; external clients keep `python -m src.mcp.server`.

