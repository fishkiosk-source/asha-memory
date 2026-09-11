# MCP Tools (23 + 1 deprecated alias)

Transport: stdio JSON-RPC 2.0 (`initialize`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `ping`). See [MCP_GUIDE](MCP_GUIDE.md) for wire format and error codes.

Locked surface (`src/mcp/tools.py:TOOL_NAMES`, `SERVER_VERSION 3.0.0`):

- **Core (6):** `remember`, `recall`, `relate`, `get_node`, `update_node`, `delete_node`
- **Agents (8):** `agent_ensure`, `agent_list`, `agent_remember`, `agent_recall`, `find_across_agents`, `promote_to_core`, `review_queue`, `set_attention`
- **Mailbox (3):** `mailbox.send`, `mailbox.read`, `mailbox.ack`
- **System (6):** `profile`, `health`, `stats`, `export`, `vacuum`, `compact`
- **Deprecated (uncounted):** `register_skill` → `remember(node_type=SKILL)` alias, removed next minor
- **Internal-only (not an MCP tool):** `rebuild_vectors` — call `POST /api/rebuild_vectors` on the dashboard; MCP has no vector-rebuild tool by design

Every `tools/call` result is a JSON string inside `{"content":[{"type":"text","text":"..."}]}`. When mail is pending, the decoded JSON also contains `mailbox_notice` (one inbox check per dispatch, empty inbox = no key).

## Core (6)

### `remember` — Store a memory node in core

Creates one node in `core.db` (single `remember_many` path inside).

| Param | Type | Required | Description |
|---|---|---|---|
| `content` | string | yes | Memory text. Clamped to `max_content_length` (500, `brain/config.json`) |
| `node_type` | string | yes | `PERSON\|TOPIC\|EVENT\|FACT\|PREFERENCE\|BOUNDARY\|AFFECT\|AGENT_NOTE\|CORE_REF\|SKILL` |
| `label` | string | no | Short headline (≤30 chars recommended). Defaults to `content[:30]`. Used by `WHO_IS`/`PATH` resolution and Manager search |
| `source` | string | no | `USER\|CORE` (default `CORE`). Agents cannot set this directly |
| `trust` | number | no | 0.0–1.0, clamped. Default 0.5 |
| `importance` | number | no | 0.0–1.0, clamped. Default 0.5 |

Return:

```json
{"node_id": "node_a1b2c3d4e5f6a7b8"}
```

Errors: `Invalid node_type: ...` → `TOOL_EXECUTION_ERROR`. Side effects: keyword index, incremental vectors, `working` layer, auto `RELATES_TO` links (≥2 shared terms, capped 10, weight `min(overlap*0.2,1.0)`), `FACT` contradiction scan.

---

### `recall` — Retrieve memories

TF-IDF / graph search over `core.db` (or one agent when called via `agent_recall`).

| Param | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Search text, node label, node_id, or `FIND ...` DSL (DSL overrides `mode`) |
| `mode` | string | no | `RELATED` (default) `\| SEMANTIC \| TIMELINE \| PATH \| CLUSTER \| WHO_IS \| WHAT_ABOUT \| RECENT \| PRUNE` |
| `bound` | integer | no | Max results (default 10) |
| `limit` | integer | no | Alias for `bound`; explicit `limit` wins |
| `offset` | integer | no | Pagination offset (default 0) |
| `include_agent_notes` | boolean | no | Include raw `AGENT_NOTE` rows (default false) |
| `node_type` | string | no | Post-filter on `node_type` |

Return:

```json
{
  "mode": "RELATED",
  "total_found": 12,
  "bound_applied": true,
  "clock": {"epoch": 1720000000, "iso": "2026-09-05T12:00:00", "date": "2026-09-05", "time": "12:00:00", "weekday": "Saturday"},
  "nodes": [
    {"node_id":"node_...","node_type":"FACT","label":"...","content":"... truncated 200","trust_level":0.8,"importance":0.7,"similarity":0.82,"age":{"added":"2 days ago","last_checked":"just now","access_count":3,"layer":"working","stale":false}}
  ]
}
```

