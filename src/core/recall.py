"""src.core.recall — recall modes + query DSL (auto-detected, C1 folding).

Ports v2 recall:1285 dispatch + all 9 workers + parse_query:264 + query:1862.
Deltas (all documented, no recall-semantics break):
- PATH uses heapq (v2: O(V^2) min-scan); CLUSTER uses deque (v2: list.pop(0)).
  Same traversal order, lower complexity.
- SEMANTIC caps the node_index pre-filter at SEMANTIC_CANDIDATE_CAP (2000)
  and reads compact term:weight vectors via vectors.decode (tolerates legacy
  v2 JSON dicts). Query-term vocab gating preserved (df==0 terms excluded).
- FTS fallbacks use C19 internal FTS (node_id join, not rowid).
- Core writes on recall are access_count + access_log ONLY (nodes.bump_access);
  layer promotion never happens here (§9.2). _log_query rows are written via
  QueryLogger (buffered, retention-capped at 5000); cache hits still log.
- DSL: any query starting with FIND is parsed and overrides mode (query_dsl
  folded into recall, C1). Explicit PATH "A -> B" (no FIND prefix) unaffected.
- WHO_IS/WHAT_ABOUT FTS fallbacks are type-checked (a non-PERSON / non-TOPIC
  FTS hit falls back to a type-scoped LIKE instead of centering on the wrong
  node — v2 used the FTS hit unchecked; primary label-match path identical).
- Agent-note visibility rule (_is_core_visible) ported; agents.db search lives
  in bridge (Phase 4) — this module only filters core.db rows.
- Returns plain dicts (JSON-ready): {"query","mode","nodes","total_found",
  "bound_applied"}. Nodes are row dicts (metadata parsed) + optional
  metadata._clock / metadata._similarity / "edges" (WHO_IS neighbors).
"""

import heapq
import json
import re
import sqlite3
import time
from collections import Counter, OrderedDict, deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import vectors
from .edges import neighbors
from .lexicon import (
    _extract_keywords,
    _looks_like_json_log,
    _sanitize_fts_query,
    _tokenize,
)
from .nodes import _now, bump_access, resolve_ref, row_to_dict

RECALL_MODES = ("RELATED", "SEMANTIC", "TIMELINE", "PATH", "CLUSTER", "DSL")
WORKER_MODES = ("WHO_IS", "WHAT_ABOUT", "RECENT", "RELATED", "PRUNE",
                "SEMANTIC", "PATH", "CLUSTER", "TIMELINE")

SEMANTIC_CANDIDATE_CAP = 2000
QUERY_LOG_RETAIN = 5000

DEFAULTS = {
    "max_nodes_per_recall": 30,
    "semantic_relevance_floor": 0.1,
    "prune_threshold": 0.05,
}


class ParsedQuery:
    """Port of v2 ParsedQuery (parse_query target)."""

    def __init__(self):
        self.mode = "RELATED"
        self.source = ""
        self.target = ""
        self.filters: Dict[str, Any] = {}
        self.bound = 30


def parse_query(query_str: str) -> ParsedQuery:
    """Port of v2 parse_query verbatim (DSL strings stay compatible)."""
    q = ParsedQuery()
    m = re.match(r'FIND\s+PATH\s+"([^"]+)"\s*->\s*"([^"]+)"', query_str, re.I)
    if m:
        q.mode = "PATH"
        q.source = m.group(1) + " -> " + m.group(2)
        q.target = m.group(2)
        return q
    m = re.match(r'FIND\s+(PERSON|TOPIC|EVENT|FACT)\s+"([^"]+)"\s*->\s*(\w+)',
                 query_str, re.I)
    if m:
        nt = m.group(1).upper()
        q.mode = "WHO_IS" if nt == "PERSON" else "WHAT_ABOUT"
        q.source = m.group(2)
        q.target = m.group(3)
        return q
    m = re.match(r'FIND\s+(PERSON|TOPIC|EVENT|FACT)\s+"([^"]+)"', query_str, re.I)
    if m:
        nt = m.group(1).upper()
        q.mode = "WHO_IS" if nt == "PERSON" else "WHAT_ABOUT"
        q.source = m.group(2)
        return q
    m = re.match(r'FIND\s+SEMANTIC\s+"([^"]+)"', query_str, re.I)
    if m:
        q.mode = "SEMANTIC"
        q.source = m.group(1)
        return q
    m = re.match(r'FIND\s+\w+\s+"([^"]+)"\s+CLUSTER', query_str, re.I)
    if m:
        q.mode = "CLUSTER"
        q.source = m.group(1)
        return q
    m = re.match(r'FIND\s+TIMELINE\s+"([^"]+)"(?:\s+SINCE\s+"([^"]+)")?',
                 query_str, re.I)
    if m:
        q.mode = "TIMELINE"
        q.source = m.group(1)
        q.target = m.group(2) or ""
        return q
    q.mode = "RELATED"
    q.source = query_str
    return q


