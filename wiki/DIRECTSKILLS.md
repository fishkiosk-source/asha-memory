# DIRECT SKILLS — In-Process Playbook (DirectMemory)

> Same logic as `SKILLS.md`, but **no MCP stdio**. Use `src/direct/provider.py:1` `DirectMemory` for Hermes/OpenClaw in-process calls. One skill tree, one call does the workflow. If your job context provides `agent_id`/`job_hint`, you are **Agent** — pass `agent` on every call; engine scopes you to isolated rows `src/agents/store.py:312`. If no `agent`, you are **Core**. This is the `Direct` counterpart to `SKILLS.md` (`src/logic/engine.py` still does resolve/heal/route).

For humans: [ARCHITECTURE](ARCHITECTURE.md) + [MCP_TOOLS](MCP_TOOLS.md) + [API](API.md) + [DASHBOARD](DASHBOARD.md).

---

## 0. The one rule (same as SKILLS)

```
if agent_param_resolves -> agents.db (WHERE agent_id = ?) else core.db
```

- **Core** (no `agent`): curated `core.db`. Curates, graduates, resolves contradictions.
- **Agent** (have `agent`): single file `agents.db` sharded by `agent_id`. You see only `agent_id = your_id` rows. Everything else via `mailbox` or ask Core.
- Engine: `src/logic/engine.py` inside `src/direct/provider.py:33 call() -> _tools.dispatch()`. Same `LogicEngine` as MCP.

All 23 tools (`src/mcp/tools.py:45`) are re-exported as **Python methods** via `DirectMemory` (`src/direct/provider.py:15`). MCP is just the stdio wrapper for external clients — Direct is for in-process use.

---

## 1. Setup

```python
from src.direct.provider import DirectMemory

# from v3 root; memory_dir defaults to ./memory
with DirectMemory("./memory") as m:
    # m is thread-safe via RLock + check_same_thread=False (C23)
    # close() on exit; or m = DirectMemory(); ...; m.close()
    pass
```

`DirectMemory.definitions()` equals `tools.definitions()` (`src/mcp/tools.py:45` vs `src/direct/provider.py:37`) — single source, same 23 names (+ deprecated `register_skill`).

No token, no HTTP. For dashboard HTTP use `src/logic/api_client.py:60 DashboardClient`; for Direct use this.

---

## 2. Logic Engine — same heal, no fail

**Wraps** `src/direct/provider.py:33 dispatch` → `src/mcp/tools.py:130 dispatch` → `src/logic/engine.py:44 heal`.

| Step | What |
|---|---|
| **Resolve agent** `resolve_agent(ref, auto_create)` | `None→core` → exact `agents.agent_id`/`agent_aliases` `src/agents/store.py:240` → `slug` hit `src/agents/store.py:179` → substring heuristic for `web scout → AshaWeb` → `auto_create=True` on writes, `False` on reads (no ghosts). `agent_ashaweb_4fee` stable `src/agents/store.py:217`. |
| **Heal, not fail** | Missing `node_type → FACT` (core) / `AGENT_NOTE` (agent), `trust`/`importance → 0.5` clamped `src/core/nodes.py:68`, invalid `mode → RELATED`, `bound 1..200`, `edge_type → RELATES_TO`. Response includes `healed` map and persists `metadata.healed_fields` ring buffer cap 5 `src/logic/engine.py:18`. |
| **Route** | Exact-ref guardrail #1: `relate`/`get`/`update`/`delete` use `resolve_ref_exact` only; `fuzzy=True` allows `LIKE`. |
| **DB commit** | Single-DB commit. Cross-DB only `promote` with `core-first-then-agents` provenance `src/agents/bridge.py:128`. |

If you have `agent_id` in context, **always** pass `agent="<id or hint>"` — engine canonicalizes `"AshaWeb"` / `"web scout"` / `"agent_ashaweb_4fee"` → `agent_ashaweb_4fee`.

---

## 3. Direct API (23 tools as methods)

### `remember` (unified)

```python
with DirectMemory("./memory") as m:
    # Core fact
    m.remember(content="Sam prefers dark mode", node_type="PREFERENCE", label="Sam UI", trust=0.9, importance=0.8, source="USER")
    # Agent note — same method, add agent
    m.remember(content="Found 3 stale FACTs", label="budget stale", agent="AshaWeb")
    m.remember(content="Hello", agent="web scout")  # heals to AGENT_NOTE, auto-ensure

    # explicit legacy
    # m.agent_remember(agent_id="agent_ashaweb_4fee", content="...", label="...")
```

