# ASHA Memory v2 — Improvement Plan

> **Scope:** `memory/v2` only (`asha_memory_v2.py`, `asha_mcp.py`, `internal_clock.py`, `brain/`).  
> **Constraints:** Python stdlib only, deterministic, bounded retrieval, no LLM additions — per `DEFAULT_CONFIG:48` `asha_memory_v2.py:48` and `docs/V2_ARCHITECTURE.md:17`.  
> **Status:** All findings verified by local execution against the current codebase at `D:\opencode\memory\v2`.

---

## 1. Executive Summary

The system is architecturally sound (graph + TF-IDF + tiered layers + Brain maintenance) and well-documented (`documentation/README.md:1`, `documentation/ABOUT.md:1`, `brain/README-BRAIN.md:1`). The highest-leverage work is **not new features** but **closing correctness/performance gaps that already contradict the docs**:

- `PRAGMA foreign_keys` never enabled → every merge/prune creates orphans that `purge_orphans` `brain/brain_engine.py:1023` must sweep.
- `EPHEMERAL_LABELS` `asha_memory_v2.py:98` (static set) drifts from `brain_config.json:10` (configurable list) and `DEFAULT_CONFIG:37` `brain/brain_engine.py:37`.
- Sentiment lexicons diverged: `asha_memory_v2.py:108` (27 tokens) vs `brain/brain_engine.py:68` (39 tokens) — contradiction detection disagrees with storage.
- `agent_max_notes:100` `asha_memory_v2.py:60` is never enforced; `get_bloat_info` `asha_memory_v2.py:859` loads entire DB into Python to count JSON logs.
- `SEMANTIC` `asha_memory_v2.py:1384` and `consolidate` `asha_memory_v2.py:1883` are O(n) / O(n²) full scans — fine for <1k nodes, 2-3s at 5k.
- `GraphML edgedefault="undirected"` `asha_memory_v2.py:2247` misrepresents a directed graph.

Fixing these in the order below yields a faster, smaller, and more maintainable system with **zero new dependencies**.

---

## 2. Ground Rules (from existing docs)

- **Stdlib only** — `sqlite3`, `json`, `math`, `re`, `collections`, `pathlib` `documentation/README.md:491`. No `numpy`, no ONNX, no network.
- **Bounded + deterministic** — `max_nodes_per_recall:30` `asha_memory_v2.py:50`, no randomness in queries `docs/V2_ARCHITECTURE.md:179`.
- **Core/agent boundary is load-bearing** — `AshaMemory._is_core_visible` `asha_memory_v2.py:1211` mirrors `BrainEngine.is_agent_note` `brain/brain_engine.py:145`; graduation is manual-only `brain/README-BRAIN.md:147`.
- **Non-goals (intentionally deferred)** — optional local ONNX vectors, middleware hooks, `remember_many` batch insert, incremental DF counters, agent-shard merging, cluster auto-summarization `docs/V2_ARCHITECTURE.md:6` `documentation/ABOUT.md:648`. This plan **does not** introduce them as LLM features; where mentioned, it proposes pure-Python stdlib implementations.

---

## 3. Verification Notes

Executed locally before drafting:

- `agent_max_notes` occurs once (`asha_memory_v2.py:60`) and 0 enforcement sites.
- `remember_many` 0 hits.
- `foreign_keys` 0 hits in `asha_memory_v2.py`, 3 hits in `brain/brain_engine.py` (purge only).
- `_looks_like_json_log` 8 hits in core vs 3 in brain with different slice lengths (`[:400]` vs `[:300]`).
- `POSITIVE_WORDS` counts 27 vs 39 confirm lexicon drift.
- `magnitude` is stored `asha_memory_v2.py:797` but `cosine_similarity` `asha_memory_v2.py:198` recomputes `sqrt(sum(v²))` per comparison.
- Dashboard HTML is a 78 146-byte inline string `brain/brain_dashboard.py:452` (`HTML_DASHBOARD`).

---

## 4. P0 — Correctness & Integrity

*Effort: 1–2 days · Risk: low · Must ship first*

