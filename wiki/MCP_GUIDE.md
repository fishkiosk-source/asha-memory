# MCP Guide — Connecting AI Clients to Asha Memory v3

How an AI or MCP client talks to the memory over **stdio JSON-RPC 2.0**. This doc is the wire-level companion to [MCP_TOOLS](MCP_TOOLS.md) (tool catalog) and [ARCHITECTURE](ARCHITECTURE.md) (data flow).

## Transport

- **Channel:** `stdin` / `stdout`, one JSON object per line, UTF-8. `stderr` is log-only (never parsed).
- **Encoding:** JSON-RPC 2.0 with `jsonrpc: "2.0"`, `id` (request) or absent (notification), `method`, `params`, `result` / `error`.
- **Process:** run from the v3 root. Stdlib only, no HTTP, no pip.

```bash
python -m src.mcp.server --memory-path ./memory
# or default: <v3root>/memory (core.db + agents.db created on first run)
```

## Lifecycle

Every session follows this order. Clients must not call `tools/call` before `initialize`.

```
1. initialize  ──►  server returns protocolVersion + capabilities + serverInfo
2. notifications/initialized  (client → server, no id, no reply)
3. tools/list | resources/list | ping  (discovery)
4. tools/call  (repeated)
5. resources/read  (optional)
```

### 1. `initialize`

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "clientInfo":{"name":"my-agent","version":"0.1.0"},
  "protocolVersion":"2025-03-26"
}}
```

Response (`src/mcp/server.py:_initialize`):

```json
{
  "protocolVersion": "2025-03-26",
  "capabilities": {"tools": {}, "resources": {}},
  "serverInfo": {"name": "asha-memory", "version": "3.0.0"}
}
```

Side effect: `MCP: client=my-agent/0.1.0` on `stderr`.

### 2. `notifications/initialized` and `notifications/cancelled`

```json
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
```

No `id`, no response. Sets `server._initialized = true`. `notifications/cancelled` is accepted and ignored.

### 3. `tools/list`

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

Returns:

```json
{"tools": [{"name":"remember","description":"...","inputSchema":{...}}, ...]}
```

23 canonical tools plus deprecated `register_skill` (see [MCP_TOOLS](MCP_TOOLS.md)). Shapes come from `src/mcp/tools.py:definitions()`.

### 4. `tools/call`

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
  "name": "recall",
  "arguments": {"query": "Sam dark mode", "mode": "RELATED", "bound": 10}
}}
```

Success envelope:

```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\n  \"mode\": \"RELATED\", ...\n}"}]}}
```

The `text` field is a JSON string of the handler result (pretty-printed, `indent=2`). Some calls also carry `mailbox_notice` inside that JSON when mail is pending (see Mail below).

Error envelope (unknown tool or bad args):

```json
{"jsonrpc":"2.0","id":3,"error":{"code":-32002,"message":"Unknown tool: ... "}}
```

### 5. `resources/list` and `resources/read`

```json
{"jsonrpc":"2.0","id":4,"method":"resources/list","params":{}}
{"jsonrpc":"2.0","id":5,"method":"resources/read","params":{"uri":"asha://memory/stats"}}
```

Resources (`src/mcp/server.py:resource_definitions`):

| URI | Description |
|---|---|
| `asha://memory/stats` | `tools.cmd_stats` aggregate |
| `asha://memory/health` | `tools.cmd_health` per-DB schema check |
| `asha://memory/profile` | `tools.cmd_profile` counts + cache hits/misses |

Read returns:

```json
{"contents":[{"uri":"asha://memory/stats","mimeType":"application/json","text":"{\n  \"core\": {...}\n}"}]}
```

Deleted vs v2: `asha://skills` and the bloat resource are gone (brain owns bloat; integrity is `health`).

### 6. `ping`

```json
{"jsonrpc":"2.0","id":6,"method":"ping","params":{}}
```

Returns `{}`. Use for liveness; always succeeds even without init.

## `open_context` — what happens on startup

`src/mcp/tools.py:open_context(memory_dir?)` runs once at server start and is the only place that opens the two databases:

```python
def open_context(memory_dir=None) -> dict:
    mem = Path(memory_dir) if memory_dir else core_db_path().parent
    mem.mkdir(parents=True, exist_ok=True)
    core = core_connect(core_db_path(mem))   # PRAGMA journal_mode=WAL etc.
    core_migrate(core)                        # converge schema (creates tables if missing)
    agents = agent_store.connect(agents_db_path(mem))
    agent_store.ensure_schema(agents)
    core.commit(); agents.commit()
    return {"memory_dir": mem, "core": core, "agents": agents,
            "clock": InternalClock(enabled=True),
            "cache": LRUCache(capacity=50),
            "config": {}}
```

