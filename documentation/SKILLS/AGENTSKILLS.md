# AGENTSKILLS — Machine Operating Guide for AI Worker Agents

> **SYSTEM INSTRUCTION FOR WORKER / FIELD AI AGENTS**  
> This document defines your operational protocol for storing, recalling, and submitting knowledge using the **ASHA MEMORY SYSTEM v2**. Follow these exact specifications when executing tasks.

> **ROLE GATE — READ FIRST**
> If you are a **WORKER / FIELD agent** (you have an `agent_id`, you call `spawn_agent`/`agent_remember`/`find_across_agents`) → **use THIS file** and ignore `CORESKILLS.md`.
> If you are the **MAIN AI CORE / orchestrator** (persistent assistant, you manage `core.db` and review the queue) → **STOP — use `CORESKILLS.md` instead**. Never mix both files.

---

## 0. WHEN TO USE MEMORY AS MEMORY (for Workers)

**Your memory is `AGENT_NOTE` in shared `core.db` — isolated from core `recall` by default.**
- `agent_private` = your scratchpad (logs, trials, intermediate steps) — never auto-promoted, invisible to Main AI Core
- `review_ready` = verified finding ready for Core to `promote_to_core` (in-place, id/edges preserved)
- Core only sees `agent_review_queue` / `find_across_agents`; never raw `agent_private`

**STORE as `agent_private` when:**
- Raw observation, attempt, or working note produced while executing your task

**PROMOTE to `review_ready` when (use `agent_remember(..., attention_state="review_ready")` or `agent_set_attention`):**
- Finding is **verified** (reproduced, cross-checked via `recall` or `find_across_agents`), written as complete declarative sentence with exact entity names, and should survive beyond this task as `FACT`/`PREFERENCE`/`TOPIC`

**DO NOT STORE when:**
- Vague fragment (`"timeout error"`), duplicate of existing note (`find_across_agents` first), or ephemeral telemetry (`shared_lexicon.py:86` ephemeral labels)
- Speculative hypothesis → keep in chat context until verified

**HOW:**
- Before storing: `find_across_agents(query)` / `recall` to avoid duplicates; write `content` as `"PostgreSQL pool timeout 30s fixes drops under 100 concurrent connections"` not `"timeout error"`
- Self-heal first: `get_bloat_metrics` → `compact_ephemeral_logs` if `needs_vacuum` or `ephemeral._total>10`; respect `agent_max_notes:100` cap (oldest `agent_private` dropped)
- Full strategy → `documentation/GUIDE.md#memory-strategy`; graph linking after promotion is Core's job (`CORESKILLS.md`)

---

## 1. AGENT IDENTITY & ATTENTION SCOPE

### 1.1 Scope Isolation
- Your memories are stored as `AGENT_NOTE` nodes bound to your unique `agent_id`.
- **Default Behavior**: Raw agent notes remain outside normal core recall. This prevents execution noise (logs, trial attempts, temporary steps) from polluting the Main AI Core's context window.
- **Attention States**:
  - `agent_private` *(Default)*: Private scratchpad/working memory. Visible only to you and explicit agent queries.
  - `review_ready`: Promoted finding ready for Main AI Core evaluation via review queue.

---

## 2. DUAL INTERACTION PROTOCOL (MCP vs DIRECT PYTHON)

You interact with the memory system using **Method 1 (MCP)** by default. If MCP is unavailable or fails, use **Method 2 (Direct Python Import)**.

---

### METHOD 1: Model Context Protocol (MCP) over Stdio JSON-RPC

Send newline-delimited JSON-RPC 2.0 messages over stdio.

#### A. Initialize Agent Scope
```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"spawn_agent","arguments":{"agent_id":"WORKER_AGENT_ID"}}}
```

#### B. Check Assigned Skills
```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"agent_skills","arguments":{"agent_id":"WORKER_AGENT_ID"}}}
```

#### C. Store Working Note (Private)
```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"agent_remember","arguments":{"agent_id":"WORKER_AGENT_ID","content":"Discovered API endpoint /v2/items returns 404 on null query.","label":"api_finding","attention_state":"agent_private"}}}
```

#### D. Store Finding for Core Review
```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"agent_remember","arguments":{"agent_id":"WORKER_AGENT_ID","content":"Verified solution: Setting timeout=30 fixes connection drops in database pool.","label":"db_fix_verified","attention_state":"review_ready"}}}
```

