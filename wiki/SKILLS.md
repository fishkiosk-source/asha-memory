# SKILLS — Unified Playbook for AI (Core + Agent)

> One skill tree. One call does the workflow. If your job context provides `agent_id`/`job_hint`, you are an **Agent** — pass it as `agent` on every tool; engine scopes you to your isolated rows `src/agents/store.py:312`. If you have no `agent`, you are **Core**. This file replaces `CORESKILLS.md` + `AGENTSKILLS.md` (kept as redirects).

For humans: [ARCHITECTURE](ARCHITECTURE.md) + [MCP_TOOLS](MCP_TOOLS.md) + [API](API.md) + [DASHBOARD](DASHBOARD.md) + [DIRECTSKILLS](DIRECTSKILLS.md) (same logic via `DirectMemory`) + `logicengine.md` (plan).

---

## 0. The one rule

```
if agent_param_resolves -> agents.db (WHERE agent_id = ?)  else  core.db
```

- **Core** (no `agent`): curated `core.db`. Curates, graduates, resolves contradictions.
- **Agent** (have `agent`): single file `agents.db` sharded by `agent_id`. You see only `agent_id = your_id` rows. Everything else via `mailbox` or ask Core.
- Skill hint in job prompt: *If you have `agent_id`, use it; else you are Core.* Logic Engine obeys.

All 23 MCP tools (`src/mcp/tools.py:45`) + Direct API (`dashboard/server.py`) share the same Logic Engine `src/logic/engine.py`. Old `agent_ensure`/`agent_remember`/`agent_recall` are still callable but deprecated — unified `remember`/`recall`/`relate`/`get`/`update`/`delete` with optional `agent` is preferred.

---

## 1. Logic Engine — what it is and what it does

**Location:** `src/logic/engine.py` (neutral package, imports `src/core/*` + `src/agents/*`, never imported by `src/core` per `wiki/MODULES.md`), `src/logic/api_client.py` for HTTP.

**Wraps:** `src/mcp/tools.py:130 dispatch` and `dashboard/server.py:45 configure` + `_post_*` handlers. Old handlers are thin shims.

**Does in one call (`AI --one call--> resolve().heal().route() --commit--> DB`):**

| Step                                                                     | What                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Resolve agent** `LogicEngine.resolve_agent(ref, *, auto_create: bool)` | 6-step: `None→core` → exact `agents.agent_id`/`agent_aliases` `src/agents/store.py:240` → `slug` hit `src/agents/store.py:179 _slugify` → source/substring heuristic (`slug LIKE %tok%`, `nodes.source LIKE`) for `web scout → AshaWeb` → `auto_create=True` → `agent_ensure` `src/agents/store.py:184` deterministic `agent_<slug>_<md5[:4]>` `src/agents/store.py:217` → validate `PREFERRED_ID_RE` `src/agents/store.py:41`. `auto_create=True` on writes (`remember`/`relate`/`mail`), `False` on reads (`recall`/`get`) — no ghost storm, `prune_empty_agents` `brain/engine.py:828` cleans ghosts after `min_age_hours`. |
| **Heal, not fail**                                                       | Missing `node_type → FACT` (core) / `AGENT_NOTE` (agent), `trust`/`importance` → `0.5` clamped `src/core/nodes.py:68`, invalid `mode` → `RELATED`, `bound` coerce `1..200`, `edge_type` → `RELATES_TO`. Returns `{"healed": {"field": "orig -> healed"}}` on response **and** persists `metadata.healed_fields: ["node_type: garbage -> FACT"]` ring buffer cap 5 `src/logic/engine.py:18` for audit (`GET /api/nodes?` shows it).                                                                                                                                                                                             |
| **Route**                                                                | Exact-ref guardrail #1: `relate`/`get`/`update`/`delete` use `resolve_ref_exact` `src/core/nodes.py:287 node_id=? + 291 label=?` only; `fuzzy=True` required for `LIKE` fallback `src/core/nodes.py:295`. No silent nondeterminism on edges.                                                                                                                                                                                                                                                                                                                                                                                   |
| **DB commit**                                                            | Single-DB commit. Cross-DB only `promote` with `core-first-then-agents` + idempotent `promoted_from` `src/agents/bridge.py:128`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| **Token for Direct API**                                                 | `src/logic/api_client.py:14 load_dashboard_token(brain_dir)` reads `brain/config.json:49 dashboard_token` (`asha-sam-…`) and injects `X-Api-Token` header. Agents have no user token — LogicEngine reads the file and supplies it, so `127.0.0.1` and `0.0.0.0:$port` both pass `dashboard/server.py:235 _check_auth` (only `health`/`ping` bypass C8). `DashboardClient(base_url, brain_dir)` wraps `GET /api/*` + `POST /api/*`.                                                                                                                                                                                             |

