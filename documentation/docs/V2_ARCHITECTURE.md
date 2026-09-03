# ASHA_MEMORY_SYSTEM v2 — Architecture

> **Status note:** this document is the v2 design record. Most items below are
> implemented in `asha_memory_v2.py`, but a few planned extras are **not**:
> optional local ONNX vectors, middleware hooks (Pluggable principle),
> incremental document-frequency counters (the vectorizer is rebuilt on demand from all nodes — see §8), agent-shard merging,
> and cluster auto-summarization (later removed as graph pollution — see
> `brain/README.md`). `remember_many()` batch insert **is implemented** (`asha_memory_v2.py:1198`, single transaction + single vectorizer rebuild) — see §8.

## Guiding Principles (preserved from v1)

| Principle | v1 | v2 |
|-----------|----|----|
| 100% Local | SQLite stdlib only | SQLite + stdlib + optional local ONNX |
| Context-Safe | Hard bounds on retrieval | Hard bounds + relevance thresholds |
| Agent-Isolated | Separate DB files | Same + cross-agent query with CORE gate |
| Portable | Single file, runs anywhere | Single file + optional perf extensions |
| Pluggable | Drop-in module | Same + middleware hooks |

## What Changes and Why

### 1. Retrieval: Keyword → TF-IDF Vector

**v1 problem:** Keyword matching misses synonyms, different phrasings, related concepts.
`"likes minimalism"` and `"prefers simple tools"` — same meaning, zero word overlap.

**v2 approach:** Pure-Python TF-IDF vectorization with cosine similarity. No external libs, no
API calls. Each node gets a sparse TF-IDF vector built from the corpus at query time or
maintained incrementally. Retrieval ranks by cosine distance instead of keyword count.

**Tokenizer:** Uses Unicode-aware `\b[\w']{2,}\b` with `re.UNICODE` flag — captures non-English
(`français`, `Müller`, `über`), contractions (`don't`, `it's`), usernames (`user123`),
and short terms (`AI`, `go`). Lowered `min_len` to 2 since IDF naturally demotes noise.
Lexicon version is tracking at `LEXICON_VERSION = 3` (combining Unicode tokenization,
no stemmer, and 2-letter stopword filtering).

**Stemmer removed.** v2 had a naive suffix stripper (`ing`, `ed`, `er`, `tion`, `ness`,
`ment`) that corrupted words like `education→educa`, `attention→atten`. Since IDF already
handles variant conflation across documents and the stemmer created inconsistency between
RELATED (stemmed keywords) and SEMANTIC (raw tokens) modes, it was removed entirely.

**Vectorizer stability.** The vectorizer cache is version-gated (`_vectorizer_data` +
`_vectorizer_version`) with a check-then-set guard. `_invalidate_vectorizer()` bumps the
version and clears the cache atomically. This prevents silent state refresh where a
concurrent invalidation replaces the vectorizer mid-use.

**Trade-off:** Slightly more CPU per query. O(n * m) vs O(k) for keyword. For <10k nodes
this is imperceptible. Add optional incremental index for >10k.

### 2. Consolidation: Jaccard → Cosine on TF-IDF

**v1 problem:** Jaccard on word sets counts "the" and "algorithm" equally. Merges unrelated
nodes with high stopword overlap, misses semantically similar nodes with different vocabulary.

**v2 approach:** Cosine similarity on TF-IDF vectors. Same merge thresholds (0.85 high,
0.50 link) but on vector similarity, not word-set Jaccard. Catches "graph beats vector"
≈ "graph outperforms vector search" — 80%+ TF-IDF cosine.

### 3. Contradiction: Regex → Sentiment-Weighted (filtered)

**v1 problem:** Only catches direct negation patterns (`not`, `hates` vs `likes`) and
hardcoded antonym pairs. Misses "I prefer X" vs "X doesn't work for me" — same topic,
opposite stance, no trigger words. Also false-positives on `it,is,and` stopwords.

**v2 approach:** Score each FACT on a positive/negative sentiment axis using a curated
word list (no ML). When two FACTS on the same topic have opposite sentiment polarity
and **meaningful overlap ≥2** (`len>3` and not in `STOPWORDS` `shared_lexicon.py:56` — filters `it,is,and`) flag as contradiction (`asha_memory_v2._detect_contradiction_v2:354`, `brain_engine.detect_contradictions:1546`). Sentiment polarity is stored as a
node attribute for future queries.

**v2 approach — Pattern Expansion:** Match structural patterns beyond negation:
- Preference: "I [verb] X" vs "I [antonym-verb] X"
- Comparison: "X beats Y" vs "Y beats X" → semantic reversal
- Certainty: "X is [definitely|always]" vs "X is [maybe|sometimes]" → confidence conflict

### 4. Memory Layers: Flat → Tiered

**v1 problem:** All nodes share one importance decay curve. Frequently accessed facts
decay the same as one-off observations.

```
v1: nodes → decay → prune
v2: nodes → working → short-term → long-term → archive
```

| Layer | Duration | Decay | Access boost | Capacity |
|-------|----------|-------|-------------|----------|
| Working | Session | None | N/A | 20 nodes (agent cap 12, janitor `Score=acc*Wa+imp*Wi-ageH*Wd` → `short_term` when `≥12` or `age≥48h`, core untouched) |
| Short-term | Days | 0.97/day | 0.10/access | 500 nodes |
| Long-term | Months | 0.995/day | 0.05/access | 5000 nodes |
| Archive | Forever | None | N/A | Unlimited |

Nodes promote to longer-term layers on repeated access. Agent `WORKING` overflow is scope-aware (`AshaMemory._update_layer_on_access:971` per-scope evict; `brain_engine.regulate_agent_working_memory:1117` + Observer `days_left` `GET /api/agent_working_preview`). This keeps frequently
relevant info fresh without manual importance tuning and prevents agent scratchpad bloat.

