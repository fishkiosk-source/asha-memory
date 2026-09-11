"""bench/bench_recall.py — perf harness (stdlib only).

Seeds N nodes (deterministic topic cycling) + chain edges + EVENT fan for
TIMELINE, then reports p50/p95 per recall mode over R reps, plus seed time.
Run from the v3 root:  python bench/bench_recall.py [--nodes 10000] [--reps 5]

This is the "will start dropping in performance" gate (Idea.md §9.6/C14):
Phase 9 fails the build on regression vs the v2 baseline. Seeding uses the
real remember_many path (index + vectors + layers + auto-link), so recall
measures production-shaped data.

NOTE (2026-09-04): the 10k gate passed decisively (see stepsdone Phase 9).
The 100k leg is DEFERRED by operator decision: seed write-amp (~40ms/node:
auto-link + contradiction scans + FTS indexing) makes 100k a ~1h+ server-class
job, unsuitable for this home system. Revisit on bigger iron; the harness
already supports --nodes 100000. --concurrent covers the writer-vs-janitor leg.
"""

import argparse
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import edges, nodes, recall
from src.core.migrate import migrate
from src.core.store import connect

TOPICS = ["quartz", "harbor", "lattice", "meadow", "cipher", "harbor",
          "sonar", "prairie", "onyx", "delta", "ember", "fjord",
          "garnet", "helix", "ion", "jungle", "kelp", "lagoon",
          "magnet", "nebula", "opal", "pinnacle", "quarry", "ridge",
          "saddle", "tundra", "umbra", "valley", "willow", "xenon",
          "yarrow", "zephyr", "acorn", "birch", "cedar", "dune",
          "estuary", "fern", "grove", "headland"]

MODES = ("RELATED", "SEMANTIC", "TIMELINE", "PATH", "CLUSTER",
         "WHO_IS", "WHAT_ABOUT", "RECENT", "PRUNE", "DSL")


def seed(conn, n: int) -> dict:
    """Deterministic seed. Returns {'nodes': [...labels], 'hub': label, ...}."""
    t0 = time.time()
    labels = []
    batch, batch_labels = [], []
    hub_id = None
    person_id = None

    def flush():
        nonlocal hub_id, person_id
        if not batch:
            return
        ids = nodes.remember_many(conn, batch)
        conn.commit()
        for item, nid, lab in zip(batch, ids, batch_labels):
            labels.append(lab)
            if item.get("label") == "Bench Hub":
                hub_id = nid
            if item.get("label") == "Bench Person":
                person_id = nid
        batch.clear()
        batch_labels.clear()

    for i in range(n):
        t = TOPICS[i % len(TOPICS)]
        u = TOPICS[(i * 7 + 3) % len(TOPICS)]
        is_event = (i % 20 == 19)
        label = f"Bench {i:06d} {t}"
        batch.append({
            "content": f"bench {t} {u} mechanics overview unit {i}",
            "node_type": "EVENT" if is_event else "FACT",
            "label": ("Bench event %06d" % i) if is_event else label,
            "trust": 0.5 + (i % 5) * 0.1,
            "importance": 0.5 + (i % 7) * 0.07,
        })
        batch_labels.append(("Bench event %06d" % i) if is_event else label)
        if len(batch) >= 2000:
            flush()
    batch.append({"content": "hub for bench traversal", "node_type": "TOPIC",
                  "label": "Bench Hub", "trust": 0.9, "importance": 0.9})
    batch_labels.append("Bench Hub")
    batch.append({"content": "bench person record", "node_type": "PERSON",
                  "label": "Bench Person", "trust": 0.9, "importance": 0.9})
    batch_labels.append("Bench Person")
    flush()

    # chain edges for PATH + EVENT fan for TIMELINE (batched commits)
    ids = [r["node_id"] for r in
           conn.execute("SELECT node_id FROM nodes ORDER BY label").fetchall()]
    hub = conn.execute("SELECT node_id FROM nodes WHERE label = 'Bench Hub'").fetchone()["node_id"]
    evts = [r["node_id"] for r in
            conn.execute("SELECT node_id FROM nodes WHERE node_type = 'EVENT' ORDER BY label").fetchall()]
    for k, eid in enumerate(evts):
        edges.relate(conn, hub, eid, "RELATES_TO", 0.8)
        if k % 500 == 499:
            conn.commit()
    id_list = [r["node_id"] for r in
               conn.execute("SELECT node_id FROM nodes WHERE label LIKE 'Bench %' ORDER BY label").fetchall()]
    for k in range(len(id_list) - 1):
        edges.relate(conn, id_list[k], id_list[k + 1], "RELATES_TO", 0.9)
        if k % 2000 == 1999:
            conn.commit()
    conn.commit()
    seed_s = time.time() - t0
    first = conn.execute("SELECT label FROM nodes WHERE label LIKE 'Bench 0%'"
                         " ORDER BY label LIMIT 1").fetchone()["label"]
    last = conn.execute("SELECT label FROM nodes WHERE label LIKE 'Bench %'"
                        " ORDER BY label DESC LIMIT 1").fetchone()["label"]
    return {"labels": labels, "hub": hub, "events": len(evts),
            "seed_s": seed_s, "first": first, "last": last,
            "person": person_id, "total": n + 2}