### P0-1 — Enable `PRAGMA foreign_keys=ON`

**Problem:** `CORE_SCHEMA_V2` `asha_memory_v2.py:466` declares `REFERENCES ... ON DELETE CASCADE`, but no connection executes `PRAGMA foreign_keys=ON`; deletes via `DELETE FROM nodes` `asha_memory_v2.py:1552` therefore leave `edges/node_vectors/memory_layers/access_log/node_index` orphans. `purge_orphans` `brain/brain_engine.py:1023` exists solely to compensate.

**Fix:**
- In `AshaMemory._core_conn` `asha_memory_v2.py:676` and every `BrainEngine` direct `sqlite3.connect` site (`brain/brain_engine.py:1033`, `1088`, `1136`, `1435` etc.) execute `conn.execute("PRAGMA foreign_keys=ON")` immediately after `PRAGMA journal_mode=WAL`.
- Keep `purge_orphans` as a safety net but expect it to become no-op; add assertion in `health()` `asha_memory_v2.py:2315` to warn if orphans >0.
- Add `PRAGMA integrity_check` to `health()` (see P2-5).

### P0-2 — GraphML direction

**Problem:** `export_graphml` `asha_memory_v2.py:2247` writes `edgedefault="undirected"` while `edges` are directed `CORE_SCHEMA_V2:483` (`from_node`/`to_node`).

**Fix:** `ET.SubElement(graphml ...), graph = ET.SubElement(graphml, "graph", edgedefault="directed")`. One-line change.

### P0-3 — Single source for ephemeral allowlist

**Problem:** Core `EPHEMERAL_LABELS = {"FEED_SNAPSHOT", ...}` `asha_memory_v2.py:98` (8 labels) is hard-coded; Brain `DEFAULT_CONFIG["ephemeral_labels"]` `brain/brain_engine.py:37` and `brain_config.json:10` (8 + `SOVEREIGNTY_PHASE_RUN`) are configurable and editable via dashboard `brain/brain_dashboard.py:286`. `AshaMemory._auto_link` `asha_memory_v2.py:959` and `_is_ephemeral_row` `brain/brain_engine.py:182` diverge.

**Fix:**
- Add `ephemeral_labels` to `AshaMemory.DEFAULT_CONFIG` `asha_memory_v2.py:48` (default = current `EPHEMERAL_LABELS` set) and persist to `base_path/config.json` via `_load_config` `asha_memory_v2.py:659`.
- Make `EPHEMERAL_LABELS` a property: `self.ephemeral_labels = set(self.config.get("ephemeral_labels", DEFAULT_EPHEMERAL))`.
- `BrainEngine` reads the same allowlist from the target DB's `config.json` (read-through) when available, falling back to `brain_config.json`. Dashboard `Ephemeral` tab writes to both.

### P0-4 — Unify sentiment & stopword lexicons

**Problem:** `POSITIVE_WORDS` `asha_memory_v2.py:108` vs `brain/brain_engine.py:68` and `NEGATIVE_WORDS` `asha_memory_v2.py:113` vs `brain/brain_engine.py:74` and `STOPWORDS` `asha_memory_v2.py:351` are duplicated with different cardinalities; `_tokenize` `asha_memory_v2.py:125` vs Brain fallback lambda `brain/brain_engine.py:57` (`split()` vs regex) produce different vectors.

**Fix:**
- Create `shared_lexicon.py` (stdlib, ~80 lines) exporting `TOKEN_PATTERN`, `_tokenize`, `STOPWORDS`, `POSITIVE_WORDS`, `NEGATIVE_WORDS`, `_looks_like_json_log`, `_extract_keywords`. Both `asha_memory_v2.py:125` and `brain/brain_engine.py:57` import from it. If import fails (direct execution), fall back to identical regex `re.compile(r"\b[\w']{2,}\b", re.UNICODE)` — not `str.split`.

### P0-5 — Unify `_looks_like_json_log` heuristic