- `content` truncated to `RECALL_CONTENT_TRUNC = 200` (use `get_node` for full text).
- `similarity` is `_similarity` from `metadata` (only `SEMANTIC` / `find_across_agents` populate it; otherwise `null`).
- `age` is `_clock` from `InternalClock.summarize_node` (added/last_checked/layer/stale).
- `total_found` is pre-pagination total; `bound_applied` is `total_found > bound`.
- `clock` is `clock.now()` or `null` when disabled.

Errors: `Invalid mode: ...` → `TOOL_EXECUTION_ERROR`.

---

### `relate` — Create a directed edge

| Param | Type | Required | Description |
|---|---|---|---|
| `from_id` | string | yes | Source `node_id` |
| `to_id` | string | yes | Target `node_id` |
| `edge_type` | string | yes | `RELATES_TO\|CONTRADICTS\|SUPPORTS\|CAUSED_BY\|PART_OF\|TRUSTS\|DISTRUSTS\|REMEMBERS\|HAS_PREFERENCE\|HAS_BOUNDARY\|HAS_AFFECT\|HAS_SKILL\|REFERS_TO\|SUMMARIZES\|PROMOTED_FROM` (see `src/core/edges.py:EDGE_TYPES`) |
| `weight` | number | no | -1.0–1.0, clamped (default 1.0) |

Return:

```json
{"status":"ok","edge_id":"edge_..."}
```

Errors: `Invalid edge_type: ...`, `relate(): cannot create edge '...' — node '...' does not exist` → `TOOL_EXECUTION_ERROR`.

---

### `get_node` — Get a single core node (full content)

| Param | Type | Required | Description |
|---|---|---|---|
| `node_id` | string | yes | Node identifier |

Return (found):

```json
{
  "node_id":"node_...","node_type":"FACT","label":"...","content":"full text (untruncated)",
  "trust_level":0.8,"importance":0.7,"similarity":null,
  "age":{"added":"2 days ago","last_checked":"just now","access_count":5,"layer":"working","stale":false},
  "clock":{"epoch":...,"iso":"..."},
  "metadata":{"contradiction_flag":true},
  "layer":"working"
}
```

Return (missing): `{"error":"not found"}`. Side effect: `bump_access` (access_count + access_log) + clock summary (pre-access `last_accessed_before`).

---

### `update_node` — Partially update a core node

| Param | Type | Required | Description |
|---|---|---|---|
| `node_id` | string | yes | Node to update |
| `label` | string | no | New label |
| `content` | string | no | New content (refreshes keyword index + vectors) |
| `trust_level` | number | no | 0.0–1.0, clamped |
| `importance` | number | no | 0.0–1.0, clamped |
| `source` | string | no | New source |
| `metadata` | string\|object | no | JSON merge patch (dict or JSON string); shallow-merged onto existing metadata |

Return: updated node (full, untruncated) or `{"error":"not found"}`. Errors: `Invalid update fields: ...`.

---

### `delete_node` — Delete a core node (cascades)

| Param | Type | Required | Description |
|---|---|---|---|
| `node_id` | string | yes | Node to delete |

Return: `{"status":"deleted"}` or `{"status":"not found"}`. Cascades: edges, vectors, layers, index, access log. `update_on_delete` decrements `df` exactly.

---

## Agents (8)

All agent tools gate on `require_agent` — unknown `agent_id` → `ValueError("unknown_agent, hint: call agent_ensure first: '<id>'")` → `TOOL_EXECUTION_ERROR` (-32002). Agents never invent IDs.

### `agent_ensure` — Issue (or return) the canonical agent ID

| Param | Type | Required | Description |
|---|---|---|---|
| `job_hint` | string | yes | Job description, e.g. `nightly scout`. Normalized to `slug` (`[a-z0-9]+` dash-joined) |
| `preferred_id` | string | no | Requested ID; if taken (agent_id or alias) returns existing (`created:false`); else honored only if free and `^[a-z0-9][a-z0-9_-]{1,40}$` |

Return:

```json
{"agent_id":"agent_nightly-scout_ab12","created":true}
```

