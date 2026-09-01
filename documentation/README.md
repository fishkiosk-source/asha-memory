# ASHA MEMORY SYSTEM v2

Local SQLite memory graph for AI entities. 100% local, zero AI calls, with
shared graph storage and attention-scoped agent notes.

## Files

| File                                | Purpose                                                 |
| ----------------------------------- | ------------------------------------------------------- |
| `asha_memory_v2.py`                 | Main system — single-file, importable, runnable demo    |
| `asha_mcp.py`                       | MCP Protocol Server — stdio JSON-RPC 2.0 interface      |
| `internal_clock.py`                 | Time context — node ages, last-checked, TODAY node      |
| `ASHA_SKILLS_REGISTRY.txt`          | v2 skill registry — 53 skills, 8 categories             |
| `brain/`                            | Autonomous maintenance engine + scheduler + dashboard   |
| `documentation/README.md`           | This file                                               |
| `documentation/GUIDE.md`            | Comprehensive AI agent usage guide                      |
| `documentation/mcp/MCP_AI_GUIDE.md` | Complete guide for MCP client integration               |
| `documentation/docs/`               | Architecture + design plans (V2, lexicon, clock)        |
| `documentation/SKILLS/`             | CORESKILLS.md / AGENTSKILLS.md — AI operating guides    |
| `humantools/asha_inspector.html`    | Standalone SQLite inspector (open any `.db` in browser) |
| `humantools/asha_graph.html`        | Visual memory graph inspector                           |

## Quick Start

```python
from asha_memory_v2 import AshaMemory

mem = AshaMemory(base_path="./my_memory")

# Store a memory
sid = mem.remember("ASHA_CORE is a memory graph system.", node_type="FACT", source="USER")

# Recall
r = mem.recall("memory graph", mode="SEMANTIC")
for n in r.nodes:
    print(n.label, n.content, n.metadata.get("_similarity"))
```

## Architecture

```
┌──────────────────────────────────────────────────┐
│                  AshaMemory                       │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐ │
│  │  CORE DB   │  │  AGENT     │  │  SKILL     │ │
│  │  core.db   │  │  SCOPES    │  │  Registry  │ │
│  └─────┬──────┘  └─────┬──────┘  └────────────┘ │
│        │               │                         │
│  ┌─────┴──────┐  ┌─────┴──────┐                  │
│  │ Memory     │  │ AGENT_NOTE │                  │
│  │ Layers     │  │ nodes in   │                  │
│  │ working    │  │ core.db    │                  │
│  │ short_term │  │ hidden from│                  │
│  │ long_term  │  │ normal     │                  │
│  │ archive    │  │ recall     │                  │
│  └────────────┘  └────────────┘                  │
│  ┌────────────┐  ┌────────────┐                 │
│  │ TF-IDF     │  │ LRU Cache  │                 │
│  │ Vectorizer │  │ (50 items) │                 │
│  └────────────┘  └────────────┘                 │
│  ┌────────────┐  ┌────────────┐                 │
│  │ Query DSL  │  │ Cosine     │                 │
│  │ Parser     │  │ Consol.    │                 │
│  └────────────┘  └────────────┘                 │
└──────────────────────────────────────────────────┘
```

## Core Concepts

### Time Context (Internal Clock)

Every node returned by `recall()` / `get_node()` carries a `_clock` summary in
its metadata: when it was added, when it was last checked (from `access_log`,
before the current query), access count, memory layer, and a `stale` flag.
A daily `TODAY` EVENT node keeps the graph self-aware — `recall("today date")`
answers with the current date/time and today's memory activity. All stdlib,
zero MCP changes.

### Nodes

Typed nodes with label, content, metadata, trust, importance, layer.

| Node Type    | Purpose                    | Example                         |
| ------------ | -------------------------- | ------------------------------- |
| `PERSON`     | Identity of subject        | `"SAM"`                         |
| `FACT`       | Ground truth knowledge     | `"ASHA_CORE is a memory graph"` |
| `PREFERENCE` | Behavioral preference      | `"Prefers honesty"`             |
| `EVENT`      | Temporal occurrence        | `"Login at 12:00"`              |
| `TOPIC`      | Subject area               | `"Machine Learning"`            |
| `AFFECT`     | Emotional/affective state  | `"Frustration event"`           |
| `BOUNDARY`   | Constraint or limit        | `"Cannot access /etc"`          |
| `SKILL`      | Capability (from registry) | `"EXECUTE_CODE"`                |
| `AGENT_NOTE` | Agent-scoped note          | `"Observed pattern X"`          |
| `CORE_REF`   | Reference back to core     | `"Core memory Y"`               |

