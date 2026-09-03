# ASHA MCP Server — AI Agent Guide

This document describes the Model Context Protocol (MCP) server for the ASHA
memory system. If you are an AI agent connected to this server, use this guide
to understand every tool, resource, and workflow available to you.

---

## Table of Contents

1. [What This Is](#what-this-is)
2. [Connecting](#connecting)
3. [Tools Overview](#tools-overview)
4. [Tool Reference](#tool-reference)
   - [Skill Tools](#skill-tools)
   - [Core Memory Tools](#core-memory-tools)
   - [Agent Tools](#agent-tools)
   - [Query Tools](#query-tools)
   - [System Tools](#system-tools)
5. [Resources](#resources)
6. [Workflows](#workflows)
7. [Error Handling](#error-handling)
8. [Protocol Notes](#protocol-notes)

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
| `node_type` | string | no | Optional post-filter: `PERSON`\|`FACT`\|`PREFERENCE`\|`EVENT`\|`TOPIC`\|`AFFECT`\|`BOUNDARY`\|`SKILL`\|`AGENT_NOTE`\|`CORE_REF` |

Returns `{"mode": ..., "total_found": N, "clock": {...}, "nodes": [...]}`.
`clock` is today's date/time snapshot from the internal clock. Each node
includes `node_id`, `node_type`, `label`, `content` (truncated to 200 chars),
`trust_level`, `importance`, `similarity` (semantic score), and `age` (per-node
temporal summary: `added`, `last_checked`, `access_count`, `layer`, `stale`).

**Recall modes explained:**

| Mode | What it does | Best for |
|------|-------------|----------|
| `SEMANTIC` | TF-IDF vector similarity against query text | Open-ended search: "What do I know about X?" |
| `RELATED` | Follow edges from a starting node | Navigation: "What is connected to this node?" |
| `WHO_IS` | Case-insensitive search for node_type=`PERSON` + label contains query | "Tell me about user Sam" |
| `WHAT_ABOUT` | Case-insensitive label or content match | Targeted lookup by name/title |
| `PATH` | Shortest path between two nodes (query as `"A" -> "B"`) | "How are these two things connected?" |
| `CLUSTER` | Expand outward from a node by edges | "What cluster of knowledge surrounds this?" |
| `TIMELINE` | Nodes ordered by `created_at` within time proximity | "What happened around this event?" |
| `RECENT` | Most recently created/updated nodes | "What's new?" |
| `PRUNE` | Low-importance, low-trust nodes | Cleanup candidates |

**Strategy tips:**
- Start with `SEMANTIC` when you don't know the exact label or node_id
- Use `WHO_IS` for person lookups — it's faster and more precise
- Chain `recall` with `relate`: find a node, then link new knowledge to it
- If `SEMANTIC` returns nothing relevant, widen your query or try `RECENT` first

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

Returns full node data (including a top-level `clock` snapshot and the
`metadata._clock` temporal summary) or `{"error": "not found"}`.

---

### Agent Tools

Agent notes live in `core.db` as graph-connectable `AGENT_NOTE` nodes, but raw
notes remain outside normal core recall. Each note has an attention state:
`agent_private` (default), `review_ready`, or `core_verified`.

#### `spawn_agent`

Prepare an agent scope. Default `core_shared` mode creates no database file.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent_id` | string | **yes** | Unique identifier, e.g. `worker_01` |

Returns `{"agent_id": ..., "status": "spawned"}`.

**Note:** The agent_id becomes the access key. Anyone who knows it can read
that agent's private memories. Use unique, non-guessable IDs if privacy
matters.

#### `agent_remember`

Store a graph-connected private note. Set `attention_state` to `review_ready`
when CORE should see it in the review queue.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent_id` | string | **yes** | The agent's identifier |
| `content` | string | **yes** | Note content |
| `label` | string | no | Short label (optional) |
| `node_type` | string | no | Accepted for compatibility; raw notes are always stored as `AGENT_NOTE` in `core_shared` mode |
| `attention_state` | string | no | `agent_private` or `review_ready` |

Returns `{"agent_id": ..., "node_id": "node_<hex>"}`.

#### `find_across_agents`

Search raw agent notes explicitly without changing normal core recall.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `query` | string | **yes** | Search text |
| `min_confidence` | number | no | Minimum similarity 0.0–1.0 (default 0.15) |
| `bound` | integer | no | Max results per agent (default 10) |

Returns `{"clock": ..., "total_found": N, "results": [...]}` where each result
has `_agent_id`, content, similarity, `age`, etc.

#### `promote_to_core`

Verify an agent note in place. Promotion preserves its node ID and graph links,
then changes it to the requested final type (default `FACT`).

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

Returns DB bloat signals without mutating. Use before deciding to self-heal.

No arguments. Returns:
```json
{
  "page_count": 1585, "page_size": 4096, "freelist_count": 623,
  "total_mb": 6.19, "free_mb": 2.43, "used_mb": 3.76,
  "freelist_pct": 39.3, "needs_vacuum": true,
  "ephemeral": {"FEED_SNAPSHOT":21,"RUNTIME_SAMPLE":40,"_total_ephemeral_labels":76},
  "contradicts_total": 317, "contradicts_ephemeral": 95
}
```
Check `needs_vacuum` and `ephemeral._total_ephemeral_labels > 10` to decide.

#### `compact_ephemeral_logs`

Compacts telemetry logs that cause bloat ( `FEED_SNAPSHOT`, `RUNTIME_SAMPLE`, `DAILY_STATE`, `BRAIN_MAINTENANCE_REPORT`, etc.). Keeps last N per label + TTL, deletes old edges/vectors, then auto-vacuums and rebuilds vectors.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `keep_last` | integer | no | Keep last N per label (default 3) |
| `max_age_days` | integer | no | TTL in days (default 7) |

Returns `{"removed_total": 59, "removed_per_label": {"FEED_SNAPSHOT":18}, "edges_removed":814, "vacuum":{"saved_mb":3.72}, "bloat_after":{...}}`.

> **Self-heal workflow:** `get_bloat_metrics` → if `needs_vacuum` or `ephemeral` high → `compact_ephemeral_logs` → `vacuum` if still needed. Idempotent and safe to call; core `FACT`/`PERSON` nodes are never touched.

#### `vacuum`

Runs `VACUUM` to reclaim freelist after deletes. No arguments.

Returns `{"before_mb":6.19,"after_mb":2.47,"saved_mb":3.72}`.

---

## Resources

Resources are read-only data URIs you can fetch with `resources/read`.

| URI | What you get |
|-----|-------------|
| `asha://memory/stats` | Same data as `stats` tool |
| `asha://memory/health` | Same data as `health` tool |
| `asha://memory/profile` | Same data as `profile` tool |
| `asha://memory/bloat` | Same data as `get_bloat_metrics` — ephemeral/freelist |
| `asha://skills` | Full skill registry listing |

**Reading a resource:**

```json
{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"asha://memory/stats"}}
```

Resources return structured JSON with a `data` key containing the payload.

---

## Workflows

### 1. First Contact — Discover and Initialize

```
→ initialize
→ notifications/initialized
→ tools/list           (learn all 23 tools)
→ resources/list       (learn all 5 resources)
```

### 1b. Self-Heal Check (new)

```
→ get_bloat_metrics()
  ← {"needs_vacuum":true,"freelist_pct":39.3,"ephemeral":{"FEED_SNAPSHOT":21}}
→ if needs_vacuum or ephemeral > 10: compact_ephemeral_logs(keep_last=3, max_age_days=7)
  ← {"removed_total":59,"vacuum":{"saved_mb":3.7}}
```

### 2. Store Knowledge and Link It

```
→ remember(content="User prefers dark mode", node_type="PREFERENCE", label="theme_pref")
  ← {"node_id": "node_abc123"}
→ remember(content="User mentioned light sensitivity", node_type="FACT", label="photo_sensitivity")
  ← {"node_id": "node_def456"}
→ relate(from_id="node_abc123", to_id="node_def456", edge_type="SUPPORTS")
  ← {"status": "ok"}
```

### 3. Semantic Search

```
→ recall(query="user interface preferences", mode="SEMANTIC", bound=5)
  ← {"mode": "SEMANTIC", "total_found": 2, "nodes": [...]}
```

### 4. Agent with Private Memory

```
→ spawn_agent(agent_id="research_agent")
  ← {"agent_id": "research_agent", "status": "spawned"}
→ register_skill(name="PATTERN_RECOG", description="Recognize patterns in data")
  ← {"skill": "PATTERN_RECOG", "status": "registered"}
→ assign_skill(agent_id="research_agent", skill_name="PATTERN_RECOG")
  ← {"status": "assigned"}
→ agent_remember(agent_id="research_agent", content="Observed cyclical pattern in user requests", label="pattern_001")
  ← {"node_id": "node_789ghi"}
```

### 5. Promote Agent Insight to Core

```
→ promote_to_core(agent_id="research_agent", agent_node_id="node_789ghi", new_type="FACT")
  ← {"core_node_id": "node_789ghi", "status": "promoted"}   # same node, promoted in place
```

Now any agent can find this insight via `recall`.

### 6. Cross-Agent Search

```
→ find_across_agents(query="user behavior patterns", min_confidence=0.2)
  ← {"total_found": 3, "results": [...]}
```

### 7. Health Check Before Operations

```
→ health()
  ← {"checks": ["No issues found"]}
→ stats()
  ← {"core_nodes": 120, "core_edges": 85, ...}
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
  agent_review_queue(bound?)
  agent_set_attention(agent_id, agent_node_id, attention_state)
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
```