Idempotent on `slug` and `preferred_id` (hit → `created:false`, same ID). `preferred_id` hit on `agent_id` or `agent_aliases` alias returns existing. New IDs deterministic `agent_<slug>_<hash4>` (`hash4=md5(slug)[:4]`, e.g. `AshaWeb`→`agent_ashaweb_4fee`; collision → `md5(slug:counter)`). Error only on invalid regex: `preferred_id rejected ('...'); canonical hint: agent_<slug>_<hash4>`.

---

### `agent_list` — List registered agents

No params.

Return:

```json
{"agents":[{"agent_id":"agent_test_abc1","slug":"test","job_hint":"test","created_at":1720000000}]}
```

---

### `agent_remember` — Store an agent note (always `AGENT_NOTE`)

| Param | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | yes | Canonical agent ID (call `agent_ensure` first) |
| `content` | string | yes | Note text. Clamped to `agent_max_content_length` 800 |
| `label` | string | no | Short label |
| `attention_state` | string | no | `agent_private` (default) `\| review_ready` |
| `metadata` | string\|object | no | Dict or JSON string; merged with `{"agent_id","agent_scoped":true,"attention_state"}` |

Return:

```json
{"agent_id":"agent_test_abc1","node_id":"node_..."}
```

Caps: `agent_max_notes` 100; oldest `agent_private` evicted first (never `review_ready`). Unknown agent → error above.

---

### `agent_recall` — Recall one agent's notes (isolated)

Scoped `recall` over a single agent (`WHERE agent_id = ?` on every access; `include_agent_notes=True`).

| Param | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | yes | Canonical agent ID |
| `query` | string | yes | Search text or `FIND ...` DSL |
| `mode` | string | no | Same 9 modes as `recall` |
| `bound` | integer | no | Max results (default 10) |
| `limit` | integer | no | Alias for `bound` |
| `offset` | integer | no | Pagination offset |

Return: `{"agent_id":"...","mode":"...","total_found":...,"bound_applied":...,"clock":...,"nodes":[...]}` (nodes shaped like `recall`, content truncated 200, `similarity`/`age` included, plus duplicate label versioning `label_display: "label v1/N (short)"`/`label_version: "v1/N"`/`node_id_short` when global duplicate `total>1` via `src/mcp/tools.py:158` _collect_label_versions - `v1` oldest by `created_at ASC` `vN` newest).

---

### `find_across_agents` — TF-IDF search over all agents (core use)

Cross-agent read is **core-only**; agents never call this (they use the mailbox).

| Param | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Search topic |
| `min_confidence` | number | no | Minimum `_similarity` (default 0.15) |
| `bound` | integer | no | Max results (default 10) |

Return:

```json
{"clock":{...},"total_found":3,"results":[{"node_id":"...","_agent_id":"agent_x","_similarity":0.42, ...}]}
```

Skips `core_verified` notes; `SEMANTIC` with `semantic_relevance_floor 0.0`, sorted by similarity.

---

### `promote_to_core` — Move an agent note to core (core use only)

6-step idempotent move (`src/agents/bridge.py:promote`): verify + isolation, provenance convergence, core INSERT (`trust = max(orig,0.8)`, layers+vectors, co-resident edges re-created), source deleted, core-commits-first.

| Param | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | yes | Owner agent |
| `agent_node_id` | string | yes | Agent node to promote |
| `new_type` | string | no | Target core type (`PERSON\|TOPIC\|EVENT\|FACT\|PREFERENCE\|BOUNDARY\|AFFECT\|CORE_REF\|SKILL`, default `FACT`) |
| `new_label` | string | no | Optional new label |

Return: `{"core_node_id":"node_...","status":"promoted"}` or `{"error":"agent node not found"}`.

---

### `review_queue` — List `review_ready` notes

| Param | Type | Required | Description |
|---|---|---|---|
| `bound` | integer | no | Max findings (default 20) |
| `limit` | integer | no | Alias for `bound` |

Return: `{"total_found":5,"notes":[...]}` (SQL-side `LIMIT`, ordered `updated_at DESC`).

---