If you have `agent_id` in context, **always** pass `agent: "<id or hint>"` — engine will canonicalize `"AshaWeb"` / `"web scout"` / `"agent_ashaweb_4fee"` to sticky `agent_ashaweb_4fee` and preserve `source` verbatim as `source` + `metadata.agent_hint` (intentional circularity for next resolve).

---

## 2. Unified MCP tools

`src/mcp/tools.py:747 definitions()` — same 23 names, unified shapes with optional `agent`:

### `remember` (unified `remember` + `agent_remember` + `register_skill`)

One fact per call, one node = one idea (`FACT` triggers `CONTRADICTS` scan, `SKILL` capability model).

| Param                  | Type       | Notes                                                                                                                                                                            |
| ---------------------- | ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `content`              | string yes | Clamped 500 core / 800 agent (`…`) `src/core/nodes.py:157` / `src/agents/store.py:320`                                                                                           |
| `node_type`            | string     | Optional now — heals to `FACT`/`AGENT_NOTE`. Was required.                                                                                                                       |
| `label`                | string     | ≤30 headline, `WHO_IS`/`PATH` + Manager search. Fallback `content[:30]`. Trimmed.                                                                                                |
| `agent` / `agent_id`   | string     | Hint or canonical. If present → `agents.db` `AGENT_NOTE` (caps `100`/`800`, oldest `agent_private` evicted, `review_ready` protected `src/agents/store.py:262`), else `core.db`. |
| `trust` / `importance` | number     | 0..1 clamped, default 0.5. Rank = `importance*trust`, prune floor `0.05`, graduation `≥0.7 & ≥0.6`.                                                                              |
| `source`               | string     | `CORE`/`USER` or agent source label (`AshaWeb` preserved).                                                                                                                       |
| `attention_state`      | string     | Agent only: `agent_private` (default) → `review_ready` via `set_attention` or at write time. `core_verified` promotable-only.                                                    |

```json
{"name":"remember","arguments":{"content":"Sam prefers dark mode","node_type":"PREFERENCE","label":"Sam UI","trust":0.9,"importance":0.8,"source":"USER"}}
{"name":"remember","arguments":{"content":"Found 3 stale FACTs","agent":"AshaWeb","label":"budget stale"}}
{"name":"remember","arguments":{"content":"Hello","agent":"web scout"}}
```

### `recall` (unified `recall` + `agent_recall`) — plus `find_across_agents` standalone

9 modes `src/core/recall.py:44` + DSL auto-detect `parse_query`. `FIND` overrides `mode`; `"A -> B"` also routes to `PATH`.