### Edges

Typed directed edges between nodes.

| Edge Type        | Purpose                              |
| ---------------- | ------------------------------------ |
| `RELATES_TO`     | Generic relation                     |
| `CONTRADICTS`    | Contradicting / opposing information |
| `SUPPORTS`       | Evidence or backing relation         |
| `CAUSED_BY`      | Event causal relationship            |
| `PART_OF`        | Composition / sub-component          |
| `TRUSTS`         | Person/Agent trust relation          |
| `DISTRUSTS`      | Person/Agent distrust relation       |
| `REMEMBERS`      | Memory link                          |
| `HAS_PREFERENCE` | Person → Preference                  |
| `HAS_BOUNDARY`   | Person → Boundary                    |
| `HAS_AFFECT`     | Person → Affective state             |
| `HAS_SKILL`      | Agent/Core → Skill                   |
| `REFERS_TO`      | Cross-reference pointer              |
| `SUMMARIZES`     | Summary node → Source nodes          |

## SKILL System

### Hierarchy Levels

| Level        | Tag       | Scope                      |
| ------------ | --------- | -------------------------- |
| `CORE_ONLY`  | `[CORE]`  | Built-in, always available |
| `ASSIGNABLE` | `[ASN]`   | Core assigns to agents     |
| `AGENT_AUTO` | `[AUTO]`  | Agent auto-grants on spawn |
| `AGENT_ONLY` | `[AGENT]` | Agent self-registers       |

### API

```python
# Register a skill manually (level: CORE_ONLY|ASSIGNABLE|AGENT_AUTO|AGENT_ONLY)
mem.register_skill("EXECUTE_CODE", "Execute arbitrary Python", "ASSIGNABLE", "EXECUTION")

# Load from registry file (optional; returns 0 if the file does not exist)
mem.load_skill_registry("./ASHA_SKILLS_REGISTRY.txt")

# Find skills by keyword (matches label and content)
skills = mem.find_skills(query="natural language")

# Assign a registered skill to an agent by name (creates the agent's anchor node)
edge_id = mem.assign_skill("agent_01", "EXECUTE_CODE")

# List skills available to an agent: auto-granted AGENT_AUTO skills plus
# any skills explicitly assigned via assign_skill()
agent_skills = mem.agent_skills("agent_01")
# Returns a list of skill dicts: [{"name", "description", "node_id", "metadata"}, ...]
```

> **Note on skill assignment:** the Python API and the MCP
> `assign_skill(agent_id, skill_name)` tool both take an agent identifier and a
> registered skill name. The first assignment creates the agent's `AGENT_NOTE`
> anchor node, then links it with a `HAS_SKILL` edge. Assigning an unregistered
> skill raises `ValueError`. `agent_skills(agent_id)` returns the union of
> `AGENT_AUTO` skills (auto-granted) and the skills explicitly assigned to that
> agent.

### Categories (8 total, 53 skills)

> The category counts below describe the bundled skill registry file
> (`ASHA_SKILLS_REGISTRY.txt` at the project root, loaded via
> `load_skill_registry()`). Skills can also be registered manually via
> `register_skill()`.

| Category          | Count | Examples                                 |
| ----------------- | ----- | ---------------------------------------- |
| `CORE_OPERATIONS` | 11    | INIT, SHUTDOWN, RECOVER, GET_STATUS      |
| `COMMUNICATION`   | 7     | SEND_MESSAGE, RECEIVE_MESSAGE, BROADCAST |
| `MEMORY`          | 7     | STORE, RECALL, FORGET, QUERY_LINKS       |
| `REASONING`       | 7     | LOGICAL_REASON, ANALYZE, DECOMPOSE       |
| `LEARNING`        | 7     | OBSERVE, PATTERN_RECOG, UPDATE_MODEL     |
| `CREATIVITY`      | 5     | GENERATE_TEXT, REMIX, EXPLORE            |
| `EXECUTION`       | 5     | EXECUTE_CODE, RUN_QUERY, FILE_OPS        |
| `META`            | 4     | REFLECT, PLAN, SELF_MODIFY               |

## CORE Memory

### Storing

```python
mem.remember(content="text", node_type="FACT", label="optional_label",
              source="USER", trust=0.8, importance=0.6, metadata={})

mem.relate(from_id, to_id, "RELATES_TO", weight=1.0)
```

### Recall Modes