def percentile(times: list, pct: float) -> float:
    s = sorted(times)
    idx = min(len(s) - 1, max(0, math.ceil(pct / 100 * len(s)) - 1))
    return s[idx]


def measure(conn, mode: str, query: str, bound: int, reps: int) -> dict:
    times = []
    count = 0
    for _ in range(reps):
        t0 = time.perf_counter()
        res = recall.recall(conn, query, mode=mode, bound=bound)
        times.append((time.perf_counter() - t0) * 1000)
        count = len(res["nodes"])
        conn.commit()  # persist access bumps + query_log like production
    return {"mode": mode, "bound": bound, "reps": reps, "count": count,
            "p50_ms": round(statistics.median(times), 2),
            "p95_ms": round(percentile(times, 95), 2)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="v3 recall perf harness")
    ap.add_argument("--nodes", type=int, default=10000)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--mode", default="all", choices=["all"] + list(MODES))
    ap.add_argument("--bound", type=int, default=30)
    ap.add_argument("--concurrent", action="store_true",
                    help="writer thread inserts while dedup runs (busy_timeout gate)")
    ap.add_argument("--concurrent-writes", type=int, default=200)
    args = ap.parse_args(argv)

    td = tempfile.mkdtemp(prefix="asha_v3_bench_")
    conn = connect(str(Path(td) / "core.db"))
    migrate(conn)
    conn.commit()
    info = seed(conn, args.nodes)
    print(f"seed: n={info['total']} events={info['events']} seed_s={info['seed_s']:.1f}s")

    queries = {
        "RELATED": "bench quartz harbor",
        "SEMANTIC": "bench quartz harbor",
        "TIMELINE": "Bench Hub",
        "PATH": f"{info['first']} -> {info['last']}",
        "CLUSTER": info["first"],
        "WHO_IS": "Bench Person",
        "WHAT_ABOUT": "Bench Hub",
        "RECENT": "24",
        "PRUNE": "0.5",
        "DSL": 'FIND SEMANTIC "bench quartz harbor"',
    }
    modes = MODES if args.mode == "all" else (args.mode,)
    for m in modes:
        r = measure(conn, m, queries[m], args.bound, args.reps)
        print(f"{r['mode']:10s} count={r['count']:3d} reps={r['reps']} "
              f"p50={r['p50_ms']:8.2f}ms p95={r['p95_ms']:8.2f}ms")
    conn.close()
    if args.concurrent:
        run_concurrent(Path(td) / "core.db", args.concurrent_writes)
    return 0


def run_concurrent(db_path: Path, n_writes: int) -> None:
    """MCP-style writer inserts while dedup runs: no loss, no lock errors."""
    import threading
    from brain.engine import BrainEngine
    errors: list = []

    def writer():
        try:
            wconn = connect(str(db_path))
            try:
                for i in range(0, n_writes, 50):
                    nodes.remember_many(wconn, [
                        {"content": f"concurrent writer probe {i + k} "
                                    f"zephyr quasar {k}",
                         "node_type": "FACT",
                         "label": f"Concurrent {i + k:04d}"}
                        for k in range(50)])
                    wconn.commit()
            finally:
                wconn.close()
        except Exception as e:  # noqa: BLE001 — collected, asserted below
            errors.append(e)

    eng = BrainEngine(core_path=str(db_path),
                      agents_path=str(db_path.parent / "agents.db"),
                      brain_dir=str(db_path.parent / "brain_bench"))
    t0 = time.time()
    t = threading.Thread(target=writer)
    t.start()
    res = eng.deduplicate("core")
    t.join()
    wall = time.time() - t0
    rconn = connect(str(db_path))
    try:
        have = rconn.execute("SELECT COUNT(*) FROM nodes WHERE label LIKE 'Concurrent %'").fetchone()[0]
    finally:
        rconn.close()
    ok = not errors and have == n_writes and res["core"]["status"] == "success"
    print(f"concurrent: writes={have}/{n_writes} dedup={res['core']['status']}"
          f" errors={len(errors)} wall={wall:.1f}s {'PASS' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit(f"concurrent bench FAILED: {errors!r}")


if __name__ == "__main__":
    raise SystemExit(main())
