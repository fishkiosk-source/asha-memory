"""src.core.vectors — TF-IDF with incremental df/ndocs (no per-insert full fit).

Ports v2 TfidfVectorizer math exactly (same tokenizer, same
idf = log((ndocs+1)/(df+1)) + 1, same cosine). Killed the v2 hot spot
(_load_vectorizer full SELECT + fit on EVERY insert): df/ndocs live in
vector_df / vector_meta and move incrementally
(insert -> df[term]+=1, ndocs+=1; delete -> decrement, floor 0).

Stored format (Phase 2 decision, replaces v2 JSON dicts): compact
space-joined 'term:weight' text (weights rounded to 6dp, terms sorted) +
magnitude REAL. Human-inspectable, ~3x smaller than JSON, parseable without
json.loads per row (SEMANTIC hot path, Phase 3).

Approximation note: idf drifts as df/ndocs move, so older stored vectors go
slightly stale until the next full rebuild_all() (migration / brain tick /
manual). Retrieval stays correct; scores converge on rebuild. This is the
documented trade for killing the per-insert full scan.
"""

import math
import sqlite3
from collections import Counter
from typing import Dict, List, Optional, Tuple

from .lexicon import _tokenize


def idf(ndocs: int, df: int) -> float:
    """v2 formula verbatim: log((ndocs+1)/(df+1)) + 1."""
    return math.log((ndocs + 1) / (df + 1)) + 1


def doc_tf(label: str, content: str) -> Counter:
    """Term frequencies over label + content (v2 transform input, ungated)."""
    return Counter(_tokenize((label or "") + " " + (content or "")))


def encode(vec: Dict[str, float]) -> str:
    """Compact 'term:weight' text, terms sorted for determinism."""
    return " ".join(f"{t}:{w:.6f}" for t, w in sorted(vec.items()))


def decode(s: str) -> Dict[str, float]:
    """Parse encode() output. Tolerates legacy v2 JSON dicts (migration aid)."""
    s = s or ""
    if s.startswith("{"):
        import json
        data = json.loads(s)
        return {str(k): float(v) for k, v in data.items()}
    vec: Dict[str, float] = {}
    for part in s.split():
        term, _, weight = part.rpartition(":")
        if term and weight:
            try:
                vec[term] = float(weight)
            except ValueError:
                continue
    return vec


def magnitude(vec: Dict[str, float]) -> float:
    return math.sqrt(sum(v * v for v in vec.values())) if vec else 0.0


def cosine(vec_a: Dict[str, float], vec_b: Dict[str, float],
           mag_a: Optional[float] = None, mag_b: Optional[float] = None) -> float:
    """Ports v2 TfidfVectorizer.cosine_similarity."""
    inter = set(vec_a) & set(vec_b)
    if not inter:
        return 0.0
    dot = sum(vec_a[t] * vec_b[t] for t in inter)
    na = mag_a if mag_a is not None else magnitude(vec_a)
    nb = mag_b if mag_b is not None else magnitude(vec_b)
    return dot / (na * nb) if na and nb else 0.0