### `set_attention` — Mark a note `agent_private` ↔ `review_ready`

`core_verified` is promotable-only; rejected here.

| Param | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | yes | Owner agent |
| `agent_node_id` | string | yes | Note ID |
| `attention_state` | string | yes | `agent_private \| review_ready` |

Return: `{"status":"updated"}` or `{"status":"agent node not found"}`.

---

### Bridge helpers exposed via MCP (aggregated above)

- `find_across_agents` is `bridge.search_all_agents`.
- `promote_to_core` is `bridge.promote`.
- `review_queue` is `bridge.list_review_queue`.

Direct-isolation helpers not on the MCP surface (callable via dashboard `POST /api/...` or Python): `agent_get_node`, `agent_delete_node`, `agent_relate` (isolation-checked, stamps `edges.agent_id`), `agent_digest` (newest-first list, `limit` 20 default). These are documented in [AGENTSKILLS](AGENTSKILLS.md) and [AGENTS](AGENTS.md).

## Mailbox (3)

Scopes: `core` | `agent:<id>` | `broadcast` (to-only, core sends) | `user`. Allowed: `core→agent`, `agent→core`, `agent→agent`, `core→broadcast`, `user↔core/agent`. Rejected: `agent→broadcast` (`hint: send to core`); only core may broadcast. Unknown agent on either side → `unknown_agent` error.

### `mailbox.send`

| Param | Type | Required | Description |
|---|---|---|---|
| `from` | string | yes | Sender scope `core\|agent:<id>\|user` |
| `to` | string | yes | Recipient `core\|agent:<id>\|broadcast\|user` |
| `body` | string | yes | Message text (non-empty) |

Return: `{"msg_id":"msg_..."}`. Broadcast fans out to one receipt per registered agent + core at send time.

Errors: `mailbox.send(): agent -> broadcast rejected (hint: send to core)`, `mailbox.send(): only core may broadcast (...)`, `invalid scope ...`, `unknown_agent ...`.

### `mailbox.read`

| Param | Type | Required | Description |
|---|---|---|---|
| `scope` | string | no | `core\|agent:<id>\|user\|all` (default `core`) |
| `state` | string | no | `pending\|noted\|all` (default `pending`) |
| `limit` | integer | no | Max messages (default 50) |
| `offset` | integer | no | Offset (default 0) |

Return: `{"scope":"core","messages":[{...}]}`. Reading marks `read_at` (receipt + unicast mailbox row mirror) unless `scope == "all"` (admin view, no side effect).

### `mailbox.ack`

| Param | Type | Required | Description |
|---|---|---|---|
| `scope` | string | no | `core\|agent:<id>\|user` (default `core`) |
| `msg_ids` | string\|array | no | List or JSON string; default all unacked for the scope |

Return: `{"acked": n}`.

### Injection envelope

`tools.dispatch` checks the caller's inbox (derived scope via `_scope_for`) exactly once per `tools/call` and formats pending messages via `mailbox.format_injection`. Empty inbox → response returned **unchanged** (no `mailbox_notice` key). Only the MCP dispatch wrapper touches the mailbox; store/recall code never does. Decoration failure never breaks the call.

**Sticky `[REVIEW READY]` / `[REVIEW QUEUE]`:** when any `review_ready` node exists, `_sync_review_reminders` (`src/agents/mailbox.py:260`) coalesces a `kind=review_ready` mail for `core` — single `[REVIEW READY] NODE <id> ("label") from agent:X at YYYY-MM-DD — use review_queue + promote_to_core` when `count==1`, aggregated `[REVIEW QUEUE] You have N nodes ready for review — please review via review_queue / promote_to_core` when `count>1`. It is injected on **every** core `tools/call` (sticky, until `mailbox.ack` or `promote_to_core`), rendered before normal mail and grouped as `[REVIEW READY]` vs normal `Hey Core — you have X message(s)…`.

## System (6)

### `profile` — Performance profile

No params.

Return:

```json
{"core_nodes": 123, "agents_nodes": 45, "core_query_log": 1000, "cache_hits": 12, "cache_misses": 80, "server_version": "3.0.0"}
```