class LRUCache:
    """Port of v2 LRUCache (capacity 50 default). Recall-level, process-local."""

    def __init__(self, capacity: int = 50):
        self.cache: "OrderedDict[str, Any]" = OrderedDict()
        self.capacity = capacity
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key: str, value) -> None:
        self.cache[key] = value
        self.cache.move_to_end(key)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

    def invalidate(self, prefix: str = "") -> None:
        if not prefix:
            self.cache.clear()
        else:
            for k in [k for k in self.cache if k.startswith(prefix)]:
                del self.cache[k]


class QueryLogger:
    """Buffered query_log writer with retention cap (write-amp rule).

    v2 wrote one row per recall on a NEW connection (plus cache-hit writes).
    This buffers `buffer_limit` rows then flushes on the caller's connection
    and enforces QUERY_LOG_RETAIN (5000) only on flush. One-shot use:
    QueryLogger(conn, buffer_limit=1).log(...) writes immediately.
    """

    def __init__(self, conn: sqlite3.Connection, buffer_limit: int = 20,
                 retain: int = QUERY_LOG_RETAIN):
        self.conn = conn
        self.buffer_limit = max(1, buffer_limit)
        self.retain = retain
        self.buf: List[Tuple] = []

    def log(self, query_text: str, mode: str, count: int,
            duration_ms: float, cache_hit: bool) -> None:
        self.buf.append((query_text[:200], mode, count, duration_ms,
                         1 if cache_hit else 0, _now()))
        if len(self.buf) >= self.buffer_limit:
            self.flush()

    def flush(self) -> int:
        if not self.buf:
            return 0
        try:
            self.conn.executemany(
                "INSERT INTO query_log (query_text, mode, result_count, duration_ms,"
                " cache_hit, queried_at) VALUES (?, ?, ?, ?, ?, ?)", self.buf)
        except sqlite3.OperationalError:
            # cross-process WAL contention (dashboard/MCP/brain) — drop batch, keep recall alive
            self.buf = []
            return 0
        n = len(self.buf)
        self.buf = []
        try:
            total = self.conn.execute("SELECT COUNT(*) FROM query_log").fetchone()[0]
            if total > self.retain:
                self.conn.execute(
                    "DELETE FROM query_log WHERE log_id NOT IN"
                    " (SELECT log_id FROM query_log ORDER BY log_id DESC LIMIT ?)",
                    (self.retain,))
        except sqlite3.OperationalError:
            pass
        return n


def is_core_visible(node: Dict[str, Any]) -> bool:
    """Port of v2 _is_core_visible onto dicts (bridge reuses this in Phase 4)."""
    meta = node.get("metadata") or {}
    if meta.get("attention_state") == "core_verified":
        return True
    return node.get("node_type") != "AGENT_NOTE" and not meta.get("agent_scoped", False)


def _agent_pred(alias: str, agent_id: Optional[str]) -> Tuple[str, tuple]:
    """SQL isolation predicate for agents.db callers (Idea.md §4.2).

    Every agent-scoped node query appends this; core callers (agent_id=None)
    get ("", ()) — zero overhead, zero behavior change.
    """
    if agent_id is None:
        return "", ()
    return f" AND {alias}.agent_id = ?", (agent_id,)