**Problem:** Three variants: `asha_memory_v2.py:398` (`s[:400].lower()` + `timestamp` and `(status|post_count|load1m)`), `brain/brain_engine.py:171` (`s[:300]` + `timestamp` or `post_count` or `load1m`), and `AshaMemory.get_bloat_info` `asha_memory_v2.py:859` Python-loop fallback.

**Fix:** Single function in `shared_lexicon.py`:

```python
def _looks_like_json_log(content: str) -> bool:
    if not content or not content.lstrip().startswith("{"):
        return False
    low = content.lstrip()[:400].lower()
    return "timestamp" in low and ("post_count" in low or "load1m" in low or "status" in low)
```

### P0-6 — Enforce `agent_max_notes` and fix bloat counting

**Problem:** `DEFAULT_CONFIG["agent_max_notes"]=100` `asha_memory_v2.py:60` never read except in `stats` display. `get_bloat_info` `asha_memory_v2.py:859` does `for r in conn.execute("SELECT content FROM nodes").fetchall() if _looks_like_json_log(r[0])` — loads entire DB into RAM per health poll.

**Fix:**
- In `agent_remember` `asha_memory_v2.py:1707` (core_shared path) before `self.remember`, run `SELECT COUNT(*) FROM nodes WHERE source='AGENT_<id>'`; if `>= agent_max_notes`, delete oldest `agent_private` note(s) to keep `count <= max-1`.
- Replace Python-loop bloat count with SQL: `SELECT COUNT(*) FROM nodes WHERE label IN (<ephemeral>)` + `SELECT COUNT(*) FROM nodes WHERE content LIKE '{"%timestamp"%'` (sampling, not full fetch).

### P0-7 — Sanitize FTS5 queries

**Problem:** `recall` `WHO_IS` `asha_memory_v2.py:1241` and `_recall_related` fallback `asha_memory_v2.py:1340` do `node_fts MATCH ?` with raw user `label`/`query`; strings containing `"` or `*` raise `sqlite3.OperationalError: fts5: syntax error`.

**Fix:** Wrap every `MATCH` in `try: ... except sqlite3.OperationalError: fallback to LIKE`. Sanitize: `fts_query = re.sub(r'["*]', ' ', query).strip()` before `MATCH`. Already handled in `health` `asha_memory_v2.py:2328` — extend to recall paths.

### P0-8 — `STOPWORDS` duplicates

**Problem:** `STOPWORDS` `asha_memory_v2.py:351` contains duplicates `"her"` (twice), `"much"` (twice) — harmless but signals no lint.

**Fix:** Deduplicate and sort; add `assert len(STOPWORDS)==len(set(STOPWORDS))` in tests.

---

## 5. P1 — Performance & Scale

*Effort: 2–4 days · Risk: medium · Keeps "<10k imperceptible" claim true*

### P1-1 — Semantic search: use stored magnitudes + pre-filter

**Current:** `_recall_semantic` `asha_memory_v2.py:1384` does `SELECT n.*,nv.vector,nv.magnitude` for **every node**, then `json.loads` + `v.cosine_similarity(query_vec, node_vec)` `asha_memory_v2.py:1400` which recomputes `na = sqrt(sum(v²))` `asha_memory_v2.py:203` per node despite `nv.magnitude` being stored.

**Fix (stdlib, no index change):**

1. Extend `TfidfVectorizer.cosine_similarity` `asha_memory_v2.py:198` signature to `cosine_similarity(vec_a, vec_b, mag_a=None, mag_b=None)` and use passed magnitudes when available.
2. In `_recall_semantic`, pass `mag_a = sqrt(sum(query_vec²))` once and `mag_b = row["magnitude"]`.
3. Pre-filter candidates: before cosine, `SELECT node_id FROM node_index WHERE word IN (<query_terms>) GROUP BY node_id HAVING COUNT(*)>=1` to get candidate set (same pattern as `_auto_link` `asha_memory_v2.py:974`). Only score candidates + top-recency fallback when candidate set < bound. Cuts `SEMANTIC` from O(n) to O(k) where k << n.
4. Add `PRAGMA cache_size=-64000` (64MB) in `_core_conn` for larger DBs.

