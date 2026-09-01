# Lexicon, Tokenizer & Vectorizer Stability Plan

> [!NOTE]
> **Status**: Fully Implemented (`LEXICON_VERSION = 3`). The shared tokenizer, stemmer removal, version-gated vectorizer, and 2-letter stopword filtering are active in `asha_memory_v2.py`.

Three issues identified. Plan below addresses each.

## Issue 1 — Tokenizer rejects usernames, contractions, non-English

**Root**: `\b[a-zA-Z]{3,}\b` used in four places:
| Location | Line | Used by |
|----------|------|---------|
| `TfidfVectorizer._tokenize` | 176 | TF-IDF fit/transform |
| `_extract_keywords` | 342 | node_index keyword extraction |
| `_jaccard_similarity` | 351 | consolidation / contradiction |
| `_sentiment_score` | 362 | contradiction detection |

**What breaks**:
- `@user123`, `sam_doe` → digits/underscore/`@` dropped entirely
- `don't`, `it's`, `you'll` → apostrophe splits word, often below 3-char floor
- `über`, `façon`, `Müller` → non-ASCII (nfc/umlaut/etc) treated as non-alpha
- `AI`, `it`, `go` → 2-char terms invisible even when semantically important

### Approach: three-tier tokenizer replace

Replace all four `re.findall(r"\b[a-zA-Z]{3,}\b", ...)` calls with a shared `_tokenize(text, min_len=2)` function.

**New token pattern**: `\b[\w']{2,}\b` with post-filter to handle edge cases:
- `\w` = `[a-zA-Z0-9_]` — captures usernames, digits
- `'` included so contractions (`don't`, `it's`) stay as single tokens
- Lower `min_len` from 3 to 2 so `AI`, `go`, `it` are visible to TF-IDF (IDF handles noise)
- **Non-English**: Python regex `\w` with `re.UNICODE` flag (or `re.A` for ASCII-only). Since we want non-English support, drop the `re.ASCII`/`re.A` flag so `\w` matches Unicode word chars: `ü`, `é`, `ç`, `ñ`, `Müller`, `français`, `über`.

**New shared function**:

```python
# Single source of truth for all tokenization
_TOKEN_PATTERN = re.compile(r"\b[\w']{2,}\b", re.UNICODE)

def _tokenize(text: str, min_len: int = 2) -> List[str]:
    return [w for w in _TOKEN_PATTERN.findall(text.lower()) if len(w) >= min_len]
```

**Migration**:

| Site | Old | New |
|------|-----|-----|
| `TfidfVectorizer._tokenize` (176) | `re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())` | call `_tokenize(text)` (shared func) |
| `_extract_keywords` (342) | `re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())` | call `_tokenize(text)` |
| `_jaccard_similarity` (351) | `re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())` | call `set(_tokenize(text))` |
| `_sentiment_score` (362) | `re.findall(r"\b[a-zA-Z]+\b", text.lower())` | call `set(_tokenize(text))` |

**Stopword impact**: STOPWORDS list currently contains many 2- and 3-letter words. Lowering min_len to 2 means `"ai"`, `"go"`, `"it"`, `"to"`, `"in"`, `"at"` etc. will now be tokens. Most of these are already in STOPWORDS. But for the few that aren't (`"ai"` isn't), they'll survive stopword filtering and enter the IDF. That's fine — IDF naturally demotes words that appear in many docs.

**edge: username normalization**: `@user123` → matches `@` as boundary? No, `@` is not `[\w']`. So `@user123` would tokenize to `user123` (loses `@` prefix). That's acceptable — the semantic core is `user123`.

**edge: underscore in usernames**: `sam_doe` → token `sam_doe`. Stored and searched as-is.

### Rollout
- Single commit that adds shared `_tokenize()`, replaces all four regex sites, updates `min_len`.
- Existing `node_index` and `node_vectors` become stale — callers must `rebuild_vector_index()` after upgrade (noted in changelog).

---

## Issue 2 — Naive suffix stemmer does more harm than good

**Root**: `_stem_word` at line 319:

```python
def _stem_word(word: str) -> str:
    for suffix in ("ing", "ed", "er", "est", "ly", "tion", "ness", "ment"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[:-len(suffix)]
    return word
```

**Problems**:
- False positives: `attention` → `atten`, `position` → `posi`, `action` → `ac` (the `> len(suffix) + 2` guard fails to protect sufficiently long words)
- Overlaps with IDF: TF-IDF tokenizer already requires ≥2 chars. Words that survive the floor don't need additional stemming — IDF handles conflating related forms (e.g., "running" and "run" appear in different docs, IDF measures their distinctiveness)
- Inconsistency with TF-IDF: `_extract_keywords` stems for `node_index` (RELATED mode) but `TfidfVectorizer._tokenize` doesn't (SEMANTIC mode). A query for "running" searches stem `runn` in RELATED but raw `running` in SEMANTIC — different match sets.

**Analysis of `> len(suffix) + 2` guard**:
| word | suffix | len(word) | len(suffix)+2 | guard passes? | result |
|------|--------|-----------|---------------|---------------|--------|
| running | ing | 7 | 5 | yes | runn |
| runner | er | 6 | 5 | yes | runn |
| quickly | ly | 7 | 5 | yes | quick |
| nation | tion | 6 | 6 | **no** | nation |
| education | tion | 9 | 6 | **yes** | educa ❌ |
| action | tion | 6 | 6 | **no** | action |
| cushion | ion | not in suffix list | — | — | cushion |

The guard protects 6-letter words ending in 4-letter suffixes but fails for longer words. The result is inconsistent and unpredictable.

### Approach: remove `_stem_word`, stop stemming node_index

1. Delete `_stem_word` function
2. In `_extract_keywords`, remove stem call — return raw words directly
3. Rebuild `node_index` on migration (already done in v1→v2 migration) or note that existing `node_index` entries are keyed by stemmed word and must be rebuilt

**Why remove instead of fix?**:
- A proper stemmer (Porter, Snowball) is >50 lines of logic and uses ~40 suffix rules. Adding it for one internal keyword table is disproportionate.
- Without stemming, the system still works: "runs" and "running" are distinct tokens but TF-IDF cosine similarity handles the overlap where they appear together. RELATED mode does exact keyword match, which is a different trade-off (precision over recall) — that's acceptable for that mode.
- Removing the stemmer eliminates an entire class of bugs and the inconsistency between RELATED and SEMANTIC paths.

**If stemming is truly needed later**: add a dedicated Snowball/Porter implementation as a separate optional module, not embedded in the keyword extraction hot path.

### Rollout
- Delete `_stem_word`
- In `_extract_keywords`, change `return [(_stem_word(w), s) for w, s in scored]` to `return scored` (unchanged word list)
- Add migration note: existing `node_index` records keyed by stem → run `_rebuild_node_index()` on upgrade

---

## Issue 3 — Vectorizer is live-mutable without synchronization

**Root**: `_load_vectorizer` caches to `self._vectorizer`, but other paths set `self._vectorizer = None` to force rebuild:

```
_store_node (line 945):       self._vectorizer = None
rebuild_vector_index (line 726): self._vectorizer = None
```

**Race scenario**:
1. Thread A calls `_recall_semantic` → `_load_vectorizer(conn)` → builds v1 → `self._vectorizer = v1`
2. Thread B calls `_store_node` → `self._vectorizer = None`
3. Thread C calls `_recall_semantic` → `_load_vectorizer(conn)` → builds v2 → `self._vectorizer = v2`
4. Thread A uses `v1` (which it got before the invalidation) — inconsistent but not dangerous
5. **Worse**: Thread A was mid-computation using `self._vectorizer` which now points to v2 while A still holds v1 reference — v2 gets garbage collected or v2 replaces v1 while some path still holds v1

**Specific dangerous pattern in `_get_node_vector`** (line 689):
```python
def _get_node_vector(self, conn, node_id):
    row = conn.execute(...).fetchone()
    if row:
        return json.loads(row["vector"])
    # Fall through: compute vector
    text = (node["label"] or "") + " " + (node["content"] or "")
    vec = self._load_vectorizer(conn).transform(text)
    # self._vectorizer could have been replaced DURING this call
    # by another thread that set self._vectorizer = None
```

