"""
ASHA_MEMORY_SYSTEM v2.0
=======================
Local, portable, node-based memory graph for ASHA_CORE and agents.

v2 upgrades:
  - TF-IDF vector retrieval (SEMANTIC mode) — finds meaning, not just keywords
  - PATH mode — shortest weighted path between two nodes
  - CLUSTER mode — BFS neighborhood grouped by type
  - Tiered memory layers (working → short-term → long-term → archive)
  - Cosine-based consolidation (TF-IDF instead of Jaccard)
  - Sentiment-weighted contradiction detection
  - Query DSL (FIND PERSON "SAM" -> PREFERENCE)
  - Cross-agent queries + agent-to-agent references
  - LRU query cache + batch operations
  - Profile/health introspection
  - JSON + GraphML export

v2 reads v1 databases (auto-migrates). v1 cannot read v2 databases.

Usage:
    from asha_memory_v2 import AshaMemory
    memory = AshaMemory(base_path="./asha_memory")
"""

import sqlite3
import json
import hashlib
import os
import re
import tarfile
import shutil
import math
import time
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple, Set
from dataclasses import dataclass, asdict
from contextlib import contextmanager
from collections import Counter, OrderedDict
from internal_clock import InternalClock

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "schema_version": "2.0",
    "max_nodes_per_recall": 30,
    "max_edges_per_recall": 50,
    "max_content_length": 500,
    "decay_factor_per_day": 0.99,
    "access_boost": 0.05,
    "prune_threshold": 0.05,
    "consolidation_similarity_high": 0.85,
    "consolidation_similarity_link": 0.50,
    "default_trust": 0.5,
    "default_importance": 0.5,
    "agent_max_notes": 100,
    "agent_max_content_length": 800,
    # Agent notes share core.db by default but stay outside ordinary core recall.
    # Set to legacy_shards only to retain the pre-v2.1 per-agent database model.
    "agent_memory_mode": "core_shared",
    # v2 additions
    "semantic_relevance_floor": 0.1,
    "vector_index_auto_rebuild": True,
    "cache_capacity": 50,
    "working_memory_capacity": 20,
    "short_term_promote_after": 3,
    "long_term_promote_after": 15,
    # v2 internal clock: temporal context (node ages, TODAY node). Zero MCP changes.
    "internal_clock": True,
}

NODE_TYPES = {
    "PERSON", "TOPIC", "EVENT", "FACT", "PREFERENCE",
    "BOUNDARY", "AFFECT", "AGENT_NOTE", "CORE_REF", "SKILL"
}

EDGE_TYPES = {
    "RELATES_TO", "CONTRADICTS", "SUPPORTS", "CAUSED_BY",
    "PART_OF", "TRUSTS", "DISTRUSTS", "REMEMBERS", "HAS_PREFERENCE",
    "HAS_BOUNDARY", "HAS_AFFECT", "HAS_SKILL", "REFERS_TO", "SUMMARIZES"
}

AGENT_RESTRICTED_TYPES = {"PERSON", "BOUNDARY", "SKILL"}

# v2 memory layers
MEMORY_LAYERS = {
    "working":    {"decay": 1.0,   "boost": 0.0,  "capacity": 20,   "demote_after_s": 3600},
    "short_term": {"decay": 0.97,  "boost": 0.10, "capacity": 500,  "demote_after_s": 604800},
    "long_term":  {"decay": 0.995, "boost": 0.05, "capacity": 5000, "demote_after_s": 7776000},
    "archive":    {"decay": 1.0,   "boost": 0.0,  "capacity": None, "demote_after_s": None},
}

# Ephemeral telemetry labels — append-only logs that must not bloat the graph
EPHEMERAL_LABELS = {
    "FEED_SNAPSHOT", "RUNTIME_SAMPLE", "TIME_ENTRY", "DAILY_STATE",
    "CRON_SUPERVISOR_REPORT", "BRAIN_MAINTENANCE_REPORT", "BRAIN_HISTORY",
    "SCOUT_WRAPPER_TOP_STORIES", "HN_SCOUT_TOP3", "HN_SCOUT",
}

# Lexicon version — bump when tokenizer/stemmer/stopwords change
LEXICON_VERSION = 3  # v2=Unicode tokenizer+no stemmer, v3=+2-letter stopwords

# Contradiction sentiment word lists
POSITIVE_WORDS = {
    "like","love","prefer","enjoy","good","great","excellent","amazing","best",
    "easy","fast","beautiful","perfect","recommend","awesome","fantastic","wonderful",
    "agree","support","approve","yes","true","right","correct","reliable","works",
}
NEGATIVE_WORDS = {
    "hate","dislike","avoid","bad","terrible","awful","worst","horrible",
    "hard","slow","ugly","broken","useless","disaster","pathetic","garbage",
    "disagree","oppose","reject","no","false","wrong","incorrect","unreliable","fails",
}

# ──────────────────────────────────────────────────────────────────────────────
# SHARED TOKENIZER
# ──────────────────────────────────────────────────────────────────────────────

_TOKEN_PATTERN = re.compile(r"\b[\w']{2,}\b", re.UNICODE)

def _tokenize(text: str, min_len: int = 2) -> List[str]:
    """Tokenize text into words. Captures Unicode, digits, underscores, contractions.

    - min_len=2 so 'AI', 'go', 'it' enter IDF (IDF naturally demotes noise).
    - Unicode flag so 'Müller', 'français', 'über' are visible.
    - Apostrophe kept so 'don\\'t', 'it\\'s' stay as single tokens.
    """
    return [w for w in _TOKEN_PATTERN.findall(text.lower()) if len(w) >= min_len]