| Mode         | Behavior                               | Example                                      |
| ------------ | -------------------------------------- | -------------------------------------------- |
| `RELATED`    | Keyword substring match (v1 default)   | `recall("AI", mode="RELATED")`               |
| `WHO_IS`     | 1-hop from PERSON node                 | `recall("SAM", mode="WHO_IS")`               |
| `WHAT_ABOUT` | 2-hop from TOPIC node                  | `recall("learning", mode="WHAT_ABOUT")`      |
| `RECENT`     | Temporal slice, newest first           | `recall(bound=10, mode="RECENT")`            |
| `SEMANTIC`   | TF-IDF cosine similarity (v2)          | `recall("opaque behavior", mode="SEMANTIC")` |
| `PATH`       | Shortest weighted path between 2 nodes | `recall("A -> B", mode="PATH")`              |
| `CLUSTER`    | BFS neighborhood grouped by type       | `recall("node_id", mode="CLUSTER")`          |
| `TIMELINE`   | Chronological events from a node       | `recall("node_id", mode="TIMELINE")`         |
| `PRUNE`      | Low-importance candidates for cleanup  | `recall(mode="PRUNE")`                       |

Returns `RecallResult` with `.nodes` (list), `.total_found` (int), `.mode` (str).

## AGENT System

### Shared Agent Notes

New agent notes are stored in `core.db` as graph-connectable `AGENT_NOTE`
nodes. They retain `source="AGENT_<id>"`, `agent_id`, and an attention state,
but normal `recall()` excludes them so raw worker activity cannot fill the main
AI's context window. States are `agent_private` (default), `review_ready`, and
`core_verified`. Set `agent_memory_mode` to `legacy_shards` only when an older
per-agent database deployment must be retained.

```python
# Spawn agent memory
mem.spawn_agent_memory("agent_007")

# Store agent-scoped note
mem.agent_remember("agent_007", "Observed pattern in user data", "research")

# Main AI sees only findings explicitly submitted for review
review = mem.agent_review_queue()

# Explicitly search raw agent notes when needed
results = mem.find_across_agents("relational queries", min_confidence=0.15, bound=5)
# Each result has _agent_id, _similarity
```

### Promotion (Agent → Core)

```python
# Promote in place: the node ID and graph links are preserved
promoted_id = mem.promote_to_core("agent_007", "node_id", new_type="FACT")
```

## v2-Specific Features

### TF-IDF Semantic Search

Pure-Python `TfidfVectorizer` (stdlib only). Tokenizes with a Unicode-aware pattern `\b[\w']{2,}\b` — captures non-English (`Müller`, `français`), contractions (`don't`), usernames (`user123`), and short terms (`AI`, `go`). Computes TF × smoothed IDF. Cosine similarity with configurable floor (`semantic_relevance_floor: 0.1`).

**No stemmer.** The system removed naive suffix stemming because it corrupted words like `education→educa`, `attention→atten`. IDF naturally handles variant conflation — `runs`, `running`, `ran` in different documents each contribute their own signal.

**Shared tokenizer.** All text processing (`_tokenize`, `_extract_keywords`, `_jaccard_similarity`, `_sentiment_score`) uses the same `_tokenize()` function via a shared compiled regex. No regex duplication or drift.

Vectors stored in `node_vectors` table. Version-gated cache with `_invalidate_vectorizer()` — no silent state refresh on concurrent access.

### Memory Layers

| Layer        | Promotion Trigger     | Capacity |
| ------------ | --------------------- | -------- |
| `working`    | Default for new nodes | 20       |
| `short_term` | After 3 accesses      | —        |
| `long_term`  | After 15 accesses     | —        |
| `archive`    | Manual or prune       | —        |

```python
# Access decay: weight *= decay_factor_per_day^days
# Access boost: weight += access_boost on recall
# Configurable via config key
```

### Query DSL

```
FIND PERSON "SAM" -> PREFERENCE        # Who is SAM → what preferences
FIND FACT "python" -> TOPIC            # Fact about python → topics
FIND PERSON "SAM" -> * + BOUNDARY     # Everything about SAM + boundaries
```

```python
result = mem.query('FIND PERSON "SAM" -> PREFERENCE')
# result.mode == "WHO_IS", result.nodes, result.total_found
```

### Cosine Consolidation

```python
# Auto-run during store:
# similarity > 0.85 → merge nodes (higher-trust content kept)
# similarity > 0.50 → auto-link with RELATES_TO edge
```

### Export

```python
# GraphML (Gephi, yEd)
mem.export_graphml("export.graphml")

# JSON
mem.export_json("export.json")
```