### Approach: version-gated vectorizer with rebuilder gate

Replace the simple `self._vectorizer` cache with a versioned container:

```python
_vectorizer_data = None  # holds {"version": int, "vectorizer": TfidfVectorizer}
_vectorizer_version = 0  # incremented on every invalidation
```

**New `_load_vectorizer`**:

```python
def _load_vectorizer(self, conn: sqlite3.Connection = None) -> TfidfVectorizer:
    data = self._vectorizer_data
    if data is not None:
        # Version matches → safe to return cached copy
        return data["vectorizer"]

    should_close = False
    if conn is None:
        conn = sqlite3.connect(str(self.core_db_path))
        conn.row_factory = sqlite3.Row
        should_close = True

    try:
        cursor = conn.execute("SELECT content, label FROM nodes")
        texts = [(r["label"] or "") + " " + (r["content"] or "") for r in cursor.fetchall()]

        v = TfidfVectorizer()
        if texts:
            v.fit(texts)

        # Atomic check-then-set: only set if data is still None
        if self._vectorizer_data is None:
            self._vectorizer_data = {"version": self._vectorizer_version, "vectorizer": v}
        return v
    finally:
        if should_close:
            conn.close()
```

**New `_invalidate_vectorizer`** (replaces `self._vectorizer = None`):

```python
def _invalidate_vectorizer(self):
    self._vectorizer_data = None
    self._vectorizer_version += 1
```

**Call sites**:
- `_store_node` (line 945): `self._vectorizer = None` → `self._invalidate_vectorizer()`
- `rebuild_vector_index` (line 726): `self._vectorizer = None` → `self._invalidate_vectorizer()`

**Concurrent safety**:
- Python GIL protects individual attribute assignments on CPython
- The version counter ensures that a concurrent invalidation bumps the version, and a stale `_load_vectorizer` that finished building before the invalidation will still set `_vectorizer_data` only if `self._vectorizer_data is None` after its work — but this is still racy if two `_load_vectorizer` calls both see None simultaneously.

**Better approach: local variable + late assignment**:

```python
def _load_vectorizer(self, conn: sqlite3.Connection = None) -> TfidfVectorizer:
    data = self._vectorizer_data
    if data is not None:
        return data["vectorizer"]

    # Build outside the cache — assign to local first
    v = self._build_vectorizer(conn)

    # Only cache if no one else has cached since we started
    if self._vectorizer_data is None:
        self._vectorizer_data = {"version": self._vectorizer_version, "vectorizer": v}
    return v

def _build_vectorizer(self, conn):
    # ... pure build logic, no side effects ...
```

This way:
- All callers receive a valid vectorizer (either cached or freshly built)
- The cache is only written if it's still None (safe even under GIL)
- Invalidation forces rebuild on next call

**But the real question**: do we need full concurrency? In practice, AshaMemory is used single-threaded per instance. The real bug is silent state refresh where `self._vectorizer` is replaced while another code path holds a reference to the old one. The versioned container prevents this because each path gets its own vectorizer object and the cache only changes the container.

### Rollout
1. Replace `self._vectorizer` → `self._vectorizer_data` + `self._vectorizer_version` in `__init__`
2. Replace all `self._vectorizer = None` → `self._invalidate_vectorizer()`
3. Rewrite `_load_vectorizer` with version-gated cache
4. Keep `_get_node_vector` and `_compute_and_store_vector` unchanged (they call `_load_vectorizer`)

---

## Combined migration note

All three changes together require a `rebuild_vector_index()` call after upgrade:
- Tokenizer change → vectors computed with old char set are garbage under new rules
- Stemmer removal → node_index records keyed by stemmed words must be re-extracted
- Vectorizer versioning → no data migration needed (caches rebuild on demand)

Add a one-time check in `_init_core_db` that bumps an internal `lexicon_version` in `schema_meta` and auto-runs `rebuild_vector_index()` if the version differs.

---

## Implementation Order