# ──────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MemoryNode:
    node_id: str
    node_type: str
    label: str
    content: str
    source: str
    trust_level: float
    created_at: int
    updated_at: int
    access_count: int
    importance: float
    checksum: str
    metadata: Dict[str, Any]
    edges: List[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RecallResult:
    query: str
    mode: str
    nodes: List[MemoryNode]
    total_found: int
    bound_applied: bool


# ──────────────────────────────────────────────────────────────────────────────
# TF-IDF VECTOR ENGINE (Phase 1)
# ──────────────────────────────────────────────────────────────────────────────

class TfidfVectorizer:
    """Pure-Python TF-IDF. No external libs. Corpus-wide IDF tracking."""

    def __init__(self):
        self.df = Counter()
        self.vocab = set()
        self.ndocs = 0

    def fit(self, texts: List[str]):
        for text in texts:
            terms = set(self._tokenize(text))
            for t in terms:
                self.df[t] += 1
            self.vocab.update(terms)
            self.ndocs += 1

    def transform(self, text: str) -> Dict[str, float]:
        terms = self._tokenize(text)
        tf = Counter(terms)
        vec = {}
        for t, count in tf.items():
            if t in self.vocab:
                idf = math.log((self.ndocs + 1) / (self.df.get(t, 0) + 1)) + 1
                vec[t] = count * idf
        return vec

    def cosine_similarity(self, vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
        inter = set(vec_a) & set(vec_b)
        if not inter:
            return 0.0
        dot = sum(vec_a[t] * vec_b[t] for t in inter)
        na = math.sqrt(sum(v * v for v in vec_a.values()))
        nb = math.sqrt(sum(v * v for v in vec_b.values()))
        return dot / (na * nb) if na and nb else 0.0

    def _tokenize(self, text: str) -> List[str]:
        return _tokenize(text)

    def to_dict(self) -> Dict:
        return {"df": dict(self.df), "vocab": list(self.vocab), "ndocs": self.ndocs}

    @classmethod
    def from_dict(cls, data: Dict) -> "TfidfVectorizer":
        v = cls()
        v.df = Counter(data.get("df", {}))
        v.vocab = set(data.get("vocab", []))
        v.ndocs = data.get("ndocs", 0)
        return v


# ──────────────────────────────────────────────────────────────────────────────
# LRU CACHE (Phase 7)
# ──────────────────────────────────────────────────────────────────────────────

class LRUCache:
    def __init__(self, capacity: int = 50):
        self.cache = OrderedDict()
        self.capacity = capacity

    def get(self, key: str):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key: str, value):
        self.cache[key] = value
        self.cache.move_to_end(key)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

    def invalidate(self, prefix: str = ""):
        if not prefix:
            self.cache.clear()
        else:
            to_remove = [k for k in self.cache if k.startswith(prefix)]
            for k in to_remove:
                del self.cache[k]


# ──────────────────────────────────────────────────────────────────────────────
# QUERY DSL (Phase 5)
# ──────────────────────────────────────────────────────────────────────────────

class ParsedQuery:
    def __init__(self):
        self.mode = "RELATED"
        self.source = ""
        self.target = ""
        self.filters = {}
        self.bound = 30


def parse_query(query_str: str) -> ParsedQuery:
    """
    Query DSL:
      FIND PERSON "SAM" -> PREFERENCE
      FIND SEMANTIC "query text"
      FIND PATH "A" -> "B"
      FIND FACT WHERE trust > 0.8
      FIND TOPIC "memory" CLUSTER
      FIND TIMELINE "SAM" SINCE "2026-01-01"
    Falls back to RELATED if unparseable.
    """
    q = ParsedQuery()

    # FIND PATH "A" -> "B"
    m = re.match(r'FIND\s+PATH\s+"([^"]+)"\s*->\s*"([^"]+)"', query_str, re.I)
    if m:
        q.mode = "PATH"
        q.source = m.group(1) + " -> " + m.group(2)
        q.target = m.group(2)
        return q

    # FIND PERSON/TOPIC "LABEL" -> TYPE
    m = re.match(r'FIND\s+(PERSON|TOPIC|EVENT|FACT)\s+"([^"]+)"\s*->\s*(\w+)', query_str, re.I)
    if m:
        nt = m.group(1).upper()
        q.mode = "WHO_IS" if nt == "PERSON" else "WHAT_ABOUT"
        q.source = m.group(2)
        q.target = m.group(3)
        return q

    # FIND PERSON/TOPIC "LABEL"
    m = re.match(r'FIND\s+(PERSON|TOPIC|EVENT|FACT)\s+"([^"]+)"', query_str, re.I)
    if m:
        nt = m.group(1).upper()
        q.mode = "WHO_IS" if nt == "PERSON" else "WHAT_ABOUT"
        q.source = m.group(2)
        return q

    # FIND SEMANTIC "query"
    m = re.match(r'FIND\s+SEMANTIC\s+"([^"]+)"', query_str, re.I)
    if m:
        q.mode = "SEMANTIC"
        q.source = m.group(1)
        return q

    # FIND TOPIC/... "X" CLUSTER
    m = re.match(r'FIND\s+\w+\s+"([^"]+)"\s+CLUSTER', query_str, re.I)
    if m:
        q.mode = "CLUSTER"
        q.source = m.group(1)
        return q

    # FIND TIMELINE "X" SINCE "date"
    m = re.match(r'FIND\s+TIMELINE\s+"([^"]+)"(?:\s+SINCE\s+"([^"]+)")?', query_str, re.I)
    if m:
        q.mode = "TIMELINE"
        q.source = m.group(1)
        q.target = m.group(2) or ""
        return q

    # Default to RELATED
    q.mode = "RELATED"
    q.source = query_str
    return q


# ──────────────────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return "node_" + uuid.uuid4().hex[:16]


def _edge_uuid() -> str:
    return "edge_" + uuid.uuid4().hex[:16]


def _now() -> int:
    return int(time.time())


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


STOPWORDS = {
    # 2-letter noise words (min_len=2 in tokenizer; "ai" kept as domain signal)
    "to","in","is","it","of","on","as","at","be","by","do","go","he","if","me",
    "my","no","or","so","up","us","we","an","am","hi",
    # 3+ letter noise words
    "the","and","for","are","but","not","you","all","can","had","her","was","one",
    "our","out","day","get","has","him","his","how","its","may","new","now","old",
    "see","two","who","boy","did","she","use","her","way","many","oil","sit","set",
    "run","eat","far","sea","eye","ago","off","too","any","say","man","try","ask",
    "end","why","let","put","own","tell","very","when","much","would","there","their",
    "what","said","have","each","which","will","about","could","other","after","first",
    "never","these","think","where","being","every","great","might","shall","still",
    "those","while","this","that","with","from","they","know","want","been","good",
    "much","some","time","than","them","well","were","here","look","more","only",
    "over","such","take","also","just","like","make","even","then","back","very",
}


def _extract_keywords(text: str, max_words: int = 20) -> List[Tuple[str, float]]:
    words = _tokenize(text)
    filtered = [w for w in words if w not in STOPWORDS]
    counts = Counter(filtered)
    total = len(filtered) or 1
    return [(word, count / total) for word, count in counts.most_common(max_words)]


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    set_a = set(_tokenize(text_a))
    set_b = set(_tokenize(text_b))
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _sentiment_score(text: str) -> float:
    """Return -1.0 to 1.0 sentiment score. Positive = affirmative, negative = negating."""
    words = set(_tokenize(text))
    pos = sum(1 for w in words if w in POSITIVE_WORDS)
    neg = sum(1 for w in words if w in NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def _looks_like_json_log(content: str) -> bool:
    """Heuristic: raw JSON telemetry (FEED_SNAPSHOT / RUNTIME_SAMPLE) — not knowledge."""
    if not content:
        return False
    s = content.strip()
    if not s.startswith("{"):
        return False
    low = s[:400].lower()
    return ("timestamp" in low and ("status" in low or "post_count" in low or "load1m" in low))


def _detect_contradiction_v2(text_a: str, text_b: str) -> Tuple[bool, float]:
    """
    v2 contradiction detection: sentiment polarity + negation patterns + antonyms.
    """
    # JSON telemetry logs share structural keys (timestamp/status) — never contradictions
    if _looks_like_json_log(text_a) or _looks_like_json_log(text_b):
        return False, 0.0

    a_words = set(re.findall(r"\b[a-zA-Z]+\b", text_a.lower()))
    b_words = set(re.findall(r"\b[a-zA-Z]+\b", text_b.lower()))
    shared = a_words & b_words
    if len(shared) < 3:
        return False, 0.0

    # If shared vocabulary is purely structural JSON keys, suppress (FEED_SNAPSHOT noise)
    structural = {"timestamp", "status", "post", "posts", "count", "posts_count", "load1m", "load5m", "load15m", "cp", "posts_count"}
    if shared and shared.issubset(structural):
        return False, 0.0

    confidences = []

    # 1) Sentiment polarity conflict
    sa = _sentiment_score(text_a)
    sb = _sentiment_score(text_b)
    if (sa > 0.3 and sb < -0.3) or (sa < -0.3 and sb > 0.3):
        confidences.append(0.65)

    # 2) Direct negation patterns (v1 style)
    neg_pat = r"\b(not|no|never|isnt|isn\'t|dont|don\'t|didnt|didn\'t|wasnt|wasn\'t|wont|won\'t|cant|can\'t|hates?|dislikes?)\b"
    a_has_neg = bool(re.search(neg_pat, text_a.lower()))
    b_has_neg = bool(re.search(neg_pat, text_b.lower()))
    if a_has_neg != b_has_neg and len(shared) >= 4:
        confidences.append(0.7 if len(shared) >= 5 else 0.4)

    # 3) Antonym pairs (v1 style)
    antonyms = {
        ("like","hate"),("love","hate"),("prefer","avoid"),("yes","no"),
        ("true","false"),("good","bad"),("high","low"),("fast","slow"),
        ("hot","cold"),("start","stop"),("begin","end"),("increase","decrease"),
        ("accept","reject"),("trust","distrust"),("agree","disagree"),
        ("enable","disable"),("allow","deny"),("success","failure"),
        ("easy","hard"),("best","worst"),("support","oppose"),
    }
    for w1, w2 in antonyms:
        if (w1 in a_words and w2 in b_words) or (w2 in a_words and w1 in b_words):
            confidences.append(0.6)
            break

    if not confidences:
        return False, 0.0
    return True, max(confidences)


# ──────────────────────────────────────────────────────────────────────────────
# SCHEMA DEFINITIONS
# ──────────────────────────────────────────────────────────────────────────────

CORE_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id      TEXT PRIMARY KEY,
    node_type    TEXT NOT NULL,
    label        TEXT NOT NULL,
    content      TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'CORE',
    trust_level  REAL NOT NULL DEFAULT 0.5 CHECK (trust_level >= 0 AND trust_level <= 1),
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    importance   REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
    checksum     TEXT NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    CHECK (node_type IN ('PERSON','TOPIC','EVENT','FACT','PREFERENCE','BOUNDARY','AFFECT','AGENT_NOTE','CORE_REF','SKILL'))
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id      TEXT PRIMARY KEY,
    from_node    TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    to_node      TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    edge_type    TEXT NOT NULL,
    weight       REAL NOT NULL DEFAULT 1.0 CHECK (weight >= -1 AND weight <= 1),
    created_at   INTEGER NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    CHECK (edge_type IN ('RELATES_TO','CONTRADICTS','SUPPORTS','CAUSED_BY','PART_OF','TRUSTS','DISTRUSTS','REMEMBERS','HAS_PREFERENCE','HAS_BOUNDARY','HAS_AFFECT','HAS_SKILL','REFERS_TO','SUMMARIZES')),
    UNIQUE(from_node, to_node, edge_type)
);

CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    label, content, content='nodes', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS node_index (
    word     TEXT NOT NULL,
    node_id  TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    field    TEXT NOT NULL DEFAULT 'content',
    weight   REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (word, node_id, field)
);

CREATE TABLE IF NOT EXISTS access_log (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    accessed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- v2 additions
CREATE TABLE IF NOT EXISTS node_vectors (
    node_id   TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE,
    vector    TEXT NOT NULL,
    magnitude REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS memory_layers (
    node_id     TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE,
    layer       TEXT NOT NULL DEFAULT 'short_term',
    promoted_at INTEGER,
    layer_order INTEGER NOT NULL DEFAULT 2,
    CHECK (layer IN ('working','short_term','long_term','archive'))
);

CREATE TABLE IF NOT EXISTS query_log (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    query_text TEXT NOT NULL,
    mode       TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0,
    cache_hit  INTEGER NOT NULL DEFAULT 0,
    queried_at INTEGER NOT NULL
);

-- Triggers for FTS sync
CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO node_fts(rowid, label, content) VALUES (new.rowid, new.label, new.content);
END;
CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO node_fts(node_fts, rowid, label, content) VALUES ('delete', old.rowid, old.label, old.content);
END;
CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO node_fts(node_fts, rowid, label, content) VALUES ('delete', old.rowid, old.label, old.content);
    INSERT INTO node_fts(rowid, label, content) VALUES (new.rowid, new.label, new.content);
END;
"""

AGENT_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id      TEXT PRIMARY KEY,
    node_type    TEXT NOT NULL,
    label        TEXT NOT NULL,
    content      TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'AGENT',
    trust_level  REAL NOT NULL DEFAULT 0.5 CHECK (trust_level >= 0 AND trust_level <= 1),
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    importance   REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
    checksum     TEXT NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    CHECK (node_type IN ('TOPIC','EVENT','FACT','PREFERENCE','AFFECT','AGENT_NOTE','CORE_REF'))
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id      TEXT PRIMARY KEY,
    from_node    TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    to_node      TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    edge_type    TEXT NOT NULL,
    weight       REAL NOT NULL DEFAULT 1.0 CHECK (weight >= -1 AND weight <= 1),
    created_at   INTEGER NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    CHECK (edge_type IN ('RELATES_TO','CONTRADICTS','SUPPORTS','CAUSED_BY','PART_OF','TRUSTS','DISTRUSTS','REMEMBERS','HAS_SKILL','REFERS_TO','SUMMARIZES')),
    UNIQUE(from_node, to_node, edge_type)
);

CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    label, content, content='nodes', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS node_index (
    word     TEXT NOT NULL,
    node_id  TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    field    TEXT NOT NULL DEFAULT 'content',
    weight   REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (word, node_id, field)
);

CREATE TABLE IF NOT EXISTS access_log (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    accessed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# v1 → v2 migration: add node_vectors, memory_layers, query_log tables
V1_TO_V2_MIGRATION = [
    "CREATE TABLE IF NOT EXISTS node_vectors (node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE, vector TEXT NOT NULL, magnitude REAL NOT NULL DEFAULT 0.0);",
    "CREATE TABLE IF NOT EXISTS memory_layers (node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE, layer TEXT NOT NULL DEFAULT 'short_term', promoted_at INTEGER, layer_order INTEGER NOT NULL DEFAULT 2, CHECK (layer IN ('working','short_term','long_term','archive')));",
    "CREATE TABLE IF NOT EXISTS query_log (log_id INTEGER PRIMARY KEY AUTOINCREMENT, query_text TEXT NOT NULL, mode TEXT NOT NULL, result_count INTEGER NOT NULL DEFAULT 0, duration_ms REAL NOT NULL DEFAULT 0, cache_hit INTEGER NOT NULL DEFAULT 0, queried_at INTEGER NOT NULL);",
    "UPDATE schema_meta SET value = '2.0' WHERE key = 'version';",
]


# ──────────────────────────────────────────────────────────────────────────────
# CORE CLASS
# ──────────────────────────────────────────────────────────────────────────────

class AshaMemory:
    """
    ASHA's memory system v2.0. Local, portable, graph-based memory with
    TF-IDF vector retrieval, tiered layers, query DSL, and cross-agent queries.
    """

    def __init__(self, base_path: str = "./asha_memory"):
        self.base_path = Path(base_path).resolve()
        self.core_db_path = self.base_path / "core.db"
        self.agents_dir = self.base_path / "agents"
        self.config_path = self.base_path / "config.json"
        self.backups_dir = self.base_path / "backups"

        self.base_path.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(exist_ok=True)
        self.backups_dir.mkdir(exist_ok=True)

        self.config = self._load_config()

        # v2: in-memory vectorizer + cache (init before _init_core_db for migration)
        self._vectorizer_data: Optional[Dict] = None  # {"version": int, "vectorizer": TfidfVectorizer}
        self._vectorizer_version: int = 0
        self._cache = LRUCache(capacity=self.config.get("cache_capacity", 50))
        self._query_log: List[Dict] = []
        self._cache_hits = 0
        self._cache_misses = 0

        self._init_core_db()
        self._agent_shards: Dict[str, "_AgentShard"] = {}

        # Internal clock: temporal context provider + daily TODAY context node
        self.clock = InternalClock(enabled=bool(self.config.get("internal_clock", True)))
        if self.clock.enabled and self._clock_needs_tick():
            self.clock_tick()

    # ── Config ───────────────────────────────────────────────────────────────

    def _load_config(self) -> Dict[str, Any]:
        merged = dict(DEFAULT_CONFIG)
        if self.config_path.exists():
            with open(self.config_path, "r") as f:
                loaded = json.load(f)
                merged.update(loaded)
        with open(self.config_path, "w") as f:
            json.dump(merged, f, indent=2)
        return merged

    def _save_config(self):
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)

    # ── DB connection ────────────────────────────────────────────────────────

    @contextmanager
    def _core_conn(self):
        conn = sqlite3.connect(str(self.core_db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── Init + migration ─────────────────────────────────────────────────────

    def _init_core_db(self):
        with self._core_conn() as conn:
            conn.executescript(CORE_SCHEMA_V2)
            # Check version and migrate if needed
            cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
            row = cur.fetchone()
            ver = row["value"] if row else "0.0"

            if ver == "1.0":
                for stmt in V1_TO_V2_MIGRATION:
                    conn.execute(stmt)
                # Rebuild node_index and node_vectors for existing v1 nodes
                cursor = conn.execute("SELECT node_id, label, content FROM nodes")
                for row in cursor.fetchall():
                    self._build_index(conn, row["node_id"], row["label"] or "", row["content"] or "")
                    self._compute_and_store_vector(conn, row["node_id"], row["label"] or "", row["content"] or "")
                # Init memory layer for each existing node
                cursor2 = conn.execute("SELECT node_id FROM nodes WHERE node_id NOT IN (SELECT node_id FROM memory_layers)")
                for row in cursor2.fetchall():
                    self._init_node_layer(conn, row["node_id"])
            elif ver == "0.0":
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    ("version", self.config["schema_version"])
                )

            # Lexicon version check — auto-rebuild if tokenizer/stemmer changed
            cur = conn.execute("SELECT value FROM schema_meta WHERE key='lexicon_version'")
            lrow = cur.fetchone()
            stored_lv = int(lrow["value"]) if lrow else 0
            if stored_lv < LEXICON_VERSION:
                cursor = conn.execute("SELECT node_id, label, content FROM nodes")
                for row in cursor.fetchall():
                    self._build_index(conn, row["node_id"], row["label"] or "", row["content"] or "")
                    self._compute_and_store_vector(conn, row["node_id"], row["label"] or "", row["content"] or "")
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    ("lexicon_version", str(LEXICON_VERSION))
                )

    # ── TF-IDF vectorizer ────────────────────────────────────────────────────

    def _invalidate_vectorizer(self):
        """Force rebuild on next _load_vectorizer call."""
        self._vectorizer_data = None
        self._vectorizer_version += 1

    def _build_vectorizer(self, conn: sqlite3.Connection) -> TfidfVectorizer:
        """Pure build — no side effects on self."""
        cursor = conn.execute("SELECT content, label FROM nodes")
        texts = [(r["label"] or "") + " " + (r["content"] or "") for r in cursor.fetchall()]
        v = TfidfVectorizer()
        if texts:
            v.fit(texts)
        return v

    def _load_vectorizer(self, conn: sqlite3.Connection = None) -> TfidfVectorizer:
        """Return cached or freshly built vectorizer.

        Uses version-gated cache: _invalidate_vectorizer() bumps the version
        and clears the cache. Next call rebuilds. Callers always get a valid
        vectorizer; concurrent invalidation produces a fresh build without
        corrupting the cached instance.
        """
        data = self._vectorizer_data
        if data is not None:
            return data["vectorizer"]

        should_close = False
        if conn is None:
            conn = sqlite3.connect(str(self.core_db_path))
            conn.row_factory = sqlite3.Row
            should_close = True

        try:
            v = self._build_vectorizer(conn)
            # Check-then-set: only cache if still invalidated
            if self._vectorizer_data is None:
                self._vectorizer_data = {"version": self._vectorizer_version, "vectorizer": v}
            return v
        finally:
            if should_close:
                conn.close()

    def _get_node_vector(self, conn: sqlite3.Connection, node_id: str) -> Dict[str, float]:
        """Get stored vector for a node, computing it if missing."""
        cur = conn.execute("SELECT vector FROM node_vectors WHERE node_id = ?", (node_id,))
        row = cur.fetchone()
        if row:
            return json.loads(row["vector"])

        # Compute and store
        cur = conn.execute("SELECT content, label FROM nodes WHERE node_id = ?", (node_id,))
        node = cur.fetchone()
        if not node:
            return {}

        text = (node["label"] or "") + " " + (node["content"] or "")
        vec = self._load_vectorizer(conn).transform(text)
        mag = math.sqrt(sum(v * v for v in vec.values()))
        conn.execute(
            "INSERT OR REPLACE INTO node_vectors (node_id, vector, magnitude) VALUES (?, ?, ?)",
            (node_id, json.dumps(vec), mag)
        )
        return vec

    def _compute_and_store_vector(self, conn: sqlite3.Connection, node_id: str, label: str, content: str):
        """Compute TF-IDF vector and store it."""
        text = (label or "") + " " + (content or "")
        vec = self._load_vectorizer(conn).transform(text)
        mag = math.sqrt(sum(v * v for v in vec.values()))
        conn.execute(
            "INSERT OR REPLACE INTO node_vectors (node_id, vector, magnitude) VALUES (?, ?, ?)",
            (node_id, json.dumps(vec), mag)
        )

    def rebuild_vector_index(self):
        """Rebuild all TF-IDF vectors (call after bulk import)."""
        with self._core_conn() as conn:
            # Clear existing vectors
            conn.execute("DELETE FROM node_vectors")
            self._invalidate_vectorizer()
            v = self._load_vectorizer(conn)
            cursor = conn.execute("SELECT node_id, label, content FROM nodes")
            for row in cursor.fetchall():
                text = (row["label"] or "") + " " + (row["content"] or "")
                vec = v.transform(text)
                if vec:
                    mag = math.sqrt(sum(vv * vv for vv in vec.values()))
                    conn.execute(
                        "INSERT OR REPLACE INTO node_vectors (node_id, vector, magnitude) VALUES (?, ?, ?)",
                        (row["node_id"], json.dumps(vec), mag)
                    )

    def vacuum(self) -> Dict[str, Any]:
        """Reclaim freelist space. Call after bulk deletes (brain compaction)."""
        before = self.core_db_path.stat().st_size if self.core_db_path.exists() else 0
        # Need isolated connection (not the WAL-mode _core_conn helper)
        conn = sqlite3.connect(str(self.core_db_path))
        try:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
        after = self.core_db_path.stat().st_size if self.core_db_path.exists() else 0
        self._cache.invalidate()
        return {
            "before_bytes": before,
            "after_bytes": after,
            "saved_bytes": before - after,
            "saved_mb": round((before - after) / (1024 * 1024), 2),
            "before_mb": round(before / (1024 * 1024), 2),
            "after_mb": round(after / (1024 * 1024), 2),
        }

    def get_bloat_info(self) -> Dict[str, Any]:
        """Freelist + ephemeral counts for health dashboards."""
        with self._core_conn() as conn:
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
            eph_counts = {}
            for lbl in EPHEMERAL_LABELS:
                eph_counts[lbl] = conn.execute("SELECT COUNT(*) FROM nodes WHERE label = ?", (lbl,)).fetchone()[0]
            json_logs = conn.execute("SELECT COUNT(*) FROM nodes WHERE content LIKE '{\"%timestamp\"%' OR content LIKE '{%\"load1m\"%'").fetchone()[0] if False else 0
            # simpler: count via python heuristic
            try:
                json_logs = sum(1 for r in conn.execute("SELECT content FROM nodes").fetchall() if _looks_like_json_log(r[0]))
            except Exception:
                json_logs = 0
            contr = conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type='CONTRADICTS'").fetchone()[0]
            return {
                "page_count": page_count,
                "page_size": page_size,
                "freelist_count": freelist,
                "freelist_pct": round((freelist / page_count * 100) if page_count else 0, 1),
                "ephemeral_per_label": {k: v for k, v in eph_counts.items() if v},
                "json_log_nodes": json_logs,
                "contradicts_total": contr,
                "needs_vacuum": freelist > 50 and (freelist / page_count) > 0.15,
            }

    # ── Memory Layers (Phase 3) ─────────────────────────────────────────────

    LAYER_ORDER = {"working": 1, "short_term": 2, "long_term": 3, "archive": 4}

    def _init_node_layer(self, conn: sqlite3.Connection, node_id: str):
        """New nodes start in working memory."""
        now = _now()
        conn.execute(
            "INSERT OR IGNORE INTO memory_layers (node_id, layer, promoted_at, layer_order) VALUES (?, 'working', ?, 1)",
            (node_id, now)
        )

    def _update_layer_on_access(self, conn: sqlite3.Connection, node_id: str):
        """Promote node based on access frequency."""
        cur = conn.execute("SELECT layer, access_count FROM nodes n JOIN memory_layers ml ON n.node_id = ml.node_id WHERE n.node_id = ?", (node_id,))
        row = cur.fetchone()
        if not row:
            self._init_node_layer(conn, node_id)
            return

        layer = row["layer"]
        access_count = row["access_count"]

        if layer == "working" and access_count >= self.config.get("short_term_promote_after", 3):
            conn.execute(
                "UPDATE memory_layers SET layer = 'short_term', promoted_at = ?, layer_order = 2 WHERE node_id = ?",
                (_now(), node_id)
            )
        elif layer == "short_term" and access_count >= self.config.get("long_term_promote_after", 15):
            conn.execute(
                "UPDATE memory_layers SET layer = 'long_term', promoted_at = ?, layer_order = 3 WHERE node_id = ?",
                (_now(), node_id)
            )
        elif layer == "working" and access_count == 0:
            # Check if working memory is full — evict oldest if needed
            cap = self.config.get("working_memory_capacity", 20)
            count = conn.execute("SELECT COUNT(*) as c FROM memory_layers WHERE layer = 'working'").fetchone()["c"]
            if count > cap:
                oldest = conn.execute(
                    "SELECT node_id FROM memory_layers WHERE layer = 'working' ORDER BY promoted_at ASC LIMIT 1"
                ).fetchone()
                if oldest:
                    conn.execute(
                        "UPDATE memory_layers SET layer = 'short_term', promoted_at = ?, layer_order = 2 WHERE node_id = ?",
                        (_now(), oldest["node_id"])
                    )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _build_index(self, conn: sqlite3.Connection, node_id: str, label: str, content: str):
        keywords = _extract_keywords(label + " " + content)
        for word, weight in keywords:
            conn.execute(
                "INSERT OR REPLACE INTO node_index (word, node_id, field, weight) VALUES (?, ?, ?, ?)",
                (word, node_id, "content", weight)
            )

    def _node_from_row(self, row, edges: List[Dict] = None) -> MemoryNode:
        return MemoryNode(
            node_id=row["node_id"],
            node_type=row["node_type"],
            label=row["label"],
            content=row["content"],
            source=row["source"],
            trust_level=row["trust_level"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            access_count=row["access_count"],
            importance=row["importance"],
            checksum=row["checksum"],
            metadata=json.loads(row["metadata"]),
            edges=edges or []
        )

    def _bump_access(self, conn: sqlite3.Connection, node_id: str):
        conn.execute(
            "UPDATE nodes SET access_count = access_count + 1, updated_at = ? WHERE node_id = ?",
            (_now(), node_id)
        )
        conn.execute(
            "INSERT INTO access_log (node_id, accessed_at) VALUES (?, ?)",
            (node_id, _now())
        )
        self._update_layer_on_access(conn, node_id)

    def _auto_link(self, conn: sqlite3.Connection, new_node_id: str, content: str, label: str):
        # Ephemeral telemetry logs should not sprout RELATES_TO edges — they are not knowledge
        if label in EPHEMERAL_LABELS or _looks_like_json_log(content):
            return
        keywords = set(w for w, _ in _extract_keywords(content + " " + label))
        if not keywords:
            return
        # Determine scope of the new node (core vs agent-note) to prevent cross-scope contamination
        try:
            new_row = conn.execute("SELECT node_type, metadata FROM nodes WHERE node_id = ?", (new_node_id,)).fetchone()
            new_meta = json.loads(new_row["metadata"]) if new_row and new_row["metadata"] else {}
            new_is_agent = (new_row["node_type"] == "AGENT_NOTE" and new_meta.get("attention_state") != "core_verified") or bool(new_meta.get("agent_scoped") and new_meta.get("attention_state") != "core_verified")
        except Exception:
            new_is_agent = False
        placeholders = ",".join("?" * len(keywords))
        cursor = conn.execute(f"""
            SELECT node_id, COUNT(*) as overlap
            FROM node_index
            WHERE word IN ({placeholders}) AND node_id != ?
            GROUP BY node_id
            HAVING overlap >= 2
            ORDER BY overlap DESC
            LIMIT 10
        """, (*keywords, new_node_id))
        for row in cursor.fetchall():
            existing_id = row["node_id"]
            # Skip cross-scope links (core ↔ agent) and ephemeral targets
            try:
                cand = conn.execute("SELECT node_type, label, content, metadata FROM nodes WHERE node_id = ?", (existing_id,)).fetchone()
                if cand:
                    if cand["label"] in EPHEMERAL_LABELS or _looks_like_json_log(cand["content"]):
                        continue
                    cand_meta = json.loads(cand["metadata"]) if cand["metadata"] else {}
                    cand_is_agent = (cand["node_type"] == "AGENT_NOTE" and cand_meta.get("attention_state") != "core_verified") or bool(cand_meta.get("agent_scoped") and cand_meta.get("attention_state") != "core_verified")
                    if cand_is_agent != new_is_agent:
                        continue
            except Exception:
                pass
            overlap = row["overlap"]
            weight = min(overlap * 0.2, 1.0)
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO edges
                       (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (_edge_uuid(), new_node_id, existing_id, "RELATES_TO", weight, _now(), "{}")
                )
            except sqlite3.IntegrityError:
                pass

    def _check_contradictions(self, conn: sqlite3.Connection, new_node: MemoryNode):
        if new_node.node_type != "FACT":
            return []
        # Telemetry logs are not knowledge — never contradictions
        if new_node.label in EPHEMERAL_LABELS or _looks_like_json_log(new_node.content):
            return []
        keywords = [w for w, _ in _extract_keywords(new_node.content)[:5]]
        if not keywords:
            return []
        placeholders = ",".join("?" * len(keywords))
        cursor = conn.execute(f"""
            SELECT n.node_id, n.content, n.label
            FROM nodes n
            JOIN node_index idx ON n.node_id = idx.node_id
            WHERE n.node_type = 'FACT' AND n.node_id != ? AND idx.word IN ({placeholders})
            GROUP BY n.node_id
            LIMIT 20
        """, (new_node.node_id, *keywords))
        contradictions = []
        for row in cursor.fetchall():
            # skip ephemeral candidates — JSON shared keys cause false positives
            if row["label"] in EPHEMERAL_LABELS or _looks_like_json_log(row["content"]):
                continue
            is_contra, conf = _detect_contradiction_v2(new_node.content, row["content"])
            if is_contra and conf > 0.5:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO edges
                           (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (_edge_uuid(), new_node.node_id, row["node_id"], "CONTRADICTS", -conf, _now(),
                         json.dumps({"auto_detected": True, "confidence": conf, "method": "v2"}))
                    )
                except sqlite3.IntegrityError:
                    pass
                for nid in (new_node.node_id, row["node_id"]):
                    meta_cursor = conn.execute("SELECT metadata FROM nodes WHERE node_id = ?", (nid,))
                    meta_row = meta_cursor.fetchone()
                    if meta_row:
                        meta = json.loads(meta_row["metadata"])
                        meta["contradiction_flag"] = True
                        meta["contradiction_pair"] = row["node_id"] if nid == new_node.node_id else new_node.node_id
                        conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?", (json.dumps(meta), nid))
                contradictions.append({"node_id": row["node_id"], "label": row["label"], "confidence": conf})
        return contradictions

    def _resolve_node_ref(self, conn: sqlite3.Connection, ref: str) -> Optional[str]:
        """Resolve a label or ID to a node_id."""
        # Try as direct ID first
        cur = conn.execute("SELECT node_id FROM nodes WHERE node_id = ?", (ref,))
        row = cur.fetchone()
        if row:
            return row["node_id"]
        # Try as label
        cur = conn.execute("SELECT node_id FROM nodes WHERE label = ? LIMIT 1", (ref,))
        row = cur.fetchone()
        if row:
            return row["node_id"]
        # Try label like
        cur = conn.execute("SELECT node_id FROM nodes WHERE label LIKE ? LIMIT 1", (f"%{ref}%",))
        row = cur.fetchone()
        if row:
            return row["node_id"]
        return None

    def _get_neighbors(self, conn: sqlite3.Connection, node_id: str) -> List[Tuple[str, float]]:
        """Get (neighbor_id, weight) pairs."""
        cursor = conn.execute("""
            SELECT CASE WHEN from_node = ? THEN to_node ELSE from_node END as nid, weight
            FROM edges WHERE from_node = ? OR to_node = ?
        """, (node_id, node_id, node_id))
        return [(row["nid"], row["weight"]) for row in cursor.fetchall()]

    # ── CORE MEMORY OPERATIONS ───────────────────────────────────────────────

    def remember(self, content: str, node_type: str, label: str = None,
                 source: str = "CORE", trust: float = None,
                 importance: float = None, metadata: Dict = None) -> str:
        if node_type not in NODE_TYPES:
            raise ValueError(f"Invalid node_type: {node_type}")
        max_len = self.config["max_content_length"]
        if len(content) > max_len:
            content = content[:max_len-3] + "..."
        label = label or content[:30]
        trust = trust if trust is not None else self.config["default_trust"]
        importance = importance if importance is not None else self.config["default_importance"]
        metadata = metadata or {}
        node_id = _uuid()
        now = _now()
        checksum = _checksum(content)

        with self._core_conn() as conn:
            conn.execute(
                """INSERT INTO nodes
                   (node_id, node_type, label, content, source, trust_level,
                    created_at, updated_at, access_count, importance, checksum, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (node_id, node_type, label, content, source, trust,
                 now, now, 0, importance, checksum, json.dumps(metadata))
            )
            self._build_index(conn, node_id, label, content)
            if not metadata.get("clock_node"):
                self._auto_link(conn, node_id, content, label)
            # v2: store TF-IDF vector + init memory layer
            self._invalidate_vectorizer()
            self._compute_and_store_vector(conn, node_id, label, content)
            self._init_node_layer(conn, node_id)

            if node_type == "FACT":
                new_node = self._node_from_row(
                    conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
                )
                contradictions = self._check_contradictions(conn, new_node)
                if contradictions:
                    meta = json.loads(conn.execute(
                        "SELECT metadata FROM nodes WHERE node_id = ?", (node_id,)
                    ).fetchone()["metadata"])
                    meta["contradictions_detected"] = contradictions
                    conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?", (json.dumps(meta), node_id))

        # Invalidate cache on write
        self._cache.invalidate()
        return node_id

    def recall(self, query: str, mode: str = "RELATED", bound: int = None,
               include_agent_notes: bool = False) -> RecallResult:
        """
        Retrieve memories. Modes:
          WHO_IS     — 1-hop from PERSON
          WHAT_ABOUT — 2-hop from TOPIC
          RECENT     — temporal slice
          RELATED    — keyword match (v1 default)
          SEMANTIC   — TF-IDF cosine similarity (v2)
          PATH       — shortest weighted path between two nodes (v2)
          CLUSTER    — BFS neighborhood grouped by type (v2)
          TIMELINE   — chronological events connected to a node (v2)
          PRUNE      — low-importance candidates
        """
        valid_modes = ("WHO_IS","WHAT_ABOUT","RECENT","RELATED","PRUNE","SEMANTIC","PATH","CLUSTER","TIMELINE")
        if mode not in valid_modes:
            raise ValueError(f"Invalid mode: {mode}")

        bound = bound or self.config["max_nodes_per_recall"]
        start_time = time.time()
        cache_hit = False

        # Check cache
        cache_key = f"{mode}:{query}:{bound}:agent_notes={include_agent_notes}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache_hits += 1
            cache_hit = True
            self._log_query(query, mode, len(cached.nodes), 0, cache_hit)
            # Refresh temporal summaries on cache hits so ages stay honest.
            if self.clock and self.clock.enabled:
                with self._core_conn() as conn:
                    self._apply_clock_summaries(conn, cached.nodes, _now())
            return cached

        self._cache_misses += 1
        nodes = []
        total_found = 0

        # Fetch extra candidates before visibility filtering, so raw agent work
        # cannot crowd the main AI's bounded context window.
        fetch_bound = bound if include_agent_notes else max(bound * 5, bound + 25)

        with self._core_conn() as conn:
            if mode == "WHO_IS":
                nodes, total_found = self._recall_who_is(conn, query, fetch_bound)
            elif mode == "WHAT_ABOUT":
                nodes, total_found = self._recall_what_about(conn, query, fetch_bound)
            elif mode == "RECENT":
                nodes, total_found = self._recall_recent(conn, query, fetch_bound)
            elif mode == "RELATED":
                nodes, total_found = self._recall_related(conn, query, fetch_bound)
            elif mode == "SEMANTIC":
                nodes, total_found = self._recall_semantic(conn, query, fetch_bound)
            elif mode == "PATH":
                nodes, total_found = self._recall_path(conn, query, fetch_bound)
            elif mode == "CLUSTER":
                nodes, total_found = self._recall_cluster(conn, query, fetch_bound)
            elif mode == "TIMELINE":
                nodes, total_found = self._recall_timeline(conn, query, fetch_bound)
            elif mode == "PRUNE":
                nodes, total_found = self._recall_prune(conn, query, fetch_bound)

            # Internal clock: attach temporal summaries before the block closes.
            if self.clock and self.clock.enabled:
                self._apply_clock_summaries(conn, nodes, int(start_time))

        if not include_agent_notes:
            nodes = [node for node in nodes if self._is_core_visible(node)]
        total_found = len(nodes)
        nodes = nodes[:bound]

        result = RecallResult(query=query, mode=mode, nodes=nodes, total_found=total_found, bound_applied=total_found > bound)
        duration = (time.time() - start_time) * 1000
        self._cache.put(cache_key, result)
        self._log_query(query, mode, len(nodes), duration, cache_hit)
        return result

    def _is_core_visible(self, node: MemoryNode) -> bool:
        """Return whether a node belongs in the main AI's normal recall scope."""
        if node.metadata.get("attention_state") == "core_verified":
            return True
        return node.node_type != "AGENT_NOTE" and not node.metadata.get("agent_scoped", False)

    def _log_query(self, query_text: str, mode: str, count: int, duration_ms: float, cache_hit: bool):
        self._query_log.append({
            "query": query_text, "mode": mode, "count": count,
            "ms": duration_ms, "cache_hit": cache_hit, "time": _now()
        })
        if len(self._query_log) > 100:
            self._query_log = self._query_log[-100:]
        try:
            with self._core_conn() as conn:
                conn.execute(
                    "INSERT INTO query_log (query_text, mode, result_count, duration_ms, cache_hit, queried_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (query_text[:200], mode, count, duration_ms, 1 if cache_hit else 0, _now())
                )
        except sqlite3.Error:
            pass

    def _recall_who_is(self, conn, label, bound):
        cursor = conn.execute(
            "SELECT * FROM nodes WHERE node_type = 'PERSON' AND (label = ? OR label LIKE ?)",
            (label, f"%{label}%")
        )
        person_row = cursor.fetchone()
        if not person_row:
            cursor = conn.execute(
                "SELECT * FROM nodes WHERE node_id IN (SELECT rowid FROM node_fts WHERE node_fts MATCH ? LIMIT 1)",
                (label,)
            )
            person_row = cursor.fetchone()
        if not person_row:
            return [], 0
        person_id = person_row["node_id"]
        self._bump_access(conn, person_id)
        cursor = conn.execute("""
            SELECT n.*, e.edge_type, e.weight as edge_weight, e.metadata as edge_metadata
            FROM nodes n
            JOIN edges e ON (n.node_id = e.to_node AND e.from_node = ?)
                         OR (n.node_id = e.from_node AND e.to_node = ?)
            WHERE n.node_id != ?
            ORDER BY (n.importance * n.trust_level) DESC
            LIMIT ?
        """, (person_id, person_id, person_id, bound - 1))
        edges = []
        neighbor_rows = []
        for row in cursor.fetchall():
            neighbor_rows.append(row)
            edges.append({"edge_type": row["edge_type"], "weight": row["edge_weight"], "metadata": json.loads(row["edge_metadata"])})
        person = self._node_from_row(person_row)
        result = [person]
        for i, row in enumerate(neighbor_rows):
            nid = row["node_id"]
            self._bump_access(conn, nid)
            node = self._node_from_row(row, edges=[edges[i]] if i < len(edges) else [])
            result.append(node)
        cursor = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            JOIN edges e ON (n.node_id = e.to_node AND e.from_node = ?)
                         OR (n.node_id = e.from_node AND e.to_node = ?)
            WHERE n.node_id != ?
        """, (person_id, person_id, person_id))
        return result, cursor.fetchone()["c"] + 1

    def _recall_what_about(self, conn, query, bound):
        cursor = conn.execute(
            "SELECT * FROM nodes WHERE node_type = 'TOPIC' AND (label = ? OR label LIKE ?)",
            (query, f"%{query}%")
        )
        topic_row = cursor.fetchone()
        if not topic_row:
            cursor = conn.execute(
                "SELECT * FROM nodes WHERE node_id IN (SELECT rowid FROM node_fts WHERE node_fts MATCH ? LIMIT 1)",
                (query,)
            )
            topic_row = cursor.fetchone()
        if not topic_row:
            return [], 0
        topic_id = topic_row["node_id"]
        self._bump_access(conn, topic_id)
        cursor = conn.execute("""
            WITH RECURSIVE hop1 AS (
                SELECT n.*, e.edge_type, e.weight as edge_weight, 1 as hop
                FROM nodes n JOIN edges e ON (n.node_id=e.to_node AND e.from_node=?) OR (n.node_id=e.from_node AND e.to_node=?)
                WHERE n.node_id != ?
            ), hop2 AS (
                SELECT n.*, e.edge_type, e.weight as edge_weight, 2 as hop
                FROM nodes n JOIN edges e ON (n.node_id=e.to_node)
                JOIN hop1 h ON e.from_node = h.node_id
                WHERE n.node_id != ? AND n.node_id NOT IN (SELECT node_id FROM hop1)
            )
            SELECT * FROM (SELECT * FROM hop1 UNION ALL SELECT * FROM hop2) combined
            ORDER BY (combined.importance * combined.trust_level) DESC LIMIT ?
        """, (topic_id, topic_id, topic_id, topic_id, bound - 1))
        result = [self._node_from_row(topic_row)]
        seen = {topic_id}
        for row in cursor.fetchall():
            nid = row["node_id"]
            if nid not in seen:
                seen.add(nid)
                self._bump_access(conn, nid)
                result.append(self._node_from_row(row))
        cursor = conn.execute("""
            SELECT COUNT(DISTINCT n.node_id) as c FROM nodes n
            JOIN edges e1 ON (n.node_id=e1.to_node AND e1.from_node=?) OR (n.node_id=e1.from_node AND e1.to_node=?)
            WHERE n.node_id != ?
        """, (topic_id, topic_id, topic_id))
        return result, cursor.fetchone()["c"] + 1

    def _recall_recent(self, conn, hours_str, bound):
        try: hours = int(hours_str)
        except: hours = 24
        since = _now() - (hours * 3600)
        cursor = conn.execute("SELECT * FROM nodes WHERE updated_at > ? ORDER BY updated_at DESC LIMIT ?", (since, bound))
        rows = cursor.fetchall()
        result = []
        for row in rows:
            self._bump_access(conn, row["node_id"])
            result.append(self._node_from_row(row))
        cursor = conn.execute("SELECT COUNT(*) as c FROM nodes WHERE updated_at > ?", (since,))
        return result, cursor.fetchone()["c"]

    def _recall_related(self, conn, query, bound):
        keywords = [w for w, _ in _extract_keywords(query)]
        if not keywords:
            cursor = conn.execute("SELECT * FROM nodes WHERE rowid IN (SELECT rowid FROM node_fts WHERE node_fts MATCH ? LIMIT ?)", (query, bound))
            rows = cursor.fetchall()
            result = []
            for row in rows:
                self._bump_access(conn, row["node_id"])
                result.append(self._node_from_row(row))
            return result, len(rows)
        placeholders = ",".join("?" * len(keywords))
        cursor = conn.execute(f"""
            SELECT n.*, COUNT(idx.word) as match_count, SUM(idx.weight) as relevance
            FROM nodes n JOIN node_index idx ON n.node_id = idx.node_id
            WHERE idx.word IN ({placeholders})
            GROUP BY n.node_id
            ORDER BY (match_count * relevance * n.importance * n.trust_level) DESC LIMIT ?
        """, (*keywords, bound))
        rows = cursor.fetchall()
        result = []
        for row in rows:
            self._bump_access(conn, row["node_id"])
            result.append(self._node_from_row(row))
        cursor = conn.execute(f"""
            SELECT COUNT(DISTINCT n.node_id) as c FROM nodes n
            JOIN node_index idx ON n.node_id = idx.node_id WHERE idx.word IN ({placeholders})
        """, (*keywords,))
        return result, cursor.fetchone()["c"]

    def _recall_prune(self, conn, threshold_str, bound):
        try: threshold = float(threshold_str)
        except: threshold = self.config["prune_threshold"]
        old = _now() - (30 * 24 * 3600)
        cursor = conn.execute(
            "SELECT * FROM nodes WHERE importance < ? AND access_count < 3 AND updated_at < ? ORDER BY importance ASC, updated_at ASC LIMIT ?",
            (threshold, old, bound)
        )
        rows = cursor.fetchall()
        result = [self._node_from_row(row) for row in rows]
        cursor = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE importance < ? AND access_count < 3 AND updated_at < ?",
            (threshold, old)
        )
        return result, cursor.fetchone()["c"]

    # ── V2: SEMANTIC mode (Phase 2) ──────────────────────────────────────────

    def _recall_semantic(self, conn, query_text, bound):
        v = self._load_vectorizer(conn)
        query_vec = v.transform(query_text)
        if not query_vec:
            return [], 0

        floor = self.config.get("semantic_relevance_floor", 0.1)
        cursor = conn.execute("""
            SELECT n.*, nv.vector, nv.magnitude
            FROM nodes n
            LEFT JOIN node_vectors nv ON n.node_id = nv.node_id
        """)
        scored = []
        for row in cursor.fetchall():
            node_vec_str = row["vector"] if isinstance(row, sqlite3.Row) else (row.get("vector") if isinstance(row, dict) else None)
            node_vec = json.loads(node_vec_str) if node_vec_str else {}
            sim = v.cosine_similarity(query_vec, node_vec)
            if sim >= floor:
                scored.append((sim, row))
        scored.sort(key=lambda x: -x[0])
        result = []
        for sim, row in scored[:bound]:
            self._bump_access(conn, row["node_id"])
            node = self._node_from_row(row)
            node.metadata["_similarity"] = round(sim, 4)
            result.append(node)
        return result, len(scored)

    # ── V2: PATH mode (Phase 2) ──────────────────────────────────────────────

    def _recall_path(self, conn, query, bound):
        parts = query.split("->")
        if len(parts) != 2:
            return [], 0
        start_id = self._resolve_node_ref(conn, parts[0].strip())
        end_id = self._resolve_node_ref(conn, parts[1].strip())
        if not start_id or not end_id:
            return [], 0

        # Dijkstra
        distances = {start_id: 0}
        prev = {}
        unvisited = {start_id}
        visited = set()

        while unvisited:
            current = min(unvisited, key=lambda x: distances.get(x, float('inf')))
            unvisited.remove(current)
            visited.add(current)
            if current == end_id:
                break
            for nid, weight in self._get_neighbors(conn, current):
                if nid in visited:
                    continue
                dist = distances[current] + (1.0 - weight)
                if dist < distances.get(nid, float('inf')):
                    distances[nid] = dist
                    prev[nid] = current
                    unvisited.add(nid)

        if end_id not in prev and start_id != end_id:
            return [], 0

        # Reconstruct
        path_ids = []
        current = end_id
        while current in prev:
            path_ids.append(current)
            current = prev[current]
        path_ids.append(start_id)
        path_ids.reverse()

        result = []
        for nid in path_ids[:bound]:
            self._bump_access(conn, nid)
            row = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (nid,)).fetchone()
            if row:
                result.append(self._node_from_row(row))
        return result, len(path_ids)

    # ── V2: CLUSTER mode (Phase 2) ───────────────────────────────────────────

    def _recall_cluster(self, conn, query, bound):
        seed_id = self._resolve_node_ref(conn, query)
        if not seed_id:
            return [], 0
        seen = {seed_id}
        queue = [seed_id]
        cluster = []
        while queue and len(cluster) < bound:
            current = queue.pop(0)
            row = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (current,)).fetchone()
            if row:
                self._bump_access(conn, current)
                cluster.append(self._node_from_row(row))
            for nid, _ in self._get_neighbors(conn, current):
                if nid not in seen:
                    seen.add(nid)
                    queue.append(nid)
        return cluster, len(seen)

    # ── V2: TIMELINE mode (Phase 2) ──────────────────────────────────────────

    def _recall_timeline(self, conn, query, bound):
        """Return chronologically ordered EVENT nodes connected to a PERSON/TOPIC."""
        node_id = self._resolve_node_ref(conn, query)
        if not node_id:
            return [], 0
        cursor = conn.execute("""
            SELECT DISTINCT n.* FROM nodes n
            JOIN edges e ON (n.node_id = e.to_node AND e.from_node = ?)
                         OR (n.node_id = e.from_node AND e.to_node = ?)
            WHERE n.node_type = 'EVENT'
            ORDER BY n.created_at DESC
            LIMIT ?
        """, (node_id, node_id, bound))
        rows = cursor.fetchall()
        result = []
        for row in rows:
            self._bump_access(conn, row["node_id"])
            result.append(self._node_from_row(row))
        cursor = conn.execute("""
            SELECT COUNT(DISTINCT n.node_id) as c FROM nodes n
            JOIN edges e ON (n.node_id = e.to_node AND e.from_node = ?)
                         OR (n.node_id = e.from_node AND e.to_node = ?)
            WHERE n.node_type = 'EVENT'
        """, (node_id, node_id))
        return result, cursor.fetchone()["c"]

    # ── Shorthand methods ────────────────────────────────────────────────────

    def get_person(self, label: str) -> Optional[Dict[str, Any]]:
        result = self.recall(label, mode="WHO_IS", bound=20)
        if not result.nodes:
            return None
        return {"person": result.nodes[0].to_dict(), "related": [n.to_dict() for n in result.nodes[1:]], "total_found": result.total_found, "bound_applied": result.bound_applied}

    def get_topic(self, label: str) -> Optional[Dict[str, Any]]:
        result = self.recall(label, mode="WHAT_ABOUT", bound=30)
        if not result.nodes:
            return None
        return {"topic": result.nodes[0].to_dict(), "related": [n.to_dict() for n in result.nodes[1:]], "total_found": result.total_found, "bound_applied": result.bound_applied}

    def recent(self, hours: int = 24) -> List[Dict[str, Any]]:
        result = self.recall(str(hours), mode="RECENT", bound=15)
        return [n.to_dict() for n in result.nodes]

    def relate(self, from_id: str, to_id: str, edge_type: str, weight: float = 1.0, metadata: Dict = None) -> str:
        if edge_type not in EDGE_TYPES:
            raise ValueError(f"Invalid edge_type: {edge_type}")
        edge_id = _edge_uuid()
        with self._core_conn() as conn:
            from_ok = conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (from_id,)).fetchone()
            to_ok = conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (to_id,)).fetchone()
            if not from_ok or not to_ok:
                missing = from_id if not from_ok else to_id
                raise ValueError(f"relate(): cannot create edge '{edge_type}' — node '{missing}' does not exist")
            conn.execute(
                """INSERT OR REPLACE INTO edges
                   (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (edge_id, from_id, to_id, edge_type, weight, _now(), json.dumps(metadata or {}))
            )
        self._cache.invalidate()
        return edge_id

    def delete(self, node_id: str) -> bool:
        with self._core_conn() as conn:
            cursor = conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
            self._cache.invalidate()
            return cursor.rowcount > 0

    def update_trust(self, node_id: str, new_trust: float) -> bool:
        with self._core_conn() as conn:
            cursor = conn.execute("UPDATE nodes SET trust_level = ?, updated_at = ? WHERE node_id = ?", (max(0, min(1, new_trust)), _now(), node_id))
            self._cache.invalidate()
            return cursor.rowcount > 0

    def update_importance(self, node_id: str, new_importance: float) -> bool:
        with self._core_conn() as conn:
            cursor = conn.execute("UPDATE nodes SET importance = ?, updated_at = ? WHERE node_id = ?", (max(0, min(1, new_importance)), _now(), node_id))
            self._cache.invalidate()
            return cursor.rowcount > 0

    def get_node(self, node_id: str) -> Optional[MemoryNode]:
        with self._core_conn() as conn:
            cursor = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
            row = cursor.fetchone()
            if row:
                before = _now()
                self._bump_access(conn, row["node_id"])
                node = self._node_from_row(row)
                if self.clock and self.clock.enabled:
                    self._apply_clock_summaries(conn, [node], before)
                return node
            return None

    # ── Internal Clock (temporal context) ────────────────────────────────────

    def _apply_clock_summaries(self, conn: sqlite3.Connection, nodes: List[MemoryNode],
                               before_epoch: int):
        """Attach _clock temporal summaries to nodes.

        last_checked is read from access_log strictly before before_epoch so a
        node just returned by this very query still reports its previous check.
        """
        for node in nodes:
            last = self.clock.last_accessed_before(conn, node.node_id, before_epoch)
            row = conn.execute(
                "SELECT access_count FROM nodes WHERE node_id = ?", (node.node_id,)
            ).fetchone()
            count = row["access_count"] if row else node.access_count
            node.metadata["_clock"] = self.clock.summarize_node(
                node, last_accessed=last, access_count=count)

    def _clock_needs_tick(self) -> bool:
        """True when the TODAY context node is missing or dated yesterday."""
        today = self.clock.now()["date"]
        with self._core_conn() as conn:
            row = conn.execute(
                "SELECT metadata FROM nodes WHERE node_type = 'EVENT' AND label = 'TODAY'"
            ).fetchone()
        if not row:
            return True
        meta = json.loads(row["metadata"])
        return meta.get("date") != today

    def clock_tick(self) -> Optional[str]:
        """Create or refresh the TODAY context node (date/time + daily activity).

        The node is a regular EVENT node (label TODAY, source CLOCK) so ordinary
        recall answers "what is today's date / what happened today" from the
        memory graph itself. Returns the node_id or None when clock is disabled.
        """
        if not self.clock or not self.clock.enabled:
            return None
        with self._core_conn() as conn:
            summary = self.clock.today_summary(conn)
            row = conn.execute(
                "SELECT node_id, metadata FROM nodes WHERE node_type = 'EVENT' AND label = 'TODAY'"
            ).fetchone()
        content = self.clock.build_tick_content(summary)
        if row:
            meta = json.loads(row["metadata"])
            if not meta.get("clock_node"):
                return None  # a user-owned node claims the TODAY label — leave it
            meta["date"] = summary["date"]
            meta["tick_at"] = summary["epoch"]
            with self._core_conn() as conn:
                conn.execute(
                    "UPDATE nodes SET content = ?, updated_at = ?, metadata = ? WHERE node_id = ?",
                    (content, _now(), json.dumps(meta), row["node_id"])
                )
                self._build_index(conn, row["node_id"], "TODAY", content)
                self._compute_and_store_vector(conn, row["node_id"], "TODAY", content)
            self._invalidate_vectorizer()
            self._cache.invalidate()
            return row["node_id"]
        return self.remember(content, node_type="EVENT", label="TODAY", source="CLOCK",
                             trust=1.0, importance=0.5,
                             metadata={"clock_node": True, "date": summary["date"],
                                       "tick_at": summary["epoch"]})

    def clock_now(self) -> Optional[Dict[str, Any]]:
        """Current date/time snapshot from the internal clock."""
        return self.clock.now() if self.clock and self.clock.enabled else None

    # ── Query DSL (Phase 5) ──────────────────────────────────────────────────

    def query(self, query_str: str, bound: int = None) -> RecallResult:
        """Query DSL entry point. Parses query_str and dispatches to appropriate mode."""
        pq = parse_query(query_str)
        return self.recall(pq.source, mode=pq.mode, bound=bound or pq.bound)

    # ── AGENT OPERATIONS ─────────────────────────────────────────────────────

    def _agent_memory_mode(self) -> str:
        mode = self.config.get("agent_memory_mode", "core_shared")
        if mode not in {"core_shared", "legacy_shards"}:
            raise ValueError("agent_memory_mode must be 'core_shared' or 'legacy_shards'")
        return mode

    def _agent_metadata(self, agent_id: str, metadata: Dict = None,
                        attention_state: str = "agent_private") -> Dict:
        if attention_state not in {"agent_private", "review_ready", "core_verified"}:
            raise ValueError("Invalid agent attention_state")
        result = dict(metadata or {})
        result.update({
            "agent_id": agent_id,
            "agent_scoped": True,
            "attention_state": attention_state,
        })
        return result

    def _shared_agent_node(self, agent_id: str, node_id: str) -> Optional[MemoryNode]:
        node = self.get_node(node_id)
        if not node or node.metadata.get("agent_id") != agent_id:
            return None
        return node

    def spawn_agent_memory(self, agent_id: str) -> str:
        """Prepare agent memory without creating a database in core_shared mode."""
        if self._agent_memory_mode() == "core_shared":
            return f"core://agents/{agent_id}"
        agent_db_path = self.agents_dir / f"agent_{agent_id}.db"
        if agent_db_path.exists():
            return str(agent_db_path)
        conn = sqlite3.connect(str(agent_db_path))
        conn.executescript(AGENT_SCHEMA_V2)
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)", ("agent_id", agent_id))
        conn.commit()
        conn.close()
        self._agent_shards[agent_id] = _AgentShard(str(agent_db_path), agent_id, self.config)
        return str(agent_db_path)

    def _get_agent_shard(self, agent_id: str) -> "_AgentShard":
        if agent_id not in self._agent_shards:
            agent_db_path = self.agents_dir / f"agent_{agent_id}.db"
            if not agent_db_path.exists():
                self.spawn_agent_memory(agent_id)
            self._agent_shards[agent_id] = _AgentShard(str(agent_db_path), agent_id, self.config)
        return self._agent_shards[agent_id]

    def agent_remember(self, agent_id: str, content: str, label: str = None,
                       node_type: str = "AGENT_NOTE", metadata: Dict = None,
                       attention_state: str = "agent_private") -> str:
        if self._agent_memory_mode() == "core_shared":
            if node_type not in {"TOPIC", "EVENT", "FACT", "PREFERENCE", "AFFECT", "AGENT_NOTE", "CORE_REF"}:
                raise ValueError(f"Agent cannot write node_type: {node_type}")
            max_len = self.config.get("agent_max_content_length", 800)
            if len(content) > max_len:
                content = content[:max_len-3] + "..."
            # Raw agent work is always stored as AGENT_NOTE. The caller may use
            # richer types only after an explicit promotion/review decision.
            return self.remember(
                content=content, node_type="AGENT_NOTE", label=label,
                source=f"AGENT_{agent_id}", trust=0.5, importance=0.5,
                metadata=self._agent_metadata(agent_id, metadata, attention_state)
            )
        return self._get_agent_shard(agent_id).remember(content, label, node_type, metadata)

    def agent_digest(self, agent_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        if self._agent_memory_mode() == "core_shared":
            with self._core_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE source = ? ORDER BY updated_at DESC LIMIT ?",
                    (f"AGENT_{agent_id}", limit)
                ).fetchall()
            return [self._node_from_row(row).to_dict() for row in rows]
        return self._get_agent_shard(agent_id).get_recent_notes(limit)

    def agent_review_queue(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return agent findings explicitly marked as ready for core review."""
        if self._agent_memory_mode() == "legacy_shards":
            return []
        with self._core_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE node_type = 'AGENT_NOTE' ORDER BY updated_at DESC"
            ).fetchall()
        queued = []
        for row in rows:
            node = self._node_from_row(row)
            if node.metadata.get("attention_state") == "review_ready":
                queued.append(node.to_dict())
                if len(queued) >= limit:
                    break
        return queued

    def agent_set_attention(self, agent_id: str, agent_node_id: str,
                            attention_state: str) -> bool:
        """Move a shared agent note between private and review-ready states."""
        if self._agent_memory_mode() == "legacy_shards":
            raise ValueError("Agent attention states require agent_memory_mode='core_shared'")
        if attention_state not in {"agent_private", "review_ready"}:
            raise ValueError("attention_state must be 'agent_private' or 'review_ready'")
        node = self._shared_agent_node(agent_id, agent_node_id)
        if not node or node.node_type != "AGENT_NOTE":
            return False
        meta = dict(node.metadata)
        meta["attention_state"] = attention_state
        meta["attention_updated_at"] = _now()
        with self._core_conn() as conn:
            conn.execute("UPDATE nodes SET metadata = ?, updated_at = ? WHERE node_id = ?",
                         (json.dumps(meta), _now(), node.node_id))
        self._cache.invalidate()
        return True

    def agent_lookup(self, agent_id: str, core_node_id: str) -> Optional[Dict[str, Any]]:
        node = self.get_node(core_node_id)
        if not node:
            return None
        digest = {"core_node_id": core_node_id, "label": node.label, "content": node.content,
                  "node_type": node.node_type, "trust_level": node.trust_level,
                  "source": "CORE_LOOKUP", "looked_up_at": _now()}
        if self._agent_memory_mode() == "legacy_shards":
            self._get_agent_shard(agent_id).remember(
                content=json.dumps(digest), label=f"CORE_LOOKUP:{node.label}",
                node_type="CORE_REF", metadata={"core_node_id": core_node_id, "read_only": True}
            )
        return digest

    def promote_to_core(self, agent_id: str, agent_node_id: str,
                        new_type: str = None, new_label: str = None) -> Optional[str]:
        if self._agent_memory_mode() == "core_shared":
            node = self._shared_agent_node(agent_id, agent_node_id)
            if not node:
                return None
            target_type = new_type or "FACT"
            if target_type not in NODE_TYPES or target_type in {"AGENT_NOTE", "CORE_REF"}:
                raise ValueError(f"Invalid promoted node_type: {target_type}")
            meta = dict(node.metadata)
            meta.update({
                "attention_state": "core_verified",
                "promoted_from_agent": agent_id,
                "promoted_at": _now(),
                "original_node_type": "AGENT_NOTE",
            })
            with self._core_conn() as conn:
                conn.execute(
                    "UPDATE nodes SET node_type = ?, label = ?, metadata = ?, updated_at = ? WHERE node_id = ?",
                    (target_type, new_label or node.label, json.dumps(meta), _now(), node.node_id)
                )
            self._cache.invalidate()
            return node.node_id
        shard = self._get_agent_shard(agent_id)
        agent_node = shard.get_node(agent_node_id)
        if not agent_node:
            return None
        core_id = self.remember(
            content=agent_node["content"], node_type=new_type or agent_node["node_type"],
            label=new_label or agent_node["label"], source=f"AGENT_{agent_id}",
            trust=agent_node["trust_level"] * 0.8,
            metadata={"promoted_from_agent": agent_id, "original_node_id": agent_node_id}
        )
        shard.mark_promoted(agent_node_id, core_id)
        return core_id

    def scan_agent_refs(self, agent_id: str) -> List[Dict[str, Any]]:
        if self._agent_memory_mode() == "core_shared":
            with self._core_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE node_type = 'CORE_REF' AND source = ? ORDER BY created_at DESC",
                    (f"AGENT_{agent_id}",)
                ).fetchall()
            return [self._node_from_row(row).to_dict() for row in rows
                    if not row["metadata"] or not json.loads(row["metadata"]).get("fulfilled")]
        return self._get_agent_shard(agent_id).get_pending_refs()

    # ── V2: Cross-agent query (Phase 6) ──────────────────────────────────────

    def find_across_agents(self, topic: str, min_confidence: float = 0.15, bound: int = 20) -> List[Dict]:
        """
        Search all agent shards for notes matching a topic using TF-IDF similarity.
        Returns ranked list of notes with agent_id attached.
        """
        if self._agent_memory_mode() == "core_shared":
            recalled = self.recall(topic, mode="SEMANTIC", bound=max(bound * 5, 50), include_agent_notes=True)
            results = []
            for node in recalled.nodes:
                agent_id = node.metadata.get("agent_id")
                if not agent_id or node.metadata.get("attention_state") == "core_verified":
                    continue
                similarity = node.metadata.get("_similarity", 0)
                if similarity >= min_confidence:
                    item = node.to_dict()
                    item["_agent_id"] = agent_id
                    item["_similarity"] = similarity
                    results.append(item)
            results.sort(key=lambda x: -x.get("_similarity", 0))
            return results[:bound]

        results = []
        for agent_db in sorted(self.agents_dir.glob("agent_*.db")):
            agent_id = agent_db.stem.replace("agent_", "")
            shard = self._get_agent_shard(agent_id)
            agent_results = shard.semantic_search(topic, min_confidence, bound // 2)
            for r in agent_results:
                r["_agent_id"] = agent_id
                results.append(r)
        results.sort(key=lambda x: -x.get("_similarity", 0))
        return results[:bound]

    def agent_refer_to(self, from_agent_id: str, to_agent_id: str, node_id: str, note: str = ""):
        """Agent creates a reference to another agent's finding."""
        meta = {"target_agent": to_agent_id, "target_node": node_id, "note": note}
        if self._agent_memory_mode() == "core_shared":
            return self.remember(
                content=f"Reference to {to_agent_id}'s node {node_id}: {note}",
                node_type="CORE_REF", label=f"AGENT_REF:{to_agent_id}",
                source=f"AGENT_{from_agent_id}",
                metadata=self._agent_metadata(from_agent_id, meta)
            )
        self._get_agent_shard(from_agent_id).remember(
            content=f"Reference to {to_agent_id}'s node {node_id}: {note}",
            node_type="CORE_REF", metadata=meta
        )

    # ── MAINTENANCE ───────────────────────────────────────────────────────────

    def consolidate(self) -> Tuple[int, int]:
        """
        v2 consolidation using TF-IDF cosine similarity.
        Merges nodes with similarity >= 0.85, links with >= 0.50.
        """
        merged = 0
        edges_created = 0

        with self._core_conn() as conn:
            cursor = conn.execute("SELECT n.node_id, n.content, n.label, nv.vector FROM nodes n LEFT JOIN node_vectors nv ON n.node_id = nv.node_id")
            nodes = cursor.fetchall()

            # Load or build vectorizer
            v = self._load_vectorizer(conn)
            high_thresh = self.config["consolidation_similarity_high"]
            link_thresh = self.config["consolidation_similarity_link"]

            for i in range(len(nodes)):
                for j in range(i + 1, len(nodes)):
                    a, b = nodes[i], nodes[j]
                    vec_a = json.loads(a["vector"]) if a["vector"] else v.transform((a["label"] or "") + " " + (a["content"] or ""))
                    vec_b = json.loads(b["vector"]) if b["vector"] else v.transform((b["label"] or "") + " " + (b["content"] or ""))
                    sim = v.cosine_similarity(vec_a, vec_b)

                    if sim >= high_thresh:
                        new_content = a["content"]
                        if b["content"] not in a["content"]:
                            new_content += " | " + b["content"]
                        if len(new_content) > self.config["max_content_length"]:
                            new_content = new_content[:self.config["max_content_length"]-3] + "..."
                        conn.execute("UPDATE nodes SET content = ?, updated_at = ? WHERE node_id = ?", (new_content, _now(), a["node_id"]))
                        conn.execute("UPDATE edges SET from_node = ? WHERE from_node = ?", (a["node_id"], b["node_id"]))
                        conn.execute("UPDATE edges SET to_node = ? WHERE to_node = ?", (a["node_id"], b["node_id"]))
                        conn.execute("DELETE FROM nodes WHERE node_id = ?", (b["node_id"],))
                        merged += 1
                    elif sim >= link_thresh:
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO edges
                                   (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (_edge_uuid(), a["node_id"], b["node_id"], "RELATES_TO", sim, _now(),
                                 json.dumps({"auto_consolidation": True, "similarity": sim, "method": "tfidf"}))
                            )
                            edges_created += 1
                        except sqlite3.IntegrityError:
                            pass

        if merged:
            self.rebuild_vector_index()
        self._cache.invalidate()
        return merged, edges_created

    def run_decay(self) -> List[str]:
        """
        v2 decay: layer-aware. Working/archive don't decay.
        Short-term decays faster than long-term.
        """
        prune_candidates = []
        threshold = self.config["prune_threshold"]

        with self._core_conn() as conn:
            for layer_name, cfg in MEMORY_LAYERS.items():
                if cfg["decay"] >= 1.0:
                    continue  # working + archive: no decay
                decay = cfg["decay"]
                boost = cfg["boost"]
                cursor = conn.execute("""
                    SELECT n.node_id, n.importance, n.access_count, n.updated_at
                    FROM nodes n
                    JOIN memory_layers ml ON n.node_id = ml.node_id
                    WHERE ml.layer = ?
                """, (layer_name,))
                for row in cursor.fetchall():
                    nid = row["node_id"]
                    imp = row["importance"]
                    days_old = (_now() - row["updated_at"]) / 86400
                    new_imp = imp * (decay ** days_old)
                    new_imp += min(row["access_count"] * boost, 0.3)
                    new_imp = min(1.0, new_imp)
                    conn.execute("UPDATE nodes SET importance = ? WHERE node_id = ?", (new_imp, nid))
                    if new_imp < threshold and row["access_count"] < 3:
                        prune_candidates.append(nid)

        return prune_candidates

    def prune_candidates(self, threshold: float = None) -> List[Dict[str, Any]]:
        threshold = threshold or self.config["prune_threshold"]
        result = self.recall(str(threshold), mode="PRUNE", bound=50)
        return [n.to_dict() for n in result.nodes]

    # ── SKILL OPERATIONS (v1 compat) ─────────────────────────────────────────

    def register_skill(self, name: str, description: str, level: str,
                       category: str, domain: str = "",
                       hierarchy_access: List[str] = None,
                       asha_use: str = "", agent_use: str = "",
                       metadata: Dict = None) -> str:
        meta = {"skill_level": level, "category": category, "domain": domain,
                "hierarchy_access": hierarchy_access or [], "asha_use": asha_use, "agent_use": agent_use}
        if metadata: meta.update(metadata)
        return self.remember(content=description, node_type="SKILL", label=name,
                             source="CORE", trust=1.0, importance=0.9, metadata=meta)

    def load_skill_registry(self, filepath: str = None) -> int:
        if filepath is None:
            candidates = [self.base_path / "ASHA_SKILLS_REGISTRY.txt",
                          Path(__file__).parent / "ASHA_SKILLS_REGISTRY.txt",
                          Path("ASHA_SKILLS_REGISTRY.txt")]
            for p in candidates:
                if p.exists(): filepath = str(p); break
        if not filepath or not os.path.exists(filepath): return 0
        content = Path(filepath).read_text(encoding="utf-8")
        lines = content.split("\n")
        count = 0
        current_skill = None
        skill_data = {}
        current_category = "UNCATEGORIZED"
        current_domain = ""
        block_category = current_category
        block_domain = current_domain
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[[CATEGORY:"):
                m = re.match(r"\[\[CATEGORY:(\w+)\]\]", stripped)
                if m:
                    current_category = m.group(1)
                    dm = re.search(r"Domain:(.+)", stripped)
                    if dm: current_domain = dm.group(1).strip()
            elif stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
                if current_skill and skill_data:
                    desc = skill_data.get("DESC", "")
                    level = skill_data.get("LEVEL", "CORE_ONLY")
                    asha = skill_data.get("ASHA", "")
                    agent = skill_data.get("AGENT", "")
                    ha = [l.strip() for l in level.split("|")]
                    self.register_skill(name=current_skill, description=desc, level=level,
                                        category=block_category, domain=block_domain,
                                        hierarchy_access=ha, asha_use=asha, agent_use=agent,
                                        metadata={"source_file": filepath, "raw_level": level})
                    count += 1
                current_skill = stripped[1:-1]
                block_category = current_category
                block_domain = current_domain
                skill_data = {}
            elif ":" in stripped and current_skill:
                key, _, val = stripped.partition(":")
                skill_data[key.strip()] = val.strip()
        if current_skill and skill_data:
            desc = skill_data.get("DESC", "")
            level = skill_data.get("LEVEL", "CORE_ONLY")
            asha = skill_data.get("ASHA", "")
            agent = skill_data.get("AGENT", "")
            ha = [l.strip() for l in level.split("|")]
            self.register_skill(name=current_skill, description=desc, level=level,
                                category=block_category, domain=block_domain,
                                hierarchy_access=ha, asha_use=asha, agent_use=agent,
                                metadata={"source_file": filepath, "raw_level": level})
            count += 1
        return count

    def find_skills(self, level: str = None, category: str = None,
                    query: str = None, bound: int = 30) -> List[Dict[str, Any]]:
        with self._core_conn() as conn:
            cursor = conn.execute("SELECT * FROM nodes WHERE node_type = 'SKILL' ORDER BY importance DESC")
            results = []
            for row in cursor.fetchall():
                meta = json.loads(row["metadata"])
                matches = True
                if level:
                    if level not in meta.get("skill_level", ""): matches = False
                if category and matches:
                    cat = meta.get("category", "").upper()
                    if cat != category.upper() and category.upper() not in cat: matches = False
                if query and matches:
                    kw = [w.lower() for w in re.findall(r"\b\w{3,}\b", query)]
                    content = (row["label"] + " " + row["content"]).lower()
                    if not any(k in content for k in kw): matches = False
                if matches:
                    results.append({"name": row["label"], "description": row["content"],
                                    "node_id": row["node_id"], "metadata": meta})
                    if len(results) >= bound: break
            return results

    def assign_skill(self, agent_id: str, skill_name: str,
                     weight: float = 1.0, metadata: Dict = None) -> str:
        """Assign a registered skill to an agent by name.

        Resolves the agent (creating its AGENT_NOTE anchor node on first
        assignment) and the SKILL node, then links them with a HAS_SKILL edge.
        Raises ValueError when the skill is not registered.
        """
        with self._core_conn() as conn:
            agent = conn.execute(
                "SELECT node_id FROM nodes WHERE node_type = 'AGENT_NOTE' AND label = ?",
                (agent_id,)).fetchone()
            skill = conn.execute(
                "SELECT node_id FROM nodes WHERE node_type = 'SKILL' AND label = ?",
                (skill_name,)).fetchone()
        if not skill:
            raise ValueError(f"Unknown skill: {skill_name}. Register it first or load ASHA_SKILLS_REGISTRY.txt.")
        if not agent:
            person_node_id = self.remember(
                content=f"Agent scope anchor for {agent_id}", node_type="AGENT_NOTE",
                label=agent_id, source=f"AGENT_{agent_id}", trust=0.5, importance=0.3,
                metadata=self._agent_metadata(agent_id, None, "agent_private"))
        else:
            person_node_id = agent["node_id"]
        meta = dict(metadata or {})
        meta.setdefault("agent_id", agent_id)
        return self.relate(person_node_id, skill["node_id"], "HAS_SKILL", weight=weight, metadata=meta)

    def agent_skills(self, agent_id: str) -> List[Dict[str, Any]]:
        """Skills the agent actually holds: auto-granted AGENT_AUTO skills plus
        any skills explicitly assigned via assign_skill()."""
        skills = self.find_skills(level="AGENT_AUTO")
        seen = {s["name"] for s in skills}
        with self._core_conn() as conn:
            rows = conn.execute(
                """SELECT n.* FROM edges e JOIN nodes n ON n.node_id = e.to_node
                   WHERE e.edge_type = 'HAS_SKILL' AND n.node_type = 'SKILL'
                   AND e.from_node IN (SELECT node_id FROM nodes
                                       WHERE node_type = 'AGENT_NOTE' AND label = ?)""",
                (agent_id,)).fetchall()
        for row in rows:
            node = self._node_from_row(row)
            if node.label not in seen:
                skills.append({"name": node.label, "description": node.content,
                               "node_id": node.node_id, "metadata": node.metadata})
                seen.add(node.label)
        return skills

    # ── EXPORT / IMPORT (Phase 8) ────────────────────────────────────────────

    def export(self, path: str = None) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = path or str(self.backups_dir / f"asha_memory_{timestamp}.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            tar.add(self.core_db_path, arcname="core.db")
            tar.add(self.config_path, arcname="config.json")
            for agent_db in self.agents_dir.glob("agent_*.db"):
                tar.add(agent_db, arcname=f"agents/{agent_db.name}")
        return path

    def import_memory(self, path: str, merge: bool = False) -> bool:
        if not os.path.exists(path): return False
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with tarfile.open(path, "r:gz") as tar:
                tar.extractall(tmpdir)
            if not merge:
                if self.core_db_path.exists(): self.core_db_path.unlink()
                for agent_db in self.agents_dir.glob("agent_*.db"): agent_db.unlink()
            src_core = Path(tmpdir) / "core.db"
            if src_core.exists():
                if merge:
                    self._merge_core_db(src_core)
                else:
                    shutil.copy2(src_core, self.core_db_path)
            src_agents = Path(tmpdir) / "agents"
            if src_agents.exists():
                for agent_db in src_agents.glob("agent_*.db"):
                    dest = self.agents_dir / agent_db.name
                    if not merge or not dest.exists():
                        shutil.copy2(agent_db, dest)
            self._init_core_db()
            self._agent_shards.clear()
            self._invalidate_vectorizer()
            self._cache.invalidate()
        return True

    def _merge_core_db(self, src_path: str) -> int:
        """Merge nodes, edges, and vectors from a source core DB into the live
        core. Existing node_ids are kept; edges are only copied when both
        endpoints exist in the merged graph."""
        merged = 0
        src = sqlite3.connect(str(src_path))
        src.row_factory = sqlite3.Row
        try:
            with self._core_conn() as conn:
                known = {r["node_id"] for r in conn.execute("SELECT node_id FROM nodes").fetchall()}
                for row in src.execute("SELECT * FROM nodes").fetchall():
                    if row["node_id"] not in known:
                        conn.execute(
                            """INSERT INTO nodes (node_id, node_type, label, content, source,
                               trust_level, importance, created_at, updated_at, access_count,
                               checksum, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (row["node_id"], row["node_type"], row["label"], row["content"],
                             row["source"], row["trust_level"], row["importance"],
                             row["created_at"], row["updated_at"], row["access_count"],
                             row["checksum"], row["metadata"]))
                        known.add(row["node_id"])
                        merged += 1
                for row in src.execute("SELECT * FROM edges").fetchall():
                    if row["from_node"] in known and row["to_node"] in known:
                        try:
                            conn.execute(
                                """INSERT OR REPLACE INTO edges
                                   (edge_id, from_node, to_node, edge_type, weight, created_at, metadata)
                                   VALUES (?,?,?,?,?,?,?)""",
                                (row["edge_id"], row["from_node"], row["to_node"],
                                 row["edge_type"], row["weight"], row["created_at"], row["metadata"]))
                        except sqlite3.Error:
                            pass
                src_rows = src.execute("SELECT * FROM node_vectors").fetchall() if self._table_exists(src, "node_vectors") else []
                for row in src_rows:
                    if row["node_id"] in known:
                        try:
                            conn.execute(
                                "INSERT OR REPLACE INTO node_vectors (node_id, vector, magnitude) VALUES (?,?,?)",
                                (row["node_id"], row["vector"], row["magnitude"]))
                        except sqlite3.Error:
                            pass
            return merged
        finally:
            src.close()

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    # ── V2: JSON export (Phase 8) ────────────────────────────────────────────

    def export_json(self, path: str = None) -> str:
        """Export all memory as JSON for non-Python tools."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = path or str(self.backups_dir / f"asha_memory_{timestamp}.json")
        with self._core_conn() as conn:
            nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes").fetchall()]
            edges = [dict(r) for r in conn.execute("SELECT * FROM edges").fetchall()]
        data = {
            "schema_version": "2.0",
            "exported_at": _now(),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": nodes,
            "edges": edges,
            "stats": self.stats(),
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return path

    # ── V2: GraphML export (Phase 8) ─────────────────────────────────────────

    def export_graphml(self, path: str = None) -> str:
        """
        Export as GraphML for Gephi / yEd visualization.
        Node types become colors, edge types become line styles.
        """
        import xml.etree.ElementTree as ET
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = path or str(self.backups_dir / f"asha_memory_{timestamp}.graphml")

        graphml = ET.Element("graphml", xmlns="http://graphml.graphdrawing.org/xmlns")
        # Key definitions
        ET.SubElement(graphml, "key", id="k_label", for_="node", attr_name="label", attr_type="string")
        ET.SubElement(graphml, "key", id="k_type", for_="node", attr_name="node_type", attr_type="string")
        ET.SubElement(graphml, "key", id="k_trust", for_="node", attr_name="trust_level", attr_type="double")
        ET.SubElement(graphml, "key", id="k_imp", for_="node", attr_name="importance", attr_type="double")
        ET.SubElement(graphml, "key", id="k_weight", for_="edge", attr_name="weight", attr_type="double")
        ET.SubElement(graphml, "key", id="k_etype", for_="edge", attr_name="edge_type", attr_type="string")

        graph = ET.SubElement(graphml, "graph", edgedefault="undirected")

        with self._core_conn() as conn:
            for row in conn.execute("SELECT * FROM nodes").fetchall():
                n = ET.SubElement(graph, "node", id=row["node_id"])
                ET.SubElement(n, "data", key="k_label").text = str(row["label"] or "")
                ET.SubElement(n, "data", key="k_type").text = str(row["node_type"] or "")
                ET.SubElement(n, "data", key="k_trust").text = str(row["trust_level"] or 0)
                ET.SubElement(n, "data", key="k_imp").text = str(row["importance"] or 0)

            for row in conn.execute("SELECT * FROM edges").fetchall():
                e = ET.SubElement(graph, "edge", id=row["edge_id"],
                                  source=row["from_node"], target=row["to_node"])
                ET.SubElement(e, "data", key="k_weight").text = str(row["weight"] or 1.0)
                ET.SubElement(e, "data", key="k_etype").text = str(row["edge_type"] or "")

        tree = ET.ElementTree(graphml)
        tree.write(path, xml_declaration=True, encoding="utf-8")
        return path

    # ── STATS ─────────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._core_conn() as conn:
            core_nodes = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
            core_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
            type_counts = {}
            cursor = conn.execute("SELECT node_type, COUNT(*) as c FROM nodes GROUP BY node_type")
            for row in cursor.fetchall(): type_counts[row["node_type"]] = row["c"]
            layer_counts = {}
            cursor = conn.execute("SELECT layer, COUNT(*) as c FROM memory_layers GROUP BY layer")
            for row in cursor.fetchall(): layer_counts[row["layer"]] = row["c"]
        agent_count = len(list(self.agents_dir.glob("agent_*.db")))
        with self._core_conn() as conn:
            shared_agent_sources = conn.execute(
                "SELECT COUNT(DISTINCT source) as c FROM nodes WHERE source LIKE 'AGENT_%'"
            ).fetchone()["c"]
        return {
            "core_nodes": core_nodes, "core_edges": core_edges,
            "core_type_breakdown": type_counts,
            "memory_layer_breakdown": layer_counts,
            "agent_shards": agent_count,
            "shared_agent_sources": shared_agent_sources,
            "agent_memory_mode": self._agent_memory_mode(),
            "base_path": str(self.base_path), "config": self.config,
        }

    # ── V2: profile & health (Phase 7) ───────────────────────────────────────

    def profile(self) -> Dict[str, Any]:
        """Query performance profile."""
        recent = self._query_log[-50:] if self._query_log else []
        avg_ms = sum(q["ms"] for q in recent) / len(recent) if recent else 0
        total = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / total if total else 0
        with self._core_conn() as conn:
            vector_count = conn.execute("SELECT COUNT(*) as c FROM node_vectors").fetchone()["c"]
            node_count = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        return {
            "recent_avg_ms": round(avg_ms, 2),
            "cache_hit_rate": round(hit_rate, 3),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "vector_index_freshness": f"{vector_count}/{node_count} nodes indexed",
            "query_log_size": len(self._query_log),
            "cache_size": len(self._cache.cache),
        }

    def health(self) -> List[str]:
        """Integrity check. Returns list of issues found."""
        issues = []
        with self._core_conn() as conn:
            orphans = conn.execute("""
                SELECT COUNT(*) as c FROM edges e
                LEFT JOIN nodes n1 ON n1.node_id = e.from_node
                LEFT JOIN nodes n2 ON n2.node_id = e.to_node
                WHERE n1.node_id IS NULL OR n2.node_id IS NULL
            """).fetchone()["c"]
            if orphans: issues.append(f"{orphans} orphaned edges")

            # Check FTS
            try:
                conn.execute("SELECT rowid FROM node_fts LIMIT 1").fetchall()
            except Exception:
                issues.append("FTS index corrupted")

            # Vector index freshness
            vc = conn.execute("SELECT COUNT(*) as c FROM node_vectors").fetchone()["c"]
            nc = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
            if vc < nc * 0.5:
                issues.append(f"Vector index stale ({vc}/{nc} nodes indexed)")

            # Schema version
            ver = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if not ver or ver["value"] != "2.0":
                issues.append(f"Schema version mismatch: {ver['value'] if ver else 'unknown'}")
        return issues or ["No issues found"]


# ──────────────────────────────────────────────────────────────────────────────
# AGENT SHARD CLASS
# ──────────────────────────────────────────────────────────────────────────────

class _AgentShard:
    """Internal class representing an agent's memory shard."""

    def __init__(self, db_path: str, agent_id: str, config: Dict):
        self.db_path = db_path
        self.agent_id = agent_id
        self.config = config

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def remember(self, content: str, label: str = None, node_type: str = "AGENT_NOTE",
                 metadata: Dict = None) -> str:
        if node_type not in ("TOPIC", "EVENT", "FACT", "PREFERENCE", "AFFECT", "AGENT_NOTE", "CORE_REF"):
            raise ValueError(f"Agent cannot write node_type: {node_type}")
        max_len = self.config.get("agent_max_content_length", 800)
        if len(content) > max_len:
            content = content[:max_len-3] + "..."
        label = label or content[:30]
        node_id = _uuid()
        now = _now()
        checksum = _checksum(content)
        metadata = metadata or {}
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO nodes
                   (node_id, node_type, label, content, source, trust_level,
                    created_at, updated_at, access_count, importance, checksum, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (node_id, node_type, label, content, f"AGENT_{self.agent_id}", 0.5,
                 now, now, 0, 0.5, checksum, json.dumps(metadata))
            )
            keywords = _extract_keywords(label + " " + content)
            for word, weight in keywords:
                conn.execute(
                    "INSERT OR REPLACE INTO node_index (word, node_id, field, weight) VALUES (?, ?, ?, ?)",
                    (word, node_id, "content", weight)
                )
        return node_id

    def get_recent_notes(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cursor = conn.execute("SELECT * FROM nodes ORDER BY updated_at DESC LIMIT ?", (limit,))
            return [{"node_id": r["node_id"], "node_type": r["node_type"], "label": r["label"],
                     "content": r["content"], "created_at": r["created_at"],
                     "metadata": json.loads(r["metadata"])} for r in cursor.fetchall()]

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            cursor = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
            row = cursor.fetchone()
            if row: return {"node_id": row["node_id"], "node_type": row["node_type"],
                           "label": row["label"], "content": row["content"],
                           "trust_level": row["trust_level"], "metadata": json.loads(row["metadata"])}
            return None

    def mark_promoted(self, agent_node_id: str, core_node_id: str):
        with self._conn() as conn:
            cursor = conn.execute("SELECT metadata FROM nodes WHERE node_id = ?", (agent_node_id,))
            row = cursor.fetchone()
            if row:
                meta = json.loads(row["metadata"])
                meta["promoted_to_core"] = core_node_id
                meta["promoted_at"] = _now()
                conn.execute("UPDATE nodes SET metadata = ? WHERE node_id = ?", (json.dumps(meta), agent_node_id))

    def get_pending_refs(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cursor = conn.execute("SELECT * FROM nodes WHERE node_type = 'CORE_REF' ORDER BY created_at DESC")
            refs = []
            for row in cursor.fetchall():
                meta = json.loads(row["metadata"])
                if not meta.get("fulfilled"):
                    refs.append({"node_id": row["node_id"], "label": row["label"],
                                 "content": row["content"], "metadata": meta})
            return refs

    # ── V2: Agent semantic search (Phase 6) ──────────────────────────────────

    def semantic_search(self, topic: str, min_confidence: float = 0.15, bound: int = 10) -> List[Dict]:
        """
        Search agent's own notes by TF-IDF similarity to topic string.
        Uses in-memory computation (agent shards are typically small).
        """
        with self._conn() as conn:
            cursor = conn.execute("SELECT node_id, label, content FROM nodes")
            rows = cursor.fetchall()
            if not rows:
                return []

            # Build mini vectorizer on agent's content
            v = TfidfVectorizer()
            v.fit([(r["label"] or "") + " " + (r["content"] or "") for r in rows])
            query_vec = v.transform(topic)

            if not query_vec:
                return []

            scored = []
            for r in rows:
                text = (r["label"] or "") + " " + (r["content"] or "")
                doc_vec = v.transform(text)
                sim = v.cosine_similarity(query_vec, doc_vec)
                if sim >= min_confidence:
                    scored.append((sim, r))

            scored.sort(key=lambda x: -x[0])
            results = []
            for sim, r in scored[:bound]:
                results.append({
                    "node_id": r["node_id"], "label": r["label"],
                    "content": r["content"], "_similarity": round(sim, 4)
                })
            return results


# ──────────────────────────────────────────────────────────────────────────────
# MODULE ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("ASHA_MEMORY_SYSTEM v2.0")
    print("=" * 50)

    mem = AshaMemory(base_path="./demo_v2_memory")

    # Demo: Remember SAM
    sam_id = mem.remember(
        content="Builds AI systems. Direct communicator. Prefers honesty over agreeableness.",
        node_type="PERSON", label="SAM", source="USER", trust=0.7, importance=0.8
    )
    print(f"Stored SAM: {sam_id}")

    # Demo: Remember a preference
    pref_id = mem.remember(
        content="Comfortable with AI opacity and emergent behavior. Treats it as feature.",
        node_type="PREFERENCE", label="SAM_AI_attitude", source="CORE", trust=0.8
    )
    mem.relate(sam_id, pref_id, "HAS_PREFERENCE", weight=1.0)
    print(f"Stored preference: {pref_id}")

    # Demo: SEMANTIC recall — find content that MEANS the same thing
    result = mem.recall("direct honest communicator", mode="SEMANTIC", bound=5)
    print(f"\nSEMANTIC recall 'direct honest communicator':")
    print(f"  Found {result.total_found} nodes")
    for node in result.nodes:
        print(f"  - [{node.node_type}] {node.label}: {node.content[:60]} (sim: {node.metadata.get('_similarity', 'N/A')})")

    result = mem.recall("emergent opacity", mode="SEMANTIC", bound=5)
    print(f"\nSEMANTIC recall 'emergent opacity':")
    print(f"  Found {result.total_found} nodes")
    for node in result.nodes:
        print(f"  - [{node.node_type}] {node.label}: {node.content[:60]} (sim: {node.metadata.get('_similarity', 'N/A')})")

    # Demo: Query DSL
    result = mem.query('FIND PERSON "SAM" -> PREFERENCE')
    print(f"\nQuery DSL 'FIND PERSON \"SAM\" -> PREFERENCE':")
    print(f"  Mode: {result.mode}, Found: {result.total_found}")

    # Demo: Spawn agent + cross-agent query
    mem.spawn_agent_memory("demo_007")
    mem.agent_remember("demo_007", "Graph databases beat vector search for relational queries.", "research")
    mem.agent_remember("demo_007", "The agent should prefer simplicity over cleverness.", "preference")

    cross = mem.find_across_agents("relational queries")
    print(f"\nCross-agent search 'relational queries': {len(cross)} results")
    for r in cross:
        print(f"  - Agent {r['_agent_id']}: {r['label']} (sim: {r.get('_similarity', 'N/A')})")

    # Demo: Profile
    print(f"\nProfile: {json.dumps(mem.profile(), indent=2)}")
    print(f"Health: {mem.health()}")
    print(f"Stats: {json.dumps(mem.stats(), indent=2)}")

    # Cleanup (comment out to persist for inspection)
    # import shutil
    # shutil.rmtree("./demo_v2_memory", ignore_errors=True)
