# Lexicon

> Single source of truth for tokenizer, stopwords, sentiment, and ephemeral heuristics. Pure stdlib, zero dependencies.

`src/core/lexicon.py:1` — **v2 look-up only** — verbatim copy of `v2/shared_lexicon.py` (only header `V3 PROVENANCE` added). No logic changed in v3.

---

## Overview

| Concern | Export | Line |
|---|---|---|
| Tokenizer | `_TOKEN_PATTERN`, `_tokenize` | `src/core/lexicon.py:25`, `src/core/lexicon.py:27` |
| Stopwords | `_TWO_LETTER_STOPWORDS`, `_THREE_PLUS_STOPWORDS`, `STOPWORDS` | `src/core/lexicon.py:42`, `src/core/lexicon.py:47`, `src/core/lexicon.py:60` |
| Version | `LEXICON_VERSION` | `src/core/lexicon.py:65` |
| Sentiment | `POSITIVE_WORDS`, `NEGATIVE_WORDS`, `_sentiment_score` | `src/core/lexicon.py:71`, `src/core/lexicon.py:78`, `src/core/lexicon.py:121` |
| Ephemeral | `DEFAULT_EPHEMERAL_LABELS`, `EPHEMERAL_LABELS`, `_looks_like_json_log` | `src/core/lexicon.py:90`, `src/core/lexicon.py:97`, `src/core/lexicon.py:132` |
| Helpers | `_extract_keywords`, `_jaccard_similarity`, `_sentiment_score`, `_sanitize_fts_query` | `src/core/lexicon.py:103`, `src/core/lexicon.py:111`, `src/core/lexicon.py:121`, `src/core/lexicon.py:143` |

Previously duplicated between `asha_memory_v2.py` and `brain/brain_engine.py` with drift (27 vs 39 positives, split vs regex tokenize, `[:400]` vs `[:300]` slice). This file is the deduped canonical copy.

---

## Tokenizer

### Pattern

`src/core/lexicon.py:25`:

```python
_TOKEN_PATTERN = re.compile(r"\b[\w']{2,}\b", re.UNICODE)
```

| Property | Value |
|---|---|
| Regex | `\b[\w']{2,}\b` |
| Flags | `re.UNICODE` |
| Min match length | 2 chars (regex-level) |
| Char class | `\w` = `[a-zA-Z0-9_]` + Unicode letters/digits per `UNICODE` flag, plus `'` |

`_tokenize` applies a second `min_len` filter after lowercasing:

`src/core/lexicon.py:27`:

```python
def _tokenize(text: str, min_len: int = 2) -> List[str]:
    return [w for w in _TOKEN_PATTERN.findall(text.lower()) if len(w) >= min_len]
```

| Param | Default | Effect |
|---|---|---|
| `text` | — | Lowercased before matching |
| `min_len` | `2` | Post-regex floor; `AI`/`go`/`it` enter IDF and are naturally demoted rather than dropped |

### Unicode / digit / underscore handling

- **Unicode**: `re.UNICODE` makes `\w` match `Müller`, `français`, `über` as single tokens. Without it only ASCII would match.
- **Digits**: `\w` includes `0-9`, so `load1m`, `3d` (if length >=2) are tokens.
- **Underscore**: `\w` includes `_`, so `agent_private` is one token (not split).
- **Apostrophe**: `'` inside `[\w']` keeps contractions intact — `don't` / `it's` stay as single tokens `don't`, `it's`.

### Examples

| Input | Tokens | Why |
|---|---|---|
| `"don't, Müller"` | `["don't", "müller"]` | Apostrophe kept, Unicode lowered and matched, comma is boundary |
| `"AI/go"` | `["ai", "go"]` | `/` is boundary, both 2-char tokens pass `min_len=2` |
| `"I am AI"` | `["am", "ai"]` | `I` is 1 char — regex `\b[\w']{2,}\b` never emits it; `am` passes regex but is later removed by stopwords |
| `"load1m 3d _test"` | `["load1m", "3d", "_test"]` | Digits and underscore are `\w` |
| `""` | `[]` | Empty input returns empty list |

---

## Stopwords

Deduped, sorted for readability. Two disjoint sets unioned into `STOPWORDS`.

`src/core/lexicon.py:60`:

```python
STOPWORDS = _TWO_LETTER_STOPWORDS | _THREE_PLUS_STOPWORDS
assert len(STOPWORDS) == len(set(STOPWORDS))
assert len(STOPWORDS) == len(_TWO_LETTER_STOPWORDS) + len(_THREE_PLUS_STOPWORDS)
```

### `_TWO_LETTER_STOPWORDS` — 25 entries

`src/core/lexicon.py:42`:

```
to, in, is, it, of, on, as, at, be, by, do, go, he, if, me,
my, no, or, so, up, us, we, an, am, hi
```

Noise introduced by `min_len=2`. `ai` is **kept** as a domain signal (not in this set). `go`/`do` are here despite tokenizer matching them — stopword removal, not tokenizer length, is what silences them.

### `_THREE_PLUS_STOPWORDS` — 115 entries

`src/core/lexicon.py:47`:

```
the, and, for, are, but, not, you, all, can, had, her, was, one,
our, out, day, get, has, him, his, how, its, may, new, now, old,
see, two, who, boy, did, she, use, way, many, oil, sit, set,
run, eat, far, sea, eye, ago, off, too, any, say, man, try, ask,
end, why, let, put, own, tell, very, when, much, would, there, their,
what, said, have, each, which, will, about, could, other, after, first,
never, these, think, where, being, every, great, might, shall, still,
those, while, this, that, with, from, they, know, want, been, good,
some, time, than, them, well, were, here, look, more, only,
over, such, take, also, just, like, make, even, then, back
```

### Combined

| Set | Count |
|---|---|
| `_TWO_LETTER_STOPWORDS` | 25 |
| `_THREE_PLUS_STOPWORDS` | 115 |
| `STOPWORDS` | **140** |

Doc shorthand "~90 three-plus" is approximate; the file holds 115. `STOPWORDS` is the filtered set used by `_extract_keywords` and TF-IDF-adjacent indexing.

### `LEXICON_VERSION`

`src/core/lexicon.py:65`:

```python
LEXICON_VERSION = 3  # v2=Unicode tokenizer+no stemmer, v3=+2-letter stopwords
```

| Version | Meaning |
|---|---|
| 2 | Switched to Unicode-aware regex tokenizer, removed stemmer |
| 3 | Added 25 two-letter stopwords; `STOPWORDS` disjointness asserted; bumped when tokenizer or stopword set changes |

Persisted in `core.db` `schema_meta.lexicon_version` for migration checks. Bump this when either set or `_TOKEN_PATTERN` changes, then backfill `node_index`/`node_vectors`.

---

## Sentiment

### Word lists

`src/core/lexicon.py:71` and `src/core/lexicon.py:78` — brain's larger list wins, deduped via set.

**`POSITIVE_WORDS` — 39 entries:**

```
like, likes, liked, love, loves, loved, prefer, prefers, preferred,
enjoy, enjoys, enjoyed, good, great, excellent, amazing, best,
easy, fast, beautiful, perfect, recommend, recommends, awesome, fantastic, wonderful,
agree, agrees, agreed, support, supports, approve, approves, yes, true, right, correct, reliable, works
```

**`NEGATIVE_WORDS` — 35 entries:**

```
hate, hates, hated, dislike, dislikes, disliked, avoid, avoids, avoided,
bad, terrible, awful, worst, horrible, hard, slow, ugly, broken, useless,
disaster, pathetic, garbage, disagree, disagrees, oppose, opposes, reject, rejects,
no, false, wrong, incorrect, unreliable, fails, failed
```

Note: doc shorthand "~39 pos / ~32 neg" — actual file is 39 / 35. Overlap is intentional for contradiction detection (e.g., `yes` vs `no`, `agree` vs `disagree`).

### `_sentiment_score`

`src/core/lexicon.py:121`:

```python
def _sentiment_score(text: str) -> float:
    words = set(_tokenize(text))
    pos = sum(1 for w in words if w in POSITIVE_WORDS)
    neg = sum(1 for w in words if w in NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total
```

| Input | Tokens (set) | pos | neg | total | Score |
|---|---|---|---|---|---|
| `"I love this, it works great"` | `{love, this, works, great}` (this is stopword-filtered only in keywords, not here — scoring uses raw token set) | 3 (`love`, `works`, `great`) | 0 | 3 | `1.0` |
| `"hate slow broken"` | `{hate, slow, broken}` | 0 | 3 | 3 | `-1.0` |
| `"love but hate"` | `{love, but, hate}` | 1 | 1 | 2 | `0.0` |
| `"hello world"` | `{hello, world}` | 0 | 0 | 0 | `0.0` |

- Returns range `[-1.0, 1.0]`. Positive = affirmative, negative = negating.
- Uses `set(_tokenize(text))` so repeated words count once per document (not TF).
- Consumed by `brain/engine.py` contradiction scan to find sentiment clashes between co-resident nodes.

---

## Ephemeral / Telemetry

### Canonical label set

`src/core/lexicon.py:90`:

```python
DEFAULT_EPHEMERAL_LABELS = {
    "FEED_SNAPSHOT", "RUNTIME_SAMPLE", "TIME_ENTRY", "DAILY_STATE",
    "CRON_SUPERVISOR_REPORT", "BRAIN_MAINTENANCE_REPORT", "BRAIN_HISTORY",
    "SCOUT_WRAPPER_TOP_STORIES", "HN_SCOUT_TOP3", "HN_SCOUT",
}
```

10 entries. Default for `DEFAULTS["ephemeral_labels"]` (`brain/config.json` — the only live config; `memory/config.json` is informational only since C17 superseded, never read). `EPHEMERAL_LABELS` at `src/core/lexicon.py:97` is a backwards-compat alias (`EPHEMERAL_LABELS = DEFAULT_EPHEMERAL_LABELS`).