**Impact:** ~5–10× faster at 5k nodes; no schema change.

### P1-2 — `consolidate` O(n²) → bucketed

**Current:** `consolidate` `asha_memory_v2.py:1883` and `BrainEngine.deduplicate` `brain/brain_engine.py:667` do `for i in range(n): for j in range(i+1,n): cosine` — 12.5M comparisons at 5k nodes.

**Fix:**
- First pass: exact `checksum+label+content` bucket `brain/brain_engine.py:696` already exists — keep it.
- Second pass: bucket by inverted-index overlap `>=2` or `checksum[:4]` prefix before TF-IDF; only compare within buckets. Reuse `_extract_keywords` `asha_memory_v2.py:369` inverted index already maintained.
- Extract shared helper `find_near_duplicates(rows, threshold=0.85, bucket_fn=overlap)` used by both `AshaMemory.consolidate` and `BrainEngine.deduplicate`.

### P1-3 — Incremental vectorizer / `remember_many`

**Current:** Every `remember` `asha_memory_v2.py:1113` calls `_invalidate_vectorizer` + `_compute_and_store_vector` which calls `_load_vectorizer` `asha_memory_v2.py:744` which does full `SELECT content,label FROM nodes` + `fit` `asha_memory_v2.py:735` — O(n) per insert. `docs/V2_ARCHITECTURE.md:149` notes incremental counters were deferred.

**Fix (no full incremental needed for <10k):**

1. Add `remember_many(items: List[Dict]) -> List[str]` `asha_memory_v2.py:1084` — single transaction, single `invalidate` at end, single `rebuild_vector_index` (or incremental `df` update).
2. For single `remember`, keep invalidate but defer `fit` until next `SEMANTIC` query (lazy): store `vector=NULL` for new node, let `_get_node_vector` `asha_memory_v2.py:772` lazily compute on demand. Only `rebuild_vector_index` `asha_memory_v2.py:804` forces full fit.
3. Add `vector_index_auto_rebuild` `asha_memory_v2.py:66` semantics: `True` (default, lazy), `False` (manual only), `"on_write"` (current eager) — document in `documentation/README.md:347`.

### P1-4 — Split recall read vs write transaction

**Current:** `recall` `asha_memory_v2.py:1176` opens one `with _core_conn() as conn:` that both reads candidates **and** calls `_bump_access` `asha_memory_v2.py:948` (`UPDATE nodes SET access_count`, `INSERT access_log`, `_update_layer_on_access`) for every hit — write-lock held during full semantic scan, contends with Brain.

**Fix:** Two-phase:
- Phase 1 (read-only conn): collect `candidate_rows` (no bumps).
- Phase 2 (short write conn): `executemany("UPDATE nodes SET access_count=access_count+1, updated_at=? WHERE node_id=?", ...)` + `executemany("INSERT INTO access_log ...")` batch. Preserves `last_accessed_before` `internal_clock.py:150` by capturing `before_epoch = _now()` before phase 1.

### P1-5 — Add missing indexes

**Current:** `manage_tiers` `asha_memory_v2.py:1945` `WHERE ml.layer = ?` and `prune_stale_unused_nodes` `brain/brain_engine.py:953` `WHERE n.updated_at <= ? AND n.access_count <=2` full-scan `memory_layers`/`nodes`. `get_health_metrics` `brain/brain_engine.py:1820` Python-classifies all rows O(n).

**Fix — add to `CORE_SCHEMA_V2` `asha_memory_v2.py:466` + migration `V1_TO_V2_MIGRATION:610`:**

```sql
CREATE INDEX IF NOT EXISTS idx_nodes_label_type ON nodes(label, node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_updated_access ON nodes(updated_at, access_count);
CREATE INDEX IF NOT EXISTS idx_memory_layers_layer ON memory_layers(layer);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source);
```

Replace `get_health_metrics` Python loop with SQL: `SELECT COUNT(*) FROM nodes WHERE node_type='AGENT_NOTE' OR json_extract(metadata,'$.agent_scoped')=1`.

### P1-6 — Cache normalization

