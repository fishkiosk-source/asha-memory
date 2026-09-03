# ASHA Memory System — AI Agent Guide

An AI agent's guide to using the ASHA MCP server: when and why to use memory,
which tools to call, and how to structure knowledge for reliable retrieval.

---

## Table of Contents

1. [What This Is](#what-this-is)
2. [Memory Strategy](#memory-strategy)
   - [What Belongs in Memory](#what-belongs-in-memory)
   - [Core vs. Agent Shard](#core-vs-agent-shard)
   - [Structuring Nodes](#structuring-nodes)
   - [Edge Strategy](#edge-strategy)
   - [Trust and Importance Calibration](#trust-and-importance-calibration)
   - [Labeling Conventions](#labeling-conventions)
   - [Recall Decision Tree](#recall-decision-tree)
   - [Memory Lifecycle and Decay](#memory-lifecycle-and-decay)
   - [Anti-Patterns](#anti-patterns)
3. [Connecting](#connecting)
4. [Tools Overview](#tools-overview)
5. [Tool Reference](#tool-reference)
   - [Skill Tools](#skill-tools)
   - [Core Memory Tools](#core-memory-tools)
   - [Agent Tools](#agent-tools)
   - [Query Tools](#query-tools)
   - [System Tools](#system-tools)
6. [Resources](#resources)
7. [Workflows](#workflows)
8. [Error Handling](#error-handling)
9. [Protocol Notes](#protocol-notes)

---

## What This Is

This MCP server wraps `ASHA_MEMORY_SYSTEM v2`, a tiered memory system with
TF-IDF semantic search, node-edge knowledge graphs, shared agent-note scopes, and
a skill registry. It communicates over **stdio transport** using JSON-RPC 2.0.

**What you can do:**
- Store and retrieve memories (core memory, shared across all agents)
- Link memories with typed, weighted relationships
- Search semantically using TF-IDF vector similarity
- Store private agent working notes without exposing them to normal core recall
- Manage a skill registry with assignment to agents
- Run system health checks and gather performance profiles
- Get temporal context — node ages, last-checked times, and a live TODAY
  context node — through the memory system itself (no MCP changes)

---

## Memory Strategy

This is the most important section. Knowing *which tool to call* is easy.
Knowing *what to store, where, and when* determines whether your memory system
helps or becomes noise.

### What Belongs in Memory

**Store in memory when:**
- The information is **durable across sessions** — user preferences, confirmed
  facts, long-term goals, agent identities, learned patterns
- The knowledge might be needed by **another agent or future instance** of
  yourself
- The information is **verified or agreed upon** — not speculative
- You've learned something that **changes how you should behave** going forward
- You encounter a **named entity** (person, project, system) with attributes

**Do NOT store in memory when:**
- The information is **ephemeral** — single-turn context, temporary variables,
  intermediate reasoning steps
- You're merely **repeating what's already stored** — check with `recall` first
- The content is **too vague to retrieve** — "something about a thing" won't
  match any search query
- You have **more than ~5 items in a single turn** — batch or prioritize
- The data is better kept in **conversation context** (the model's prompt window)

**Rule of thumb:** If you'd want it back in a week, store it. If it only
matters for the next 5 minutes, keep it in context.

### Core Attention vs. Agent Notes

The system has one graph database and two attention scopes:

| Aspect | Normal core recall | Agent-scoped notes |
|--------|--------------------|--------------------|
| Storage | `core.db` | `core.db` |
| Visibility | Main AI context | Explicit agent search/digest only |
| Graph edges | Full graph | Full graph |
| Default state | `core_verified` | `agent_private` |
| Best for | Confirmed shared knowledge | Raw observations and task working state |

`agent_remember` writes an `AGENT_NOTE` with an agent source and metadata.
Normal `recall()` deliberately excludes raw agent notes. An agent can mark a
finding `review_ready`; CORE reads those through `agent_review_queue()` and
promotes accepted findings in place. Promotion preserves node ID and edges.

Set `agent_memory_mode` to `legacy_shards` only to retain the older per-agent
SQLite file behaviour during a transition.

### Structuring Nodes

**Content is the most important field** — it's what gets TF-IDF vectorized for
semantic search. Write content as complete, keyword-rich sentences.

Bad: `"dark mode"`
Good: `"User prefers dark mode for all development environments because of eye strain"`

The second form will match searches for "preferences", "development", "eye
strain", "dark", "mode", "UI", "theme" — the first only matches "dark" and
"mode".

**Label** is optional but powerful — it enables direct lookup via `WHAT_ABOUT`
and `WHO_IS` modes. Use labels for things you'll want to find by name:
people, projects, named concepts.

**Node type** is a coarse categorization. It's not used in semantic search
(the full content is). Use it for:
- `PERSON` — enables `WHO_IS` mode
- `BOUNDARY` — enables rule-filtering workflows
- `PREFERENCE` — enables preference-tracking queries
- The rest are organizational — pick the closest fit

### Edge Strategy

Edges turn isolated nodes into a navigable knowledge graph. Use them
sparingly and deliberately.

**When to create an edge:**
- Two facts are causally related → `CAUSED_BY`
- One fact supports another → `SUPPORTS`
- A person has a preference → `HAS_PREFERENCE` (person → preference)
- A person has a boundary → `HAS_BOUNDARY` (person → boundary)
- Information contradicts existing knowledge → `CONTRADICTS` (important for
  flagging uncertainty)
- An event is part of a larger topic → `PART_OF`
- You create a summary of existing nodes → `SUMMARIZES` (summary → source)

**When NOT to create an edge:**
- Don't connect everything to everything — that creates noise, not signal
- Don't use `RELATES_TO` as a default for "these two things exist at the same
  time" — use it only when there's a genuine semantic relationship
- Don't create redundant edges — if A `SUPPORTS` B, you don't also need
  A `RELATES_TO` B

**Good graph structure:**
- A person node → `HAS_PREFERENCE` → multiple preference nodes
- A person node → `HAS_BOUNDARY` → boundary nodes
- A fact node → `SUPPORTS` → conclusion node
- An event node → `CAUSED_BY` → prior event node
- A topic node → `PART_OF` → broader topic node

**Label-based linking trick:** When you `remember` a new node, you can set
its `label` to match an existing node's label. Then use `WHAT_ABOUT` recall
to find the old node, and `relate` the two by their returned IDs.

### Trust and Importance Calibration

These two fields control memory lifecycle:

**Trust (0.0–1.0, default 0.5):**
- `0.9–1.0`: Certain knowledge — user explicitly confirmed, observed directly
- `0.6–0.8`: Likely true — inferred, reported by reliable source
- `0.3–0.5`: Speculative — unverified, needs confirmation (this is the default)
- `0.0–0.2`: Low confidence — rumor, contradiction, needs re-validation

**Importance (0.0–1.0, default 0.5):**
- `0.9–1.0`: Critical — user's core identity, safety boundaries, system rules
- `0.6–0.8`: Important — durable preferences, key facts about people
- `0.3–0.5`: Normal — typical observations (this is the default)
- `0.0–0.2`: Trivial — temporary notes, may be pruned

**How they interact:**
- **Decay:** *Importance* decays over time (tier-dependent: `short_term` 0.97/day, `long_term` 0.995/day, `working`/`archive` 1.0 no decay). *Trust* does NOT decay — it is provenance reliability and only changes via `update_trust` or promotion. High importance = high survival chance against `manage_tiers`/`run_decay`.
- **Pruning:** The `PRUNE` recall mode finds nodes with `importance < prune_threshold (0.05)` **and** `access_count < 3` **and** `updated_at < now - 30 days`. Brain `prune_stale_unused_nodes` adds `importance < prune_importance_floor` **and** no edges **and** `protected_types` excluded. Low importance + low access + no links → candidate.
- **Access boost:** Each time a node is recalled (via `_bump_access` / `update_layer_on_access`), its *importance* (tier boost 0.10/0.05) and `access_count` are bumped, promoting `working → short_term (3 accesses) → long_term (15 accesses)`. Frequently accessed nodes stay alive.

**Strategy:** Set `importance` based on long-term value, `trust` based on
confidence. Things you want to survive indefinitely need high importance.
Things you're unsure about should have low trust so they eventually decay.

### Labeling Conventions

Labels are case-insensitive in `WHO_IS` and `WHAT_ABOUT` modes. Follow
consistent conventions:

- People: `"Sam"`, `"Alice"` (first name or handle)
- Projects: `"Project Phoenix"`, `"ASHA v2"`
- Concepts: `"dark_mode_preference"`, `"privacy_boundary"`
- Events: `"meeting_2026_07_15"`, `"deploy_v2"`

Avoid generic labels like `"note"`, `"fact"`, `"memory"` — they defeat the
purpose of direct lookup.

### Recall Decision Tree

When you need to find something, use this decision tree:

```
1. Do you know the exact node_id?
   → get_node(node_id)

2. Do you know the node's label?
   → recall(query="label", mode="WHAT_ABOUT")

3. Is it a person?
   → recall(query="name", mode="WHO_IS")

4. Do you want free-form semantic search?
   → recall(query="describe what you want", mode="SEMANTIC", bound=5-10)

5. Do you want to see everything recent?
   → recall(query="", mode="RECENT", bound=20)

6. Do you want to find keyword-related content?
   → recall(query="keywords", mode="RELATED")   # keyword/FTS, not graph-neighbor — use CLUSTER for edge expansion

7. Do you want to see how two things connect?
   → recall(query="label_A -> label_B", mode="PATH")

8. Do you want a cluster around a topic?
   → recall(query="node_id", mode="CLUSTER")

9. Do you want to find old/low-value nodes for cleanup?
   → recall(query="", mode="PRUNE", bound=20)

10. Do you want a sequence of events?
    → recall(query="node_id or keyword", mode="TIMELINE")
```

**If SEMANTIC returns nothing:**
- Widen your query (fewer specific terms)
- Check if the data exists at all with `stats()`
- Try `RECENT` mode to see what's actually stored
- The TF-IDF index may need rebuilding — call `rebuild_vector_index`

**If SEMANTIC returns low-relevance results:**
- Check the `similarity` scores — results below 0.15 are weak
- Tighten your query with more specific terms
- Raise `bound` to see more candidates, then filter by similarity

### Memory Lifecycle and Decay

The system has four memory layers that nodes transition through:

```
WORKING (20, agent cap 12) → SHORT_TERM (500) → LONG_TERM (5000) → ARCHIVE (∞)
 agent janitor Score=acc*Wa+imp*Wi-ageH*Wd, max_age 48h → short_term (agent-only)
```

- **Working:** Most recently created/accessed nodes (capacity 20, decay 1.0 no decay). **Agent `WORKING` is capped at 12/20** and swept by `regulate_agent_working_memory` `brain_engine.py:1117` (`high_water:12`, `batch:5`, `max_age:48h`, `Wa1.5/Wi4.0/Wd0.15`); least useful `agent_private` demoted to `short_term`, `review_ready`/`core_verified` never touched, core `WORKING` never touched (scope-aware `AshaMemory._update_layer_on_access:971`). Preview `Observer` tab shows `days_left/score/demote_next`.
- **Short-term:** Nodes that survive initial pruning (promoted after 3 accesses, decay 0.97, boost 0.10, cap 500)
- **Long-term:** Durable knowledge (promoted after 15 accesses, decay 0.995, boost 0.05, cap 5000)
- **Archive:** Old but preserved — not in active vector index (decay 1.0)

**You don't control layers directly** — they're managed by access count and
decay. But you influence them through:
- **High importance** → node resists moving toward archive (survives `prune_threshold:0.05` check) and raises `Score` (`Wi:4.0`) so survives janitor
- **Frequent recall** → access boost bumps importance + `access_count`, node stays in active layers and promotes (`Wa:1.5`)
- **`PRUNE` mode** → find nodes in the danger zone before they're gone
- **Caps:** `agent_max_notes:100` enforced — oldest `agent_private` dropped at `agent_remember` when full; `agent_working_high_water:12` enforced by janitor

### Tuning (P1)

- `sqlite_cache_size: -64000` (KB, negative) in `config.json` — increase for >10k nodes; applied in `_core_conn`/`BrainEngine._connect_db`.
- `consolidation_bucket_prefix:4` / `consolidation_bucket_overlap:2` — tune dedup bucketing (caps buckets >150, only when `len>200`).
- `ephemeral_labels` (10) — unified allowlist `shared_lexicon.py:86` + `config.json`; Brain dashboard `Ephemeral` tab writes to both.
- `vector_index_auto_rebuild` — lazy (`True`) defers `fit` until next `SEMANTIC`; `remember_many` does single rebuild.

### Anti-Patterns

| Anti-pattern | Why it hurts | Better approach |
|-------------|--------------|-----------------|
| Storing everything | Vector index fills with noise; relevant results get buried | Be selective — see "What Belongs in Memory" |
| Generic content | "User said something about AI" won't match any search | Write keyword-rich, specific sentences |
| No edges | Isolated facts that never connect | Link related knowledge — especially people to their attributes |
| All trust = 0.5 | No signal for what's reliable | Calibrate trust: 0.9 for confirmed, 0.3 for speculative |
| All importance = 0.5 | Everything decays at the same rate | Set importance high for things that must survive |
| Flooding in one turn | Pushes out other working memory | Batch into 3-5 nodes max per interaction |
| `RELATES_TO` on everything | Graph becomes fully connected = useless | Only create meaningful, specific edge types |
| Never recalling before storing | Duplicates accumulate | Check with `SEMANTIC` recall before storing similar info |
| Mixing raw agent work into core recall | Main AI context fills with worker detail | Keep notes scoped; use review queue then promote verified findings |

---

## Connecting

The server reads one JSON-RPC message per line from stdin, and writes one
response per line to stdout. Stderr carries server logs.

| Step | Message |
|------|---------|
| 1 | `initialize` — negotiate protocol version |
| 2 | `notifications/initialized` — confirm ready |
| 3 | `tools/list` — discover available tools |
| 4 | Use `tools/call` to invoke any tool |

**Example initialize:**

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"my-agent","version":"1.0"}}}
```

Response includes the server's capabilities (tools, resources) and protocol
version (`2025-03-26`).

After initialize, send `notifications/initialized` (no id, no response):

```json
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

---

## Tools Overview

23 tools in 5 categories:

| Category | Tools | Purpose |
|----------|-------|---------|
| **Skills** | `register_skill`, `find_skills`, `assign_skill`, `agent_skills` | Manage agent capabilities |
| **Core Memory** | `remember`, `recall`, `relate`, `get_node` | Read/write shared knowledge |
| **Agent Memory** | `spawn_agent`, `agent_remember`, `find_across_agents`, `agent_review_queue`, `agent_set_attention`, `promote_to_core` | Scoped agent work and review |
| **Query** | `query_dsl` | Structured DSL queries |
| **System** | `profile`, `health`, `stats`, `rebuild_vector_index`, `export_json`, `get_bloat_metrics`, `compact_ephemeral_logs`, `vacuum` | Maintenance, bloat self-heal, and introspection |

---

## Tool Reference

### Skill Tools

#### `register_skill`

Register a new capability that can later be assigned to agents.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Unique identifier, e.g. `EXECUTE_CODE` |
| `description` | string | yes | What the skill does |
| `level` | string | no | `CORE_ONLY`, `ASSIGNABLE` (default), `AGENT_AUTO`, `AGENT_ONLY` |
| `tags` | string | no | Comma-separated keywords for search |

Returns `{"skill": "<name>", "level": "<level>", "status": "registered"}`.

#### `find_skills`

Search registered skills by keyword across name, content, and tags.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `query` | string | yes | Free-text search |
| `level` | string | no | Filter: `CORE_ONLY`, `ASSIGNABLE`, `AGENT_AUTO`, `AGENT_ONLY` |

Returns `{"total_found": N, "skills": [{"name": ..., "level": ..., "description": ...}, ...]}`.

#### `assign_skill`

Record a `HAS_SKILL` edge from an agent to a skill.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent_id` | string | yes | The agent's identifier |
| `skill_name` | string | yes | Skill name from the registry |

Returns `{"agent_id": ..., "skill": ..., "status": "assigned"}`.

> **Note:** this tool creates the graph edge `agent_id → skill_name`
> (`HAS_SKILL`), creating the agent's anchor node on first assignment. It raises
> an error when `skill_name` is not a registered skill. The assigned skill then
> shows up in `agent_skills` alongside the auto-granted `AGENT_AUTO` skills.

#### `agent_skills`

List the skills available to an agent.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent_id` | string | yes | Agent identifier |

Returns `{"agent_id": ..., "skills": [list of skill dicts]}`.

> **Note:** the returned list is the auto-granted `AGENT_AUTO` skills plus any
> skills explicitly assigned to this agent via `assign_skill`.

---

### Core Memory Tools

Core memory is a **shared** knowledge base. Every agent can read and write to
it. Store things that should be visible to everyone.

#### `remember`

Store a memory node in core memory.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `content` | string | **yes** | The memory content text |
| `node_type` | string | **yes** | One of: `PERSON`, `FACT`, `PREFERENCE`, `EVENT`, `TOPIC`, `AFFECT`, `BOUNDARY`, `SKILL`, `AGENT_NOTE`, `CORE_REF` |
| `label` | string | no | Short human-readable label (useful for recall by label) |
| `source` | string | no | Origin identifier: `USER`, `CORE`, `AGENT`, or a specific agent_id |
| `trust` | number | no | Confidence 0.0–1.0 (default 0.5) |
| `importance` | number | no | Importance 0.0–1.0 (default 0.5) |

Returns `{"node_id": "node_<hex>"}`. Save this ID — you need it for `relate`.

> **Batch insert:** `remember_many(items)` `asha_memory_v2.py:1198` — single transaction, single vectorizer rebuild, same args as `remember` per item. Use for bulk/telemetry ingestion.

**Choosing node_type:**
- `PERSON` — information about a specific person
- `FACT` — factual knowledge
- `PREFERENCE` — likes, dislikes, preferences
- `EVENT` — timestamped occurrences
- `TOPIC` — subject-area definitions
- `AFFECT` — emotional states or reactions
- `BOUNDARY` — constraints, rules, limits
- `SKILL` — capability descriptions (also used by skill registry)
- `AGENT_NOTE` — notes about agents (use agent tools for private notes)
- `CORE_REF` — reference pointer

#### `recall`

Search core memory. Supports multiple retrieval modes.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `query` | string | **yes** | Search text, node label, or node_id |
| `mode` | string | no | `RELATED` (default), `WHO_IS`, `WHAT_ABOUT`, `SEMANTIC`, `PATH`, `CLUSTER`, `TIMELINE`, `RECENT`, `PRUNE` |
| `bound` | integer | no | Max results (default 10) |
| `limit` | integer | no | Alias for `bound` |
| `include_agent_notes` | boolean | no | Include raw agent notes in recall (default false) |
| `node_type` | string | no | Optional post-filter: `PERSON`\|`FACT`\|`PREFERENCE`\|`EVENT`\|`TOPIC`\|`AFFECT`\|`BOUNDARY`\|`SKILL`\|`AGENT_NOTE`\|`CORE_REF` (applied after retrieval) |

Returns `{"mode": ..., "total_found": N, "clock": {...}, "nodes": [...]}`.
`clock` is today's date/time snapshot from the internal clock. Each node
includes `node_id`, `node_type`, `label`, `content` (truncated to 200 chars),
`trust_level`, `importance`, `similarity` (semantic score), and `age` (per-node
temporal summary: `added`, `last_checked`, `access_count`, `layer`, `stale`).

**Recall modes explained:**

| Mode | What it does | Best for |
|------|-------------|----------|
| `SEMANTIC` | TF-IDF vector similarity against query text (pre-filtered via `node_index` overlap + stored `magnitude` reuse, FTS `MATCH` sanitized) | Open-ended search: "What do I know about X?" |
| `RELATED` | Keyword `node_index` overlap + FTS5 `MATCH` fallback (not graph-neighbor; use `CLUSTER` for edge expansion) | Keyword/FTS search for related content |
| `WHO_IS` | Case-insensitive search for node_type=`PERSON` + label contains query | "Tell me about user Sam" |
| `WHAT_ABOUT` | Case-insensitive label or content match | Targeted lookup by name/title |
| `PATH` | Shortest path between two nodes (query as `"A" -> "B"`) | "How are these two things connected?" |
| `CLUSTER` | BFS expand outward from a node by edges + FTS | "What cluster of knowledge surrounds this?" |
| `TIMELINE` | Nodes ordered by `created_at` within time proximity | "What happened around this event?" |
| `RECENT` | Most recently created/updated nodes | "What's new?" |
| `PRUNE` | Low-importance + low `access_count` + stale nodes | Cleanup candidates |

> `recall` cache key is normalized (`lower()` + whitespace collapse, except `PATH` preserves case). FTS queries are sanitized via `_sanitize_fts_query` — `"`/`*` stripped with `LIKE` fallback on `OperationalError` (P0-7). `ephemeral_labels` (10) and unified `_looks_like_json_log` (`[:400]` + `timestamp && (post_count|load1m|status)`) `shared_lexicon.py:128` are excluded from `SEMANTIC` auto-link.

#### `relate`

Create a directed edge between two nodes. Edges form the knowledge graph.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `from_id` | string | **yes** | Source node_id |
| `to_id` | string | **yes** | Target node_id |
| `edge_type` | string | **yes** | One of the types below |
| `weight` | number | no | Edge strength 0.0–1.0 (default 1.0) |

Returns `{"status": "ok"}`.

**Edge types and their semantics:**

| Edge Type | Meaning | Example |
|-----------|---------|---------|
| `RELATES_TO` | General association | Memory A relates to Memory B |
| `CONTRADICTS` | Opposing information | Preference A contradicts Preference B |
| `SUPPORTS` | Evidence or backing | Fact A supports Conclusion B |
| `CAUSED_BY` | Causal relationship | Event A caused_by Event B |
| `PART_OF` | Composition | Feature A is part_of System B |
| `TRUSTS` | Trust relationship | Agent A trusts Person B |
| `DISTRUSTS` | Distrust relationship | Agent A distrusts Source B |
| `REMEMBERS` | Agent-to-memory link | Agent A remembers Memory B |
| `HAS_PREFERENCE` | Person-to-preference link | Person Sam has_preference for CLI |
| `HAS_BOUNDARY` | Person-to-boundary link | Person Sam has_boundary about data |
| `HAS_AFFECT` | Person-to-affect link | Person Sam has_affect frustration |
| `HAS_SKILL` | Agent-to-skill link | Agent worker_01 has_skill EXECUTE_CODE |
| `REFERS_TO` | Reference pointer | Memory A refers_to Memory B |
| `SUMMARIZES` | Summarization link | Summary node A summarizes Memory B |

#### `get_node`

Retrieve a single node by its ID.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `node_id` | string | **yes** | The node identifier |

Returns full node data or `{"error": "not found"}`.

---

### Agent Tools

Agent notes are stored in the shared graph but are **attention-scoped**. Raw
notes do not appear in normal core recall; they are available through explicit
agent search, digest, or the review queue.

#### `spawn_agent`

Prepare an agent scope. In the default `core_shared` mode this creates no file.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent_id` | string | **yes** | Unique identifier, e.g. `worker_01` |

Returns `{"agent_id": ..., "status": "spawned"}`.

**Note:** The agent_id becomes the access key. Anyone who knows it can read
that agent's private memories. Use unique, non-guessable IDs if privacy
matters.

#### `agent_remember`

Store an `AGENT_NOTE` in `core.db` with the agent's identity and attention
state. `agent_private` is the default; use `review_ready` for a finding CORE
should review.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent_id` | string | **yes** | The agent's identifier |
| `content` | string | **yes** | Note content |
| `label` | string | no | Short label (optional) |
| `node_type` | string | no | Accepted for compatibility; raw notes are always stored as `AGENT_NOTE` in `core_shared` mode |
| `attention_state` | string | no | `agent_private` or `review_ready` |

Returns `{"agent_id": ..., "node_id": "node_<hex>"}`.

#### `find_across_agents`

Search raw agent notes explicitly. This does not change normal core recall.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `query` | string | **yes** | Search text |
| `min_confidence` | number | no | Minimum similarity 0.0–1.0 (default 0.15) |
| `bound` | integer | no | Max results per agent (default 10) |

Returns `{"clock": ..., "total_found": N, "results": [...]}` where each result
has `_agent_id`, content, similarity, `age`, etc.

#### `promote_to_core`

Verify an agent note in place. Its node ID and graph relationships are kept;
the node becomes the requested final type (default `FACT`).

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent_id` | string | **yes** | Agent identifier |
| `agent_node_id` | string | **yes** | The node_id in the agent's shard |
| `new_type` | string | no | Override node_type (e.g. promote a note to `FACT`) |

Returns `{"core_node_id": <same node_id, promoted in place>, "status": "promoted"}`
or `{"error": "agent node not found"}`.

#### `agent_review_queue`

Return agent findings marked `review_ready`. This is the intended inbox for
CORE; it avoids injecting every worker note into ordinary recall.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `bound` | integer | no | Maximum findings (default 20) |

Returns `{"total_found": N, "notes": [...]}` — each note is a full node dict
(use `note.metadata.agent_id` to identify the agent).

#### `agent_set_attention`

Move one shared agent note between `agent_private` and `review_ready`.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent_id` | string | **yes** | Owner agent |
| `agent_node_id` | string | **yes** | Agent note node ID |
| `attention_state` | string | **yes** | `agent_private` or `review_ready` |

Returns `{"status": "updated"}` or `{"status": "agent node not found"}`.

---

### Query Tools

#### `query_dsl`

Run a structured DSL query string against core memory. Supports three forms:

| Form | Syntax | Description |
|------|--------|-------------|
| FIND + type + label + edge | `FIND PERSON "Sam" -> PREFERENCE` | Find person by label, then follow PREFERENCE edges |
| FIND SEMANTIC | `FIND SEMANTIC "machine learning"` | Semantic vector search |
| FIND PATH | `FIND PATH "node_A" -> "node_B"` | Shortest path between two label-matched nodes |

The DSL handler parses these strings and dispatches to the appropriate recall
mode.

---

### System Tools

#### `profile`

Returns performance data: recent average query time, cache hit rate, vector
index freshness, query log size, cache size. No arguments.

#### `health`

Runs integrity checks. Returns `{"checks": [...]}` — if no issues are found,
returns `["No issues found"]`.

#### `stats`

Returns aggregate memory statistics: node/edge counts, type breakdowns, memory
layer breakdowns (working/short_term/long_term/archive), legacy shard count,
and current configuration values. No arguments.

#### `rebuild_vector_index`

Forces a full rebuild of the TF-IDF vector index from scratch. Useful after
bulk imports or if the index becomes stale. No arguments. Returns
`{"status": "rebuilt"}`.

#### `export_json`

Exports all core memory as a JSON file.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `path` | string | **yes** | Output file path |

Returns `{"path": ..., "status": "exported"}`.

#### `get_bloat_metrics`

No-mutation self-heal signal: freelist %, ephemeral log counts, `CONTRADICTS` noise.

Returns same as `asha://memory/bloat` — use before deciding to compact.

#### `compact_ephemeral_logs`

Caps `FEED_SNAPSHOT`/`RUNTIME_SAMPLE` etc: `keep_last` (default 3) + `max_age_days` (default 7). Removes stale edges, vacuums, rebuilds vectors. Safe — `PERSON`/`FACT` never touched.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `keep_last` | integer | no | Keep last N per label |
| `max_age_days` | integer | no | TTL days |

#### `vacuum`

`VACUUM` reclaims freelist. Returns `{"before_mb":6.19,"after_mb":2.47,"saved_mb":3.72}`.

---

## Resources

Resources are read-only data URIs you can fetch with `resources/read`.

| URI | What you get |
|-----|-------------|
| `asha://memory/stats` | Same data as `stats` tool |
| `asha://memory/health` | Same data as `health` tool |
| `asha://memory/profile` | Same data as `profile` tool |
| `asha://memory/bloat` | Same data as `get_bloat_metrics` |
| `asha://skills` | Full skill registry listing |

**Reading a resource:**

```json
{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"asha://memory/stats"}}
```

Resources return structured JSON with a `data` key containing the payload.

---

## Time Context (Internal Clock)

The memory system carries its own clock (stdlib only, no MCP changes).

- **Node summaries:** every node from `get_node` includes a `_clock` metadata
  object: `added` / `added_at`, `last_checked` / `last_checked_at`, `access_count`,
  `layer`, and `stale` (not checked in 7 days). Example:

  ```json
  "_clock": {"added": "2 weeks ago", "last_checked": "3 days ago",
             "access_count": 12, "layer": "long_term", "stale": false}
  ```

- **Honest freshness:** `last_checked` comes from `access_log` *before* the
  current query's access, so a node returned by your own recall still reports
  its previous check. `access_count` is the live post-access count.
- **TODAY context node:** a daily `EVENT` node (label `TODAY`, source `CLOCK`)
  holds the current date/time and today's memory activity (nodes added,
  accessed, edges created, queries run). Ask for it with normal recall:

  ```
  recall(query="what is today's date", mode="SEMANTIC")
  ```

- **Freshness workflow:** `cmd_recall` now serializes the top-level `clock`
  (today's date/time) and a per-node `age` summary in the response body. For
  the full detail, follow up with `get_node(node_id)` — its response includes
  the complete `_clock` metadata object.

---

## Workflows

### 1. First Contact — Discover and Initialize

```
→ initialize
→ notifications/initialized
→ tools/list           (learn all 23 tools)
→ resources/list       (learn all 5 resources)
```

### 1b. Self-Heal (agents should run this first)

```
→ get_bloat_metrics() → if needs_vacuum or ephemeral._total > 10: compact_ephemeral_logs()
```

### 2. Store Knowledge About a Person (Complete Pattern)

Store a person profile, then link preferences and facts to it.

```
→ remember(content="Sam builds AI systems and prefers CLI over GUI tools.",
           node_type="PERSON", label="Sam", source="USER", trust=0.9, importance=0.8)
  ← {"node_id": "node_person_sam"}

→ remember(content="Sam prefers dark mode in all development environments to reduce eye strain.",
           node_type="PREFERENCE", label="theme", trust=0.8, importance=0.6)
  ← {"node_id": "node_pref_theme"}

→ remember(content="Sam is comfortable with AI systems that exhibit opaque emergent behavior.",
           node_type="PREFERENCE", label="ai_attitude", trust=0.7, importance=0.7)
  ← {"node_id": "node_pref_ai"}

→ relate(from_id="node_person_sam", to_id="node_pref_theme", edge_type="HAS_PREFERENCE")
→ relate(from_id="node_person_sam", to_id="node_pref_ai", edge_type="HAS_PREFERENCE")
```

### 3. Before Storing, Check What Exists

Always recall before storing to avoid duplicates.

```
→ recall(query="Sam dark mode preference", mode="SEMANTIC", bound=3)
  ← if found with high similarity → use existing node_id, don't re-store
  ← if not found → safe to store new node
```

### 4. Semantic Search

```
→ recall(query="user interface preferences and eye strain", mode="SEMANTIC", bound=5)
  ← {"mode": "SEMANTIC", "total_found": 2, "nodes": [...]}
```

### 5. Agent with Private Memory

```
→ spawn_agent(agent_id="research_agent")
  ← {"agent_id": "research_agent", "status": "spawned"}

→ register_skill(name="PATTERN_RECOG",
                 description="Recognize patterns in data across multiple sources")
  ← {"skill": "PATTERN_RECOG", "status": "registered"}

→ assign_skill(agent_id="research_agent", skill_name="PATTERN_RECOG")
  ← {"status": "assigned"}

→ agent_remember(agent_id="research_agent",
                 content="Observed cyclical pattern in user requests every 2 weeks",
                 label="pattern_001")
  ← {"node_id": "node_789ghi"}
```

### 6. Promote Agent Insight to Core

```
→ promote_to_core(agent_id="research_agent", agent_node_id="node_789ghi", new_type="FACT")
  ← {"core_node_id": "node_789ghi", "status": "promoted"}   # same node, promoted in place
```

Now any agent can find this insight via `recall` in core.

### 7. Cross-Agent Search

```
→ find_across_agents(query="user behavior patterns", min_confidence=0.2)
  ← {"total_found": 3, "results": [...]}
```

### 8. Health Check Before Operations

```
→ health()
  ← {"checks": ["No issues found"]}
→ stats()
  ← {"core_nodes": 120, "core_edges": 85, ...}
```

### 9. Graph Navigation

Starting from a known node, explore its connections.

```
→ get_node(node_id="node_some_id")
  ← full node data

→ recall(query="node_some_id", mode="RELATED")
  ← all nodes connected by edges

→ recall(query="node_some_id", mode="CLUSTER", bound=15)
  ← expanded neighborhood
```

### 10. Regular Maintenance

```
# Before each session:
→ stats()           # check how many nodes exist
→ health()          # verify integrity

# If search seems broken:
→ rebuild_vector_index()

# Periodically:
→ recall(query="", mode="PRUNE", bound=20)  # find decay candidates
# Review and decide what to keep or let decay
```

---

## Error Handling

All errors follow the JSON-RPC 2.0 format with an `error` object containing
`code`, `message`, and optional `data`.

| Code | Constant | When it happens |
|------|----------|-----------------|
| `-32700` | PARSE_ERROR | JSON parse failure |
| `-32600` | INVALID_REQUEST | Missing method field |
| `-32601` | METHOD_NOT_FOUND | Unknown method name |
| `-32602` | INVALID_PARAMS | Missing required tool arguments |
| `-32603` | INTERNAL_ERROR | Unexpected server error |
| `-32001` | TOOL_NOT_FOUND | `tools/call` with unknown tool name |
| `-32002` | TOOL_EXECUTION_ERROR | Tool handler raised an error (e.g. invalid args) |
| `-32003` | RESOURCE_NOT_FOUND | `resources/read` with unknown URI |

**Example error:**

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "error": {
    "code": -32002,
    "message": "Invalid arguments for remember: remember() missing 1 required positional argument: 'content'"
  }
}
```

---

## Protocol Notes

- **Transport:** stdio — one JSON-RPC message per line (newline-delimited)
- **Protocol version:** MCP `2025-03-26`
- **JSON-RPC version:** `2.0`
- **Initialization handshake** — clients should send `initialize` (and
  `notifications/initialized`) first to negotiate the protocol version. The
  server accepts requests before the handshake, but the handshake is required
  by MCP-compliant clients.
- **Notifications** (no `id` field) receive no response
- **Requests** (with `id` field) always receive a response (result or error)
- **Ping** is supported: `{"jsonrpc":"2.0","id":1,"method":"ping"}` → `{"result": {}}`
- **String encoding:** all strings are UTF-8
- **Vector index** is rebuilt automatically when the lexicon version changes;
  `rebuild_vector_index` forces a manual rebuild

---

## Quick Reference Card

```
TOOLS:
  register_skill(name, description, level?, tags?)
  find_skills(query, level?)
  assign_skill(agent_id, skill_name)
  agent_skills(agent_id)
  remember(content, node_type, label?, source?, trust?, importance?)
  recall(query, mode?, bound?, limit?, include_agent_notes?)
  relate(from_id, to_id, edge_type, weight?)
  get_node(node_id)
  spawn_agent(agent_id)
  agent_remember(agent_id, content, label?, node_type?, attention_state?)
  find_across_agents(query, min_confidence?, bound?)
  promote_to_core(agent_id, agent_node_id, new_type?)
  query_dsl(query)
  profile()
  health()
  stats()
  rebuild_vector_index()
  export_json(path)
  get_bloat_metrics()
  compact_ephemeral_logs(keep_last?, max_age_days?)
  vacuum()

RESOURCES:
  asha://memory/stats
  asha://memory/health
  asha://memory/profile
  asha://memory/bloat
  asha://skills

NODE TYPES:
  PERSON FACT PREFERENCE EVENT TOPIC AFFECT BOUNDARY SKILL AGENT_NOTE CORE_REF

EDGE TYPES:
  RELATES_TO CONTRADICTS SUPPORTS CAUSED_BY PART_OF TRUSTS DISTRUSTS
  REMEMBERS HAS_PREFERENCE HAS_BOUNDARY HAS_AFFECT HAS_SKILL REFERS_TO SUMMARIZES

RECALL MODES:
  SEMANTIC RELATED WHO_IS WHAT_ABOUT PATH CLUSTER TIMELINE RECENT PRUNE

SKILL LEVELS:
  CORE_ONLY ASSIGNABLE AGENT_AUTO AGENT_ONLY

TRUST GUIDE:
  0.9–1.0 = confirmed/direct  0.6–0.8 = likely  0.3–0.5 = speculative  0.0–0.2 = low

IMPORTANCE GUIDE:
  0.9–1.0 = critical  0.6–0.8 = important  0.3–0.5 = normal  0.0–0.2 = trivial

ALWAYS recall before storing to check for duplicates.
Set importance for survival, trust for confidence.
Draft as an `agent_private` note, submit `review_ready` findings, then promote
verified insights in place.