- Creates `memory/` if absent and creates `core.db` / `agents.db` on first run.
- Applies every pending migration idempotently (`src/core/migrate.py`, `src/agents/store.py:ensure_schema`).
- Initializes `InternalClock(enabled=True)` and `LRUCache(capacity=50)`.
- `close_context` closes both DBs on server exit. `dispatch()` commits both DBs on success and rolls back both on exception.

## Memory-path resolution

| Input | Resolution | Code |
|---|---|---|
| `--memory-path ./memory` | `Path("./memory").resolve()` | `src/mcp/server.py:main` |
| `--memory-path D:\data\asha` | that directory | same |
| omitted / `None` | `Path(__file__).parents[2] / "memory"` (i.e. `<v3root>/memory`) | `main() → tools.open_context` |
| `tools.open_context(None)` | `core_db_path().parent` | `src/core/store.py:memory_dir` |

Parent directories are created. `export` requires its parent dir to exist (otherwise `ValueError`). Paths for resources and file-serving are jailed (`dashboard/server.py:safe_join`).

## Dispatch and mailbox injection

Every `tools/call` goes through `src/mcp/tools.py:dispatch`:

1. Look up handler in `_HANDLERS`; unknown name raises `ValueError`.
2. Call handler; any `TypeError` / `KeyError` is re-raised as `ValueError("Invalid arguments...")`.
3. Derive the caller scope via `_scope_for` — mailbox send/read/ack use their `from`/`scope`, agent tools use `agent_id`, otherwise `core`.
4. Call `mailbox.check_inbox(scope)` **once** (for `core` also runs `_sync_review_reminders` to coalesce `[REVIEW READY]`/`[REVIEW QUEUE]`), attach `mailbox_notice` only when mail is pending (empty inbox → result object returned unchanged, no noise). `review_ready` mail is sticky — returned on every core call until `ack`/promote. Decoration failure never breaks the tool call.
5. Commit both DBs on success, rollback both on exception.

## Error codes

`src/mcp/server.py` uses these JSON-RPC codes:

| Code | Constant | When |
|---|---|---|
| `-32700` | `PARSE_ERROR` | stdin line is not valid JSON |
| `-32600` | `INVALID_REQUEST` | `method` missing/empty |
| `-32601` | `METHOD_NOT_FOUND` | unknown method string |
| `-32602` | `INVALID_PARAMS` | missing `name` in `tools/call` or `uri` in `resources/read` |
| `-32603` | `INTERNAL_ERROR` | unhandled exception in `handle_line` |
| `-32001` | `TOOL_NOT_FOUND` | reserved (not currently emitted; unknown tool maps to -32002) |
| `-32002` | `TOOL_EXECUTION_ERROR` | handler raised `ValueError` (unknown tool, invalid args, `unknown_agent` hint, `invalid scope`, `agent -> broadcast rejected`, etc.) |
| `-32003` | `RESOURCE_NOT_FOUND` | `resources/read` URI not in the three known URIs |

Common `TOOL_EXECUTION_ERROR` messages:

- `Unknown tool: <name>`
- `Invalid arguments for <tool>: ...`
- `unknown_agent, hint: call agent_ensure first: '<id>'`
- `invalid scope '...' as sender|recipient`
- `mailbox.send(): agent -> broadcast rejected (hint: send to core)` / `only core may broadcast (...)`
- `Invalid node_type: ...` / `Invalid edge_type: ...` / `Invalid attention_state`
- `export(): parent dir does not exist: ...`

## Example session

Below is a minimal session (initialize → tools/list → recall → remember). Each line is one JSON object on stdio.

**Client → Server: initialize**

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"example","version":"0.1"}}}
```

**Server → Client**

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{},"resources":{}},"serverInfo":{"name":"asha-memory","version":"3.0.0"}}}
```

**Client → Server: initialized notification**

```json
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
```

**Client → Server: tools/list**

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

**Server → Client (truncated)**