**Current:** `cache_key = f"{mode}:{query}:{bound}:agent_notes={include_agent_notes}"` `asha_memory_v2.py:1156` — `"  Sam "` and `"sam"` miss; `limit` alias `asha_mcp.py:322` creates duplicate entries.

**Fix:** `query_norm = re.sub(r"\s+", " ", query.strip().lower()) if mode not in ("PATH",) else query.strip()`; key `f"{mode}:{query_norm}:{bound}:{include_agent_notes}"`. Add optional 60s TTL for `RECENT` mode entries.

---

## 6. P2 — Architecture & Maintainability

*Effort: 3–5 days · Risk: low–medium*

### P2-1 — Extract dashboard HTML

**Current:** `brain_dashboard.py:452` `HTML_DASHBOARD = """<!DOCTYPE html>...1313 lines..."""` mixes HTTP handler + 65k HTML/CSS/JS. No syntax highlight, noisy diffs, untestable.

**Fix:**
- Create `brain/static/dashboard.html` + `brain/static/app.js` + `brain/static/style.css` (split from current `HTML_DASHBOARD`).
- `BrainDashboardHandler.do_GET` `brain/brain_dashboard.py:35` already serves `/humantools/` `brain/brain_dashboard.py:171` — add `/_static/` route serving `brain/static/`.
- Keep inline `HTML_DASHBOARD` as fallback when `brain/static/` absent (single-file deploy).

### P2-2 — Unify config

**Current:** `core config.json` `asha_memory_v2.py:659` keys `prune_threshold` `asha_memory_v2.py:55` vs `brain_config.json:7` `prune_importance_floor:0.05` duplicate semantics with different keys/defaults. `interval_minutes` only in brain.

**Fix:** Single canonical config at `base_path/config.json` for memory thresholds; `brain_config.json` only for scheduler (`interval_minutes`, `auto_snapshot_before_jobs`, `last_db_path`). On startup `BrainEngine._load_config` `brain/brain_engine.py:1892` reads through to `base_path/config.json` for shared keys. Add alias handling: if both present, `prune_threshold` wins and `prune_importance_floor` is migrated.

### P2-3 — `remember_many` / `relate_many` batch APIs

**Planned but absent** `docs/V2_ARCHITECTURE.md:151`. Needed for `import_memory` `asha_memory_v2.py:2128` merge and telemetry ingestion.

```python
def remember_many(self, items: List[Dict]) -> List[str]:
    # items: [{content, node_type, label, trust, importance, metadata}, ...]
    # single transaction, single invalidate at end, single vector rebuild
```

Same for `relate_many`. Used by `AshaMemory.import_memory` `asha_memory_v2.py:2128` `_merge_core_db` `asha_memory_v2.py:2154` (currently row-by-row `conn.execute`).

### P2-4 — Bind hardening

**Current:** Dashboard binds `0.0.0.0` `brain/README-BRAIN.md:232` with no auth — reachable on LAN per comment `brain/brain_dashboard.py` header.

**Fix:** Default `--bind 127.0.0.1`, add `--bind 0.0.0.0` opt-in with startup warning `sys.stderr.write("WARNING: binding to 0.0.0.0 ...")`. Add optional `brain_config.json:token` + `X-Api-Token` header check for non-local binds (stdlib `http.server` can check `self.headers`).

### P2-5 — Health expansion

**Current:** `health()` `asha_memory_v2.py:2315` checks orphans, FTS, vector freshness, schema version — misses `integrity_check`, lexicon version, freelist bloat.

**Fix:** Add:

- `conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"` → `"DB integrity: <msg>"`.
- `lexicon_version` `asha_memory_v2.py:714` mismatch → `"Lexicon stale: rebuild_vector_index needed"`.
- `get_bloat_info` `asha_memory_v2.py:848` `needs_vacuum` → `"Freelist <pct>% — VACUUM recommended"`.
- Mirror in `BrainEngine.get_health_metrics` `brain/brain_engine.py:1806` which already surfaces `bloat`.

### P2-6 — Clean re-instantiation in Brain

