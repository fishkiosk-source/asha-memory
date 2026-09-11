[README.md](https://github.com/user-attachments/files/32126212/README.md)
# Asha Memory v3

![ASHA Memory Logo](https://github.com/fishkiosk-source/asha-memory/blob/main/wiki/logo/AshaMemorySMALL.png)

> Local-first, pure-Python, zero-dependency memory graph — two WAL databases, one brain, one dashboard.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org) [![stdlib only](https://img.shields.io/badge/deps-stdlib%20only-green)](wiki/ARCHITECTURE.md) [![tests 135](https://img.shields.io/badge/tests-135%20passing-brightgreen)](tests/) ![MCP 2025-03-26](https://img.shields.io/badge/mcp-2025--03--26-orange) [![license MIT](https://img.shields.io/badge/license-MIT-lightgrey)](#license)

A curated knowledge graph that survives reboots, agents, and cron spam — without a vector DB, without pip, without the cloud.

```
                 ┌──────────────┐      ┌──────────────┐
  AI clients ───▶│  MCP server  │─────▶│   core.db    │◀── core recall
  (stdio JSON)   │  (23 tools)  │──┐   │  knowledge   │
                 └──────────────┘  │   └──────────────┘
                                   │   ┌──────────────┐
                                   └──▶│  agents.db   │◀── isolated agent notes
                                       │  + mailbox   │◀── core↔agent↔user mail
                                       └──────────────┘
                                              ▲  ▲
                        ┌──────────┐          │  │      ┌──────────────┐
                        │  Brain   │──────────┘  └──────│  Dashboard   │
                        │ (janitor)│  maintains   views │  (12 tabs)   │
                        └──────────┘                    └──────────────┘
```

---

## Why v3?

`v2` (~2600 lines, single `core.db`) hit the single-WAL ceiling: every new feature added scan/lock cost on one file. `v3` is a **modularity + separation release** — same 9 recall modes, same result shape, but isolated hot paths.

| What changed | How |
|---|---|
| **Two DBs** | `core.db` (curated) + `agents.db` (all agents sharded by `agent_id`) — two WALs, two contention domains |
| **Incremental TF-IDF** | `vector_df`/`vector_meta` + compact `term:weight` vectors — no full-corpus refit on every `remember` |
| **Internal-content FTS5** | `node_fts(label, content, node_id UNINDEXED)` + scoped `AFTER UPDATE OF label,content` trigger — 153ms → 1.2ms per 175 access bumps |
| **Unified ephemeral** | `ephemeral_events` + legacy `nodes` where `label IN ephemeral_labels` — `compact`/`stats`/`bloat`/`manager` all count both tables (fix 2026-09-11) |
| **Zero deps** | `sqlite3, json, math, re, pathlib, http.server` only — `src/core/lexicon.py` + `src/core/clock.py` are verbatim `v2` carries |

Full rationale: [`wiki/ARCHITECTURE.md`](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/ARCHITECTURE.md) · Decision log `C1–C26`: [`wiki/RULINGS.md`](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/RULINGS.md)

---

## 60-second tour

- **core.db** — curated knowledge. `recall(..., mode=RELATED|SEMANTIC|...)` reads only here. Only `src/core/*` + bridge promotions write.
- **agents.db** — one file, all agents, `WHERE agent_id = ?` on every query. Agents never see each other's notes.
- **mailbox** (in `agents.db`) — `core↔agent`, `agent→agent`, `core→broadcast`, `user↔both` with `mailbox_receipts` fan-out and `check_inbox` auto-inject on every MCP call.
- **bridge** (`src/agents/bridge.py`) — the *only* dual-DB module. Idempotent MOVE `agents → core` (`trust = max(orig,0.8)`, `core_verified`, provenance in `metadata.promoted_from`).
- **brain** (`brain/engine.py` + `brain/scheduler.py`) — 11 janitor jobs (`dedup`, `compact`, `tiers`, `agent_working`, `core_helper`, `prune_empty_agents`, ...), snapshots, health, single-flight daemon.
- **dashboard** (`dashboard/server.py`) — human control plane, `http.server` stdlib, 12 tabs, token-gated, path-jailed.
- **direct** (`src/direct/provider.py:DirectMemory`) — same 23 tools via `RLock` + `check_same_thread=False` for Hermes/OpenClaw, no stdio.

Code map: [`wiki/MODULES.md`](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/MODULES.md) · DB schemas: [`wiki/DATABASE.md`](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/DATABASE.md)

---

## Features

- **9 recall modes** — `RELATED, SEMANTIC, TIMELINE, PATH, CLUSTER, WHO_IS, WHAT_ABOUT, RECENT, PRUNE, DSL` (`FIND ...` auto-detected) — `src/core/recall.py:44`
- **23 MCP tools** — `remember/recall/relate/get_node/update_node/delete_node/agent_ensure/agent_recall/find_across_agents/promote_to_core/.../mailbox.send|read|ack` — [`wiki/MCP_TOOLS.md`](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/MCP_TOOLS.md)
- **Agent isolation** — `agent_ensure(job_hint)` → `agent_<slug>_<hash4>` deterministic, `require_agent` gates, caps `800 chars / 100 notes` per agent
- **Mailbox** — unicast + `broadcast` fan-out, `noted_at`/`read_at`/`acked_at` receipts, sticky `[REVIEW READY]` reminder
- **Brain** — `dedup` (exact+semantic), `compact` unified (TTL `1h` + `keep_last` 2, preserves newest `keep`), `tiers` decay, `contradictions` sentiment-clash, `discover` band `[0.5,0.85)`, `vacuum` + `backup()` snapshots
- **Dashboard** — Overview / Maintenance / Graduate / Observer / Core Helper / Contradicts / Ephemeral / Graph (Canvas, no D3) / Manager / System / Statistics / Config / Mail

---

## Quick start

```bash
# stdlib only — no pip
git clone <repo> && cd "asha_memory v3"

# 1. Dashboard (human)
python -m dashboard.server --port 8500
# -> http://127.0.0.1:8500  (token in brain/config.json:dashboard_token)

# 2. MCP server (AI clients, stdio)
python -m src.mcp.server --memory-path ./memory

# 3. Direct in-process (Hermes/OpenClaw)
from src.direct.provider import DirectMemory
m = DirectMemory("./memory")
m.remember("Asha loves local LLMs", label="PREFERENCE")
m.recall("local LLM", mode="SEMANTIC")

# tests + bench
python -m pytest tests/ -q          # 135 tests
python -m bench.bench_recall --nodes 10000  # p50/p95 gate
```

---

## Dashboard

`python -m dashboard.server --port 8500 --memory-path ./memory`

| Tab | What |
|---|---|
| **Overview** | `core`/`agents` health + bloat + ephemeral (unified nodes+events) + candidates |
| **Maintenance** | One-click `compact`/`dedup`/`tiers`/… per DB |
| **Ephemeral** | `ephemeral_labels` + `ephemeral_ignored`, `Compact Now` (unified), `Bring DB up to date` |
| **Manager** | Native CRUD (`/api/nodes?db=` + row endpoints), health-filtered ephemeral view shows both sources |
| **Graduate / Observer / Contradicts** | Review queue, WORKING janitor, contradiction ledger |

Auth: `X-Api-Token` header from `brain/config.json:dashboard_token` — `GET /api/health` + `/wiki` stay public. Full REST: [`wiki/API.md`](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/API.md) · Live docs at `http://127.0.0.1:8500/wiki`.

---

## Configuration

Single live config: `brain/config.json` (never `memory/config.json`).

```json
{
  "interval_minutes": 1440,
  "ephemeral_labels": ["CRON_MONITOR_REPORT", "MOLTBOOK_HEARTBEAT", ...],
  "ephemeral_keep_last": 2,
  "ephemeral_max_age_hours": 1,
  "dedup_similarity_threshold": 0.85,
  "dashboard_token": "asha-sam-..."
}
```

Tune in Config tab or `POST /api/config`. Keys, defaults, and migration notes: [`wiki/BRAIN.md#8`](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/BRAIN.md).

---

## Project layout

```
asha_memory v3/
  src/core/      # core.db: store, schema+migrate, nodes, edges, recall, vectors, layers, lexicon, clock
  src/agents/    # agents.db: store, bridge, mailbox
  src/mcp/       # stdio JSON-RPC 2.0 server + 23 tool dispatch
  src/direct/    # DirectMemory (RLock, no stdio)
  src/logic/     # Logic Engine resolve/heal/route
  brain/         # engine.py + scheduler.py + config.json + logs/ snapshots/
  dashboard/     # server.py + static/ (12 modules)
  memory/        # DATA ONLY — gitignored, never hand-edit
  tools/         # migrate_v2_to_v3.py (read-only on v2)
  bench/ tests/ wiki/
```

Import rules (enforced by review): `src/core` never imports `brain`; only `src/agents/bridge.py` opens both DBs; only `src/mcp/tools.py` calls `mailbox.check_inbox`.

---

## Docs

| Doc | Answers |
|---|---|
| [ARCHITECTURE](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/ARCHITECTURE.md) | Why two DBs, data flow, principles |
| [MODULES](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/MODULES.md) | Every package/file/function |
| [DATABASE](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/DATABASE.md) | Schemas, indexes, triggers, FTS |
| [MCP_TOOLS](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/MCP_TOOLS.md) | 23 tools, params, injection |
| [BRAIN](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/BRAIN.md) | Jobs, scheduler, snapshots, config |
| [DASHBOARD](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/DASHBOARD.md) | Tabs, auth, REST |
| [SKILLS](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/SKILLS.md) | Unified playbook (Core+Agent, MCP + Direct) |
| [MIGRATION](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/MIGRATION.md) | v2→v3 splitter |
| [OPERATIONS](https://github.com/fishkiosk-source/asha-memory/tree/main/wiki/OPERATIONS.md) | Runbook: start/stop, backup, token |

Wiki is served live at `http://127.0.0.1:8500/wiki` (no token).

---
Built for agents that need to remember — locally, reliably, and with provenance.

![CoreMap](https://github.com/fishkiosk-source/asha-memory/blob/main/scshots/CoreMh.png) ![AgentMap](https://github.com/fishkiosk-source/asha-memory/blob/main/scshots/AgentMh.png)
