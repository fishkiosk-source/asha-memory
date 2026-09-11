# Clock

> Temporal context for the memory graph. Pure stdlib (`sqlite3`, `datetime`, `time`) — no AI calls, no external dependencies.

`src/core/clock.py:1` — **v2 look-up only** — verbatim copy of `v2/internal_clock.py` (only header `V3 PROVENANCE` added). V3 note: `clock_tick` writes `TODAY` only to `core.db`; agents and mailbox use stateless `now()`/`humanize()`; `today_summary()`/`graph_activity()` take a `conn` parameterized per-DB in Phase 2+.

---

## Overview

| Export | Line | Purpose |
|---|---|---|
| `InternalClock` | `src/core/clock.py:38` | Temporal provider — the only clock in the system |
| `now` | `src/core/clock.py:49` | Snapshot: epoch + ISO + date + time + weekday |
| `humanize` | `src/core/clock.py:66` | Relative phrase: "just now" / "3 days ago" |
| `summarize_node` | `src/core/clock.py:103` | Per-node temporal card (added / last_checked / stale) |
| `last_accessed_before` | `src/core/clock.py:156` | Previous `access_log` touch before current query |
| `today_summary` | `src/core/clock.py:167` | Today's counts (added / accessed / edges / queries) |
| `graph_activity` | `src/core/clock.py:183` | Last N hours activity |
| `build_tick_content` | `src/core/clock.py:214` | Content for the daily `TODAY` context node |

DB methods need `conn.row_factory = sqlite3.Row` (indexed access `row["la"]`). All epochs are seconds (float or int), local time via `datetime.fromtimestamp`.

---

## `InternalClock`

`src/core/clock.py:38`:

```python
class InternalClock:
    def __init__(self, enabled: bool = True, stale_after_days: int = 7):
        self.enabled = enabled
        self.stale_after_days = max(1, stale_after_days)
```

| Param | Default | Effect |
|---|---|---|
| `enabled` | `True` | Feature flag — callers check it before attaching `_clock` metadata to recall results |
| `stale_after_days` | `7` | Clamped `max(1, ...)` — threshold for `stale` in `summarize_node`. Days → seconds `* 86400` |

No DB handle is stored. The same instance is shared between core and agent paths; DB-aware methods receive `conn` per call.

---

## Time primitives

### `now() -> Dict[str, Any]`

`src/core/clock.py:49`:

```python
def now(self) -> Dict[str, Any]:
    now = time.time()
    dt = datetime.fromtimestamp(now)
    return {"epoch": int(now), "iso": dt.isoformat(timespec="seconds"),
            "date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"),
            "weekday": dt.strftime("%A")}
```

Returns local-time snapshot. `epoch` is `int(time.time())`, `iso` is `YYYY-MM-DDTHH:MM:SS`.

Example output:

```json
{"epoch": 1714824000, "iso": "2024-05-04T14:30:00", "date": "2024-05-04", "time": "14:30:00", "weekday": "Saturday"}
```

Stateless — no DB. Used by agents, mailbox, and `today_summary` (which extends it).

### `_day_start_epoch(now_epoch?) -> float`

`src/core/clock.py:61`:

```python
def _day_start_epoch(self, now_epoch: Optional[float] = None) -> float:
    now = now_epoch if now_epoch is not None else time.time()
    dt = datetime.fromtimestamp(now)
    return datetime(dt.year, dt.month, dt.day).timestamp()
```

Midnight today in local time as epoch float. Parameterized for testing; default is `time.time()`. Used by `today_summary` to bound `WHERE created_at >= ?` queries.

### `humanize(epoch, now_epoch?) -> str`

`src/core/clock.py:66`:

```python
def humanize(self, epoch: float, now_epoch: Optional[float] = None) -> str:
```

Relative phrase for an epoch. `now_epoch` overrides `time.time()` for deterministic tests.