### 5. Retrieval Modes: Extend with Semantic + Path

**v1 modes:** WHO_IS (1-hop), WHAT_ABOUT (2-hop), RECENT, RELATED, PRUNE

**v2 adds:**
- **SEMANTIC** — TF-IDF cosine similarity across all nodes, ranked by relevance.
  Returns nodes that mean the same thing even with different wording.
- **PATH** — Find shortest weighted path between two nodes. Answers "how is SAM
  connected to the Python project?" via graph traversal.
- **CLUSTER** — Return all nodes within N hops of a topic, grouped by type and
  summarized. For "give me everything about SAM's preferences."
- **TIMELINE** — Chronological reconstruction of EVENTS connected to a PERSON
  or TOPIC. Orders by created_at, filters by edge type.

### 6. Agent Memory: Read-Only Cross-Shard Queries

**v1:** Agents write to their own shard. CORE reads via agent_digest(). No cross-agent
or agent-to-agent queries.

**v2 adds:**
- **CORE cross-query:** `find_across_agents(topic, min_confidence)` explicitly
  searches attention-scoped agent notes in the shared graph (`core.db` `core_shared` mode). Normal core recall
  remains free of raw agent work (boundary `BrainEngine.is_agent_note:176` mirrors `AshaMemory._is_core_visible`).
- **Agent-to-agent ref:** Agent can write `REFERS_TO(agent_id, node_id)` — a reference
  to another agent's finding. CORE resolves and links.
- **Agent merge:** *Not implemented* — remains in `legacy_shards` planning only; `core_shared` is canonical (`asha_memory_v2.py:78`). Would be dedup by content checksum if built.

### 7. Query DSL: Programmatic Memory Access

**v1:** All access via Python method calls (`memory.recall(...)`, `memory.relate(...)`)

**v2 adds a query string DSL for concise, composable memory access:**

```
FIND PERSON "SAM" -> PREFERENCE        # 1-hop from SAM to preferences
FIND FACT WHERE trust > 0.8            # filter by attribute
FIND PATH "SAM" -> "python_project"    # shortest path
FIND SEMANTIC "likes simple tools"     # TF-IDF similarity
FIND TIMELINE "SAM" SINCE "2026-01-01"  # chrono events
```

Parsed by a small recursive descent parser in ~100 lines of stdlib Python. Returns
the same `RecallResult` type.

### 8. Performance: Incremental Index + Query Cache

**v1:** Keyword index rebuilt on every node insert via INSERT OR REPLACE.
Full table scan for consolidation.

**v2 (implemented):**
- **TF-IDF index:** `remember` invalidates vectorizer and defers `fit` until next `SEMANTIC` (lazy `vector_index_auto_rebuild:True`) or single rebuild via `remember_many` (P1-3). Full incremental DF counters are *deferred* — current implementation rebuilds on demand from all nodes (`_load_vectorizer` full `SELECT content,label FROM nodes` + `fit`). Planned incremental counters would update DF on insert and rebuild only after >20% growth.
- **Stored magnitude reuse:** `TfidfVectorizer.cosine_similarity(mag_a, mag_b)` reuses `node_vectors.magnitude` (P1-1) instead of recomputing `sqrt(sum(v²))`; `SEMANTIC` pre-filters via `node_index` overlap (`HAVING COUNT(*)>=1`) to O(k) candidates.
- **Consolidation bucketing:** `consolidate`/`deduplicate` bucket by `checksum[:4]` + `node_index` overlap>=2, cap 150 per bucket, only when `len>200` (P1-2 `consolidation_bucket_prefix:4`/`overlap:2` tunable).
- **Query result cache:** LRU cache for recent recall() results (configurable size, default 50, normalized key `lower()` + whitespace collapse except `PATH`). Bumps access counts on hit (split read/write transaction, P1-4). Invalidated on relevant node write.
- **Batch insert:** `remember_many([...])` `asha_memory_v2.py:1198` — single transaction for bulk loading (implemented, not planned).

### 9. Serialization: Export/Import with Schema Migration

**v1:** Simple tar.gz of DB files. No version compatibility checks.

**v2:**
- Export includes schema version + migration scripts
- Import checks version and auto-migrates if needed (forwards compatible)
- Export as JSON option for interop with non-Python tools:
  ```
  memory.export_json(path) → nodes.json, edges.json, agents/
  ```
- Export as graphml for visualization tools (Gephi, yEd)

### 10. Monitoring & Introspection

**v1:** `memory.stats()` returns basic counts.

**v2 adds:**
- `memory.profile()` — per-query timing, cache hit rate, index freshness
- `memory.health()` — integrity check, orphaned edges, broken FTS, size estimates
- `memory.graphml(path)` — export for visual graph exploration

## What Stays the Same

- Node types + edge types schema (backward compatible) — `GraphML` now `edgedefault="directed"` (`asha_memory_v2.py:2501`, P0-2)
- Bounded retrieval (hard caps preserved, configurable) — `agent_max_notes:100` enforced (P0-6), FTS sanitized (P0-7)
- Agent shard isolation model — `PRAGMA foreign_keys=ON` enforced (`asha_memory_v2.py:647`, P0-1), ephemeral allowlist unified 10 labels (`shared_lexicon.py:86`, P0-3)
- Deterministic (no randomness, reproducible queries)
- Stdlib-first philosophy (TF-IDF, cosine, graph traversal all pure Python)

## What Goes Away

- Raw keyword matching as primary retrieval (replaced by TF-IDF)
- Jaccard-based consolidation (replaced by cosine)
- Flat decay (replaced by tiered memory layers)
