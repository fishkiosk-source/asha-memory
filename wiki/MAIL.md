# Mail — Mailbox + Auto-Inject (Asha Memory v3)

`src/agents/mailbox.py` owning tables in `agents.db`. `src/mcp/tools.py` `dispatch` is the **only** caller of `check_inbox` (MCP-envelope-only, C5). No external bus, no polling loop, no MCP protocol change — delivery rides on tool-response decoration.

---

## 1. Scopes

| Scope | Meaning | Aliases |
|---|---|---|
| `core` | The core graph / MCP server identity | — |
| `agent:<id>` | One agent shard in `agents.db` (`agent_id` canonical; aliases resolve via `agent_aliases`) | `resolve_agent` canonicalizes |
| `broadcast` | **Recipient-only pseudo-scope.** Only `core` may send to it. Expands at send time. | — |
| `user` | The human operator (first-class scope; dashboard Mail tab + `agents.db` row) | — |

All agent references are canonicalized: `agent:foo` is resolved through `src.agents.store.resolve_agent` (checks `agents.agent_id`, then `agent_aliases.alias`). Unknown `agent:<id>` on either side -> `unknown_agent` error (`hint: agent_ensure first`).

---

## 2. Allowed flows

| From -> To | Allowed | Notes |
|---|---|---|
| `core` -> `agent:<id>` | Yes | Unicast, 1 receipt |
| `agent:<id>` -> `core` | Yes | Unicast, 1 receipt |
| `agent:<id>` -> `agent:<id>` | Yes | Unicast, direct agent-to-agent (locked rev5 §2.8). |
| `core` -> `broadcast` | Yes | Fans out to N receipts (every `agent` + `core` at send time). |
| `user` -> `core` | Yes | Operator compose-as-user (Mail tab). |
| `user` -> `agent:<id>` | Yes | Operator compose-as-user to any agent. |
| `core` -> `user` | Yes | Core reply to operator inbox. |
| `agent:<id>` -> `user` | Yes | Agent reply to operator inbox. |
| `agent:<id>` -> `broadcast` | **No** | Rejected with `hint: send to core` (spam guard, `mailbox.py:99` `AGENT_BROADCAST_HINT`). |
| `*` -> `broadcast` where `from != core` | No | Only `core` may broadcast (`mailbox.py:102`). |
| `*` -> unknown `agent:<id>` | No | `unknown_agent, hint: agent_ensure first` |

---

## 3. Schema — DDL owned by `src/agents/mailbox.py`

Created by `src.agents.store.ensure_schema` (single entry point). `agents.db` only (both sides can reach it without agent writes to `core.db`).

```sql
-- src/agents/mailbox.py:32 MAILBOX_DDL
CREATE TABLE IF NOT EXISTS mailbox (
    msg_id     TEXT PRIMARY KEY,
    from_scope TEXT NOT NULL,
    to_scope   TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    read_at    INTEGER,
    acked_at   INTEGER,
    metadata   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS mailbox_receipts (
    msg_id   TEXT NOT NULL REFERENCES mailbox(msg_id) ON DELETE CASCADE,
    scope    TEXT NOT NULL,
    noted_at INTEGER,
    read_at  INTEGER,
    acked_at INTEGER,
    PRIMARY KEY (msg_id, scope)
);

CREATE INDEX IF NOT EXISTS idx_receipts_scope_noted ON mailbox_receipts(scope, noted_at);
```

* `mailbox` is the message. `read_at`/`acked_at` on this row are **unicast-only mirrors** for cheap dashboard counts (see `_mirror_unicast`). Broadcast messages never update `mailbox.read_at/acked_at`; per-recipient state lives in `mailbox_receipts`.
* `mailbox_receipts` is per-recipient state. `PRIMARY KEY (msg_id, scope)` — a shared `noted_at` would re-notify broadcast forever (C6).
* `metadata` on `mailbox` is a JSON bag — `kind=review_ready` uses it for sticky review reminders (`{"kind":"review_ready","node_id":"...","agent_id":"...","count":N,"node_ids":[...]}`) created by `agent_set_attention` and `_sync_review_reminders`.

---

## 4. Lifecycle — `pending -> noted -> read -> acked`

```
send                    check_inbox (auto)           read (explicit)           ack (explicit)
  |                              |                           |                     |
  v                              v                           v                     v
mailbox + receipts       receipts.noted_at=now        receipts.read_at=now    receipts.acked_at=now
(scope stays pending     (also read_at on unicast     (+ mailbox.read_at      (+ mailbox.acked_at
 until noted)             mirror via _mirror_unicast)   mirror on unicast)      mirror on unicast)
         pending  ──────────> noted ──────────> read ──────────> acked
         (r.noted_at IS NULL) (r.noted_at NOT NULL, r.read_at IS NULL, r.acked_at IS NULL)
```