| Diff (`now - epoch`) | Returns | Notes |
|---|---|---|
| `< 0` | `"in the future"` | Clock skew guard |
| `< 60s` | `"just now"` | |
| `< 60 min` | `"N minute(s) ago"` | `int(minutes)` |
| `< 24 h` | `"N hour(s) ago"` | `int(hours)` |
| `< 7 d` | `"N day(s) ago"` | `int(days)` |
| `< 4.345 weeks` | `"N week(s) ago"` | `round(weeks)` |
| `< 12 months` | `"N month(s) ago"` | `round(months)`, month = `30.44d` |
| `>= 12 months` | `"N year(s) ago"` | `round(years)`, year = `365.25d` |

Pluralization via `_unit` at `src/core/clock.py:92`:

```python
@staticmethod
def _unit(count: int, unit: str) -> str:
    if count <= 0:
        count = 1
    label = unit if count == 1 else unit + "s"
    return f"{count} {label} ago"
```

Examples (with `now_epoch` fixed):

```python
clock = InternalClock()
clock.humanize(now_epoch - 45)              # "just now"
clock.humanize(now_epoch - 180)             # "3 minutes ago"
clock.humanize(now_epoch - 5*3600)          # "5 hours ago"
clock.humanize(now_epoch - 3*86400)         # "3 days ago"
clock.humanize(now_epoch - 14*86400)        # "2 weeks ago"
clock.humanize(now_epoch - 60*86400)        # "2 months ago"
clock.humanize(now_epoch - 400*86400)       # "1 year ago"
clock.humanize(now_epoch + 100)             # "in the future"
```

Agents use this directly to render `last_checked` without a DB call.

---

## Node summaries

### `summarize_node(node, last_accessed?, access_count?, now_epoch?) -> Dict`

`src/core/clock.py:103`:

```python
def summarize_node(self, node, last_accessed: Optional[float] = None,
                   access_count: Optional[int] = None,
                   now_epoch: Optional[float] = None) -> Dict[str, Any]:
```

Per-node temporal card. Accepts `MemoryNode`, `dict`, or any object with `created_at`/`updated_at`/`access_count` attributes (via `_fields` at `src/core/clock.py:142`).

| Param | Source | Fallback |
|---|---|---|
| `last_accessed` | Previous `access_log` timestamp **before** current query (from `last_accessed_before`) | `updated_at` from node |
| `access_count` | Live post-bump count | `node.access_count` (may predate current access) |
| `now_epoch` | Explicit now for testing | `time.time()` |

Resolution:

```python
fields = self._fields(node)
created = fields.get("created_at")
updated = fields.get("updated_at")
layer = fields.get("layer") or "working"
last = last_accessed if last_accessed is not None else updated
```

Returns:

```python
{
    "added":            humanize(created, now),  # or None
    "added_at":         created,                 # raw epoch or None
    "last_checked":     humanize(last, now),     # or None
    "last_checked_at":  last,                    # raw epoch or None
    "access_count":     int,
    "layer":            str,                     # "working" default
    "stale":            bool,                    # see below
}
```

Example:

```python
node = {"created_at": now-10*86400, "updated_at": now-2*86400, "layer": "short_term", "access_count": 4}
clock.summarize_node(node, last_accessed=now-2*86400, access_count=5, now_epoch=now)
# → {"added": "1 week ago", "added_at": ..., "last_checked": "2 days ago",
#    "last_checked_at": ..., "access_count": 5, "layer": "short_term", "stale": False}
```

Why `last_accessed_before` matters: `AshaMemory._bump_access` sets `updated_at = now` on every recall. Reading `updated_at` directly would make every returned node claim `"last checked: just now"`. The clock reads `access_log` **before** the current bump, so the phrase reflects the *previous* visit.

### Stale logic

`src/core/clock.py:132`:

```python
def _is_stale(self, created: Optional[float], last: Optional[float], now: float) -> bool:
    threshold = self.stale_after_days * 86400.0
    if last is not None:
        return (now - last) > threshold
    if created is not None:
        return (now - created) > threshold
    return False
```

| Inputs | Stale when |
|---|---|
| `last` is not None | `now - last > stale_after_days * 86400` |
| `last` is None, `created` is not None | `now - created > threshold` |
| Both None | `False` |

Default `stale_after_days=7` → threshold `604800s`. `summarize_node` exposes this as `stale: bool` alongside `last_checked`; brain's `tiers`/`age_prune` use the same notion for layer demotion and triple-gate pruning.

### `_fields(node) -> Dict`

`src/core/clock.py:142`:

```python
@staticmethod
def _fields(node) -> Dict[str, Any]:
    if isinstance(node, dict): return node
    if hasattr(node, "to_dict"): return node.to_dict()
    keys = ("node_id","node_type","label","content","source","trust_level",
            "created_at","updated_at","access_count","importance","checksum","metadata","layer")
    return {k: getattr(node, k, None) for k in keys}
```

Polymorphic accessor so `summarize_node` works with dicts, `MemoryNode`, or row-like objects without importing the node class.

---

## Graph activity (DB-aware)

All methods below take `conn: sqlite3.Connection` parameterized per-DB (Phase 2+). Caller must set `conn.row_factory = sqlite3.Row`.

Helper at `src/core/clock.py:203`:

```python
@staticmethod
def _count(conn, sql, params=()) -> int:
    try: return conn.execute(sql, params).fetchone()[0]
    except sqlite3.OperationalError: return 0
```

`OperationalError` → `0` (table missing during early migration / empty DB).

### `last_accessed_before(conn, node_id, before_epoch) -> Optional[float]`

`src/core/clock.py:156`:

```python
def last_accessed_before(self, conn, node_id: str, before_epoch: float) -> Optional[float]:
    row = conn.execute(
        "SELECT MAX(accessed_at) AS la FROM access_log WHERE node_id = ? AND accessed_at < ?",
        (node_id, before_epoch)).fetchone()
    if row and row["la"] is not None:
        return float(row["la"])
    return None
```

Most recent `access_log` timestamp strictly `< before_epoch`. Pass the recall's start epoch so the current bump is excluded. Returns `None` if never accessed before (caller falls back to `updated_at`).

### `today_summary(conn) -> Dict[str, Any]`

`src/core/clock.py:167`:

```python
def today_summary(self, conn) -> Dict[str, Any]:
    day_start = self._day_start_epoch()
    summary = self.now()
    summary["day_start_epoch"] = int(day_start)
    summary["nodes_added_today"] = self._count(conn, "SELECT COUNT(*) FROM nodes WHERE created_at >= ?", (day_start,))
    summary["nodes_accessed_today"] = self._count(conn, "SELECT COUNT(DISTINCT node_id) FROM access_log WHERE accessed_at >= ?", (day_start,))
    summary["edges_created_today"] = self._count(conn, "SELECT COUNT(*) FROM edges WHERE created_at >= ?", (day_start,))
    summary["queries_today"] = self._count(conn, "SELECT COUNT(*) FROM query_log WHERE queried_at >= ?", (day_start,))
    return summary
```

Extends `now()` with today's activity counts. `day_start` is midnight local today.

Example return:

```json
{
  "epoch": 1714824000, "iso": "2024-05-04T14:30:00", "date": "2024-05-04",
  "time": "14:30:00", "weekday": "Saturday",
  "day_start_epoch": 1714780800,
  "nodes_added_today": 12,
  "nodes_accessed_today": 34,
  "edges_created_today": 5,
  "queries_today": 27
}
```

Feeds `build_tick_content` and the `TODAY` context node. Phase 2+ signature is parameterized — callers pass `core` or `agents` conn.

### `graph_activity(conn, hours=24) -> Dict[str, Any]`

