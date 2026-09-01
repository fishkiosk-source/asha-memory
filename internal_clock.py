"""
ASHA_INTERNAL_CLOCK
===================
Temporal context module for ASHA_MEMORY_SYSTEM v2. Pure Python stdlib only
(sqlite3, datetime, time) — no external dependencies, no AI calls.

Gives AI entities a sense of time without changing the MCP contract:

  - now()            — current date/time snapshot (epoch + ISO + weekday)
  - humanize()       — "3 days ago", "2 weeks ago", "just now"
  - summarize_node() — per-node mini summary: added / last checked / layer
  - today_summary()  — today's memory activity (nodes added/accessed, queries)
  - graph_activity() — what changed in the last N hours
  - build_tick_content() — content for the daily TODAY context node

The "last checked" value is read from access_log BEFORE the current query's
access, because AshaMemory._bump_access sets updated_at = now on every recall.
Without this, every returned node would claim "last checked: just now".

Usage:
    from internal_clock import InternalClock
    clock = InternalClock()
    print(clock.now())
"""

import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, Optional


class InternalClock:
    """Temporal context provider for the memory graph."""

    def __init__(self, enabled: bool = True, stale_after_days: int = 7):
        self.enabled = enabled
        self.stale_after_days = max(1, stale_after_days)

    # ──────────────────────────────────────────────────────────────────────────
    # TIME PRIMITIVES
    # ──────────────────────────────────────────────────────────────────────────

    def now(self) -> Dict[str, Any]:
        """Current time snapshot: epoch, ISO, date, time, weekday (local)."""
        now = time.time()
        dt = datetime.fromtimestamp(now)
        return {
            "epoch": int(now),
            "iso": dt.isoformat(timespec="seconds"),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M:%S"),
            "weekday": dt.strftime("%A"),
        }

    def _day_start_epoch(self, now_epoch: Optional[float] = None) -> float:
        now = now_epoch if now_epoch is not None else time.time()
        dt = datetime.fromtimestamp(now)
        return datetime(dt.year, dt.month, dt.day).timestamp()

    def humanize(self, epoch: float, now_epoch: Optional[float] = None) -> str:
        """Relative human phrase for an epoch: 'just now', '3 days ago', ..."""
        now = now_epoch if now_epoch is not None else time.time()
        diff = now - epoch
        if diff < 0:
            return "in the future"
        if diff < 60:
            return "just now"
        minutes = diff / 60.0
        if minutes < 60:
            return self._unit(int(minutes), "minute")
        hours = minutes / 60.0
        if hours < 24:
            return self._unit(int(hours), "hour")
        days = hours / 24.0
        if days < 7:
            return self._unit(int(days), "day")
        weeks = days / 7.0
        if weeks < 4.345:
            return self._unit(round(weeks), "week")
        months = days / 30.44
        if months < 12:
            return self._unit(round(months), "month")
        years = days / 365.25
        return self._unit(round(years), "year")

    @staticmethod
    def _unit(count: int, unit: str) -> str:
        if count <= 0:
            count = 1
        label = unit if count == 1 else unit + "s"
        return f"{count} {label} ago"

    # ──────────────────────────────────────────────────────────────────────────
    # NODE SUMMARIES
    # ──────────────────────────────────────────────────────────────────────────

    def summarize_node(self, node, last_accessed: Optional[float] = None,
                       access_count: Optional[int] = None,
                       now_epoch: Optional[float] = None) -> Dict[str, Any]:
        """Per-node temporal summary. Accepts MemoryNode, dict, or any object
        with created_at / updated_at / access_count attributes.

        last_accessed = previous access time from access_log (exclude the
        access triggered by the query itself). Falls back to updated_at.
        access_count = live count override (post-bump); falls back to the
        node's own field, which may predate the current query's access.
        """
        fields = self._fields(node)
        now = now_epoch if now_epoch is not None else time.time()
        created = fields.get("created_at")
        updated = fields.get("updated_at")
        layer = fields.get("layer") or "working"
        last = last_accessed if last_accessed is not None else updated
        if access_count is None:
            access_count = fields.get("access_count", 0)
        return {
            "added": self.humanize(created, now) if created else None,
            "added_at": created,
            "last_checked": self.humanize(last, now) if last else None,
            "last_checked_at": last,
            "access_count": access_count,
            "layer": layer,
            "stale": self._is_stale(created, last, now),
        }

    def _is_stale(self, created: Optional[float], last: Optional[float],
                  now: float) -> bool:
        threshold = self.stale_after_days * 86400.0
        if last is not None:
            return (now - last) > threshold
        if created is not None:
            return (now - created) > threshold
        return False

    @staticmethod
    def _fields(node) -> Dict[str, Any]:
        if isinstance(node, dict):
            return node
        if hasattr(node, "to_dict"):
            return node.to_dict()
        keys = ("node_id", "node_type", "label", "content", "source",
                "trust_level", "created_at", "updated_at", "access_count",
                "importance", "checksum", "metadata", "layer")
        return {k: getattr(node, k, None) for k in keys}

    # ──────────────────────────────────────────────────────────────────────────
    # GRAPH ACTIVITY (DB-aware; takes a sqlite3 connection)
    # ──────────────────────────────────────────────────────────────────────────

    def last_accessed_before(self, conn: sqlite3.Connection,
                             node_id: str, before_epoch: float) -> Optional[float]:
        """Most recent access_log timestamp strictly before before_epoch."""
        row = conn.execute(
            "SELECT MAX(accessed_at) AS la FROM access_log WHERE node_id = ? AND accessed_at < ?",
            (node_id, before_epoch)
        ).fetchone()
        if row and row["la"] is not None:
            return float(row["la"])
        return None

    def today_summary(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """Date/time now + counts of today's memory activity."""
        day_start = self._day_start_epoch()
        summary = self.now()
        summary["day_start_epoch"] = int(day_start)
        summary["nodes_added_today"] = self._count(
            conn, "SELECT COUNT(*) FROM nodes WHERE created_at >= ?", (day_start,))
        summary["nodes_accessed_today"] = self._count(
            conn, "SELECT COUNT(DISTINCT node_id) FROM access_log WHERE accessed_at >= ?",
            (day_start,))
        summary["edges_created_today"] = self._count(
            conn, "SELECT COUNT(*) FROM edges WHERE created_at >= ?", (day_start,))
        summary["queries_today"] = self._count(
            conn, "SELECT COUNT(*) FROM query_log WHERE queried_at >= ?", (day_start,))
        return summary

    def graph_activity(self, conn: sqlite3.Connection,
                       hours: int = 24) -> Dict[str, Any]:
        """What changed in the memory graph over the last N hours."""
        since = time.time() - hours * 3600.0
        return {
            "since_hours": hours,
            "since_epoch": int(since),
            "nodes_created": self._count(
                conn, "SELECT COUNT(*) FROM nodes WHERE created_at >= ?", (since,)),
            "nodes_updated": self._count(
                conn, "SELECT COUNT(*) FROM nodes WHERE updated_at >= ?", (since,)),
            "nodes_accessed": self._count(
                conn, "SELECT COUNT(DISTINCT node_id) FROM access_log WHERE accessed_at >= ?",
                (since,)),
            "edges_created": self._count(
                conn, "SELECT COUNT(*) FROM edges WHERE created_at >= ?", (since,)),
            "queries_run": self._count(
                conn, "SELECT COUNT(*) FROM query_log WHERE queried_at >= ?", (since,)),
        }

    @staticmethod
    def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
        try:
            return conn.execute(sql, params).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    # ──────────────────────────────────────────────────────────────────────────
    # TODAY CONTEXT NODE
    # ──────────────────────────────────────────────────────────────────────────

    def build_tick_content(self, summary: Dict[str, Any]) -> str:
        """Human-readable content for the daily TODAY context node."""
        return (
            f"Today is {summary['weekday']} {summary['date']} at {summary['time']} "
            f"(epoch {summary['epoch']}). Memory activity today: "
            f"{summary['nodes_added_today']} nodes added, "
            f"{summary['nodes_accessed_today']} accessed, "
            f"{summary['edges_created_today']} edges created, "
            f"{summary['queries_today']} queries run."
        )