| Param                    | Notes                                                                                                                                                                                                                                                                                                    |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `query`                  | text / label / `node_id` / `FIND …` DSL                                                                                                                                                                                                                                                                  |
| `mode`                   | `RELATED` (default, keyword `node_index` ) · `SEMANTIC` (TF-IDF cosine floor 0.1, `SEMANTIC_CANDIDATE_CAP 2000`) · `WHO_IS` (1-hop `PERSON`) · `WHAT_ABOUT` (2-hop `TOPIC`) · `PATH` (Dijkstra `1-weight`) · `CLUSTER` (BFS) · `TIMELINE` (`EVENT`s newest) · `RECENT` (`hours`) · `PRUNE` (`threshold`) |
| `agent`                  | If resolved → scoped `agent_recall` `src/agents/store.py:344` (always `include_agent_notes=True` for own rows). If `None` + `include_agent_notes=True` → explicit `bridge.search_all_agents` `src/agents/bridge.py:58` merge (never implicit — guardrail #3).                                            |
| `bound`/`limit`+`offset` | Pagination after `max(bound*5, bound+25)` pre-filter. `total_found`/`bound_applied` in response.                                                                                                                                                                                                         |

```json
{"name":"recall","arguments":{"query":"budget","mode":"RELATED","bound":10}}
{"name":"recall","arguments":{"query":"hello world","agent":"AshaWeb"}}
{"name":"recall","arguments":{"query":"FIND PERSON \"Sam\""}}
{"name":"recall","arguments":{"query":"Sam -> Budget","mode":"PATH"}}
```

`find_across_agents {query, min_confidence, bound}` stays **core-only** standalone (agents never call it — ask via `mailbox`).

### `relate` (unified) + `get`/`update`/`delete` (unified)

- `relate {from_id, to_id, edge_type, weight, agent?, fuzzy?}` — `from_id`/`to_id` may be exact `node_id` or exact `label`; weight `-1..1` clamped `src/core/edges.py`. `fuzzy:false` default (guardrail #1); `fuzzy:true` allows `LIKE`. If `agent` → `agent_relate` `src/agents/store.py:378` stamps `edges.agent_id`.
- `get {node_id, agent?}` — exact label fallback, `bump_access` `src/core/nodes.py:201` + `InternalClock` `src/core/clock.py`.
- `update {node_id, label?, content?, trust_level?, importance?, metadata?, agent?}` — shallow-merge `metadata`, refresh `node_index` + `vectors` `src/core/vectors.py:133`.
- `delete {node_id, agent?}` — cascades + `update_on_delete` `src/core/vectors.py:125`.

### Mail (standalone) + Admin + Promote/Review

- `mailbox.send {from, to, body}` / `mailbox.read {scope, state, limit}` / `mailbox.ack {scope, msg_ids?}` — `from`/`to` heal `"AshaWeb"` → `agent:agent_ashaweb_4fee` `src/logic/engine.py:285`; `agent->broadcast` → `core` + `was_broadcast_attempt`. `with_inbox_notice` `src/mcp/tools.py:117` injects `mailbox_notice` on every `tools/call` only when pending.
- `promote_to_core {agent, agent_node_id, new_type?, new_label?}` — top-level composite, not in `manage`. `resolve_agent(auto_create=False)` must hit owner; 6-step move `src/agents/bridge.py:101`.
- `review_queue {bound}` / `set_attention {agent, agent_node_id, attention_state}`.
- `admin {action:"health"|"stats"|…}` maps to `profile`/`health`/`stats`/`export`/`vacuum`/`compact`.

Legacy `agent_ensure` / `agent_list` / `agent_remember` etc. remain as deprecated aliases (one minor).

---

## 3. Direct API (unified) + Dashboard

`dashboard/server.py` `ThreadingHTTPServer`, `127.0.0.1:8500` default `--host`/`--port` `dashboard/server.py:2005` ( `0.0.0.0` needs reverse proxy, no CORS). Auth `brain/config.json → dashboard_token` `src/logic/api_client.py:14`; header `X-Api-Token` only, no `?token=`, no localhost bypass (`GET /health`/`/ping` open) `dashboard/server.py:235`.

- **For agents:** `src/logic/api_client.py:60 DashboardClient(base_url, brain_dir)` auto-loads token and injects header. You don't handle tokens — pass `agent` hint and client does `load_dashboard_token`.
- **New unified REST:** `POST /api/remember {content, agent?, …}` `dashboard/server.py:1608 _post_remember` (heal + `healed` + `agents` infer) and `GET|POST /api/recall?q=&agent=&mode=&bound=` `dashboard/server.py:1047 _api_recall` (scoped). Fallback `POST /api/node_add` `dashboard/server.py:1532` and `GET /api/nodes?agent=&q=` `dashboard/server.py:733 _api_nodes` now also accept `?agent=AshaWeb` (infer `agents.db` when `?db=` omitted `dashboard/server.py:1494 _agent_db_infer`) and canonicalize via `_resolve_agent_param(auto_create=True/False)`.
- **Polished:** `POST /api/graduate {node_ids, agent?}` `dashboard/server.py:1327` and `POST /api/mailbox/send {from, to}` `dashboard/server.py:1889` now heal `agent` hints (e.g. `web scout` → `agent_ashaweb_4fee`).
- **Existing:** `GET /api/status?db=all` `GET /api/nodes?db=…` `GET /api/graph?db=&limit=` `POST /api/node_update|delete|bulk_nodes` `POST /api/edge_add|delete` `GET /api/mailbox?scope=&state=` etc. `wiki/API.md` + `wiki/DASHBOARD.md` 12 tabs (Overview/Maintenance/Graduate/Observer/Contradicts/Ephemeral/Graph/Manager/System/Statistics/Config/Mail) unchanged; badges poll `App.api` `dashboard/static/app.js:57`.

```bash
curl -H "X-Api-Token: $(jq -r .dashboard_token brain/config.json)" \
  "http://127.0.0.1:8500/api/nodes?agent=AshaWeb&q=budget"
curl -X POST -H "X-Api-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"content":"hello","agent":"web scout"}' http://127.0.0.1:8500/api/remember
```

Or via client:

```python
from src.logic.api_client import DashboardClient
client = DashboardClient(brain_dir="brain")  # token auto
client.post("/api/remember", json={"content":"hello","agent":"web scout"})
```

---

## 4. When to `remember` / `relate` / `recall`

Same table as before, unified:

| Rule                 | Detail                                                                                                                                      |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| One fact per node    | Split compound statements. `label` ≤30.                                                                                                     |
| `node_type`          | `PERSON/TOPIC/EVENT/FACT/PREFERENCE/BOUNDARY/AFFECT/AGENT_NOTE/CORE_REF/SKILL`. `FACT` triggers contradiction scan `src/core/nodes.py:415`. |
| `trust`/`importance` | honesty matters — drives ranking/pruning/graduation. Healed to 0.5 if missing.                                                              |
| Telemetry            | JSON-log-shaped or allowlist `ephemeral_labels` → `ephemeral_events` table, not graph `src/core/lexicon.py:_looks_like_json_log`.           |

`relate`: intentional `SUPPORTS`/`CONTRADICTS`/`SUMMARIZES`/`HAS_*`; auto `RELATES_TO` catches `≥2` shared terms top 10. Guard: endpoints exist or `node … does not exist`; `fuzzy` needed for `LIKE`.

`recall`: start `RELATED`; use `SEMANTIC` for synonym drift, `WHO_IS`/`WHAT_ABOUT` for entity neighborhoods, `PATH` for connectivity.

---

## 5. Memory Use — relevance, not habit (Core vs Agent)

### Core (curator, no `agent`)

- **Do use** when past context materially helps current task: continuity, user facts, your facts, who you are, preferences, boundaries, prior decisions. Prevent repeating work.
- **Retrieve narrowly:** one focused `recall` with tight `query` + minimal `bound`; treat hits as contextual evidence, not absolute truth — prefer current conversation when conflict.
- **Do not use** when irrelevant/outdated or conversation already sufficient. Never force `recall` just because memory exists. Fresh small-talk → skip.
- **Curation:** weekly `Contradicts` → `Graduate` → `Observer` → `Ephemeral` → `Run FULL` → read `System` log `wiki/CORESKILLS.md:84`.

### Agent (worker, have `agent`)

- **Do use** when past context helps *your* work — previous tasks, findings, handoff context. Avoid re-doing.
- **Retrieve narrowly:** one `recall` with `agent:"<you>"` + tight `query`. You are fenced (`WHERE agent_id=?`).
- **Do not use** when irrelevant or already in prompt. No habit `recall`.
- **Eviction vs decay:** write cap `100`/`800` `src/agents/store.py:47` (oldest `agent_private` evicted, `review_ready` protected) vs WORKING decay `Score = access_count·Wa + importance·Wi − age·Wd` `brain/engine.py:regulate_agent_working_memory` (`Wa1.5/Wi4.0/Wd0.15, high_water12, max_age48h`) — re-`recall` what matters to bump `access_count`.
- **Flag what matters:** `set_attention(..., "review_ready")` or `remember(attention_state="review_ready")` → `review_queue`/Graduate + sticky `[REVIEW READY]/[REVIEW QUEUE]` mail `src/agents/mailbox.py:260` injected every core `tools/call` until promoted/acked. Promote is a **move** — your copy disappears, provenance `metadata.promoted_from` stays in core.

---

## 6. Mail discipline

- Check `mailbox_notice` on **every** `tools/call` (`mailbox_notice` only when pending); handle via `mailbox.read → ack`. Only `ack` clears badges. Dashboard `Mail` tab polls `GET /api/mailbox?scope=user|all`.
- Scopes: `core ↔ agent` (task), `agent ↔ agent` (coordinate), `core → broadcast` (fans out per agent+core), `user ↔` both. Prefer direct `agent:<id>` over `broadcast`. `agent → broadcast` rejected `hint: send to core` (healed to `core`).
- Keep messages short, actionable, one matter each. `ack` what you handled. Task humans via `to=user` (they see Mail tab).
- Silent injection already covers `review_ready` — no need to `mailbox.send` after `set_attention`.

---

## 7. Don'ts

### For all

- Don't invent IDs — resolver canonicalizes hints; `agent_ensure` is now optional (auto-create on write).
- Don't `LIKE`-guess edges — use exact `label` or `node_id`; set `fuzzy:true` only when you mean it.
- Don't rely on implicit cross-agent scope — `recall` without `agent` stays core; set `include_agent_notes:true` explicitly to merge, or use `find_across_agents` (core-only).
- Don't store telemetry as nodes — use `ephemeral_events` via `compact`/`API`.

### Agent extras

- Don't call `find_across_agents` (core-only) — ask via `mailbox.send(to=core)`.
- Don't `agent → broadcast`.

### Core extras

- Don't hand-edit `memory/*.db` / `brain/config.json` without `POST /api/config {reload:true}` or `engine.reload_config()`.

---

## 8. Safety & curation (weekly)

1. **Contradicts** `GET /api/contradictions?status=pending&db=core|agents` → `POST /api/contradiction_action` / `POST /api/contradiction_auto_resolve`.
2. **Graduate** `GET /api/graduate_preview` → `POST /api/graduate {node_ids, agent?}` (`agent` hint now heals).
3. **Observer** `GET /api/agent_working_preview` → `POST /api/regulate_agent_working {dry_run}`.
4. **Ephemeral** `GET /api/ephemeral_candidates` → `POST /api/ephemeral_allowlist` → `POST /api/compact_ephemeral`.
5. **Maintenance → Run FULL** → `System` log + `POST /api/create_snapshot` before scary ops; rollback `POST /api/restore_snapshot` (pre-rollback backup auto).

`update_node` shallow-merges `metadata`; `delete_node` cascades + `df` `src/core/vectors.py:125`; `export` `tar.gz` + `manifest.json` `sha256`.

---

## 9. Workflow examples (unified)

### Onboard preference (Core, no agent)

```json
{"name":"remember","arguments":{"content":"Sam is a product manager at Acme","node_type":"PERSON","label":"Sam","trust":0.9,"importance":0.8,"source":"USER"}}
{"name":"remember","arguments":{"content":"Sam prefers dark mode","node_type":"PREFERENCE","label":"Sam UI"}}
{"name":"relate","arguments":{"from_id":"Sam","to_id":"Sam UI","edge_type":"HAS_PREFERENCE"}}
{"name":"recall","arguments":{"query":"FIND PERSON \"Sam\""}}
```

With `fuzzy` edge + agent hint:

```json
{"name":"relate","arguments":{"from_id":"Sam","to_id":"Sam UI","edge_type":"HAS_PREFERENCE","fuzzy":false}}
```

### Nightly scout (Agent, have `agent`)

```json
{"name":"remember","arguments":{"content":"Found 3 stale FACTs overlapping 'budget'","agent":"nightly scout","label":"budget stale"}}
{"name":"remember","arguments":{"content":"budget deadline 2026-10-01","agent":"agent_nightly-scout_ab12","label":"budget deadline","attention_state":"review_ready"}}
{"name":"recall","arguments":{"query":"budget","agent":"agent_nightly-scout_ab12","mode":"RELATED"}}
{"name":"mailbox.send","arguments":{"from":"agent:agent_nightly-scout_ab12","to":"core","body":"please promote node_xyz"}}
```

Via Direct API (token auto via file):

```bash
curl -H "X-Api-Token: $TOKEN" -d '{"content":"Found 3 stale","agent":"AshaWeb"}' http://127.0.0.1:8500/api/remember
curl -H "X-Api-Token: $TOKEN" "http://127.0.0.1:8500/api/recall?q=budget&agent=AshaWeb"
```

### Graduate (Core review)

```json
{"name":"review_queue","arguments":{"bound":20}}
{"name":"promote_to_core","arguments":{"agent":"AshaWeb","agent_node_id":"node_agent_xyz","new_type":"FACT"}}
{"name":"recall","arguments":{"query":"budget deadline"}}
```

Direct API polished: `POST /api/graduate {"node_ids":["node_…"],"agent":"web scout"}` → heals `web scout → agent_ashaweb_4fee`.

### Mail + human task

```json
{"name":"mailbox.send","arguments":{"from":"core","to":"user","body":"Weekly review done"}}
{"name":"mailbox.send","arguments":{"from":"AshaWeb","to":"core","body":"need context on budget"}} 
```

Heals `"AshaWeb"` → `agent:agent_ashaweb_4fee`.

---

## 10. Where things live

- MCP: `src/mcp/server.py` stdio, `src/mcp/tools.py:130 dispatch` + `with_inbox_notice` `src/logic/engine.py`.
- Logic Engine: `src/logic/engine.py` + `src/logic/api_client.py` (`load_dashboard_token`, `DashboardClient`).
- Dashboard: `dashboard/server.py` `ThreadingHTTPServer` `127.0.0.1:8500` `--host 0.0.0.0` `dashboard/server.py:2005`, `_check_auth` `dashboard/server.py:235`, `_resolve_agent_param` `dashboard/server.py:1461`.
- Brain: `brain/engine.py` 9 jobs, `brain/scheduler.py` canonical order, `memory/.lock` reentrant.
- DBs: `memory/core.db` + `memory/agents.db` (`PRAGMA WAL+NORMAL+FK+busy_timeout8000`) `src/core/store.py:37`.

Mistakes in old `CORESKILLS.md`/`AGENTSKILLS.md` (e.g. `node_type` now optional, `trust` defaults healed, `agent_ensure` now optional, `fuzzy`/`include_agent_notes` explicit, `source` circularity intentional, token via file for agents) are fixed here. Use this single file.