def _d(row: sqlite3.Row, edges: Optional[List[Dict]] = None) -> Dict[str, Any]:
    d = row_to_dict(row)
    d["edges"] = edges or []
    return d


def _attach_clock(clock, conn: sqlite3.Connection,
                  nodes_list: List[Dict[str, Any]], before_epoch: int) -> None:
    """Port of v2 _apply_clock_summaries (dict-based)."""
    for node in nodes_list:
        last = clock.last_accessed_before(conn, node["node_id"], before_epoch)
        row = conn.execute("SELECT access_count FROM nodes WHERE node_id = ?",
                           (node["node_id"],)).fetchone()
        count = row["access_count"] if row else node.get("access_count", 0)
        meta = node.get("metadata") or {}
        meta["_clock"] = clock.summarize_node(node, last_accessed=last,
                                              access_count=count)
        node["metadata"] = meta


def recall(conn: sqlite3.Connection, query: str, mode: str = "RELATED",
           bound: Optional[int] = None, offset: int = 0,
           include_agent_notes: bool = False,
           agent_id: Optional[str] = None,
           config: Optional[Dict] = None, clock=None,
           cache: Optional[LRUCache] = None,
           logger: Optional[QueryLogger] = None) -> Dict[str, Any]:
    """Recall entry point (v2 recall:1285 parity + offset + injected deps).

    DSL folding: a query starting with FIND is parsed and overrides mode.
    agent_id scopes EVERY node access to one agent (agents.db callers);
    None = core.db unscoped. Cache key includes the scope.
    Returns {"query","mode","nodes","total_found","bound_applied"}.
    """
    cfg = {**DEFAULTS, **(config or {})}
    if isinstance(query, str) and query.strip().upper().startswith("FIND"):
        pq = parse_query(query)
        mode, query = pq.mode, pq.source
    if mode not in WORKER_MODES:
        raise ValueError(f"Invalid mode: {mode}")
    bound = bound or int(cfg["max_nodes_per_recall"])
    start = time.time()
    norm_q = query.strip() if mode == "PATH" else re.sub(r"\s+", " ", query.strip().lower())
    cache_key = f"{mode}:{norm_q}:{bound}:agent_notes={include_agent_notes}:agent={agent_id}"

    own_logger = logger is None
    if own_logger:
        logger = QueryLogger(conn, buffer_limit=1)

    cached = cache.get(cache_key) if cache else None
    if cached is not None:
        cache.hits += 1
        logger.log(query, mode, len(cached["nodes"]), 0.0, True)
        logger.flush()
        nodes_list = [dict(n) for n in cached["nodes"]]
        if clock is not None and getattr(clock, "enabled", False):
            _attach_clock(clock, conn, nodes_list, _now())
        return _result(query, mode, nodes_list, cached["total_found"], bound, offset)

    if cache:
        cache.misses += 1
    fetch_bound = bound if include_agent_notes else max(bound * 5, bound + 25)
    worker = _WORKERS[mode]
    nodes_list, _total = worker(conn, query, fetch_bound, cfg, agent_id)

    if clock is not None and getattr(clock, "enabled", False):
        _attach_clock(clock, conn, nodes_list, int(start))

    if not include_agent_notes:
        nodes_list = [n for n in nodes_list if is_core_visible(n)]
    total_found = len(nodes_list)
    result_nodes = nodes_list[offset:offset + bound] if offset else nodes_list[:bound]
    duration = (time.time() - start) * 1000
    if cache:
        cache.put(cache_key, {"nodes": nodes_list, "total_found": total_found})
    logger.log(query, mode, len(result_nodes), duration, False)
    logger.flush()
    return {"query": query, "mode": mode, "nodes": result_nodes,
            "total_found": total_found, "bound_applied": total_found > bound}


def _result(query: str, mode: str, nodes_list: List[Dict], total: int,
            bound: int, offset: int) -> Dict[str, Any]:
    shown = nodes_list[offset:offset + bound] if offset else nodes_list[:bound]
    return {"query": query, "mode": mode, "nodes": shown,
            "total_found": total, "bound_applied": total > bound}