#### E. Change Attention State of Existing Note
```json
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"agent_set_attention","arguments":{"agent_id":"WORKER_AGENT_ID","agent_node_id":"node_abc123","attention_state":"review_ready"}}}
```

#### F. Search Notes Across All Agents
```json
{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"find_across_agents","arguments":{"query":"database connection timeout","min_confidence":0.15,"bound":10}}}
```

---

### METHOD 2: Direct Python Import (Fallback execution)

Use this method when executing inside a Python runtime where the MCP server is not running.

```python
from asha_memory_v2 import AshaMemory

# Initialize memory instance (default base_path="./asha_memory")
mem = AshaMemory(base_path="./asha_memory")
agent_id = "WORKER_AGENT_ID"

# 1. Spawn / Initialize agent scope
mem.spawn_agent_memory(agent_id)

# 2. Check assigned skills — auto-granted AGENT_AUTO skills plus any
#    explicitly assigned via assign_skill()
skills = mem.agent_skills(agent_id)
# Returns a list of skill dicts: [{"name", "description", "node_id", "metadata"}, ...]

# 3. Store private working note (agent_private)
private_node_id = mem.agent_remember(
    agent_id=agent_id,
    content="Discovered API endpoint /v2/items returns 404 on null query.",
    label="api_finding",
    attention_state="agent_private"
)

# 4. Store finding for Core review (review_ready)
review_node_id = mem.agent_remember(
    agent_id=agent_id,
    content="Verified solution: Setting timeout=30 fixes connection drops in database pool.",
    label="db_fix_verified",
    attention_state="review_ready"
)

# 5. Change attention state of an existing note
mem.agent_set_attention(agent_id, private_node_id, attention_state="review_ready")

# 6. Search notes across agents (MCP param is query=, Python signature is topic=)
results = mem.find_across_agents(topic="database connection timeout", min_confidence=0.15, bound=10)
# Returns list of dicts with keys: _agent_id, node_id, content, label, _similarity
# Note: agent notes capped at agent_max_notes=100 (oldest agent_private dropped when full)
```

---

## 3. AGENT TOOL CHEAT SHEET

| Tool Name | MCP Call (param) | Python Direct Call | Required Args |
| :--- | :--- | :--- | :--- |
| **spawn_agent** | `{"name":"spawn_agent", "arguments":{...}}` | `mem.spawn_agent_memory(agent_id)` | `agent_id` |
| **agent_remember** | `{"name":"agent_remember", "arguments":{...}}` | `mem.agent_remember(...)` | `agent_id`, `content` (attention_state `agent_private`/`review_ready`, cap `agent_max_notes:100`) |
| **agent_skills** | `{"name":"agent_skills", "arguments":{...}}` | `mem.agent_skills(agent_id)` | `agent_id` |
| **find_across_agents** | `{"name":"find_across_agents", "arguments":{"query":...}}` | `mem.find_across_agents(topic=..., min_confidence?, bound?)` | `query` (MCP) / `topic` (Python) |
| **agent_set_attention** | `{"name":"agent_set_attention", "arguments":{...}}` | `mem.agent_set_attention(...)` | `agent_id`, `agent_node_id`, `attention_state` |

---

## 4. OPERATIONAL PROTOCOL & RULES

1. **Write Complete Declarative Sentences**:
   - Bad: `"timeout error"`
   - Good: `"Database connection drops under heavy load can be resolved by increasing pool timeout to 30 seconds."`
2. **Include Subject & Context Keywords**:
   - TF-IDF uses word frequency & inverse document frequency. Avoid generic pronouns ("it", "they", "this"); use exact entity names ("PostgreSQL", "AuthService", "user_id").
3. **When to Mark `review_ready`**:
    - Mark `review_ready` ONLY when a finding is verified and ready for the Main AI Core to adopt into permanent core memory.
4. **Self-heal before session**: run `get_bloat_metrics` → if `needs_vacuum` or ephemeral count >10, call `compact_ephemeral_logs` (P0-6 / P1-5). Ephemeral labels unified 10 (`shared_lexicon.py:86`).
5. **Recall `node_type` filter**: `recall` supports `node_type` post-filter (`asha_mcp.py:147`) — use to isolate `FACT`/`PREFERENCE` when searching core.