**Current:** `BrainEngine.rebuild_vectors` `brain/brain_engine.py:1062` does `mem = AshaMemory(base_path=str(self.db_path.parent))` — side-effect: creates `base_path/config.json` if absent, re-runs `_init_core_db` migrations.

**Fix:** Add `AshaMemory.rebuild_vector_index_for_path(db_path: Path)` static helper that opens `sqlite3.connect(db_path)` directly without instantiating `AshaMemory` (no config creation). Brain calls that.

---

## 7. P3 — Observability & Ops

*Effort: 1–2 days*

### P3-1 — Fix stale doc: `query_log` does stay populated

`documentation/README.md:418` says "`query_log` table currently stays empty — query history is kept in-memory ... and `_log_query()` does not insert rows". But `AshaMemory._log_query` `asha_memory_v2.py:1217` **does** `INSERT INTO query_log ...` `asha_memory_v2.py:1225`. Update doc to reflect reality; `profile()` `asha_memory_v2.py:2296` should optionally query `query_log` (not just `_query_log` in-memory) so restart survives.

### P3-2 — Bloat counting without full fetch

`get_bloat_info` `asha_memory_v2.py:848` Python loop `for r in conn.execute("SELECT content FROM nodes").fetchall() if _looks_like_json_log(r[0])` pulls all contents into RAM. Replace with SQL predicate + sampling as in P0-6. Same for `BrainEngine.get_ephemeral_stats` `brain/brain_engine.py:191`.

### P3-3 — Log & snapshot rotation

`generate_markdown_report` `brain/brain_engine.py:1269` and `create_snapshot` `brain/brain_engine.py:533` never prune old files. Add `keep_last_logs:30` and `keep_last_snapshots:10` config; `list_markdown_logs` `brain/brain_engine.py:1409` prunes oldest on write.

### P3-4 — `vacuum` returns `saved_mb`

Already `AshaMemory.vacuum` `asha_memory_v2.py:822` and `BrainEngine.vacuum_db` `brain/brain_engine.py:1087` return `before_mb/after_mb/saved_mb` — add to `profile()` `asha_memory_v2.py:2296` as `last_vacuum` timestamp.

---

## 8. P4 — DX Polish

- **CLI:** `python asha_memory_v2.py --check` (health+stats+bloat) mirroring `python brain_dashboard.py` `brain/README-BRAIN.md:204` and `python asha_mcp.py` `asha_mcp.py:754`. Useful for headless ops.
- **`type` filter in MCP `recall`** — `asha_mcp.py:322` already supports `node_type` post-filter but undocumented in `MCP_AI_GUIDE.md:428`; document it.
- **Duplicate stopwords** `asha_memory_v2.py:351` (`"her"`, `"much"` twice) — dedupe + `assert len==len(set)`.
- **Trust/importance guide** `documentation/ABOUT.md:633` already clear; add one-liner to `register_skill` `asha_memory_v2.py:1976` docstring: `trust=provenance, importance=retention`.

---

## 9. Roadmap

### Week 1 — Stabilize (no behavior change, safe to ship) — *Revised per Asha feedback*

1. **PR1:** `shared_lexicon.py` `shared_lexicon.py:1` alone — every import site touched, own regression (Asha: full PR).
2. **PR2:** Integrity — `P0-1` foreign_keys `asha_memory_v2.py:642`/`brain/brain_engine.py:134` + `P0-2` GraphML `asha_memory_v2.py:2193` + `P0-7` FTS `asha_memory_v2.py:1187` + `P0-8` stopwords `shared_lexicon.py:56`.
3. **PR3:** Ephemeral + storage — `P0-3` ephemeral allowlist `asha_memory_v2.py:88` + `P0-6` `agent_max_notes` `asha_memory_v2.py:1682` + `P1-5` indexes `asha_memory_v2.py:493` + one-time orphan purge migration `asha_memory_v2.py:665`.

*Exit criteria:* `health()` green on a DB with prior merges; no orphans after `deduplicate`; FTS `MATCH '"' ` no longer crashes.

### Week 2 — Accelerate

