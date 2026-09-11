# Migration (v2 → v3)

One-shot splitter: `tools/migrate_v2_to_v3.py` — reads the v2 `asha_memory/core.db` **read-only** (`sqlite3.connect(f"file:{v2db}?mode=ro", uri=True)`) and builds two v3 DBs + configs. V2 is never modified (integrity-gated: `PRAGMA integrity_check` must be `ok` or the tool aborts).

## Usage

```bash
python tools/migrate_v2_to_v3.py <v2dir> --v3-memory ./memory --v3-brain ./brain
python tools/migrate_v2_to_v3.py <v2dir> --v3-memory ./memory --v3-brain ./brain --dry-run   # plan without writing
python tools/migrate_v2_to_v3.py <v2dir> --v3-memory ./memory --v3-brain ./brain --force     # rebuild targets from scratch
python tools/migrate_v2_to_v3.py <v2dir> --v3-memory ./memory --dry-run | jq .              # JSON report
```

| Flag | Effect |
|---|---|
| (none) | Refuses if `memory/core.db` or `memory/agents.db` already has rows |
| `--dry-run` | Classifies + plans edges/registry/config, returns JSON, writes nothing |
| `--force` | Unlinks existing `core.db*` / `agents.db*` (WAL/SHM too) then rebuilds |

Replay is deterministic (sorted IDs, same splits) — re-run any time with `--force` to get an identical result.

## Read-only guarantee

```python
src = sqlite3.connect(f"file:{v2db}?mode=ro", uri=True)  # tools/migrate_v2_to_v3.py:88
src.row_factory = sqlite3.Row
assert src.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
```

No `INSERT`/`UPDATE`/`DELETE` ever touches the v2 file.

## Classification (locked)

```python
# tools/migrate_v2_to_v3.py:57 + src/core/lexicon.py:132
def _is_agent_row(node_type, meta):  # mirrors brain is_agent_note
    return ((node_type == "AGENT_NOTE" and att != "core_verified")
            or (bool(meta.get("agent_scoped")) and att != "core_verified"))

def _is_telemetry(label, content, allowlist):
    return label in allowlist or _looks_like_json_log(content or "")
    # _looks_like_json_log: lstrip + startswith("{") + "timestamp" in low[:400]
    #                       and ("status" in low or "post_count" in low or "load1m" in low)
```

Priority order per row:

1. `attention_state == "core_verified"` → **core** always (verified wins, even if telemetry-looking — `verified_telemetry_kept_core` in report).
2. Else `label IN allowlist OR _looks_like_json_log(content)` → **ephemeral_events** in its own DB (`eph_core` vs `eph_agents` split by `_is_agent_row`).
3. Else `_is_agent_row` → **agents.db** sharded by canonical `agent_id`.
4. Else → **core.db**.

Allowlist = `v2 config.json:ephemeral_labels` or `v2 brain/brain_config.json:ephemeral_labels` (sorted, reported as `allowlist`).

## What moved (live run 2026-09-04, full audit in `../ideas/MIGRATION_REPORT.md`)

| V2 input | → core.db | → agents.db | → ephemeral | Total |
|---|---|---|---|---|
| **279 nodes** | 79 core | 177 agents | 5 core + 18 agents → `ephemeral_events` | 279 ✓ |
| **1022 edges** | 80 copied | 799 copied | 143 dropped | 1022 ✓ |
| **1977 access_log** | 1202 | 690 | 85 dropped (telemetry) | 1977 ✓ |
| **132 query_log** | 134 (132 + 2 verify reads) capped 5000 | — | — | ✓ |

## Edge plan

Edges copy **only when both endpoints land in the same target DB** (no phantom cross-DB links):

- `a in core && b in core` → `core.db` (verbatim `edge_id/type/weight`).
- `a in agents && b in agents` → `agents.db` stamped `agent_id = canonical(from_owner)` (`NOT NULL`).
- `a in eph || b in eph` → dropped `ephemeral-endpoint` (14).
- `a in core && b in agents` (or vice versa) → dropped `cross-scope` (129) — still discoverable via `find_across_agents` (TF-IDF over all agents).
- Otherwise → `missing-endpoint`.

Result: `copy_core 80 + copy_agents 799 = 879` copied; `dropped 143 (129 cross-scope + 14 ephemeral-endpoint)`; `reconcile_ok` when `copied + dropped == v2`.

## Registry — deterministic (29 → 27)

```python
# tools/migrate_v2_to_v3.py:172 — sorted for determinism
for original in sorted(agent_ids):  # alphabetical
    slug = _slugify(original)        # lower, [a-z0-9]+, dash-joined
    if slug in seen_slugs:           # collision → merge
        canon[original] = canon[keeper_of_slug[slug]]
        add_alias(agents, original, canon[original])
    else:
        res = agent_ensure(agents, original,
              preferred_id=original if PREFERRED_ID_RE.match(original) else None)
        canon[original] = res["agent_id"]   # lowercase IDs preserved verbatim;
                                            # rest → agent_<slug>_<hex4>
        if res["agent_id"] != original:
            add_alias(agents, original, res["agent_id"])
```