### Introspection

```python
mem.profile()
# {
#   "recent_avg_ms": 72.0,
#   "cache_hit_rate": 0.0,
#   "cache_hits": 0,
#   "cache_misses": 3,
#   "vector_index_freshness": "2/2 nodes indexed",
#   "query_log_size": 3,
#   "cache_size": 3
# }

mem.health()
# ["No issues found"] or list of warnings

mem.stats()
# {
#   "core_nodes": 2,
#   "core_edges": 1,
#   "core_type_breakdown": {PERSON: 1, PREFERENCE: 1},
#   "memory_layer_breakdown": {working: 2},
#   "agent_shards": 1,
#   "config": {...}
# }
```

## Configuration

Default config (auto-saved to `config.json` in base_path):

```python
DEFAULT_CONFIG = {
    "schema_version": "2.0",
    "max_nodes_per_recall": 30,
    "max_edges_per_recall": 50,
    "max_content_length": 500,
    "decay_factor_per_day": 0.99,
    "access_boost": 0.05,
    "prune_threshold": 0.05,
    "consolidation_similarity_high": 0.85,
    "consolidation_similarity_link": 0.5,
    "default_trust": 0.5,
    "default_importance": 0.5,
    "agent_max_notes": 100,
    "agent_max_content_length": 800,
    "agent_memory_mode": "core_shared",
    "semantic_relevance_floor": 0.1,
    "vector_index_auto_rebuild": True,
    "cache_capacity": 50,
    "working_memory_capacity": 20,
    "short_term_promote_after": 3,
    "long_term_promote_after": 15,
    "internal_clock": True,
}
```

Override any key by placing it in `config.json` — values merge at startup.

## Schema (v2)

Created by `CORE_SCHEMA_V2` in `asha_memory_v2.py` (simplified below; see the
source for full CHECK constraints and FTS triggers).

### Core Tables

```sql
CREATE TABLE nodes (
    node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL,
    label TEXT NOT NULL, content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'CORE',
    trust_level REAL NOT NULL DEFAULT 0.5 CHECK (trust_level BETWEEN 0 AND 1),
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
    checksum TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
    CHECK (node_type IN ('PERSON','TOPIC','EVENT','FACT','PREFERENCE',
           'BOUNDARY','AFFECT','AGENT_NOTE','CORE_REF','SKILL'))
);

CREATE TABLE edges (
    edge_id TEXT PRIMARY KEY, from_node TEXT NOT NULL REFERENCES nodes ON DELETE CASCADE,
    to_node TEXT NOT NULL REFERENCES nodes ON DELETE CASCADE, edge_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight BETWEEN -1 AND 1),
    created_at INTEGER NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(from_node, to_node, edge_type)
);

-- External-content full-text index (synced by AFTER INSERT/UPDATE/DELETE triggers)
CREATE VIRTUAL TABLE node_fts USING fts5(label, content, content='nodes', content_rowid='rowid');

CREATE TABLE node_index (word TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES nodes,
    field TEXT NOT NULL DEFAULT 'content', weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (word, node_id, field));

CREATE TABLE access_log (log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES nodes ON DELETE CASCADE, accessed_at INTEGER NOT NULL);

CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- v2: TF-IDF vectors
CREATE TABLE node_vectors (
    node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE,
    vector TEXT NOT NULL, magnitude REAL NOT NULL DEFAULT 0.0
);

-- v2: Memory layers
CREATE TABLE memory_layers (
    node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE,
    layer TEXT NOT NULL DEFAULT 'short_term', promoted_at INTEGER,
    layer_order INTEGER NOT NULL DEFAULT 2,
    CHECK (layer IN ('working','short_term','long_term','archive'))
);

-- v2: Query log
CREATE TABLE query_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT, query_text TEXT NOT NULL,
    mode TEXT NOT NULL, result_count INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0, cache_hit INTEGER NOT NULL DEFAULT 0,
    queried_at INTEGER NOT NULL
);
```

