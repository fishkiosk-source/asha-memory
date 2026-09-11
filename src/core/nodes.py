"""src.core.nodes — remember / remember_many / get / delete / update_* for core.db.

Ports v2 AshaMemory node CRUD (remember:1179, remember_many:1228, delete:1759,
update_trust/importance:1765, get_node:1777, _build_index:1015, _auto_link:1054,
contradiction check:1104) with the Phase 2 deltas:
- single remember() delegates to the remember_many() bulk path (write-amp rule)
- NO full-corpus vector fit per insert: vectors.update_on_insert (incremental)
- NO layer promotion on access: layers.init_layer on insert only (C-write-amp)
- get_node() is pure read; access writes live in bump_access() (recall, Phase 3)
- trust/importance clamped to [0,1] everywhere (v2 only clamped on update;
  CHECK would IntegrityError on remember — clamp + document instead)
- update_node() is the MCP update/delete equivalent (C1): partial update with
  index + vector refresh on label/content change, metadata shallow-merged

Callers own the transaction: use `with connect(path) as conn:` (commits on
clean exit, rolls back on exception) or conn.commit() explicitly.
"""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from . import vectors
from .layers import get_layer, init_layer
from .lexicon import (
    DEFAULT_EPHEMERAL_LABELS,
    STOPWORDS,
    _extract_keywords,
    _looks_like_json_log,
    _sentiment_score,
)

NODE_TYPES = {
    "PERSON", "TOPIC", "EVENT", "FACT", "PREFERENCE",
    "BOUNDARY", "AFFECT", "AGENT_NOTE", "CORE_REF", "SKILL",
}

DEFAULTS = {
    "max_content_length": 500,
    "default_trust": 0.5,
    "default_importance": 0.5,
    "ephemeral_labels": sorted(DEFAULT_EPHEMERAL_LABELS),
}

UPDATABLE_FIELDS = {"label", "content", "trust_level", "importance", "source", "metadata"}


def _uuid() -> str:
    return "node_" + uuid.uuid4().hex[:16]


def _edge_uuid() -> str:
    return "edge_" + uuid.uuid4().hex[:16]


def _now() -> int:
    return int(time.time())


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _ephemeral_labels(config: Optional[Dict] = None):
    if config:
        return set(config.get("ephemeral_labels", DEFAULTS["ephemeral_labels"]))
    return set(DEFAULTS["ephemeral_labels"])


def _is_agent_scope(node_type: str, metadata: Dict) -> bool:
    """Scope predicate shared by auto_link (ports v2 inline checks)."""
    return ((node_type == "AGENT_NOTE"
             and metadata.get("attention_state") != "core_verified")
            or bool(metadata.get("agent_scoped")
                    and metadata.get("attention_state") != "core_verified"))


def build_index(conn: sqlite3.Connection, node_id: str, label: str, content: str) -> int:
    """(Re)build node_index rows for a node. Returns keyword count."""
    keywords = _extract_keywords((label or "") + " " + (content or ""))
    for word, weight in keywords:
        conn.execute(
            "INSERT OR REPLACE INTO node_index (word, node_id, field, weight)"
            " VALUES (?, ?, ?, ?)", (word, node_id, "content", weight))
    return len(keywords)


def clear_index(conn: sqlite3.Connection, node_id: str) -> None:
    conn.execute("DELETE FROM node_index WHERE node_id = ?", (node_id,))


def row_to_dict(row: sqlite3.Row, layer: Optional[str] = None) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    except (ValueError, TypeError):
        d["metadata"] = {}
    if layer is not None:
        d["layer"] = layer
    return d


def remember(conn: sqlite3.Connection, content: str, node_type: str,
             label: Optional[str] = None, source: str = "CORE",
             trust: Optional[float] = None, importance: Optional[float] = None,
             metadata: Optional[Dict] = None,
             config: Optional[Dict] = None) -> str:
    """Single insert — delegates to remember_many (bulk path). Returns node_id."""
    return remember_many(conn, [{
        "content": content, "node_type": node_type, "label": label,
        "source": source, "trust": trust, "importance": importance,
        "metadata": metadata,
    }], config=config)[0]