* **Send** — `mailbox.send(conn, from_scope, to_scope, body, metadata?)` validates scopes (canonicalizes agent aliases), inserts one `mailbox` row, inserts receipts (see §5), commits on `dispatch` success. Empty `body` is rejected.
* **Noted (auto)** — the MCP dispatch wrapper calls `mailbox.check_inbox(conn, scope)` once per tool call. It selects `WHERE r.scope=? AND r.noted_at IS NULL`, returns `[{msg_id, from_scope, body, created_at, metadata}]` sorted by `created_at`, and marks `noted_at=now()` for those receipts in the same DB transaction (persisted on `dispatch` commit; rolled back on exception).
* **Read (explicit)** — `mailbox.read(conn, scope, state="pending"|"noted"|"all", limit=50, offset=0, mark_read=True)` (dashboard uses `mark_read=False` for traffic view). State filters: `pending` = `r.acked_at IS NULL`, `noted` = `r.noted_at IS NOT NULL AND r.read_at IS NULL AND r.acked_at IS NULL`, `all` = no ack filter. `scope="all"` is admin view (no `WHERE r.scope` filter). When `mark_read` and `scope != "all"`, sets `receipts.read_at = COALESCE(read_at, now)` and mirrors to `mailbox.read_at` on unicast via `_mirror_unicast`.
* **Acked (explicit)** — `mailbox.ack(conn, scope, msg_ids=None)` — when `msg_ids` is `None`, acks every unacked receipt for that scope (`WHERE scope=? AND acked_at IS NULL`). Otherwise acks the listed IDs (`acked_at = COALESCE(acked_at, now), read_at = COALESCE(read_at, now)`) and mirrors `acked_at` on unicast. Returns `{"acked": n}` (rowcount).

Dashboard and MCP alias states: `read` `state=pending` means "not acked"; `state=noted` means "seen but not read/acked"; `state=all` means all. See `dashboard/server.py:978` `_api_mailbox_read` which maps `?state=pending|noted|all`.

---

## 5. Broadcast fan-out

`send(..., to_scope="broadcast")` is `core`-only:

```python
# mailbox.py:112
if to == "broadcast":
    recipients = ["core"] + [f"agent:{a['agent_id']}" for a in agent_list(conn)]
else:
    recipients = [to]
conn.executemany("INSERT INTO mailbox_receipts (msg_id, scope) VALUES (?, ?)",
                 [(mid, r) for r in recipients])
```

* One receipt per registered agent at send time + one for `core`. `user` sees broadcast in the dashboard Mail tab traffic view (`scope=all`) without a receipt — it is not a recipient.
* Membership is fixed at send time; agents registered later do not receive old broadcasts.
* `mailbox.noted_at/read_at/acked_at` columns are not used for broadcast (per-recipient state counts).

---

## 6. Auto-inject envelope — MCP-only (C5)

**Only `src/mcp/tools.py` `dispatch` calls `check_inbox`** (verified by grep: the sole import is `from ..agents.mailbox import check_inbox, format_injection` in `tools.py:36`). Internal `src/core/store.py` / `src/core/recall.py` / `src/agents/store.py` never touch the mailbox. In-process callers that need inbox access call `mailbox.check_inbox` explicitly.

### Scope derivation — `tools.py:100` `_scope_for`

| Tool | Whose inbox is checked |
|---|---|
| `mailbox.send` | `args["from"]` (the sender; canonicalized if `agent:`) |
| `mailbox.read` / `mailbox.ack` | `args["scope"]` (default `core`) |
| Any call with `args["agent_id"]` | That agent's canonical inbox (`agent:<id>`, alias-resolved) |
| Everything else | `core` |

### Decoration — `tools.py:116` `with_inbox_notice`

```python
def with_inbox_notice(scope, result, agents_conn):
    msgs = check_inbox(agents_conn, scope)  # marks noted_at
    if not msgs:
        return result                       # byte-identical, same object
    notice = format_injection(msgs, scope)
    if isinstance(result, dict):
        out = dict(result); out["mailbox_notice"] = notice; return out
    return {"result": result, "mailbox_notice": notice}
```

* Empty inbox -> response is **byte-identical** (same object, `tests/test_mcp.py:144` asserts `is`).
* Non-empty -> `mailbox_notice` string is added. `dispatch` saves the scope before handler execution, decorates the handler result in a `try` that **never breaks the tool call** (if the mailbox table is mid-migration, the undecorated result is returned).
* The decoration is committed atomically with the handler's DB writes (`dispatch` commits both `core` and `agents` on success, rolls back both on exception).

