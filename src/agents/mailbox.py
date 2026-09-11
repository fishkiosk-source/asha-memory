"""src.agents.mailbox — mailbox + receipts in agents.db.

Table owner (DDL below, created by agents.store.ensure_schema). Full spec:
Idea.md rev8 §5/C6. Scopes: 'core' | 'agent:<id>' | 'broadcast' (to-only,
core sends) | 'user'. Allowed: core->agent, agent->core, agent->agent,
core->broadcast, user<->core/agent. agent->broadcast REJECTED
(hint: send to core). Unknown agent on either side -> unknown_agent error
(hint: agent_ensure first).

Receipts: unicast = 1 receipt for the single recipient; broadcast fans out to
one receipt per registered agent + core at send time (user sees broadcast via
the Mail tab admin view, no receipt). Unicast read/ack mirrors read_at/acked_at
onto the mailbox row for cheap dashboard counts.

ONLY caller of check_inbox is src/mcp/tools.py dispatch (MCP-envelope-only,
C5). Internal store/recall code never touches this module.
"""

import json
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

AGENT_BROADCAST_HINT = "hint: send to core"

# Receipt-state order for dashboard filters
RECEIPT_STATES = ("pending", "noted", "read", "acked")

# DDL owned here, created by agents.store.ensure_schema (single entry point).
MAILBOX_DDL = [
    """CREATE TABLE IF NOT EXISTS mailbox (
        msg_id     TEXT PRIMARY KEY,
        from_scope TEXT NOT NULL,
        to_scope   TEXT NOT NULL,
        body       TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        read_at    INTEGER,
        acked_at   INTEGER,
        metadata   TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS mailbox_receipts (
        msg_id   TEXT NOT NULL REFERENCES mailbox(msg_id) ON DELETE CASCADE,
        scope    TEXT NOT NULL,
        noted_at INTEGER,
        read_at  INTEGER,
        acked_at INTEGER,
        PRIMARY KEY (msg_id, scope)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_receipts_scope_noted ON mailbox_receipts(scope, noted_at)",
]

_AGENT_RE = re.compile(r"^agent:(.+)$")


def _now() -> int:
    return int(time.time())


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:16]


def _resolve_agent_id(conn: sqlite3.Connection, agent: str) -> str:
    """Canonical ID for agent:<ref> (aliases resolve); raises unknown_agent."""
    from .store import UNKNOWN_AGENT_HINT, resolve_agent
    ref = agent.split(":", 1)[1]
    row = resolve_agent(conn, ref)
    if not row:
        raise ValueError(f"{UNKNOWN_AGENT_HINT}: {agent!r}")
    return f"agent:{row['agent_id']}"


def _norm_scope(conn: sqlite3.Connection, scope: str, *, allow_broadcast: bool,
                role: str) -> str:
    """Validate + canonicalize a scope string (agent aliases resolved)."""
    if scope == "core" or scope == "user":
        return scope
    if scope == "broadcast":
        if not allow_broadcast:
            raise ValueError(f"broadcast not allowed as {role}")
        return scope
    m = _AGENT_RE.match(scope)
    if m:
        return _resolve_agent_id(conn, scope)
    raise ValueError(f"invalid scope {scope!r} as {role}")


def send(conn: sqlite3.Connection, from_scope: str, to_scope: str, body: str,
         metadata: Optional[Dict] = None) -> Dict[str, Any]:
    """Create a message + its receipt(s). Returns {"msg_id": ...}.

    agent -> broadcast is rejected (spam guard, hint: send to core).
    broadcast (core-only) fans out to every registered agent + core.
    """
    if not body or not body.strip():
        raise ValueError("mailbox.send(): body required")
    if _AGENT_RE.match(from_scope or "") and to_scope == "broadcast":
        raise ValueError(f"mailbox.send(): agent -> broadcast rejected ({AGENT_BROADCAST_HINT})")
    frm = _norm_scope(conn, from_scope, allow_broadcast=False, role="sender")
    if to_scope == "broadcast" and frm != "core":
        raise ValueError("mailbox.send(): only core may broadcast"
                         f" ({AGENT_BROADCAST_HINT})")
    to = _norm_scope(conn, to_scope, allow_broadcast=True, role="recipient")

    mid, now = _msg_id(), _now()
    conn.execute(
        "INSERT INTO mailbox (msg_id, from_scope, to_scope, body, created_at, metadata)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (mid, frm, to, body, now, json.dumps(metadata or {})))
    if to == "broadcast":
        from .store import agent_list
        recipients = ["core"] + [f"agent:{a['agent_id']}" for a in agent_list(conn)]
    else:
        recipients = [to]
    conn.executemany("INSERT INTO mailbox_receipts (msg_id, scope) VALUES (?, ?)",
                     [(mid, r) for r in recipients])
    return {"msg_id": mid}


def check_inbox(conn: sqlite3.Connection, scope: str) -> List[Dict[str, Any]]:
    """Un-noted messages for one scope; marks them noted (MCP wrapper only).

    Returns [{msg_id, from_scope, body, created_at, metadata}].
    For core, also syncs REVIEW_READY reminders so existing pending nodes
    (pre-wire) surface without a new transition.
    """
    if scope == "core":
        try:
            _sync_review_reminders(conn)
        except Exception:
            pass
    rows = conn.execute(
        """SELECT m.msg_id, m.from_scope, m.body, m.created_at, m.metadata
           FROM mailbox m JOIN mailbox_receipts r ON r.msg_id = m.msg_id
           WHERE r.scope = ? AND r.noted_at IS NULL
           ORDER BY m.created_at""", (scope,)).fetchall()
    msgs = []
    for r in rows:
        try:
            meta = json.loads(r["metadata"] or "{}")
        except ValueError:
            meta = {}
        msgs.append({"msg_id": r["msg_id"], "from_scope": r["from_scope"],
                     "body": r["body"], "created_at": r["created_at"],
                     "metadata": meta})
    # sticky review reminders: core sees pending REVIEW_READY until acked/promoted
    if scope == "core":
        try:
            review_rows = conn.execute(
                """SELECT m.msg_id, m.from_scope, m.body, m.created_at, m.metadata
                   FROM mailbox m JOIN mailbox_receipts r ON r.msg_id = m.msg_id
                   WHERE r.scope='core' AND r.acked_at IS NULL
                     AND json_extract(m.metadata,'$.kind')='review_ready'
                   ORDER BY m.created_at""").fetchall()
            existing = {m["msg_id"] for m in msgs}
            for r in review_rows:
                if r["msg_id"] not in existing:
                    try:
                        meta = json.loads(r["metadata"] or "{}")
                    except ValueError:
                        meta = {}
                    msgs.append({"msg_id": r["msg_id"], "from_scope": r["from_scope"],
                                 "body": r["body"], "created_at": r["created_at"],
                                 "metadata": meta})
        except Exception:
            pass
    if msgs:
        now = _now()
        # only mark un-noted as noted (sticky reviews already noted stay)
        try:
            conn.executemany("UPDATE mailbox_receipts SET noted_at = COALESCE(noted_at, ?)"
                             " WHERE msg_id = ? AND scope = ? AND noted_at IS NULL",
                             [(now, m["msg_id"], scope) for m in msgs])
        except Exception:
            conn.executemany("UPDATE mailbox_receipts SET noted_at = ?"
                             " WHERE msg_id = ? AND scope = ?",
                             [(now, m["msg_id"], scope) for m in msgs])
    return msgs


def read(conn: sqlite3.Connection, scope: str, state: str = "pending",
         limit: int = 50, offset: int = 0,
         mark_read: bool = True) -> List[Dict[str, Any]]:
    """Explicit full-message read for a scope (or scope 'all' admin view).

    state: pending (not acked) | noted (seen, not read/acked) | all.
    Reading marks read_at (receipt + unicast mailbox row mirror).
    """
    if state not in ("pending", "noted", "all"):
        raise ValueError(f"invalid state: {state!r}")
    if scope == "all":
        where = ""
        params: tuple = ()
    else:
        where, params = "WHERE r.scope = ?", (scope,)
    if state == "pending":
        where += (" AND" if where else "WHERE") + " r.acked_at IS NULL"
    elif state == "noted":
        where += (" AND" if where else "WHERE") + \
                 " r.noted_at IS NOT NULL AND r.read_at IS NULL AND r.acked_at IS NULL"
    rows = conn.execute(
        f"""SELECT m.msg_id, m.from_scope, m.to_scope, m.body, m.created_at,
                   m.metadata, r.scope AS receipt_scope,
                   r.noted_at, r.read_at AS r_read, r.acked_at AS r_acked
            FROM mailbox m JOIN mailbox_receipts r ON r.msg_id = m.msg_id
            {where} ORDER BY m.created_at LIMIT ? OFFSET ?""",
        (*params, limit, offset)).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["metadata"] or "{}")
        except ValueError:
            meta = {}
        out.append({"msg_id": r["msg_id"], "from_scope": r["from_scope"],
                    "to_scope": r["to_scope"], "body": r["body"],
                    "created_at": r["created_at"], "metadata": meta,
                    "receipt_scope": r["receipt_scope"], "noted_at": r["noted_at"],
                    "read_at": r["r_read"], "acked_at": r["r_acked"]})
    if out and mark_read and scope != "all":
        now = _now()
        conn.executemany("UPDATE mailbox_receipts SET read_at = COALESCE(read_at, ?)"
                         " WHERE msg_id = ? AND scope = ?",
                         [(now, m["msg_id"], scope) for m in out])
        _mirror_unicast(conn, [m["msg_id"] for m in out], "read_at", now)
    return out


def _mirror_unicast(conn: sqlite3.Connection, msg_ids: List[str],
                    col: str, now: int) -> None:
    """Mirror read/ack timestamps onto unicast mailbox rows (cheap counts)."""
    for mid in msg_ids:
        n = conn.execute("SELECT COUNT(*) FROM mailbox_receipts WHERE msg_id = ?",
                         (mid,)).fetchone()[0]
        to = conn.execute("SELECT to_scope FROM mailbox WHERE msg_id = ?",
                          (mid,)).fetchone()
        if n == 1 and to and to["to_scope"] != "broadcast":
            conn.execute(f"UPDATE mailbox SET {col} = COALESCE({col}, ?)"
                         " WHERE msg_id = ?", (now, mid))


def ack(conn: sqlite3.Connection, scope: str,
        msg_ids: Optional[List[str]] = None) -> Dict[str, int]:
    """Ack one/all unacked messages for a scope. Returns {"acked": n}."""
    if msg_ids is None:
        rows = conn.execute("SELECT msg_id FROM mailbox_receipts"
                            " WHERE scope = ? AND acked_at IS NULL", (scope,)).fetchall()
        msg_ids = [r["msg_id"] for r in rows]
    if not msg_ids:
        return {"acked": 0}
    now = _now()
    total = 0
    for mid in msg_ids:
        cur = conn.execute("UPDATE mailbox_receipts SET acked_at = COALESCE(acked_at, ?),"
                           " read_at = COALESCE(read_at, ?) WHERE msg_id = ? AND scope = ?"
                           " AND acked_at IS NULL", (now, now, mid, scope))
        total += cur.rowcount
    _mirror_unicast(conn, msg_ids, "acked_at", now)
    return {"acked": total}


def _scope_label(scope: str) -> str:
    if scope == "core":
        return "Hey Core"
    m = _AGENT_RE.match(scope or "")
    if m:
        return f"Hey Agent {m.group(1)}"
    if scope == "user":
        return "Hey Operator"
    return f"Hey {scope}"


def _sync_review_reminders(conn: sqlite3.Connection) -> None:
    """Ensure a coalesced REVIEW_READY reminder exists for core if any.

    Pending without reminder -> create aggregated mail; stale reminder
    after all promoted/demoted -> ack it. Called only for scope core
    inside check_inbox (best-effort, never raises).
    """
    try:
        # pending review nodes
        rows = conn.execute(
            """SELECT node_id, agent_id, label, created_at, updated_at
               FROM nodes WHERE node_type='AGENT_NOTE'
                 AND json_extract(metadata,'$.attention_state')='review_ready'
               ORDER BY updated_at DESC""").fetchall()
        count = len(rows)
        # pending review mails for core
        mails = conn.execute(
            """SELECT m.msg_id, m.metadata FROM mailbox m
               JOIN mailbox_receipts r ON r.msg_id=m.msg_id
               WHERE r.scope='core' AND r.acked_at IS NULL
                 AND json_extract(m.metadata,'$.kind')='review_ready'""").fetchall()
        if count == 0:
            # stale reminders -> ack them
            if mails:
                now = _now()
                for r in mails:
                    conn.execute("UPDATE mailbox_receipts SET acked_at=COALESCE(acked_at,?),"
                                 " read_at=COALESCE(read_at,?) WHERE msg_id=? AND scope='core'",
                                 (now, now, r["msg_id"]))
                    conn.execute("UPDATE mailbox SET acked_at=COALESCE(acked_at,?) WHERE msg_id=?",
                                 (now, r["msg_id"]))
            return
        # count >0: if we already have correct coalesced state, do nothing
        # For single-node case we keep per-node mails (one per node); for
        # multi-node we want a single aggregated mail to avoid spam.
        # Detect multi: if count>1 and we have exactly 1 aggregated mail with matching count, keep.
        if count > 1:
            if len(mails) == 1:
                try:
                    meta = json.loads(mails[0]["metadata"] or "{}")
                    if meta.get("count") == count:
                        return
                except Exception:
                    pass
            # need aggregated mail — remove per-node mails and create one
            for r in mails:
                conn.execute("DELETE FROM mailbox WHERE msg_id=?", (r["msg_id"],))
            first = rows[0]
            body = (f"REVIEW_READY: You have {count} nodes ready for review -- "
                    f"please review via review_queue / promote_to_core. "
                    f"(Latest: \"{first['label']}\" from agent:{first['agent_id']})")
            mid, now = _msg_id(), _now()
            conn.execute(
                "INSERT INTO mailbox (msg_id, from_scope, to_scope, body, created_at, metadata)"
                " VALUES (?,?,?,?,?,?)",
                (mid, f"agent:{first['agent_id']}", "core", body, now,
                 json.dumps({"kind": "review_ready", "count": count,
                            "node_ids": [r["node_id"] for r in rows]})))
            conn.execute("INSERT INTO mailbox_receipts (msg_id, scope) VALUES (?,?)", (mid, "core"))
        else:
            # count ==1: ensure a mail for this node exists (idempotent)
            nid = rows[0]["node_id"]
            for r in mails:
                try:
                    meta = json.loads(r["metadata"] or "{}")
                    if meta.get("node_id") == nid:
                        return
                except Exception:
                    pass
            # create missing single mail (use store path's formatting)
            if not mails:
                aid = rows[0]["agent_id"]
                label = rows[0]["label"] or ""
                try:
                    from datetime import datetime
                    ts = datetime.fromtimestamp(int(rows[0]["updated_at"] or rows[0]["created_at"])).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    ts = str(rows[0]["updated_at"])
                body = (f"REVIEW_READY: NODE {nid} (\"{label}\") from agent:{aid} "
                        f"ready for review at {ts} -- use review_queue / promote_to_core to graduate.")
                mid, now = _msg_id(), _now()
                conn.execute(
                    "INSERT INTO mailbox (msg_id, from_scope, to_scope, body, created_at, metadata)"
                    " VALUES (?,?,?,?,?,?)",
                    (mid, f"agent:{aid}", "core", body, now,
                     json.dumps({"kind": "review_ready", "node_id": nid,
                               "agent_id": aid, "label": label})))
                conn.execute("INSERT INTO mailbox_receipts (msg_id, scope) VALUES (?,?)", (mid, "core"))
    except Exception:
        pass


def format_injection(messages: List[Dict[str, Any]], scope: str) -> str:
    """Auto-inject envelope (response decoration, not a tool).

    Includes per-message date and preview so the agent can prioritize
    without an extra read call. Count alone was confusing (2026-09-05).
    REVIEW_READY mails (kind=review_ready) are rendered distinctly and
    aggregated when >1.
    """
    from datetime import datetime
    # split review vs normal
    review = [m for m in messages if (m.get("metadata") or {}).get("kind") == "review_ready"]
    normal = [m for m in messages if (m.get("metadata") or {}).get("kind") != "review_ready"]
    parts: List[str] = []
    if review:
        # aggregated mail with count>1 should render as queue even if len==1
        is_aggregated = False
        agg_count = None
        if len(review) == 1:
            try:
                ac = (review[0].get("metadata") or {}).get("count")
                if isinstance(ac, int) and ac > 1:
                    is_aggregated = True
                    agg_count = ac
            except Exception:
                pass
        if len(review) == 1 and not is_aggregated:
            r = review[0]
            meta = r.get("metadata") or {}
            nid = meta.get("node_id", "?")
            aid = meta.get("agent_id", r.get("from_scope", "?").replace("agent:", ""))
            label = meta.get("label", "")
            try:
                ts = datetime.fromtimestamp(int(r["created_at"])).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts = str(r.get("created_at", ""))
            parts.append(
                f"[REVIEW READY] NODE {nid} (\"{label}\") from agent:{aid} at {ts} "
                f"-- use review_queue + promote_to_core to graduate. "
                f"Body: \"{(r.get('body') or '').strip()[:160]}\"")
        else:
            # aggregated or multiple per-node mails
            if is_aggregated:
                total = agg_count
                # for aggregated single mail, show body directly
                r = review[0]
                parts.append(
                    f"[REVIEW QUEUE] You have {total} nodes ready for review -- "
                    f"please review via review_queue / promote_to_core (use mailbox.ack after).")
                parts.append(f"  Detail: \"{(r.get('body') or '').strip()[:180]}\"")
                # also list node_ids from metadata if present
                try:
                    ids = (r.get("metadata") or {}).get("node_ids") or []
                    for nid in ids[:3]:
                        parts.append(f"  - NODE {nid}")
                    if len(ids) > 3:
                        parts.append(f"  ...and {len(ids)-3} more")
                except Exception:
                    pass
            else:
                counts = [ (m.get("metadata") or {}).get("count") for m in review ]
                total = max([c for c in counts if isinstance(c, int)] or [len(review)])
                parts.append(
                    f"[REVIEW QUEUE] You have {total} nodes ready for review -- "
                    f"please review via review_queue / promote_to_core (use mailbox.ack after).")
                for m in review[:3]:
                    meta = m.get("metadata") or {}
                    nid = meta.get("node_id", "?")
                    aid = meta.get("agent_id", "")
                    label = meta.get("label", "")
                    parts.append(f"  - NODE {nid} \"{label}\" from agent:{aid}")
                if len(review) > 3:
                    parts.append(f"  ...and {len(review)-3} more")
    if normal:
        froms = sorted({m["from_scope"] for m in normal})
        n = len(normal)
        lines = []
        for m in normal:
            try:
                ts = datetime.fromtimestamp(int(m["created_at"])).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts = str(m.get("created_at", ""))
            preview = (m.get("body") or "").strip().replace("\n", " ")[:120]
            lines.append(f'  - from {m["from_scope"]} at {ts} -- "{preview}"')
        detail = "\n".join(lines)
        parts.append(
            f"{_scope_label(scope)} -- you have {n} message(s) from "
            f"{', '.join(froms)} -- use mailbox.read / mailbox.ack to handle.\n{detail}")
    if not parts:
        return ""
    # combine with header for core
    header = f"{_scope_label(scope)} -- you have {len(messages)} total notification(s):\n" if (review and normal) else ""
    return header + "\n".join(parts)