def remember_many(conn: sqlite3.Connection, items: List[Dict[str, Any]],
                  config: Optional[Dict] = None,
                  extra_cols: Optional[Dict[str, str]] = None) -> List[str]:
    """Bulk insert in one transaction. Returns node_ids in order.

    Per item: {content, node_type, label?, source?, trust?, importance?, metadata?}.
    Same commit does: nodes row + node_index + auto RELATES_TO links +
    incremental vector + working layer + FACT contradiction check.
    extra_cols: constant column values for stores with extra NOT NULL columns
    (agents.db: {"agent_id": ...}). Keys allowlisted to block SQL injection.
    """
    cfg = {**DEFAULTS, **(config or {})}
    if not items:
        return []
    extra_cols = extra_cols or {}
    unknown_cols = set(extra_cols) - {"agent_id"}
    if unknown_cols:
        raise ValueError(f"Invalid extra_cols: {sorted(unknown_cols)}")
    eph = set(cfg["ephemeral_labels"])
    now = _now()
    max_len = int(cfg["max_content_length"])
    ids: List[str] = []
    col_names = ("node_id, node_type, label, content, source, trust_level,"
                 " created_at, updated_at, access_count, importance, checksum, metadata"
                 + (", " + ", ".join(sorted(extra_cols)) if extra_cols else ""))
    placeholders = ", ".join(["?"] * (12 + len(extra_cols)))
    extra_vals = [extra_cols[k] for k in sorted(extra_cols)]

    for it in items:
        content = it.get("content", "") or ""
        node_type = it.get("node_type", "FACT")
        if node_type not in NODE_TYPES:
            raise ValueError(f"Invalid node_type: {node_type}")
        if len(content) > max_len:
            content = content[:max_len - 3] + "..."
        label = it.get("label") or content[:30]
        trust = _clamp01(it.get("trust", cfg["default_trust"])
                         if it.get("trust") is not None else cfg["default_trust"])
        importance = _clamp01(it.get("importance", cfg["default_importance"])
                              if it.get("importance") is not None else cfg["default_importance"])
        metadata = it.get("metadata") or {}
        source = it.get("source", "CORE")
        node_id = _uuid()
        ids.append(node_id)

        conn.execute(
            f"""INSERT INTO nodes ({col_names})
                VALUES ({placeholders})""",
            (node_id, node_type, label, content, source, trust, now, now,
             0, importance, _checksum(content), json.dumps(metadata), *extra_vals))
        build_index(conn, node_id, label, content)
        if not metadata.get("clock_node"):
            _auto_link(conn, node_id, label, content, eph,
                       agent_id=extra_cols.get("agent_id"))
        vectors.update_on_insert(conn, node_id, label, content)
        init_layer(conn, node_id, now)

        if node_type == "FACT":
            found = _check_contradictions(conn, node_id, label, content, eph)
            if found:
                meta = _get_metadata(conn, node_id)
                meta["contradictions_detected"] = found
                conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?",
                             (json.dumps(meta), node_id))
    return ids


def get_node(conn: sqlite3.Connection, node_id: str,
             with_layer: bool = True) -> Optional[Dict[str, Any]]:
    """Pure-read fetch (no access bump — see bump_access)."""
    row = conn.execute("SELECT * FROM nodes WHERE node_id = ?",
                       (node_id,)).fetchone()
    if not row:
        return None
    return row_to_dict(row, get_layer(conn, node_id) if with_layer else None)


def bump_access(conn: sqlite3.Connection, node_id: str) -> bool:
    """Recall-side access write: access_count + access_log ONLY (no layer moves)."""
    now = _now()
    cur = conn.execute(
        "UPDATE nodes SET access_count = access_count + 1, updated_at = ?"
        " WHERE node_id = ?", (now, node_id))
    if cur.rowcount == 0:
        return False
    conn.execute("INSERT INTO access_log (node_id, accessed_at) VALUES (?, ?)",
                 (node_id, now))
    return True


def delete_node(conn: sqlite3.Connection, node_id: str) -> bool:
    """Delete + incremental df decrement. FK CASCADE clears edges/vectors/
    layers/index/access rows; FTS trigger clears the FTS entry."""
    row = conn.execute("SELECT label, content FROM nodes WHERE node_id = ?",
                       (node_id,)).fetchone()
    if not row:
        return False
    vectors.update_on_delete(conn, node_id, row["label"] or "", row["content"] or "")
    cur = conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
    return cur.rowcount > 0