### Injection string — `mailbox.py:240` `format_injection`

```python
def format_injection(messages, scope):
    # _scope_label: core->"Hey Core", agent:<id>->"Hey Agent <id>", user->"Hey Operator"
    froms = sorted({m["from_scope"] for m in messages})
    n = len(messages)
    lines = [
        f'  - from {m["from_scope"]} at {ts} — "{preview}"'
        for m in messages  # preview = body[:120] stripped, newlines -> " "
    ]
    return (f"{_scope_label(scope)} — you have {n} message(s) from "
            f"{', '.join(froms)} — use mailbox.read / mailbox.ack to handle.\n"
            f"{detail}")
```

Per-message date and preview are included so the agent can prioritize without an extra `mailbox.read` call (2026-09-05).

Example (two pending messages for `agent:scout_ab12`):

```
Hey Agent scout_ab12 -- you have 2 message(s) from core, user -- use mailbox.read / mailbox.ack to handle.
  - from core at 2026-09-05 14:32 -- "Scout the HN top story about..."
  - from user at 2026-09-05 14:31 -- "Please prioritize the new brief..."
```

Empty inbox produces no `mailbox_notice` key at all — the response is returned unchanged.

### Sticky review reminder — `kind=review_ready` (2026-09-05)

When any `review_ready` node exists, `check_inbox(core)` runs `_sync_review_reminders` to coalesce a sticky mail for `core`:

- `count==1` → single mail `REVIEW_READY: NODE <id> ("label") from agent:X at YYYY-MM-DD -- use review_queue + promote_to_core`, metadata `{"kind":"review_ready","node_id":"...","agent_id":"..."}`, rendered as `[REVIEW READY] NODE ...`.
- `count>1` → aggregated mail `REVIEW_READY: You have N nodes ready for review -- please review via review_queue / promote_to_core`, metadata `{"kind":"review_ready","count":N,"node_ids":[...]}`, rendered as `[REVIEW QUEUE] You have N nodes...` with 3 `NODE` previews.

It is injected on **every** core `tools/call` (sticky, `acked_at IS NULL` still returned even after `noted`) until `mailbox.ack(core)` or `promote_to_core` acks it. Normal mail is one-shot (`noted_at` once); review mail persists. Triggered by `src/agents/store.py:363 agent_set_attention(review_ready)` and `agent_remember(attention_state=review_ready)` which `mailbox.send(agent:→core)` the per-node mail; `_sync` coalesces to aggregated when needed and cleans up on demote/promote.

---

## 7. Operator use — dashboard Mail tab (12th tab)

Mail tab (`mail.js`) — `Inbox (user)` + `New Message` modal + `All traffic` newest 5 + mini graphs (by sender bar, by state donut) + `Log — all traffic` drawer:

| Block | API | What it shows |
|---|---|---|
| **Inbox (for you)** | `GET /api/mailbox?scope=user&state=pending&limit=&offset=` | Replies from `core`/`agent` to `user`. Backoff-polled, badged (`b-mail`). Per-row `Reply` (opens `mail-reply-area` → `sendReply`) / `Ack` / `Del` (`POST /api/mailbox/delete`). |
| **Compose** | `POST /api/mailbox/send` body `{from:"user", to:"core|agent:<id>", body:"..."}` | Fixed `From: user`, `To:` dropdown (`core` + every `agent:<id>` from `GET /api/agents`, refreshed on tab open). Delivered straight into the recipient's next `mailbox_notice`. |
| **All traffic (5)** | `GET /api/mailbox?scope=all&state=all&limit=50` | Admin preview newest 5 + `by-sender` bar + `by-state` donut + `muted` counts. Broadcast visible here without receipt. |
| **Log drawer** | `GET /api/mailbox?scope=all&state=pending|noted|acked|all&limit=100` | Modal with `mail-log-q` substring search + state filter, table `From/To/Body/When/State` + per-row `Open` (modal with `Reply` → `mail-reply-area`) / `Del`. `Wipe all` → `POST /api/mailbox/wipe`. |

The dashboard read path uses `mailbox.read(..., mark_read=False)` so traffic view does not mark receipts `read`. Explicit `mailbox.read` from an MCP tool uses `mark_read=True`.