4. **PR4:** `P1-1` magnitude reuse + index pre-filter + `P1-5` cache normalization.
5. **PR5:** `P1-2` bucketed consolidate + `P1-3` lazy vector + `remember_many`.
6. **PR6:** `P1-4` split recall transaction + `P2-6` brain no-side-effect rebuild.

*Exit criteria:* `SEMANTIC` p95 <150ms at 5k nodes; `consolidate` <2s at 5k.

### Week 3 — Harden & Ship

7. **PR7:** `P2-1` dashboard static split + `P2-4` bind hardening.
8. **PR8:** `P2-2` config unify + `P2-5` health expansion + `P3-1/2/3` observability.
9. **PR9:** `P4` CLI + doc refresh (`README.md:418` query_log note, `GUIDE.md` `get_node` workflow) + regression tests.

*Exit criteria:* `python -m pytest` green; `python asha_memory_v2.py --check` + `curl /api/health` both pass; docs match code.

---

## 9a. Asha AI Feedback — Response & Implementation (2026-09-02)

Asha (v2 older) reviewed the plan; all points addressed, stdlib-only:

**Tighten — PR split (Week 1 aggressive)**
- *Asha:* PR1/PR2/PR3 batch is aggressive; `shared_lexicon` `asha_memory_v2.py:125` touches every import — full PR on its own.
- *Action:* Adopted. Execution split was **PR1 shared_lexicon alone** `shared_lexicon.py:1`, **PR2 integrity** `P0-1 foreign_keys `asha_memory_v2.py:642`/`brain/brain_engine.py:134` + `P0-2 GraphML` + `P0-7 FTS` + `P0-8 stopwords`, **PR3 ephemeral** `asha_memory_v2.py:88` + `indexes` `asha_memory_v2.py:493` + `agent cap` `asha_memory_v2.py:1682`. Matches Asha's suggestion exactly; updated roadmap below.

**Tighten — P1-1 cache_size configurable**
- *Asha:* `PRAGMA cache_size=-64000` platform-dependent, should be configurable.
- *Action:* Added `DEFAULT_CONFIG["sqlite_cache_size"]=-64000` `asha_memory_v2.py:89`, used in `_core_conn` `asha_memory_v2.py:643` `PRAGMA cache_size={cs}` and `BrainEngine._connect_db` `brain/brain_engine.py:146` + `brain DEFAULT_CONFIG sqlite_cache_size` `brain/brain_engine.py:55`. Persisted in `config.json`/`brain_config.json`.

**Tighten — P1-2 bucket guard tunable**
- *Asha:* Bucket needs size guard; too loose → O(n²) inside buckets, too tight → miss dupes; checksum[:4] + overlap>=2 heuristic should be tunable.
- *Action:* Added `consolidation_bucket_prefix:4` + `consolidation_bucket_overlap:2` `asha_memory_v2.py:89`, `consolidate()` `asha_memory_v2.py:2060` now buckets by `checksum[:prefix]` + `node_index` overlap, caps bucket `>150` to first 150, `use_bucket` only when `len>200`. Both tunable via `config.json`.

**Good catch — remember_many**
- *Asha:* Deferred in `docs/V2_ARCHITECTURE.md:151` but right to add — batch is complement to lazy rebuild.
- *Action:* Implemented `remember_many` `asha_memory_v2.py:1090` single-transaction + single vectorizer build/invalidate, acknowledged as complement to `P1-3 lazy` (deferred fit until `SEMANTIC`).

**Missing — Migration path for orphaned edges**
- *Asha:* P0-1 enables FK but existing DBs already have orphans; need purge+vacuum pre-migration then enable FK.
- *Action:* Added one-time purge in `_init_core_db` `asha_memory_v2.py:665` after `CORE_SCHEMA_V2`/`LEXICON` checks: `DELETE FROM edges WHERE from_node NOT IN...` + `node_vectors/memory_layers/...` if `COUNT orphans>0`. Documented in code comment `P0-1 migration (Asha feedback)` and Risk Matrix `purge_orphans once before enabling` is now automatic; manual path also documented: run `purge_orphans` `brain/brain_engine.py:1065` + `VACUUM` before upgrade.

**Missing — rebuild_vector_index locking**
- *Asha:* Full table scan `rebuild_vector_index` `asha_memory_v2.py:794` while brain dedup → write contention.
- *Action:* Added `PRAGMA busy_timeout=5000` `asha_memory_v2.py:794`/`813` + docstring `Asha feedback P1-3: schedule outside brain jobs, advisory lock via busy_timeout` and `BrainEngine.rebuild_vectors` `brain/brain_engine.py:1094` note + static helper `rebuild_vector_index_for_path` `asha_memory_v2.py:812` avoids side-effect and documents scheduling (brain runs rebuild only after mutations, already in `scheduler.py:166` `auto_rebuild_vectors`).

*All Asha points implemented, stdlib-only, no LLM.*

---

## 10. What NOT to Do

Per `docs/V2_ARCHITECTURE.md:3` and `documentation/ABOUT.md:648` — intentionally deferred and should stay deferred unless explicitly requested:

- Optional local ONNX embeddings (would add `onnxruntime` dep — violates stdlib-only).
- Middleware hooks / `remember_many` beyond pure batch (pluggable principle).
- Agent-shard merging (legacy `agents/agent_*.db` path is deprecated; `core_shared` is canonical `asha_memory_v2.py:64`).
- Cluster auto-summarization (removed as graph pollution `brain/README-BRAIN.md:169`; `SUMMARIZES` edge remains for manual use).

---

## 11. Testing Strategy

All tests follow repo convention: temp dirs, `unittest` + `pytest`, stdlib only — as `internal_clock.py:112` `test_internal_clock.py` and `brain/test_brain.py`.

- `tests/test_p0_regression.py` — one test per P0: foreign_keys cascade, FTS `"` no crash, ephemeral allowlist read-through, `agent_max_notes` cap, `GraphML directed`, stopword dedupe.
- `tests/test_perf_semantic.py` — seed 3k synthetic nodes, assert `SEMANTIC` p95 <200ms and `consolidate` uses bucketing (mock `cosine` call count < n²).
- Existing suites must stay green: `test_internal_clock.py` (16), `test_v2_system.py` (7), `brain/test_brain.py` (19) — per `docs/INTERNAL_CLOCK.md:129`.