- 29 distinct v2 IDs → 27 canonical agents (2 slug-collision merges fixed real fragmentation: `asha_core`/`asha-core`, `cron-sovereignty`/`cron_sovereignty` — alphabetically first keeper wins).
- Every old spelling kept as `agent_aliases(alias → agent_id)` so `resolve_agent` still finds it.
- `legacy-unknown` created if any agent note lacked `metadata.agent_id`.
- Per-agent caps (100 notes / 800 chars v2 parity) **bypassed** to preserve data; over-cap agents reported (`over_cap_100`), health flags.

## Bulk load

| Concern | How |
|---|---|
| **Nodes** | `executemany INSERT` core vs agents (agents rows carry `canon[agent_id]`). |
| **Layers** | `memory_layers` rows carried as stored (`layer`, `promoted_at`, `layer_order`) — not re-initialized. |
| **Logs** | `query_log` (capped `LIMIT 5000` by SELECT) → core; `access_log` split by `core_ids` vs `agent_ids_set`; telemetry access dropped. |
| **Telemetry** | `ephemeral_events(label, body, created_at, metadata)` per-DB. |
| **FTS** | Triggers on `INSERT` + explicit `core_nodes.build_index(conn, node_id, label, content)` for moved rows. |
| **Vectors** | **Not carried** — v2 JSON format discarded; rebuilt once per DB via `core_vectors.rebuild_all(conn)` (`report["vectors"] = {core, agents}` with `1438` / `1895` terms live). |
| **Schema** | `core_migrate(core)` + `agent_store.ensure_schema(agents)` before load. |

## Per-DB health verify

```python
from brain.engine import BrainEngine
eng = BrainEngine(core_path=str(mem/"core.db"), agents_path=str(mem/"agents.db"), brain_dir=str(brain_dir))
health = eng.health("both")
# report["health"] = {core_ok: health["core"]["check"]["ok"],
#                     agents_ok: health["agents"]["check"]["ok"],
#                     combined: ..., ok: core_ok and agents_ok}
```

Must be `ok:true` on both DBs; reconcile block must show `reconcile_nodes.ok` + `edges.reconcile_ok`. Spot-check review queue, aliases (`agent_aliases`), and recall.

## Config mapping (9 changes — all listed in `../ideas/MIGRATION_REPORT.md`)

`memory/config.json` = informational only (never read at runtime: `_note: "live config is brain/config.json"`). `brain/config.json` = single live config (`brain/engine.py:DEFAULTS` overlaid with v2 brain values).

1. `last_db_path` (Linux-absolute) → **dropped** (relative `memory/` now).
2. `interval_minutes` **2880** → migrated (default was 60).
3. `max_unused_days` **5** → migrated (default was 4).
4. `ephemeral_labels` — v2's 9 migrated (v3 default had 10 incl. `BRAIN_HISTORY` — v2 wins).
5. `contradiction_auto_resolve` **true** → migrated (was `false` opt-in) ⚠️ — auto-resolve will act during brain runs; flip in Config tab if undesired.
6. `dashboard_token` **preserved** + perm warning (`chmod 600` where possible; Windows: limit ACLs) — dashboard + any token-gated API now require it.
7. `agent_working_demote_batch` **3** → migrated (was 5).
8. Core keys carried namespaced into `memory/config.json` (`core.*` / `agents.*` / `shared.*` — informational).
9. Token warning recorded (see 6).

> **C17 superseded**: `memory/config.json wins` was overruled by operator directive **2026-09-05** — `brain/config.json` is now the **only** config file (no overlay, no shared section, no write-through). `WIKI_DIR` note in directive: dashboard resolves brain via `_default_brain_dir()` (`<v3root>/brain`), not `dashboard/`.

## Replay / verify

```bash
python tools/migrate_v2_to_v3.py ../asha_memory\ v2/asha_memory --v3-memory ./memory --v3-brain ./brain --dry-run | jq .moved,.edges,.agents
python tools/migrate_v2_to_v3.py ../asha_memory\ v2/asha_memory --v3-memory ./memory --v3-brain ./brain --force
# verify
python -c "from brain.engine import BrainEngine; e=BrainEngine(core_path='memory/core.db',agents_path='memory/agents.db',brain_dir='brain'); print(e.health('both'))"
```

- Re-run any time with `--force` (deterministic: sorted IDs, same splits).
- Verify: per-DB `health` must be `ok:true`; reconcile blocks `reconcile_nodes.ok` + `edges.reconcile_ok`; spot-check review queue, aliases, recall.
- Vectors are rebuilt, not carried — do not expect v2 vectors after migration.