| Param | Notes |
|---|---|
| `content` yes | 500 core / 800 agent clamped `src/core/nodes.py:157` / `src/agents/store.py:320` |
| `node_type` | Optional — heals to `FACT`/`AGENT_NOTE` |
| `label` | ≤30 headline, fallback `content[:30]` |
| `agent`/`agent_id` | If present → `agents.db` `AGENT_NOTE` (caps 100/800, oldest `agent_private` evicted `src/agents/store.py:262`), else `core.db` |
| `trust`/`importance` | 0..1 clamped, default 0.5 |
| `attention_state` | Agent only: `agent_private` (default) → `review_ready` |

### `recall` (unified) + `find_across_agents` (core-only)

```python
m.recall(query="budget", mode="RELATED", bound=10)
m.recall(query="hello world", agent="AshaWeb")
m.recall(query='FIND PERSON "Sam"')  # DSL auto-detect, overrides mode
m.recall(query="Sam -> Budget", mode="PATH")
m.recall(query="budget", include_agent_notes=True)  # core + explicit bridge merge (guardrail #3)
m.find_across_agents(query="budget", bound=10)  # core-only TF-IDF across all agents
```

9 modes `src/core/recall.py:44`: `RELATED` (default keyword), `SEMANTIC` (TF-IDF cosine ≥0.1, cap 2000), `WHO_IS` (1-hop PERSON), `WHAT_ABOUT` (2-hop TOPIC), `PATH` (Dijkstra 1-weight), `CLUSTER` (BFS), `TIMELINE` (EVENTs newest), `RECENT`, `PRUNE`. `FIND` / `"A -> B"` overrides `mode`. `bound`/`limit`+`offset` paginated.

### `relate` / `get` / `update` / `delete` (unified)

```python
m.relate(from_id="Sam", to_id="Sam UI", edge_type="HAS_PREFERENCE", weight=0.9, fuzzy=False)
m.relate(from_id="node_a", to_id="node_b", edge_type="SUPPORTS", agent="AshaWeb")  # stamps edges.agent_id
m.get_node(node_id="Sam")  # exact label fallback, bumps access + InternalClock
m.get_node(node_id="node_xyz", agent="AshaWeb")  # scoped
m.update_node(node_id="node_xyz", label="new label", importance=0.9, metadata={"note":"x"})
m.delete_node(node_id="node_xyz", agent="AshaWeb")
```