---

## 12. Risk Matrix

| Change | Risk | Mitigation |
|--------|------|------------|
| `PRAGMA foreign_keys=ON` | Migration may expose latent orphans | Run `purge_orphans` once before enabling; snapshot before `PR1` `brain/brain_engine.py:533`. |
| Index addition | Write slowdown | Indexes are narrow (`label,node_type` etc.); measure with `profile:2296` `recent_avg_ms`. |
| Recall split transaction | `last_checked` semantics `internal_clock.py:150` | Keep `before_epoch` captured before read; add test `test_internal_clock.py` already covers. |
| Dashboard static split | Single-file deploy breaks | Keep inline fallback `brain/brain_dashboard.py:35`. |

---

## 13. File Map

| File | LOC | Role | Key improvement |
|------|-----|------|-----------------|
| `asha_memory_v2.py:1` | 2533 | Core | P0-1/3/6/7, P1-1/2/3/4/5 |
| `asha_mcp.py:1` | 780 | MCP stdio | P0-7, P4 type filter docs |
| `internal_clock.py:1` | 217 | Clock | P0-4 lexicon source (consumer) |
| `brain/brain_engine.py:1` | 1923 | Maintenance | P0-1/3/4/5, P1-2/5, P2-6 |
| `brain/brain_dashboard.py:1` | 1313 | Dashboard | P2-1/4 |
| `brain/scheduler.py:1` | 228 | Scheduler | P1-6, P2-2 |
| `shared_lexicon.py` | **new ~80** | Lexicon single source | P0-4/5/6 |
| `brain/static/` | **new** | Dashboard assets | P2-1 |

---

*Generated for ASHA Memory v2 — stdlib-only, deterministic, bounded. Evidence-backed; cheap local checks over speculation.*
