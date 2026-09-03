"""
SHARED LEXICON — Single source of truth for ASHA Memory v2
============================================================
Tokenizer, stopwords, sentiment word lists, and telemetry heuristics.
Pure Python stdlib only. Used by asha_memory_v2.py and brain/brain_engine.py.

Keeps both modules in sync — previously duplicated with drift:
  - asha_memory_v2.POSITIVE_WORDS (27) vs brain.POSITIVE_WORDS (39)
  - _tokenize fallback lambda (split) vs regex
  - _looks_like_json_log slice [:400] vs [:300] with different predicates
"""

import re
from collections import Counter
from typing import List, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# TOKENIZER — single source of truth
# ──────────────────────────────────────────────────────────────────────────────

_TOKEN_PATTERN = re.compile(r"\b[\w']{2,}\b", re.UNICODE)

def _tokenize(text: str, min_len: int = 2) -> List[str]:
    """Tokenize text into words. Captures Unicode, digits, underscores, contractions.

    - min_len=2 so 'AI', 'go', 'it' enter IDF (IDF naturally demotes noise).
    - Unicode flag so 'Müller', 'français', 'über' are visible.
    - Apostrophe kept so 'don't', 'it's' stay as single tokens.
    """
    return [w for w in _TOKEN_PATTERN.findall(text.lower()) if len(w) >= min_len]


# ──────────────────────────────────────────────────────────────────────────────
# STOPWORDS — deduped, sorted for readability
# ──────────────────────────────────────────────────────────────────────────────

# 2-letter noise words (min_len=2 in tokenizer; "ai" kept as domain signal)
_TWO_LETTER_STOPWORDS = {
    "to","in","is","it","of","on","as","at","be","by","do","go","he","if","me",
    "my","no","or","so","up","us","we","an","am","hi",
}

_THREE_PLUS_STOPWORDS = {
    "the","and","for","are","but","not","you","all","can","had","her","was","one",
    "our","out","day","get","has","him","his","how","its","may","new","now","old",
    "see","two","who","boy","did","she","use","way","many","oil","sit","set",
    "run","eat","far","sea","eye","ago","off","too","any","say","man","try","ask",
    "end","why","let","put","own","tell","very","when","much","would","there","their",
    "what","said","have","each","which","will","about","could","other","after","first",
    "never","these","think","where","being","every","great","might","shall","still",
    "those","while","this","that","with","from","they","know","want","been","good",
    "some","time","than","them","well","were","here","look","more","only",
    "over","such","take","also","just","like","make","even","then","back",
}

STOPWORDS = _TWO_LETTER_STOPWORDS | _THREE_PLUS_STOPWORDS
assert len(STOPWORDS) == len(set(STOPWORDS)), "STOPWORDS deduped (P4)"  # P4: no duplicates like her/much
assert len(STOPWORDS) == len(_TWO_LETTER_STOPWORDS) + len(_THREE_PLUS_STOPWORDS), "STOPWORDS sets disjoint (P4)"

# Lexicon version — bump when tokenizer/stopwords change
LEXICON_VERSION = 3  # v2=Unicode tokenizer+no stemmer, v3=+2-letter stopwords

# ──────────────────────────────────────────────────────────────────────────────
# SENTIMENT WORD LISTS — unified (brain's larger list wins, deduped)
# ──────────────────────────────────────────────────────────────────────────────

POSITIVE_WORDS = {
    "like", "likes", "liked", "love", "loves", "loved", "prefer", "prefers", "preferred",
    "enjoy", "enjoys", "enjoyed", "good", "great", "excellent", "amazing", "best",
    "easy", "fast", "beautiful", "perfect", "recommend", "recommends", "awesome", "fantastic", "wonderful",
    "agree", "agrees", "agreed", "support", "supports", "approve", "approves", "yes", "true", "right", "correct", "reliable", "works",
}

NEGATIVE_WORDS = {
    "hate", "hates", "hated", "dislike", "dislikes", "disliked", "avoid", "avoids", "avoided",
    "bad", "terrible", "awful", "worst", "horrible", "hard", "slow", "ugly", "broken", "useless",
    "disaster", "pathetic", "garbage", "disagree", "disagrees", "oppose", "opposes", "reject", "rejects",
    "no", "false", "wrong", "incorrect", "unreliable", "fails", "failed",
}

# ──────────────────────────────────────────────────────────────────────────────
# EPHEMERAL / TELEMETRY
# ──────────────────────────────────────────────────────────────────────────────

# Canonical ephemeral label set — also the default for DEFAULT_CONFIG["ephemeral_labels"]
DEFAULT_EPHEMERAL_LABELS = {
    "FEED_SNAPSHOT", "RUNTIME_SAMPLE", "TIME_ENTRY", "DAILY_STATE",
    "CRON_SUPERVISOR_REPORT", "BRAIN_MAINTENANCE_REPORT", "BRAIN_HISTORY",
    "SCOUT_WRAPPER_TOP_STORIES", "HN_SCOUT_TOP3", "HN_SCOUT",
}

# Backwards-compat alias used by older imports
EPHEMERAL_LABELS = DEFAULT_EPHEMERAL_LABELS

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

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
    s = content.lstrip()
    if not s.startswith("{"):
        return False
    low = s[:400].lower()
    return ("timestamp" in low and ("status" in low or "post_count" in low or "load1m" in low))


def _sanitize_fts_query(query: str) -> str:
    """Remove FTS5 syntax-breaking characters so MATCH never throws OperationalError."""
    if not query:
        return ""
    # Strip FTS5 operators that break MATCH syntax
    sanitized = re.sub(r'["*:\-]', ' ', query)
    # Collapse whitespace
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    return sanitized