def get_ndocs(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM vector_meta WHERE key = 'ndocs'").fetchone()
    return int(row["value"]) if row else 0


def _set_ndocs(conn: sqlite3.Connection, ndocs: int) -> None:
    conn.execute(
        "INSERT INTO vector_meta (key, value) VALUES ('ndocs', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(max(0, ndocs)),))


def get_df(conn: sqlite3.Connection, term: str) -> int:
    row = conn.execute(
        "SELECT df FROM vector_df WHERE term = ?", (term,)).fetchone()
    return int(row["df"]) if row else 0


def _bump_df(conn: sqlite3.Connection, terms, delta: int) -> None:
    for t in terms:
        if delta > 0:
            conn.execute(
                "INSERT INTO vector_df (term, df) VALUES (?, 1)"
                " ON CONFLICT(term) DO UPDATE SET df = df + 1", (t,))
        else:
            conn.execute("UPDATE vector_df SET df = df - 1 WHERE term = ?", (t,))
    conn.execute("DELETE FROM vector_df WHERE df <= 0")


def _weights(conn: sqlite3.Connection, tf: Counter, ndocs: int) -> Dict[str, float]:
    df_map = {t: get_df(conn, t) for t in tf}
    return {t: c * idf(ndocs, df_map[t]) for t, c in tf.items()}


def update_on_insert(conn: sqlite3.Connection, node_id: str,
                     label: str, content: str) -> Dict[str, float]:
    """Incremental insert: ndocs+=1, df[term]+=1 per doc term-set, store vector."""
    tf = doc_tf(label, content)
    _set_ndocs(conn, get_ndocs(conn) + 1)
    _bump_df(conn, set(tf), +1)
    vec = _weights(conn, tf, get_ndocs(conn))
    conn.execute(
        "INSERT OR REPLACE INTO node_vectors (node_id, vector, magnitude)"
        " VALUES (?, ?, ?)", (node_id, encode(vec), magnitude(vec)))
    return vec


def update_on_delete(conn: sqlite3.Connection, node_id: str,
                     label: str, content: str) -> None:
    """Incremental delete: df[term]-=1 (floor 0), ndocs-=1 (floor 0), drop row."""
    _bump_df(conn, set(doc_tf(label, content)), -1)
    _set_ndocs(conn, get_ndocs(conn) - 1)
    conn.execute("DELETE FROM node_vectors WHERE node_id = ?", (node_id,))


def refresh_on_content_change(conn: sqlite3.Connection, node_id: str,
                              old_label: str, old_content: str,
                              new_label: str, new_content: str) -> Dict[str, float]:
    """Content edit: decrement old terms, increment new terms, restow vector."""
    _bump_df(conn, set(doc_tf(old_label, old_content)), -1)
    tf = doc_tf(new_label, new_content)
    _bump_df(conn, set(tf), +1)
    vec = _weights(conn, tf, get_ndocs(conn))
    conn.execute(
        "INSERT OR REPLACE INTO node_vectors (node_id, vector, magnitude)"
        " VALUES (?, ?, ?)", (node_id, encode(vec), magnitude(vec)))
    return vec


def get_vector(conn: sqlite3.Connection,
               node_id: str) -> Tuple[Dict[str, float], float]:
    """Stored (vector, magnitude) for SEMANTIC (Phase 3). Missing row -> ({}, 0.0)."""
    row = conn.execute(
        "SELECT vector, magnitude FROM node_vectors WHERE node_id = ?",
        (node_id,)).fetchone()
    if not row:
        return {}, 0.0
    return decode(row["vector"]), float(row["magnitude"])


def rebuild_all(conn: sqlite3.Connection) -> Dict[str, int]:
    """Full batch recompute (migration / brain tick / manual only).

    Matches v2 fit semantics exactly, then stores compact vectors.
    Returns {'nodes': n, 'terms': vocab_size}.
    """
    rows = conn.execute("SELECT node_id, label, content FROM nodes").fetchall()
    texts = [((r["label"] or "") + " " + (r["content"] or "")) for r in rows]
    df: Counter = Counter()
    for text in texts:
        for t in set(_tokenize(text)):
            df[t] += 1
    ndocs = len(rows)
    conn.execute("DELETE FROM vector_df")
    conn.execute("DELETE FROM node_vectors")
    if df:
        conn.executemany("INSERT INTO vector_df (term, df) VALUES (?, ?)",
                         list(df.items()))
    for r in rows:
        tf = Counter(_tokenize((r["label"] or "") + " " + (r["content"] or "")))
        vec = {t: c * idf(ndocs, df[t]) for t, c in tf.items()}
        conn.execute(
            "INSERT INTO node_vectors (node_id, vector, magnitude) VALUES (?, ?, ?)",
            (r["node_id"], encode(vec), magnitude(vec)))
    _set_ndocs(conn, ndocs)
    return {"nodes": ndocs, "terms": len(df)}