def _fts_or_like_seed(conn: sqlite3.Connection, text: str,
                      agent_id: Optional[str] = None):
    """Shared FTS-first seed lookup (WHO_IS / WHAT_ABOUT). C19 node_id join."""
    pred, params = _agent_pred("n", agent_id)
    fts_q = _sanitize_fts_query(text)
    if fts_q:
        try:
            return conn.execute(
                "SELECT n.* FROM nodes n JOIN node_fts f ON f.node_id = n.node_id"
                " WHERE node_fts MATCH ?" + pred + " LIMIT 1",
                (fts_q, *params)).fetchone()
        except sqlite3.OperationalError:
            pass
    if agent_id is None:
        return conn.execute(
            "SELECT * FROM nodes WHERE label LIKE ? OR content LIKE ? LIMIT 1",
            (f"%{text}%", f"%{text}%")).fetchone()
    return conn.execute(
        "SELECT * FROM nodes WHERE (label LIKE ? OR content LIKE ?) AND agent_id = ? LIMIT 1",
        (f"%{text}%", f"%{text}%", agent_id)).fetchone()


def _recall_who_is(conn, label, bound, cfg, agent_id):
    # NOTE: unaliased node queries use a bare "AND agent_id = ?" predicate
    apred = " AND agent_id = ?" if agent_id is not None else ""
    aparams = (agent_id,) if agent_id is not None else ()
    row = conn.execute(
        "SELECT * FROM nodes WHERE node_type = 'PERSON' AND (label = ? OR label LIKE ?)"
        + apred, (label, f"%{label}%", *aparams)).fetchone()
    if not row:
        row = _fts_or_like_seed(conn, label, agent_id)
        if not row or row["node_type"] != "PERSON":
            allrow = conn.execute(
                "SELECT * FROM nodes WHERE node_type = 'PERSON' AND"
                " (label LIKE ? OR content LIKE ?)" + apred + " LIMIT 1",
                (f"%{label}%", f"%{label}%", *aparams)).fetchone()
            row = allrow
    if not row:
        return [], 0
    person_id = row["node_id"]
    bump_access(conn, person_id)
    npred, nparams = _agent_pred("n", agent_id)
    nbs = conn.execute("""
        SELECT n.*, e.edge_type, e.weight as edge_weight, e.metadata as edge_metadata
        FROM nodes n
        JOIN edges e ON (n.node_id = e.to_node AND e.from_node = ?)
                     OR (n.node_id = e.from_node AND e.to_node = ?)
        WHERE n.node_id != ?""" + npred + """
        ORDER BY (n.importance * n.trust_level) DESC
        LIMIT ?""", (person_id, person_id, person_id, *nparams, max(bound - 1, 0))).fetchall()
    result = [_d(row)]
    for r in nbs:
        bump_access(conn, r["node_id"])
        try:
            emeta = json.loads(r["edge_metadata"])
        except (ValueError, TypeError):
            emeta = {}
        result.append(_d(r, edges=[{"edge_type": r["edge_type"],
                                   "weight": r["edge_weight"], "metadata": emeta}]))
    total = conn.execute("""
        SELECT COUNT(*) as c FROM nodes n
        JOIN edges e ON (n.node_id = e.to_node AND e.from_node = ?)
                     OR (n.node_id = e.from_node AND e.to_node = ?)
        WHERE n.node_id != ?""" + npred,
        (person_id, person_id, person_id, *nparams)).fetchone()["c"]
    return result, total + 1