`src/core/clock.py:183`:

```python
def graph_activity(self, conn, hours: int = 24) -> Dict[str, Any]:
    since = time.time() - hours * 3600.0
    return {
        "since_hours": hours, "since_epoch": int(since),
        "nodes_created":  self._count(conn, "SELECT COUNT(*) FROM nodes WHERE created_at >= ?", (since,)),
        "nodes_updated":  self._count(conn, "SELECT COUNT(*) FROM nodes WHERE updated_at >= ?", (since,)),
        "nodes_accessed": self._count(conn, "SELECT COUNT(DISTINCT node_id) FROM access_log WHERE accessed_at >= ?", (since,)),
        "edges_created":  self._count(conn, "SELECT COUNT(*) FROM edges WHERE created_at >= ?", (since,)),
        "queries_run":    self._count(conn, "SELECT COUNT(*) FROM query_log WHERE queried_at >= ?", (since,)),
    }
```

Sliding window over last N hours (default 24). `nodes_updated` counts `updated_at` bumps (includes recall bumps — interpret alongside `nodes_accessed`).

Example (`hours=24`):

```json
{"since_hours": 24, "since_epoch": 1714737600, "nodes_created": 8, "nodes_updated": 42, "nodes_accessed": 30, "edges_created": 3, "queries_run": 19}
```

### `build_tick_content(summary) -> str`

`src/core/clock.py:214`:

```python
def build_tick_content(self, summary: Dict[str, Any]) -> str:
    return (
        f"Today is {summary['weekday']} {summary['date']} at {summary['time']} "
        f"(epoch {summary['epoch']}). Memory activity today: "
        f"{summary['nodes_added_today']} nodes added, "
        f"{summary['nodes_accessed_today']} accessed, "
        f"{summary['edges_created_today']} edges created, "
        f"{summary['queries_today']} queries run."
    )
```

Human-readable content for the daily `TODAY` context node. Takes the dict returned by `today_summary`.

Example output:

```
Today is Saturday 2024-05-04 at 14:30:00 (epoch 1714824000). Memory activity today: 12 nodes added, 34 accessed, 5 edges created, 27 queries run.
```

This string is stored as the `TODAY` node's `content` (label `TODAY`, type `CONTEXT`) so recall can inject temporal grounding into the LLM context.

---

## Core vs agents usage

| Caller | Methods | DB? | Notes |
|---|---|---|---|
| **Core** (`src/core/store.py` `clock_tick`) | `today_summary(conn)` → `build_tick_content(summary)` | Writes `TODAY` node **only to `core.db`** | Upserts the daily context node; no agent DB touch |
| **Agents** (`src/agents/store.py`, mailbox) | `now()`, `humanize(epoch, now_epoch?)` | Stateless, no `conn` | Renders timestamps and `last_checked` phrases locally; `summarize_node` with `last_accessed_before(agents_conn, ...)` when an agent conn is available |
| **Brain / dashboard** | `today_summary(conn)`, `graph_activity(conn)` | Parameterized `conn` (core or agents) | Health/stats panels call with either DB; `OperationalError` safe |

Rule: `clock_tick` never opens `agents.db`. `DirectMemory` `src/direct/provider.py:1` shares the same `InternalClock` instance as MCP (`ctx["clock"]`) and injects the same `clock` top-level + `age` per node. If today's summary is needed for agents, call `InternalClock().today_summary(agents_conn)` explicitly with that connection. This preserves the two-WAL invariant — one writer per DB, no cross-DB transaction.

---

## Provenance

- **v2 look-up only**: `clock.py` is `v2/internal_clock.py` verbatim except the `V3 PROVENANCE` header. No logic changed in v3.
- Import: `from src.core.clock import InternalClock` (`InternalClock` is the only public export).
- Keep `src/core/clock.py` in sync with `v2/internal_clock.py` for recall-parity checks; changes must update both and the 121-test harness.