```json
{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"remember","description":"Store a memory node in core.","inputSchema":{"type":"object","properties":{"content":{"type":"string","description":"The memory content text"},"node_type":{"type":"string","description":"Node type","enum":["PERSON","TOPIC","EVENT","FACT","PREFERENCE","BOUNDARY","AFFECT","AGENT_NOTE","CORE_REF","SKILL"]},"label":{"type":"string","description":"Short label (optional)"},"source":{"type":"string","description":"Origin: USER|CORE"},"trust":{"type":"number","description":"Confidence 0.0-1.0"},"importance":{"type":"number","description":"Importance 0.0-1.0"}},"required":["content","node_type"]}}, {"name":"recall","description":"Retrieve memories. DSL strings (FIND ...) auto-detected.","inputSchema":{"type":"object","properties":{"query":{"type":"string","description":"Search text, node label, node_id, or FIND ... DSL"},"mode":{"type":"string","description":"Recall mode","enum":["RELATED","WHO_IS","WHAT_ABOUT","SEMANTIC","PATH","CLUSTER","TIMELINE","RECENT","PRUNE"]},"bound":{"type":"integer","description":"Max results (default 10)"},"limit":{"type":"integer","description":"Alias for bound"},"offset":{"type":"integer","description":"Pagination offset"},"include_agent_notes":{"type":"boolean","description":"Include raw agent notes (default false)"},"node_type":{"type":"string","description":"Optional post-filter","enum":["PERSON","TOPIC","EVENT","FACT","PREFERENCE","BOUNDARY","AFFECT","AGENT_NOTE","CORE_REF","SKILL"]}},"required":["query"]}}]}}
```

**Client → Server: recall**

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"recall","arguments":{"query":"Sam dark mode","mode":"RELATED","bound":5}}}
```

**Server → Client**

```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\n  \"mode\": \"RELATED\",\n  \"total_found\": 2,\n  \"bound_applied\": false,\n  \"clock\": {\"epoch\": 1720000000, \"iso\": \"2026-09-05T12:00:00\", \"date\": \"2026-09-05\", \"time\": \"12:00:00\", \"weekday\": \"Saturday\"},\n  \"nodes\": [{\"node_id\": \"node_abc123\", \"node_type\": \"PREFERENCE\", \"label\": \"Sam UI\", \"content\": \"Sam prefers dark mode\", \"trust_level\": 0.9, \"importance\": 0.8, \"similarity\": null, \"age\": {\"added\": \"2 days ago\", \"last_checked\": \"just now\", \"stale\": false}}]\n}"}]}}
```

**Client → Server: remember**

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"remember","arguments":{"content":"Sam prefers dark mode for late-night work","node_type":"PREFERENCE","label":"Sam UI","trust":0.9,"importance":0.8}}}
```

**Server → Client**

```json
{"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"{\n  \"node_id\": \"node_a1b2c3d4e5f6a7b8\"\n}"}]}}
```

If the caller's inbox had pending mail, the `text` JSON would also contain a `mailbox_notice` string. Normal mail:

```json
{
  "node_id": "node_a1b2c3d4e5f6a7b8",
  "mailbox_notice": "Hey Core -- you have 1 message(s) from agent:scout -- use mailbox.read / mailbox.ack to handle.\n  - from agent:scout at 2026-09-05 12:00 -- \"please promote node_xyz: nightly finding...\""
}
```

Sticky review reminder (when any `review_ready` node exists — injected on **every** core call until ack/promote):

```json
{
  "node_id": "node_a1b2c3d4e5f6a7b8",
  "mailbox_notice": "[REVIEW READY] NODE node_abc (\"My Label\") from agent:agent_scout at 2026-09-06 11:27 -- use review_queue + promote_to_core to graduate. Body: \"REVIEW_READY: NODE ...\""
}
```
```json
{
  "mailbox_notice": "[REVIEW QUEUE] You have 3 nodes ready for review -- please review via review_queue / promote_to_core (use mailbox.ack after).\n  Detail: \"REVIEW_READY: You have 3 nodes...\"\n  - NODE node_a\n  - NODE node_b\n  - NODE node_c"
}
```

## Notes

- `bound` and `limit` are aliases on recall-like tools; explicit `limit` wins even when `0`. Defaults: `recall` 10, `agent_recall` 10, `find_across_agents` 10, `review_queue` 20.
- `recall` content is truncated to 200 chars; `get_node` returns full content.
- Notifications (`notifications/initialized`, `notifications/cancelled`) never produce a response, even if an `id` is present.
- The server is single-threaded stdio; `brain` overlap uses the inter-process `memory/.lock` (Phase 6).