- `relate` weight `-1..1` clamped `src/core/edges.py`; `fuzzy:false` default (guardrail #1).
- `update_node` shallow-merges `metadata`, refreshes `node_index`+`vectors` `src/core/vectors.py:133`.
- `delete_node` cascades + `update_on_delete` `src/core/vectors.py:125`.

### Mail + Admin + Promote/Review

```python
m.mailbox_send(from_scope="core", to_scope="agent:agent_ashaweb_4fee", body="please promote node_xyz")
m.mailbox_read(scope="agent:agent_ashaweb_4fee", state="pending", limit=20)
m.mailbox_ack(scope="core")  # ack all unacked
m.promote_to_core(agent_id="agent_ashaweb_4fee", agent_node_id="node_agent_xyz", new_type="FACT")
m.review_queue(bound=20)
m.set_attention(agent_id="agent_ashaweb_4fee", agent_node_id="node_xyz", attention_state="review_ready")
m.health(); m.stats(db="all"); m.profile(); m.vacuum(); m.compact(keep_last=3, max_age_days=7)
m.export(path="/tmp/asha.tar.gz")
m.agent_ensure(job_hint="nightly scout", preferred_id=None); m.agent_list()
```

- `mailbox_send` heals `"AshaWeb"` → `agent:agent_ashaweb_4fee` `src/logic/engine.py:285`; `agent->broadcast` rejected `hint: send to core` (healed to `core`). `with_inbox_notice` injects `mailbox_notice` only when pending `src/mcp/tools.py:117`.
- `promote_to_core` 6-step move `src/agents/bridge.py:101`; `set_attention` toggles `agent_private↔review_ready`.
- `export` writes `tar.gz` + `manifest.json` via `backup()` API.

---

## 4. When to `remember` / `relate` / `recall`

| Rule | Detail |
|---|---|
| One fact per node | Split compounds. `label` ≤30. |
| `node_type` | `PERSON/TOPIC/EVENT/FACT/PREFERENCE/BOUNDARY/AFFECT/AGENT_NOTE/CORE_REF/SKILL`. `FACT` triggers contradiction scan. |
| `trust`/`importance` | Drives ranking/pruning/graduation. Healed to 0.5 if missing. |
| Telemetry | JSON-log-shaped or allowlist `ephemeral_labels` → `ephemeral_events` table, not graph `src/core/lexicon.py:132`. |

`relate`: intentional `SUPPORTS`/`CONTRADICTS` etc.; auto `RELATES_TO` on ≥2 shared terms.

---

## 5. Memory Use — relevance, not habit

**Core** (no `agent`): use when past context materially helps — continuity, user facts, preferences, boundaries. One narrow `recall` with tight query + minimal bound; treat hits as evidence, not truth. Curation: weekly `Contradicts → Graduate → Observer → Ephemeral → Run FULL → System log`.

**Agent** (have `agent`): one `m.recall(query, agent="<you>")` with tight query (`WHERE agent_id=?`). Write cap `100`/`800` vs WORKING decay `Score = acc*Wa + imp*Wi − ageH*Wd` (`brain/engine.py:regulate_agent_working_memory`, `Wa1.5/Wi4.0/Wd0.15, high_water12, max_age48h`). Flag `set_attention(..., "review_ready")` → `review_queue` + sticky `[REVIEW READY]` mail `src/agents/mailbox.py:260`.

---

## 6. Mail discipline

Check `mailbox_notice` on **every** `call()` result when pending; `mailbox_read → ack`. Scopes: `core ↔ agent`, `agent ↔ agent`, `core → broadcast`, `user ↔` both. Prefer `agent:<id>` over `broadcast`. Silent injection already covers `review_ready`.

---

## 7. Don'ts

- Don't invent IDs — resolver canonicalizes; `agent_ensure` optional (auto-create on write).
- Don't `LIKE`-guess edges — use exact `label`/`node_id`; `fuzzy:true` only when meant.
- Don't rely on implicit cross-agent scope — `recall` without `agent` stays core; `include_agent_notes:true` explicitly merges.
- Don't store telemetry as nodes — use `ephemeral_events`.
- Agent extras: Don't call `find_across_agents` (core-only) — ask via `mailbox_send(to_scope="core")`. Don't `agent → broadcast`.

---

## 8. Safety & curation (weekly)

1. `m.health()` / `m.stats()` check.
2. Contradictions: `detect` auto on `FACT` write → triage `update`/`resolve` (or via `brain/engine.py`).
3. Graduate: `m.review_queue()` → `m.promote_to_core()` (hint `agent="web scout"` heals).
4. Observer: `get_agent_working_preview` → `regulate_agent_working_memory`.
5. Ephemeral: `get_ephemeral_stats` → `discover_ephemeral_candidates` → `compact`.
6. `m.vacuum()` + snapshot via `brain/engine.py` when bloated.

---

## 9. Workflow examples (Direct)

### Onboard preference (Core)

```python
from src.direct.provider import DirectMemory
with DirectMemory("./memory") as m:
    a = m.remember(content="Sam is a product manager at Acme", node_type="PERSON", label="Sam", trust=0.9, importance=0.8, source="USER")
    b = m.remember(content="Sam prefers dark mode", node_type="PREFERENCE", label="Sam UI")
    m.relate(from_id="Sam", to_id="Sam UI", edge_type="HAS_PREFERENCE")
    hits = m.recall(query='FIND PERSON "Sam"')
```

### Nightly scout (Agent)

```python
with DirectMemory("./memory") as m:
    m.remember(content="Found 3 stale FACTs overlapping 'budget'", label="budget stale", agent="nightly scout")
    m.remember(content="budget deadline 2026-10-01", label="budget deadline", agent="AshaWeb", attention_state="review_ready")
    hits = m.recall(query="budget", agent="AshaWeb")
    m.mailbox_send(from_scope="agent:agent_nightly-scout_ab12", to_scope="core", body="please promote node_xyz")
```

### Graduate (Core review)

```python
with DirectMemory("./memory") as m:
    q = m.review_queue(bound=20)
    m.promote_to_core(agent_id="agent_ashaweb_4fee", agent_node_id="node_agent_xyz", new_type="FACT")
    hits = m.recall(query="budget deadline")
```

Direct is thread-safe: `RLock` + `check_same_thread=False` (`src/core/store.py:37`, `src/agents/store.py:131`, `src/direct/provider.py:17`). `m.call(tool, args)` is the escape hatch for any of the 23 tools.

---

## 10. Where things live

- Direct: `src/direct/provider.py:1` `DirectMemory` → `src/mcp/tools.py:130 dispatch` → `src/logic/engine.py` → `src/core/*` / `src/agents/*` → `memory/core.db` + `memory/agents.db`.
- MCP: `src/mcp/server.py` stdio (same dispatch, external clients).
- Dashboard HTTP: `dashboard/server.py` (`DashboardClient` in `src/logic/api_client.py:60` for HTTP, not needed here).
- Brain: `brain/engine.py` 9+1 jobs, `brain/scheduler.py` canonical order, `memory/.lock` reentrant.
- DBs: `memory/core.db` + `memory/agents.db` (`PRAGMA WAL+NORMAL+FK+busy_timeout8000`) `src/core/store.py:37`.

This file mirrors `SKILLS.md` §1-10 but with `DirectMemory` calls. Use `SKILLS.md` for MCP stdio (`tools/call`) and dashboard `curl`/`DashboardClient`; use this file for `from src.direct.provider import DirectMemory`.
