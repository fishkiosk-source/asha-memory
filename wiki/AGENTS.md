# Agents

Agents live in the single `agents.db`, sharded by `agent_id`. Every query an agent
touches carries `WHERE agent_id = ?` — isolation is SQL-level, enforced in
`src/agents/store.py` (`require_agent` gates every entry point) and in the recall
workers (`recall(..., agent_id=)`). Cross-agent reads fail closed (return `None`/`False`,
never another agent's rows).

## Identity (core-assigned, locked ¨2.10)

Agents never invent IDs. The flow is always:

1. Core (or operator) calls `agent_ensure(job_hint, preferred_id?)`.
2. `job_hint` normalizes to a `slug` (`lowercase, [a-z0-9]+, dash-joined`).
3. Slug hit → return the existing ID (`created: false`). New slug → deterministic
   `agent_<slug>_<hash4>` where `hash4 = md5(slug)[:4]` (`created: true`); collision
   appends `md5(slug:counter)` so re-creation after ghost purge yields the same ID
   (`AshaWeb` → `agent_ashaweb_4fee` stable, `src/agents/store.py:agent_ensure`).
4. `preferred_id` is idempotent: if already taken as an `agent_id` or `agent_aliases` alias,
   return that ID (`created: false`) instead of error; else honored only if free
   and `^[a-z0-9][a-z0-9_-]{1,40}$`, otherwise rejected with the deterministic
   canonical hint.

Ghost cleanup: agents with 0 nodes are ghosts (`agents.db` `LEFT JOIN nodes`); `brain/engine.py:prune_empty_agents`
deletes them (and `mailbox_receipts` for `agent:<id>`, aliases cascade) when `brain/config.json:prune_empty_agents:true`,
age-gated by `prune_empty_agents_min_age_hours` (24). Preview via `GET /api/empty_agents`.

This fixes v2 fragmentation where one job (`AshaSovereignty` vs `ASHA_SOVEREIGNTY`
vs `asha_core` vs `asha-core`) splintered across IDs → the live migration merged two
such pairs and kept every old spelling as an `agent_aliases` row, so old references
still resolve (`resolve_agent` checks ID, then alias).
## Writing notes

`agent_remember(agent_id, content, label?, attention_state?, metadata?)`:

- Always stored as `AGENT_NOTE` (richer types are granted only via promotion — v2 parity).
- `attention_state` starts `agent_private`; `review_ready` flags it for core review.
- Caps (v2 parity): 800 chars, 100 notes/agent (oldest `agent_private` evicted first,
  `review_ready` never auto-evicted). Migration bypassed caps to preserve data.
- Same commit maintains keyword index, incremental vectors, `working` layer, and
  same-agent auto-links (cross-agent discovery is the bridge's job, not auto-link's).

## Reading notes

- `agent_recall(...)` — full recall power (all 9 modes + DSL), scoped to one agent.
- `agent_digest(limit)` — newest-first list. `agent_get_node` — single fetch.
- `find_across_agents` is **core-only**: TF-IDF over all agents, skipping `core_verified`,
  ranked with `_agent_id` attached. Agents never call it (they use the mailbox to ask).

## Attention lifecycle

```
agent_private ──set_attention──▶ review_ready ──graduate/promote──▶ core_verified (in core.db)
      ▲                                │                (MOVE: source removed, provenance kept)
      └──────── set_attention ─────────┘
```

- `set_attention` toggles private↔ready only; `core_verified` is promotable-only.
- `review_queue` / dashboard Graduate tab list `review_ready` (SQL-side `LIMIT`).
- Graduation threshold for bulk runs: `review_ready` OR (`trust≥0.7` AND `importance≥0.6`);
  explicit per-note selection bypasses the gate.
- `promote_to_core` (core/dashboard only): 6-step idempotent move — verify + isolation,
  provenance pre-check (retry converges), core INSERT (`trust = max(orig, 0.8)`, layers +
  vectors maintained, co-resident edges re-created, rest summarized), source deleted,
  core-commits-first ordering. No tombstones.

## Relating and deleting

- `agent_relate` requires both endpoints owned by the caller and stamps `edges.agent_id`
  (walks stay in-scope; `PATH`/`CLUSTER` never cross agents).
- `agent_delete_node` is isolation-checked; vectors/index cascade via FK.