### `health` — Integrity check for both DBs

No params.

Return: `{"ok": true, "core": {"ok": true, ...}, "agents": {"ok": true, ...}}` (missing tables/indexes listed).

### `stats` — Statistics per DB

| Param | Type | Required | Description |
|---|---|---|---|
| `db` | string | no | `all\|core\|agents` (default `all`) |

Return (per-DB): `{"nodes": 10, "nodes_by_type": {"FACT": 3}, "edges": 2, "db_mb": 1.234}`. Wrapped as `{"core": {...}, "agents": {...}}` when `all`.

### `export` — Export both DBs + manifest to tar.gz

| Param | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Destination `.tar.gz` path (parent dir must exist) |

Return: `{"path":"...","manifest":{"v3_version":"3.0.0","user_version":3,"created_at":...,"files":{"core.db":"<sha256>","agents.db":"<sha256>","config.json":"external\|absent"},"counts":{"core_nodes":...,"agents_nodes":...}},"status":"exported"}`. Also writes `<path>.manifest.json`. Uses `sqlite3.Connection.backup` onto an ephemeral target.

Errors: `export(): parent dir does not exist: ...` → `TOOL_EXECUTION_ERROR`.

### `vacuum` — VACUUM both DBs

No params.

Return: `{"core":{"before_mb":1.2,"after_mb":1.1},"agents":{"before_mb":0.8,"after_mb":0.7}}`.

### `compact` — Ephemeral TTL sweep

Operates on `ephemeral_events` table, not graph nodes.

| Param | Type | Required | Description |
|---|---|---|---|
| `keep_last` | integer | no | Keep last N per label (default 3) |
| `max_age_days` | integer | no | TTL days (default 7) |
| `db` | string | no | `all\|core\|agents` (default `all`) |

Return (per-DB): `{"removed_ttl": 5, "removed_cap": 2, "labels": ["label1", ...]}`. TTL always applies; `keep_last` caps the survivors (fixes the v2 no-op).

### Internal-only (not an MCP tool): `rebuild_vectors`

Full TF-IDF rebuild is a dashboard/brain operation only: `POST /api/rebuild_vectors {db?: all|core|agents}`. MCP omits it by design (incremental vectors on insert/delete are automatic; full rebuilds are maintenance, not recall-time work).

## Deprecated (uncounted)

### `register_skill`

| Param | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Skill identifier |
| `description` | string | yes | What it does (becomes `content` of a `SKILL` node) |
| `level` | string | no | Skill level (stored in `metadata.skill_level`, default `ASSIGNABLE`) |
| `tags` | string | no | Comma-separated keywords → `metadata.tags[]` |

Return: `{"skill":"...","node_id":"node_...","status":"registered","deprecated":"use remember(node_type=SKILL)"}`. Creates a `SKILL` node via `remember`; capability model is `SKILL` nodes + `HAS_SKILL` edges (C1). Removed next minor.

## Resources

`asha://memory/stats` → `cmd_stats`, `asha://memory/health` → `cmd_health`, `asha://memory/profile` → `cmd_profile`. See [MCP_GUIDE](MCP_GUIDE.md). Skills and bloat resources were deleted with the registry; bloat lives on the dashboard.

## Error cases (summary)

| Condition | Code | Message |
|---|---|---|
| Unknown tool | `-32002` | `Unknown tool: <name>` |
| Missing required arg | `-32002` | `Invalid arguments for <tool>: ...` |
| Unknown `agent_id` | `-32002` | `unknown_agent, hint: call agent_ensure first: '<id>'` |
| Invalid scope | `-32002` | `invalid scope '<scope>' as sender\|recipient` |
| `agent → broadcast` | `-32002` | `mailbox.send(): agent -> broadcast rejected (hint: send to core)` / `only core may broadcast (...)` |
| Invalid `node_type` / `edge_type` / `attention_state` | `-32002` | `Invalid ...: ...` |
| Export parent missing | `-32002` | `export(): parent dir does not exist: ...` |
| Invalid JSON on stdin | `-32700` | `Invalid JSON: ...` |
| Missing `method` | `-32600` | `Method required` |
| Unknown method | `-32601` | `Unknown method: ...` |
| Missing `name` / `uri` | `-32602` | `Tool name required` / `URI required` |
| Unknown resource | `-32003` | `Resource not found: ...` |