> **Note:** the `query_log` table currently stays empty — query history is kept
> in-memory (`profile()`'s `query_log_size`), and `_log_query()` does not insert
> rows into it.

### Agent Tables (legacy per-agent shards)

Same shape as core (nodes with `CHECK (node_type IN ('TOPIC','EVENT','FACT',
'PREFERENCE','AFFECT','AGENT_NOTE','CORE_REF'))`, edges, node_fts, node_index,
access_log, schema_meta). Only used when `agent_memory_mode = "legacy_shards"`.

## Inspector

Open `humantools/asha_inspector.html` in any browser, drag-drop or pick a `.db`
file. `humantools/asha_graph.html` renders the graph visually.

**Tabs:** Nodes, Edges, Vectors, Layers, Schema, Stats

- Vectors tab: top-10 TF-IDF terms per node with magnitude
- Layers tab: working/short_term/long_term/archive breakdown
- Handles v1 DBs gracefully (v2 tables absent → shows "v1 database")

## MCP Server (Model Context Protocol)

`asha_mcp.py` exposes the memory system as MCP tools + resources over stdio transport.
Compatible with any MCP client (Claude Desktop, Cline, Continue, etc.).

### Tools (23 total)

| Category   | Tools                                                                                                                        |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------- |
| **Skills** | `register_skill`, `find_skills`, `assign_skill`, `agent_skills`                                                              |
| **Core**   | `remember`, `recall`, `relate`, `get_node`                                                                                   |
| **Agents** | `spawn_agent`, `agent_remember`, `find_across_agents`, `agent_review_queue`, `agent_set_attention`, `promote_to_core`        |
| **Query**  | `query_dsl`                                                                                                                  |
| **System** | `profile`, `health`, `stats`, `rebuild_vector_index`, `export_json`, `get_bloat_metrics`, `compact_ephemeral_logs`, `vacuum` |

### Usage

```bash
# Start server (stdio transport)
python asha_mcp.py --base-path ./mcp_data --skills ./ASHA_SKILLS_REGISTRY.txt

# Pipe JSON-RPC requests
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python asha_mcp.py

# Store + recall
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"remember","arguments":{"content":"ASHA memory system","node_type":"FACT"}}}' | python asha_mcp.py

# Agent operations
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"spawn_agent","arguments":{"agent_id":"worker_01"}}}' | python asha_mcp.py
```

### Resources

| URI                     | Content                             |
| ----------------------- | ----------------------------------- |
| `asha://memory/stats`   | Aggregate memory statistics         |
| `asha://memory/health`  | Health check results                |
| `asha://memory/profile` | Performance profile                 |
| `asha://memory/bloat`   | Bloat metrics (ephemeral, freelist) |
| `asha://skills`         | All registered skills               |

### Protocol

- Transport: stdio (newline-delimited JSON)
- Protocol: MCP 2025-03-26 / JSON-RPC 2.0
- Methods: `initialize`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `ping`
- Stdout: JSON-RPC responses. Stderr: server logs (redirect to discard or file)

## Deployment Checklist

- [ ] Single Python file: `asha_memory_v2.py` (copy to target)
- [ ] Skills registry: `ASHA_SKILLS_REGISTRY.txt` (bundled at project root; `load_skill_registry()` returns 0 when the file is absent)
- [ ] Inspectors: `humantools/asha_inspector.html`, `humantools/asha_graph.html` (optional, for debugging)
- [ ] Dependencies: `sqlite3`, `json`, `math`, `re`, `collections`, `pathlib` (all stdlib)
- [ ] Data dir: created automatically on first `AshaMemory(base_path=<dir>)`
- [ ] Config: auto-generated `config.json` in base_path

## Complete Example

```python
from asha_memory_v2 import AshaMemory

mem = AshaMemory(base_path="./deployment")

# Load skills
mem.load_skill_registry("./ASHA_SKILLS_REGISTRY.txt")

# Core memories
sam = mem.remember("Builds AI systems.", node_type="PERSON", label="SAM", trust=0.7)
pref = mem.remember("Comfortable with AI opacity.", node_type="PREFERENCE", label="attitude", trust=0.8)
mem.relate(sam, pref, "HAS_PREFERENCE")

# Semantic recall
r = mem.recall("opaque emergent behavior", mode="SEMANTIC")
print(f"Found {r.total_found}: {[n.label for n in r.nodes]}")

# Agent
mem.spawn_agent_memory("worker_01")
mem.agent_remember("worker_01", "Observed user prefers CLI over GUI.", "observation")
# agent_skills() returns AGENT_AUTO skills plus explicitly assigned ones:
mem.register_skill("PATTERN_RECOG", "Recognize recurring patterns", "ASSIGNABLE", "LEARNING")
mem.assign_skill("worker_01", "PATTERN_RECOG")

# Cross-agent
cross = mem.find_across_agents("CLI preference")
for r in cross:
    print(r["_agent_id"], r["content"], r.get("_similarity"))

# Export
mem.export_json("./export.json")

# Stats
print(mem.stats())
print(mem.health())
```