| Step | Change | Files | Risk |
|------|--------|-------|------|
| 1 | Add shared `_tokenize()` function; replace 4 regex sites | `asha_memory_v2.py` | Low — mechanical replacement |
| 2 | Delete `_stem_word`; un-stem `_extract_keywords` return | `asha_memory_v2.py` | Low — removes code |
| 3 | Version-gate `_load_vectorizer`; replace `self._vectorizer = None` with `_invalidate_vectorizer()` | `asha_memory_v2.py` | Medium — changes caching contract |
| 4 | Add `lexicon_version` to `schema_meta`; auto-rebuild on mismatch | `asha_memory_v2.py` | Low — migration safety |
| 5 | Update `_final_check.py` and re-run | `_final_check.py` | Low — test maintenance |

---

## Issue 4 — Stopword list not adjusted for 2-char tokenizer (v2 follow-up)

**Root**: Lowering `min_len` from 3 to 2 in `_tokenize()` lets 2-letter words through.
The current `STOPWORDS` set was built for a 3-char floor and has no 2-letter entries.
Most 2-letter English words are noise; a few (like `ai`) are domain-relevant signal.

**Current 2-letter words that now enter the index** (previously blocked by `{3,}`):

| Word | Category | Keep/Filter |
|------|----------|-------------|
| `ai` | Domain: Artificial Intelligence | **KEEP** — high signal in this system |
| `go` | Programming language / verb | **FILTER** — too ambiguous, noise > signal |
| `it` | Pronoun | **FILTER** — pure noise |
| `to` | Preposition | **FILTER** |
| `in` | Preposition | **FILTER** |
| `is` | Verb | **FILTER** |
| `of` | Preposition | **FILTER** |
| `on` | Preposition | **FILTER** |
| `as` | Conjunction | **FILTER** |
| `at` | Preposition | **FILTER** |
| `be` | Verb | **FILTER** |
| `by` | Preposition | **FILTER** |
| `do` | Verb | **FILTER** (already a 2-letter verb but was blocked) |
| `he` | Pronoun | **FILTER** |
| `if` | Conjunction | **FILTER** |
| `me` | Pronoun | **FILTER** |
| `my` | Pronoun | **FILTER** |
| `no` | Determiner | **FILTER** |
| `or` | Conjunction | **FILTER** |
| `so` | Adverb | **FILTER** |
| `up` | Preposition | **FILTER** |
| `us` | Pronoun | **FILTER** |
| `we` | Pronoun | **FILTER** |
| `an` | Article | **FILTER** |
| `am` | Verb | **FILTER** |
| `hi` | Interjection | **FILTER** |

### Approach

**1. Add 25 two-letter noise words to STOPWORDS set (all except `ai`).**

```python
# New entries: common English 2-letter noise words
"to","in","is","it","of","on","as","at","be","by",
"do","go","he","if","me","my","no","or","so","up",
"us","we","an","am","hi"
```

These are words with high document frequency and near-zero semantic value in
a knowledge-graph context. They would otherwise inflate vector magnitude and
dilute cosine similarity without contributing signal.

**2. Keep `ai` in the index** — it is a domain-critical acronym for this system
(ASHA, AI entities, etc.). If it becomes noise in a particular corpus, IDF
will naturally demote it if it appears in every document.

**3. Bump `LEXICON_VERSION` from 2 to 3** to trigger auto-rebuild.

### Relationship to existing stopwords

Existing stopwords already cover many 3-letter noise words (`the`, `and`, `for`,
`are`, `but`, `not`, `you`, `all`, `can`, `had`, `her`, `was`, `one`, etc.).
The 2-letter additions fill the gap left by the `min_len` change. Together they
cover the full spectrum of English function words that the tokenizer can produce.

### No impact to sentiment

POSITIVE_WORDS and NEGATIVE_WORDS lists contain only 3+ letter words. Adding
2-letter stopwords doesn't affect sentiment scoring.

### Rollout

Single change: append 25 words to STOPWORDS in `asha_memory_v2.py`, bump
`LEXICON_VERSION` to 3, run rebuild. All 37 existing _final_check tests
continue to pass (they use 3+ letter queries like "Alice", "python", "node").