def update_node(conn: sqlite3.Connection, node_id: str, **fields) -> Optional[Dict[str, Any]]:
    """Partial update. Label/content change refreshes index + vectors.
    Metadata is shallow-merged. Returns the updated node dict (None if missing)."""
    row = conn.execute("SELECT * FROM nodes WHERE node_id = ?",
                       (node_id,)).fetchone()
    if not row:
        return None
    old_label, old_content = row["label"] or "", row["content"] or ""
    unknown = set(fields) - UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Invalid update fields: {sorted(unknown)}")

    new_label = fields.get("label", old_label)
    new_content = fields.get("content", old_content)
    sets: List[str] = []
    params: List[Any] = []
    if "label" in fields:
        sets.append("label = ?")
        params.append(new_label)
    if "content" in fields:
        sets.append("content = ?")
        params.append(new_content)
    if "trust_level" in fields:
        sets.append("trust_level = ?")
        params.append(_clamp01(fields["trust_level"]))
    if "importance" in fields:
        sets.append("importance = ?")
        params.append(_clamp01(fields["importance"]))
    if "source" in fields:
        sets.append("source = ?")
        params.append(fields["source"])
    if "metadata" in fields:
        try:
            merged = json.loads(row["metadata"] or "{}")
        except ValueError:
            merged = {}
        merged.update(fields["metadata"] or {})
        sets.append("metadata = ?")
        params.append(json.dumps(merged))
    if not sets:
        return row_to_dict(row)
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(node_id)
    conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE node_id = ?", params)

    if new_label != old_label or new_content != old_content:
        clear_index(conn, node_id)
        build_index(conn, node_id, new_label, new_content)
        vectors.refresh_on_content_change(conn, node_id, old_label, old_content,
                                          new_label, new_content)
    return get_node(conn, node_id)


def resolve_ref(conn: sqlite3.Connection, ref: str,
                agent_id: Optional[str] = None) -> Optional[str]:
    """Resolve a node_id, exact label, or label substring to node_id (v2 parity).

    agent_id scopes all three lookups (PATH/CLUSTER/TIMELINE centers on agents.db).
    """
    pred, params = (" AND agent_id = ?", (agent_id,)) if agent_id is not None else ("", ())
    row = conn.execute("SELECT node_id FROM nodes WHERE node_id = ?" + pred,
                       (ref, *params)).fetchone()
    if row:
        return row["node_id"]
    row = conn.execute("SELECT node_id FROM nodes WHERE label = ?" + pred + " LIMIT 1",
                       (ref, *params)).fetchone()
    if row:
        return row["node_id"]
    row = conn.execute("SELECT node_id FROM nodes WHERE label LIKE ?" + pred + " LIMIT 1",
                       (f"%{ref}%", *params)).fetchone()
    return row["node_id"] if row else None


