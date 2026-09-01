# Internal Clock Module — Plan

> **Status**: **Implemented** (`internal_clock.py` + `asha_memory_v2.py` hooks
> 
> + `test_internal_clock.py`, all tests green). Design decision: **pure module,
>   zero MCP changes**.
>   Time context flows from `asha_memory_v2.py` into node metadata; the MCP server
>   stays exactly as it is today.

## Problem

AI entities talking to the memory system over MCP have no sense of time. The
MCP `cmd_recall` handler serializes only `node_id`, `node_type`, `label`,
`content`, `trust_level`, `importance`, `similarity` per node — no timestamps,
no age, no freshness. An AI cannot tell whether a returned memory was written
an hour ago or a year ago, and has no idea what "today" even is.

## Goal

Give the AI temporal context through the memory system itself, without
touching `asha_mcp.py`:

1. A clock module that answers "what time is it" from the memory graph.
2. Per-node mini summaries: *"this node was added 2 weeks ago, last time
   checked 3 days ago"*.
3. A daily "today" context node so ordinary `recall` queries can surface
   date/time and recent memory activity.

## What Already Exists (no schema changes needed)

The schema already stores every timestamp the clock needs:

| Source                   | Column / table    | Meaning                                                   |
| ------------------------ | ----------------- | --------------------------------------------------------- |
| `nodes.created_at`       | epoch int         | When the node was added                                   |
| `nodes.updated_at`       | epoch int         | Last write or access (`_bump_access` sets it to `_now()`) |
| `access_log.accessed_at` | epoch int per row | Full access history — exact "last time checked"           |
| `memory_layers.layer`    | text              | Current tier (working / short_term / long_term / archive) |
| `query_log.queried_at`   | epoch int         | When queries ran — basis for "activity today"             |

## Design

### 1. New file: `internal_clock.py` (stdlib only)

A standalone `InternalClock` class matching the project's pure-stdlib
philosophy (`sqlite3`, `datetime`, `math` — no external deps, no AI calls).

| Component                    | Behavior                                                                                                             |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `now()`                      | Single source of truth for current time: epoch + ISO string                                                          |
| `humanize(epoch)`            | `"just now"`, `"3 minutes ago"`, `"2 hours ago"`, `"3 days ago"`, `"2 weeks ago"`, `"5 months ago"`, `"2 years ago"` |
| `summarize_node(node)`       | `{"added": "2 weeks ago", "last_checked": "3 days ago", "access_count": 12, "layer": "long_term", "stale": true}`    |
| `today_summary(mem)`         | Date, weekday, time + today's memory activity (nodes added today, accessed today, queries today)                     |
| `graph_activity(mem, since)` | Mini report of what changed in the last 24h / 7d                                                                     |

**Key subtlety — "last checked":** `_bump_access` sets `updated_at = _now()`
on every recall, so `updated_at` after a query always says "just now". The
clock must read the *previous* access from `access_log`:

```sql
SELECT MAX(accessed_at) FROM access_log
WHERE node_id = ? AND accessed_at < ?
```

(second parameter = the current query's start time), so "last checked
3 days ago" stays honest even though this query also touches the node.

### 2. Integration in `asha_memory_v2.py` (small, opt-in)

- `DEFAULT_CONFIG` gains `"internal_clock": true` (overridable in `config.json`).

- `AshaMemory.__init__` instantiates `self.clock = InternalClock(...)` when enabled.

- `recall()`: after the core-visibility filter, attach
  `node.metadata["_clock"] = self.clock.summarize_node(...)` to each returned
  node. Metadata enrichment automatically reaches every `.to_dict()` consumer:
  `get_person`, `get_topic`, `agent_digest`, direct Python usage.

- New method `clock_tick()`: upserts a **today context node** (label `TODAY`,
  type `EVENT`, source `CLOCK`):
  
  > "Today is Tuesday 18 August 2026, 14:37. 3 nodes added today, 12 accessed,
  > 45 queries."
  
  A daily check in `__init__` (or first `recall` of the day) keeps this node
  fresh, so the AI can literally `recall("what is today's date")` and the
  memory graph answers from its own data.

### 3. MCP stays untouched (option A)

- `cmd_get_node` already returns `metadata` → `_clock` summary is visible
  when the AI drills into a node (asha_mcp.py, `cmd_get_node`).
- The `TODAY` context node surfaces date/time + activity through normal
  `recall` / `query_dsl` / `SEMANTIC` searches.
- **Update (implemented):** `cmd_recall` and `cmd_query_dsl` now serialize a
  top-level `clock` snapshot plus a per-node `age` summary, and
  `cmd_find_across_agents` adds `age` per result — so freshness is visible
  inline, not only via `get_node`. The AI can still drill into
  `get_node(node_id)` for the full `_clock` metadata object.

## Files

| File                                   | Action                                                                                 |
| -------------------------------------- | -------------------------------------------------------------------------------------- |
| `internal_clock.py`                    | **New** — `InternalClock` class, stdlib only                                           |
| `asha_memory_v2.py`                    | **Edit** — config key, `self.clock`, `_clock` enrichment in `recall()`, `clock_tick()` |
| `asha_mcp.py`                          | **None** — unchanged by design                                                         |
| `test_internal_clock.py`               | **New** — unittest suite, temp dirs (repo convention)                                  |
| `documentation/docs/INTERNAL_CLOCK.md` | **This file**                                                                          |
| `documentation/GUIDE.md`               | **Edit** — note the `get_node` workflow for freshness                                  |

## Testing

New `test_internal_clock.py` (unittest, all in temp dirs like
`test_v2_system.py` / `brain/test_brain.py`):

1. `humanize` correctness for seconds / minutes / hours / days / weeks / months / years.
2. `summarize_node` reads `created_at`, `access_log`, `memory_layers` correctly.
3. "Last checked" comes from the *previous* access, not the current bump.
4. `recall()` attaches `_clock` to node metadata; disabled flag removes it.
5. `clock_tick()` creates a `TODAY` node on first run, updates in place after.
6. `today_summary` reports nodes added/accessed and queries today.
7. MCP `get_node` response contains the `_clock` metadata (no MCP code change).

## Milestones

1. ~~`internal_clock.py` standalone (humanize + summarize + today_summary).~~ **Done**
2. ~~Hook into `AshaMemory` (config, enrichment, `clock_tick`).~~ **Done**
3. ~~Tests~~ — **Done**: 16 tests in `test_internal_clock.py`; existing
   `test_v2_system.py` (7) and `brain/test_brain.py` (19) all green.
4. ~~Docs update (`GUIDE.md` workflow note).~~ **Done**

## Implementation Notes

- `access_count` in `_clock` is the live post-bump count (queried from `nodes`
  after the access bump), while `last_checked` is the pre-query access — the
  summary is truthful from both directions.
- The `TODAY` node skips `_auto_link` (guarded by `metadata["clock_node"]`) so
  daily refresh does not spray `RELATES_TO` edges across the graph.
- Cache hits refresh `_clock` before returning, so cached recalls never serve
  stale age summaries.
- A user-owned node labeled `TODAY` (without `clock_node` metadata) is left
  untouched by `clock_tick()`.
- `brain/test_brain.py::test_14` was updated: the core-visible `TODAY` node is
  counted in core health metrics (2 core nodes instead of 1).