| Label | Source |
|---|---|
| `FEED_SNAPSHOT` | Feed ingestion raw JSON |
| `RUNTIME_SAMPLE` | Runtime telemetry (`load1m`, `status`) |
| `TIME_ENTRY` | Time tracking |
| `DAILY_STATE` | Daily state dump |
| `CRON_SUPERVISOR_REPORT` | Supervisor cron |
| `BRAIN_MAINTENANCE_REPORT` | Brain job report |
| `BRAIN_HISTORY` | Brain history entry |
| `SCOUT_WRAPPER_TOP_STORIES` | Scout wrapper |
| `HN_SCOUT_TOP3` | HN scout (top 3) |
| `HN_SCOUT` | HN scout |

Ephemeral nodes are stored in `ephemeral_events` (or with these labels in `nodes` pre-migration), excluded from FTS/vectors/edges, and swept by the `compact` brain job.

### `_looks_like_json_log` — telemetry heuristic

`src/core/lexicon.py:132`:

```python
def _looks_like_json_log(content: str) -> bool:
    if not content:
        return False
    s = content.lstrip()
    if not s.startswith("{"):
        return False
    low = s[:400].lower()
    return ("timestamp" in low and ("status" in low or "post_count" in low or "load1m" in low))
```

| Step | Check |
|---|---|
| 1 | Falsy `content` → `False` |
| 2 | `lstrip()` then `startswith("{")` — must look like JSON object after whitespace |
| 3 | Lowercase first 400 chars (`s[:400].lower()`) |
| 4 | Return `True` iff `timestamp` is present **and** one of `status` / `post_count` / `load1m` is present |

This canonicalizes the v2 drift (`[:400]` vs `[:300]` with different predicates). The slice is intentionally narrow — it avoids scanning multi-KB bodies. Used to auto-detect untagged telemetry for the ephemeral candidate list (`GET /api/ephemeral_candidates`).

```python
_looks_like_json_log('{"timestamp": 123, "status": "ok", "post_count": 5}')  # True
_looks_like_json_log('{"timestamp": 123, "note": "hello"}')                   # False — missing status/post_count/load1m
_looks_like_json_log('  {"timestamp": 1, "load1m": 0.5}')                      # True — lstrip handles leading space
```

---

## Helpers

### `_extract_keywords`

`src/core/lexicon.py:103`:

```python
def _extract_keywords(text: str, max_words: int = 20) -> List[Tuple[str, float]]:
```

1. `_tokenize(text)` → tokens.
2. Filter `w not in STOPWORDS` (140).
3. `Counter(filtered)` → frequency.
4. Normalize by `total = len(filtered) or 1` → `count/total`.
5. Return `most_common(max_words)` as `[(word, weight)]`.

Example:

```python
_extract_keywords("the fast fast beautiful API is fast", max_words=3)
# tokens: ["the","fast","fast","beautiful","api","is","fast"]
# filtered: ["fast","fast","beautiful","api","fast"]  (the/is removed)
# counts: fast:3, beautiful:1, api:1, total 5
# → [("fast", 0.6), ("beautiful", 0.2), ("api", 0.2)]
```

Feeds `node_index` (top-20 per doc) and vector term selection.

### `_jaccard_similarity`

`src/core/lexicon.py:111`:

```python
def _jaccard_similarity(text_a: str, text_b: str) -> float:
    set_a = set(_tokenize(text_a))
    set_b = set(_tokenize(text_b))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)
```

Set-based Jaccard over token sets (stopwords **not** removed here). Empty-set guard returns `0.0`. Used by dedup/discovery thresholds (`0.85` merge, `[0.50, 0.85)` link).

```python
_jaccard_similarity("fast beautiful API", "fast API")  # 2/3 ≈ 0.666
_jaccard_similarity("", "hello")                        # 0.0
```

### `_sentiment_score`

See Sentiment section above. Signature `(_sentiment_score(text: str) -> float)` at `src/core/lexicon.py:121`.

### `_sanitize_fts_query`

`src/core/lexicon.py:143`:

```python
def _sanitize_fts_query(query: str) -> str:
    if not query:
        return ""
    sanitized = re.sub(r'["*:\-]', ' ', query)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    return sanitized
```

Removes FTS5 syntax-breaking characters (`"`, `*`, `:`, `-`) that would throw `OperationalError` on `MATCH`, then collapses whitespace. Empty/falsy input returns `""`. Called before every `node_fts MATCH` in `src/core/recall.py`.

```python
_sanitize_fts_query('title:"hello" *world:test -foo')
# → "title hello world test foo"
_sanitize_fts_query("")  # → ""
```

---

## Provenance & usage

- **v2 look-up only**: `lexicon.py` is `v2/shared_lexicon.py` verbatim except this header. No behavior diff.
- Consumers: `src/core/nodes.py` (keywords/index), `src/core/vectors.py` (token stream), `src/core/recall.py` (FTS sanitize, Jaccard gate), `brain/engine.py` (sentiment, ephemeral detection), `src/agents/store.py` (agent notes index).
- Keep `LEXICON_VERSION` and `STOPWORDS` invariants in sync with `schema_meta` and `migrate.py` when changing `[lexicon.py](../src/core/lexicon.py)`.