def _get_metadata(conn: sqlite3.Connection, node_id: str) -> Dict:
    row = conn.execute("SELECT metadata FROM nodes WHERE node_id = ?",
                       (node_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["metadata"] or "{}")
    except ValueError:
        return {}


def _auto_link(conn: sqlite3.Connection, new_node_id: str, label: str,
               content: str, ephemeral_labels,
               agent_id: Optional[str] = None) -> int:
    """Keyword-overlap RELATES_TO links (ports v2 _auto_link logic).

    Guards: ephemeral nodes neither link out nor are linked to; no cross-scope
    (core <-> agent) links; overlap >= 2, top 10, weight min(overlap*0.2, 1.0).
    agents.db hardening (documented deviation): candidates stay within the same
    agent_id (cross-agent discovery is bridge.search_all_agents' job) and new
    edges are stamped with agent_id (NOT NULL in agents.db).
    Returns links created.
    """
    if label in ephemeral_labels or _looks_like_json_log(content):
        return 0
    keywords = set(w for w, _ in _extract_keywords((content or "") + " " + (label or "")))
    if not keywords:
        return 0
    new_row = conn.execute("SELECT node_type, metadata FROM nodes WHERE node_id = ?",
                           (new_node_id,)).fetchone()
    new_meta = _get_metadata(conn, new_node_id)
    new_is_agent = _is_agent_scope(new_row["node_type"], new_meta) if new_row else False

    placeholders = ",".join("?" * len(keywords))
    scope_pred = ""
    scope_params: tuple = ()
    if agent_id is not None:
        scope_pred = " AND node_id IN (SELECT node_id FROM nodes WHERE agent_id = ?)"
        scope_params = (agent_id,)
    cands = conn.execute(
        f"""SELECT node_id, COUNT(*) as overlap FROM node_index
            WHERE word IN ({placeholders}) AND node_id != ?{scope_pred}
            GROUP BY node_id HAVING overlap >= 2 ORDER BY overlap DESC LIMIT 10""",
        (*keywords, new_node_id, *scope_params)).fetchall()
    created = 0
    for cand in cands:
        existing_id = cand["node_id"]
        row = conn.execute("SELECT node_type, label, content FROM nodes WHERE node_id = ?",
                           (existing_id,)).fetchone()
        if row:
            if row["label"] in ephemeral_labels or _looks_like_json_log(row["content"] or ""):
                continue
            if _is_agent_scope(row["node_type"], _get_metadata(conn, existing_id)) != new_is_agent:
                continue
        try:
            if agent_id is None:
                conn.execute(
                    """INSERT OR IGNORE INTO edges
                       (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (_edge_uuid(), new_node_id, existing_id, "RELATES_TO",
                     min(cand["overlap"] * 0.2, 1.0), _now(), "{}"))
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO edges
                       (edge_id, agent_id, from_node, to_node, edge_type, weight, created_at, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (_edge_uuid(), agent_id, new_node_id, existing_id, "RELATES_TO",
                     min(cand["overlap"] * 0.2, 1.0), _now(), "{}"))
            created += 1
        except sqlite3.IntegrityError:
            pass
    return created


def _detect_contradiction(text_a: str, text_b: str):
    """Ports v2 _detect_contradiction_v2 verbatim (sentiment + negation + antonyms)."""
    if _looks_like_json_log(text_a) or _looks_like_json_log(text_b):
        return False, 0.0
    a_words = set(re.findall(r"\b[a-zA-Z]+\b", text_a.lower()))
    b_words = set(re.findall(r"\b[a-zA-Z]+\b", text_b.lower()))
    shared = {w for w in (a_words & b_words) if len(w) > 3 and w not in STOPWORDS}
    if len(shared) < 2:
        return False, 0.0
    structural = {"timestamp", "status", "post", "posts", "count", "posts_count",
                  "load1m", "load5m", "load15m", "cp"}
    if shared and shared.issubset(structural):
        return False, 0.0
    confidences = []
    sa, sb = _sentiment_score(text_a), _sentiment_score(text_b)
    if (sa > 0.3 and sb < -0.3) or (sa < -0.3 and sb > 0.3):
        confidences.append(0.65)
    neg_pat = (r"\b(not|no|never|isnt|isn\'t|dont|don\'t|didnt|didn\'t|wasnt|wasn\'t|"
               r"wont|won\'t|cant|can\'t|hates?|dislikes?)\b")
    a_has_neg = bool(re.search(neg_pat, text_a.lower()))
    b_has_neg = bool(re.search(neg_pat, text_b.lower()))
    if a_has_neg != b_has_neg and len(shared) >= 4:
        confidences.append(0.7 if len(shared) >= 5 else 0.4)
    antonyms = {
        ("like", "hate"), ("love", "hate"), ("prefer", "avoid"), ("yes", "no"),
        ("true", "false"), ("good", "bad"), ("high", "low"), ("fast", "slow"),
        ("hot", "cold"), ("start", "stop"), ("begin", "end"), ("increase", "decrease"),
        ("accept", "reject"), ("trust", "distrust"), ("agree", "disagree"),
        ("enable", "disable"), ("allow", "deny"), ("success", "failure"),
        ("easy", "hard"), ("best", "worst"), ("support", "oppose"),
    }
    for w1, w2 in antonyms:
        if (w1 in a_words and w2 in b_words) or (w2 in a_words and w1 in b_words):
            confidences.append(0.6)
            break
    if not confidences:
        return False, 0.0
    return True, max(confidences)


def _check_contradictions(conn: sqlite3.Connection, node_id: str, label: str,
                          content: str, ephemeral_labels) -> List[Dict[str, Any]]:
    """FACT-only contradiction scan on insert (ports v2 _check_contradictions).

    Creates CONTRADICTS edges (weight -conf) + contradiction_flag/pair metadata
    on both sides. Returns [{'node_id','label','confidence'}]. Telemetry excluded.
    """
    if label in ephemeral_labels or _looks_like_json_log(content or ""):
        return []
    keywords = [w for w, _ in _extract_keywords(content or "")[:5]]
    if not keywords:
        return []
    placeholders = ",".join("?" * len(keywords))
    cands = conn.execute(
        f"""SELECT n.node_id, n.content, n.label FROM nodes n
            JOIN node_index idx ON n.node_id = idx.node_id
            WHERE n.node_type = 'FACT' AND n.node_id != ? AND idx.word IN ({placeholders})
            GROUP BY n.node_id LIMIT 20""",
        (node_id, *keywords)).fetchall()
    found = []
    for cand in cands:
        if cand["label"] in ephemeral_labels or _looks_like_json_log(cand["content"] or ""):
            continue
        is_contra, conf = _detect_contradiction(content, cand["content"] or "")
        if is_contra and conf > 0.5:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO edges
                       (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (_edge_uuid(), node_id, cand["node_id"], "CONTRADICTS",
                     -conf, _now(),
                     json.dumps({"auto_detected": True, "confidence": conf, "method": "v2"})))
            except sqlite3.IntegrityError:
                pass
            for nid in (node_id, cand["node_id"]):
                meta = _get_metadata(conn, nid)
                meta["contradiction_flag"] = True
                meta["contradiction_pair"] = cand["node_id"] if nid == node_id else node_id
                conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?",
                             (json.dumps(meta), nid))
            found.append({"node_id": cand["node_id"], "label": cand["label"],
                          "confidence": conf})
    return found
