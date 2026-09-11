# Benchmarks

Harness: `bench/bench_recall.py` (stdlib only) — deterministic 10k seed (topics cycling,
chain edges, EVENT fan), p50/p95 per mode, `--reps`, `--mode`, `--bound`,
`--concurrent` (writer thread inserts while dedup runs: asserts zero loss/errors).

## Verdict @10k, cold, bound=30 (Phase 9 gate — v3 vs v2 same fixture)

| Mode | v3 | v2 cold | × |
|---|---|---|---|
| RELATED | 53 | 135 | **2.5×** |
| SEMANTIC | 171 | 262 | **1.5×** |
| TIMELINE | 9.5 | 73 | **7.7×** |
| PATH | 884 | 12,548 | **14×** (heapq vs O(V²)) |
| CLUSTER | 30 | 100 | **3.3×** (deque) |
| WHO_IS | 0.4 | 35 | **87×** |
| WHAT_ABOUT | 16 | 104 | **6.5×** |
| RECENT | 4.4 | 66 | **15×** |
| PRUNE | 0.1 | 0.2 | ~ |
| DSL | 181 | — | (folded path) |
| Warm (LRUCache) | **0.19** | ~14 | **73×** (v2 pays a fresh-connect log write per hit) |

Caveat: v2 medians were warm-cache hits; the honest engine comparison is cold-vs-cold above.
Pre-fix v3 sat at ~800ms/mode — the gate caught the unscoped-FTS-trigger regression (C20)
and the fix landed same phase. Concurrent leg: 200/200 writes, dedup success, 0 errors → PASS.

## Scaling notes

- Seed write-amp ≈ 40ms/node (auto-link + contradiction scans + FTS indexing on the production
  path) — honest, one-time, and the reason the **100k leg is deferred**: ~1h+ server-class job,
  unsuitable for this home box (operator call; harness already takes `--nodes 100000`).
  Combined with the 10k margins (1.5x–87x), headroom is substantial.
- Recall cost drivers: `fetch_bound` ×5 access bumps per call (cheap post-C20), SEMANTIC full
  scan when candidates exceed the 2000 cap, PATH Dijkstra over live graph size.

## Gate criteria (CI or manual)

Fail the build if, same machine/fixture: any mode regresses >20% vs the table above,
concurrent leg fails, or `bench --nodes 10000` errors. Re-baseline explicitly (don't
silently move the numbers) and record hardware alongside.