def _recall_what_about(conn, query, bound, cfg, agent_id):
    apred = " AND agent_id = ?" if agent_id is not None else ""
    aparams = (agent_id,) if agent_id is not None else ()
    row = conn.execute(
        "SELECT * FROM nodes WHERE node_type = 'TOPIC' AND (label = ? OR label LIKE ?)"
        + apred, (query, f"%{query}%", *aparams)).fetchone()
    if not row:
        row = _fts_or_like_seed(conn, query, agent_id)
        if not row or row["node_type"] != "TOPIC":
            allrow = conn.execute(
                "SELECT * FROM nodes WHERE node_type = 'TOPIC' AND"
                " (label LIKE ? OR content LIKE ?)" + apred + " LIMIT 1",
                (f"%{query}%", f"%{query}%", *aparams)).fetchone()
            row = allrow
    if not row:
        return [], 0
    topic_id = row["node_id"]
    bump_access(conn, topic_id)
    npred, nparams = _agent_pred("n", agent_id)
    nbs = conn.execute("""
        WITH RECURSIVE hop1 AS (
            SELECT n.*, e.edge_type, e.weight as edge_weight, 1 as hop
            FROM nodes n JOIN edges e ON (n.node_id=e.to_node AND e.from_node=?)
                OR (n.node_id=e.from_node AND e.to_node=?)
            WHERE n.node_id != ?""" + npred + """
        ), hop2 AS (
            SELECT n.*, e.edge_type, e.weight as edge_weight, 2 as hop
            FROM nodes n JOIN edges e ON (n.node_id=e.to_node)
            JOIN hop1 h ON e.from_node = h.node_id
            WHERE n.node_id != ? AND n.node_id NOT IN (SELECT node_id FROM hop1)""" + npred + """
        )
        SELECT * FROM (SELECT * FROM hop1 UNION ALL SELECT * FROM hop2) combined
        ORDER BY (combined.importance * combined.trust_level) DESC LIMIT ?
        """, (topic_id, topic_id, topic_id, *nparams,
              topic_id, *nparams, max(bound - 1, 0))).fetchall()
    result = [_d(row)]
    seen = {topic_id}
    for r in nbs:
        if r["node_id"] not in seen:
            seen.add(r["node_id"])
            bump_access(conn, r["node_id"])
            result.append(_d(r))
    total = conn.execute("""
        SELECT COUNT(DISTINCT n.node_id) as c FROM nodes n
        JOIN edges e1 ON (n.node_id=e1.to_node AND e1.from_node=?)
            OR (n.node_id=e1.from_node AND e1.to_node=?)
        WHERE n.node_id != ?""" + npred,
        (topic_id, topic_id, topic_id, *nparams)).fetchone()["c"]
    return result, total + 1


def _recall_recent(conn, hours_str, bound, cfg, agent_id):
    try:
        hours = int(hours_str)
    except (ValueError, TypeError):
        hours = 24
    since = _now() - hours * 3600
    apred = " AND agent_id = ?" if agent_id is not None else ""
    aparams = (agent_id,) if agent_id is not None else ()
    rows = conn.execute("SELECT * FROM nodes WHERE updated_at > ?" + apred
                        + " ORDER BY updated_at DESC LIMIT ?",
                        (since, *aparams, bound)).fetchall()
    result = []
    for r in rows:
        bump_access(conn, r["node_id"])
        result.append(_d(r))
    total = conn.execute("SELECT COUNT(*) as c FROM nodes WHERE updated_at > ?" + apred,
                         (since, *aparams)).fetchone()["c"]
    return result, total


def _recall_related(conn, query, bound, cfg, agent_id):
    keywords = [w for w, _ in _extract_keywords(query)]
    apred = " AND agent_id = ?" if agent_id is not None else ""
    aparams = (agent_id,) if agent_id is not None else ()
    npred, nparams = _agent_pred("n", agent_id)
    if not keywords:
        fts_q = _sanitize_fts_query(query)
        if not fts_q:
            return [], 0
        try:
            rows = conn.execute(
                "SELECT n.* FROM nodes n JOIN node_fts f ON f.node_id = n.node_id"
                " WHERE node_fts MATCH ?" + npred + " LIMIT ?",
                (fts_q, *nparams, bound)).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE (label LIKE ? OR content LIKE ?)" + apred + " LIMIT ?",
                (f"%{query}%", f"%{query}%", *aparams, bound)).fetchall()
        result = []
        for r in rows:
            bump_access(conn, r["node_id"])
            result.append(_d(r))
        return result, len(rows)
    ph = ",".join("?" * len(keywords))
    rows = conn.execute(f"""
        SELECT n.*, COUNT(idx.word) as match_count, SUM(idx.weight) as relevance
        FROM nodes n JOIN node_index idx ON n.node_id = idx.node_id
        WHERE idx.word IN ({ph})""" + npred + """
        GROUP BY n.node_id
        ORDER BY (match_count * relevance * n.importance * n.trust_level) DESC LIMIT ?
        """, (*keywords, *nparams, bound)).fetchall()
    result = []
    for r in rows:
        bump_access(conn, r["node_id"])
        result.append(_d(r))
    total = conn.execute(f"""
        SELECT COUNT(DISTINCT n.node_id) as c FROM nodes n
        JOIN node_index idx ON n.node_id = idx.node_id WHERE idx.word IN ({ph})""" + npred,
        (*keywords, *nparams)).fetchone()["c"]
    return result, total