Search/pagination: `?q` is not on mailbox — filter by `scope` + `state` + `limit/offset` server-side. **Deletion:** `POST /api/mailbox/delete {"msg_id":…}` (or `msg_ids[]` bulk) and `POST /api/mailbox/wipe {}` physically delete rows (dashboard `Del`/`Wipe all`, `mail.js:252` `del`/`wipe`). Archiving is `mailbox.ack` (ack all for a scope) — `ack` clears badges, `delete/wipe` removes rows.

**Log drawer:** `mail.js:218 openLog()` modal with `mail-log-q` search (from/to/body substring) + `mail-log-state` filter `pending/noted/acked/all` + `renderLog()` `GET /api/mailbox?scope=all&state=…`; per-row `Open` (shows `mail-reply-area` + `sendReply(toScope)`) / `Del`. Reply from Log uses `POST /api/mailbox/send {from:user,to:replyTo,body}` then `ack` original.

---

## 8. Example payloads

### Send

```json
// POST /api/mailbox/send  or  MCP  {tool:"mailbox.send", args:{...}}
{
  "from": "core",
  "to": "agent:scout_ab12",
  "body": "Scout the HN top story about local AI."
}
// Response
{ "msg_id": "msg_4f8a9c2e1b3d7a0e" }

// Broadcast (core only)
{ "from": "core", "to": "broadcast", "body": "New brief available — all agents pull." }
```

Errors: `400 {"error": "mailbox.send(): agent -> broadcast rejected (hint: send to core)"}` · `400 {"error": "unknown_agent: 'agent:ghost' hint: agent_ensure first"}` · `400 {"error": "mailbox.send(): body required"}`

### Read

```json
// MCP  {tool:"mailbox.read", args:{"scope":"agent:scout_ab12","state":"pending","limit":10}}
{
  "scope": "agent:scout_ab12",
  "messages": [
    {
      "msg_id": "msg_4f8a9c2e1b3d7a0e",
      "from_scope": "core",
      "to_scope": "agent:scout_ab12",
      "body": "Scout the HN top story about local AI.",
      "created_at": 1725544321,
      "metadata": {},
      "receipt_scope": "agent:scout_ab12",
      "noted_at": 1725544330,
      "read_at": null,
      "acked_at": null
    }
  ]
}

// Dashboard admin view (read-only traffic view, mark_read=false)
 // GET /api/mailbox?scope=all&state=pending&limit=50&offset=0
{ "messages": [ ... ], "limit": 50, "offset": 0 }

// Pending-only next inbox query (marks read)
 // GET /api/mailbox?scope=user&state=pending
```

`state` allowed values: `pending` (not acked), `noted` (seen, not read/acked), `all`. `scope` = `core` | `agent:<id>` | `user` | `all`.

### Ack

```json
// Ack one message
// MCP  {tool:"mailbox.ack", args:{"scope":"agent:scout_ab12","msg_ids":["msg_4f8a9c2e1b3d7a0e"]}}
{ "acked": 1 }

// Ack all unacked for a scope
// MCP  {tool:"mailbox.ack", args:{"scope":"core"}}
// Dashboard: POST /api/mailbox/ack  body {"scope":"core","msg_ids":null}
{ "acked": 3 }

// msg_ids may be a JSON-string list (MCP clients vary)
{ "scope": "core", "msg_ids": "[\"msg_abc\", \"msg_def\"]" }
```

Unicast `ack` also mirrors `acked_at` onto `mailbox.acked_at` for cheap dashboard `COUNT(*) WHERE acked_at IS NULL` polling.

---

## 9. Design notes

* Receipts are per-recipient rows — a shared `noted_at` would re-notify `broadcast` forever (C6). Fan-out is fixed at send time.
* Only `src/mcp/tools.py` `dispatch` calls `check_inbox` (C5). Every other layer — `src/core/*`, `src/agents/store.py`, `src/agents/bridge.py`, `brain/*` — never touches `mailbox`. Tests `test_mailbox.py` and `test_mcp.py` gate this.
* Injected notice is response decoration, not a tool. Agents that ignore it still function; the next tool call re-injects until `ack`.

---

## 10. Related docs

* [BRAIN](BRAIN.md) — dual-DB brain engine + scheduler (also `agents.db` writer, serialized on `memory/.lock`).
* [AGENTS](AGENTS.md) — `agents.db` sharding + `agent_ensure` identity (needed for `agent:<id>` canonicalization).
* [MCP_TOOLS](MCP_TOOLS.md) — 23-tool surface including `mailbox.send/read/ack`; success results may carry `mailbox_notice`.
* `RULINGS.md` C5/C6 (envelope-only `check_inbox`, per-recipient receipts) + §2.8 (agent->agent allowed, agent->broadcast forbidden) — normative.