## Recall modes (9 modes + DSL auto-detect)

All 9 workers live in `src/core/recall.py`. DSL folding: any `query` starting with `FIND` is parsed by `parse_query` and overrides `mode` (`query_dsl` folded into `recall`, C1). Explicit `PATH "A -> B"` without `FIND` prefix also routes to `PATH`.

| Mode | What it does | Key params / DSL |
|---|---|---|
| `RELATED` | Keyword overlap via `node_index` (`COUNT(words) * SUM(weight) * importance * trust`), ordered descending. Empty keywords → FTS fallback. | `FIND` falls back to `RELATED` if no DSL matches |
| `SEMANTIC` | TF-IDF cosine over `node_vectors` (`term:weight` text). Vocab gating (`df==0` query terms dropped), `semantic_relevance_floor` 0.1, `SEMANTIC_CANDIDATE_CAP` 2000 pre-filter. Single fetch + per-node score. | `FIND SEMANTIC "text"` |
| `WHO_IS` | 1-hop neighborhood of a `PERSON` node (label exact → FTS → `PERSON` LIKE). Returns person + all direct neighbors ordered `importance*trust`. | `FIND PERSON "Name"` |
| `WHAT_ABOUT` | 1-hop + 2-hop around a `TOPIC` node (same resolution ladder, `TOPIC`-checked). Returns 2-hop BFS union ordered `importance*trust`. | `FIND TOPIC "X"`, `FIND FACT "X" -> TYPE` (resolution is type-gated: `PERSON→WHO_IS`, else `WHAT_ABOUT`) |
| `PATH` | Shortest path between two nodes (`heapq` Dijkstra, edge cost `1 - weight`). Resolves labels/ids/substrings via `resolve_ref`. | `"A -> B"` (explicit) or `FIND PATH "A" -> "B"` |
| `CLUSTER` | BFS surroundings from a seed (`deque`), capped at `bound`, returns BFS order. | `FIND <any> "X" CLUSTER` (e.g. `FIND TOPIC "X" CLUSTER`; note: `PERSON/TOPIC/EVENT/FACT` use the WHO_IS/WHAT_ABOUT branch, so non-core types are needed to reach CLUSTER — kept v2 quirk) |
| `TIMELINE` | Connected `EVENT`s of a node, newest first (2-direction JOIN, `DISTINCT`, `ORDER BY created_at DESC`). | `FIND TIMELINE "X"` or `FIND TIMELINE "X" SINCE "Y"` (SINCE parsed but unused — kept v2 parity) |
| `RECENT` | Nodes with `updated_at > now - hours*3600`, newest first. `hours` parsed from `query` (default 24 on parse failure). | `RECENT` with `query="24"` etc. |
| `PRUNE` | Candidates with `importance < threshold` and `access_count < 3` and `updated_at < now - 30d`, ordered `importance ASC`. | `PRUNE` with `query="0.5"` (threshold; default `prune_threshold` 0.05) |

### `_similarity` / `_clock` injection

- `SEMANTIC` (and `find_across_agents`) attach `metadata._similarity` (rounded 4 decimals) per node; `recall` surfaces it as `similarity` in the shaped node.
- Every recall (and `get_node`, and `find_across_agents`) attaches `metadata._clock` from `InternalClock.summarize_node` (added/last_checked/layer/stale). Clock uses `last_accessed_before` (strictly before the query's `access_log` write) so the current recall does not make `last_checked` read as `just now`.

### Pagination

- `bound` (alias `limit`) + `offset` apply **after** workers return up to `max(bound*5, bound+25)` pre-filtered rows and after `include_agent_notes` filtering. `explicit limit wins` (`_bound` helper).
- `total_found` is the visible set length (post-filter, pre-pagination). `bound_applied` is `total_found > bound`.
- `node_type` on `recall` is a post-filter on the paginated window.