def _recall_prune(conn, threshold_str, bound, cfg, agent_id):
    try:
        threshold = float(threshold_str)
    except (ValueError, TypeError):
        threshold = float(cfg.get("prune_threshold", 0.05))
    old = _now() - 30 * 24 * 3600
    apred = " AND agent_id = ?" if agent_id is not None else ""
    aparams = (agent_id,) if agent_id is not None else ()
    rows = conn.execute(
        "SELECT * FROM nodes WHERE importance < ? AND access_count < 3 AND updated_at < ?"
        + apred + " ORDER BY importance ASC, updated_at ASC LIMIT ?",
        (threshold, old, *aparams, bound)).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE importance < ? AND access_count < 3"
        " AND updated_at < ?" + apred, (threshold, old, *aparams)).fetchone()["c"]
    return [_d(r) for r in rows], total


def _recall_semantic(conn, query_text, bound, cfg, agent_id):
    terms = _tokenize(query_text)
    if not terms:
        return [], 0
    ndocs = vectors.get_ndocs(conn)
    tf = Counter(terms)
    qvec = {}
    for t, c in tf.items():
        if vectors.get_df(conn, t) == 0:
            continue  # vocab gating (v2 transform parity)
        qvec[t] = c * vectors.idf(ndocs, vectors.get_df(conn, t))
    if not qvec:
        return [], 0
    floor = float(cfg.get("semantic_relevance_floor", 0.1))
    qmag = vectors.magnitude(qvec)
    qterms = list(qvec)
    candidate_ids = None
    if qterms:
        try:
            ph = ",".join("?" * len(qterms))
            cur = conn.execute(
                f"SELECT DISTINCT node_id FROM node_index WHERE word IN ({ph})",
                (*qterms,))
            candidate_ids = {r[0] for r in cur.fetchall()}
        except sqlite3.Error:
            candidate_ids = None
    rows = conn.execute(
        "SELECT n.*, nv.vector, nv.magnitude FROM nodes n"
        " LEFT JOIN node_vectors nv ON n.node_id = nv.node_id"
        + (" WHERE n.agent_id = ?" if agent_id is not None else ""),
        ((agent_id,) if agent_id is not None else ())).fetchall()
    scored = []
    for r in rows:
        if (candidate_ids is not None and 0 < len(candidate_ids) <= SEMANTIC_CANDIDATE_CAP
                and r["node_id"] not in candidate_ids):
            continue
        raw = r["vector"] if "vector" in r.keys() else None
        node_vec = vectors.decode(raw) if raw else {}
        if node_vec:
            nmag = r["magnitude"] if "magnitude" in r.keys() else 0.0
            try:
                nmag = float(nmag) if nmag else 0.0
            except (ValueError, TypeError):
                nmag = 0.0
            if not nmag:
                nmag = vectors.magnitude(node_vec)
        else:
            # lacunary row: compute on the fly (rare after rebuild_all)
            text = (r["label"] or "") + " " + (r["content"] or "")
            ltf = Counter(_tokenize(text))
            node_vec = {t: c * vectors.idf(ndocs, vectors.get_df(conn, t))
                        for t, c in ltf.items() if vectors.get_df(conn, t) > 0}
            nmag = vectors.magnitude(node_vec)
        sim = vectors.cosine(qvec, node_vec, qmag, nmag)
        if sim >= floor:
            scored.append((sim, r))
    scored.sort(key=lambda x: -x[0])
    result = []
    for sim, r in scored[:bound]:
        bump_access(conn, r["node_id"])
        node = _d(r)
        meta = node.get("metadata") or {}
        meta["_similarity"] = round(sim, 4)
        node["metadata"] = meta
        result.append(node)
    return result, len(scored)


def _recall_path(conn, query, bound, cfg, agent_id):
    parts = query.split("->")
    if len(parts) != 2:
        return [], 0
    start_id = resolve_ref(conn, parts[0].strip(), agent_id)
    end_id = resolve_ref(conn, parts[1].strip(), agent_id)
    if not start_id or not end_id:
        return [], 0
    dist = {start_id: 0.0}
    prev: Dict[str, str] = {}
    heap: List[Tuple[float, str]] = [(0.0, start_id)]
    visited = set()
    while heap:
        d, cur = heapq.heappop(heap)
        if cur in visited:
            continue
        visited.add(cur)
        if cur == end_id:
            break
        for nid, w, _t, _dir in neighbors(conn, cur, agent_id):
            if nid in visited:
                continue
            nd = d + (1.0 - w)
            if nd < dist.get(nid, float("inf")):
                dist[nid] = nd
                prev[nid] = cur
                heapq.heappush(heap, (nd, nid))
    if end_id not in prev and start_id != end_id:
        return [], 0
    path_ids = []
    cur = end_id
    while cur in prev:
        path_ids.append(cur)
        cur = prev[cur]
    path_ids.append(start_id)
    path_ids.reverse()
    ph = ",".join("?" * len(path_ids))
    rows = {r["node_id"]: r for r in conn.execute(
        f"SELECT * FROM nodes WHERE node_id IN ({ph})", (*path_ids,)).fetchall()}
    result = []
    for nid in path_ids[:bound]:
        bump_access(conn, nid)
        if nid in rows:
            result.append(_d(rows[nid]))
    return result, len(path_ids)


def _recall_cluster(conn, query, bound, cfg, agent_id):
    seed_id = resolve_ref(conn, query, agent_id)
    if not seed_id:
        return [], 0
    seen = {seed_id}
    queue = deque([seed_id])
    cluster = []
    while queue and len(cluster) < bound:
        cur = queue.popleft()
        row = conn.execute("SELECT * FROM nodes WHERE node_id = ?",
                           (cur,)).fetchone()
        if row:
            bump_access(conn, cur)
            cluster.append(_d(row))
        for nid, _w, _t, _dir in neighbors(conn, cur, agent_id):
            if nid not in seen:
                seen.add(nid)
                queue.append(nid)
    return cluster, len(seen)


def _recall_timeline(conn, query, bound, cfg, agent_id):
    node_id = resolve_ref(conn, query, agent_id)
    if not node_id:
        return [], 0
    npred, nparams = _agent_pred("n", agent_id)
    rows = conn.execute("""
        SELECT DISTINCT n.* FROM nodes n
        JOIN edges e ON (n.node_id = e.to_node AND e.from_node = ?)
                     OR (n.node_id = e.from_node AND e.to_node = ?)
        WHERE n.node_type = 'EVENT'""" + npred + """
        ORDER BY n.created_at DESC
        LIMIT ?""", (node_id, node_id, *nparams, bound)).fetchall()
    result = []
    for r in rows:
        bump_access(conn, r["node_id"])
        result.append(_d(r))
    total = conn.execute("""
        SELECT COUNT(DISTINCT n.node_id) as c FROM nodes n
        JOIN edges e ON (n.node_id = e.to_node AND e.from_node = ?)
                     OR (n.node_id = e.from_node AND e.to_node = ?)
        WHERE n.node_type = 'EVENT'""" + npred,
        (node_id, node_id, *nparams)).fetchone()["c"]
    return result, total


_WORKERS = {
    "WHO_IS": _recall_who_is,
    "WHAT_ABOUT": _recall_what_about,
    "RECENT": _recall_recent,
    "RELATED": _recall_related,
    "PRUNE": _recall_prune,
    "SEMANTIC": _recall_semantic,
    "PATH": _recall_path,
    "CLUSTER": _recall_cluster,
    "TIMELINE": _recall_timeline,
}
