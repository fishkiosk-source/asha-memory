

# ASHA Memory v3 — Wiki

> Local-first, pure-Python, zero-dependency memory graph. Two databases, one brain, one dashboard.
> Spec: `RULINGS.md` (C1–C26) + `ARCHITECTURE.md`. Build log: `MIGRATION.md` + `brain/logs/`.

## Start here

| Doc                             | What it answers                                                   |
| ------------------------------- | ----------------------------------------------------------------- |
| [ARCHITECTURE](ARCHITECTURE.md) | What v3 is, why two DBs, how data flows, principles               |
| [MODULES](MODULES.md)           | Code map: every package, file, and public function                |
| [DATABASE](DATABASE.md)         | Schemas, tables, indexes, triggers, FTS, migration chain          |
| [MCP_TOOLS](MCP_TOOLS.md)       | The 23 MCP tools: params, shapes, errors, injection envelope      |
| [AGENTS](AGENTS.md)             | Identity, scoped CRUD, attention lifecycle, review → graduate     |
| [MAIL](MAIL.md)                 | Mailbox system: scopes, send/read/ack, broadcast, auto-inject     |
| [BRAIN](BRAIN.md)               | Janitor jobs, scheduler, snapshots, health, full config reference |
| [DASHBOARD](DASHBOARD.md)       | The 12 tabs, auth, full REST reference                            |
| [SKILLS](SKILLS.md)             | **Unified playbook for AI (Core + Agent) — Logic Engine, one skill tree, token & agent param (MCP stdio + DashboardClient)** |
| [DIRECTSKILLS](DIRECTSKILLS.md) | **Direct in-process playbook — same logic via `src/direct/provider.py:1 DirectMemory` (Hermes/OpenClaw, no MCP)** |
| [CORESKILLS](CORESKILLS.md)     | *Deprecated — redirects to SKILLS.md*                             |
| [AGENTSKILLS](AGENTSKILLS.md)   | *Deprecated — redirects to SKILLS.md*                             |
| [MIGRATION](MIGRATION.md)       | v2 → v3: what moved, ID mapping, config changes, replay           |
| [BENCHMARKS](BENCHMARKS.md)     | 10k verdict table, how to run the harness, gate criteria          |
| [OPERATIONS](OPERATIONS.md)     | Runbook: start/stop, backup/restore, token, troubleshooting       |
| [RULINGS](RULINGS.md)           | Decision log C1–C20: every locked call and why                    |
| [LEXICON](LEXICON.md)           | Tokenizer, stopwords, sentiment, ephemeral heuristics             |
| [CLOCK](CLOCK.md)               | Temporal context, InternalClock, TODAY node, humanize             |
| [API](API.md)                   | Dashboard REST reference (all GET/POST routes, cURL)              |
| [MCP_GUIDE](MCP_GUIDE.md)       | MCP stdio wire protocol, lifecycle, tools/call, mailbox inject    |

## The system in 60 seconds

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

- **core.db** — curated knowledge. Core recall reads it; only core writes it (+ bridge promotions).
- **agents.db** — one file, all agents, sharded by `agent_id`. Agents only ever see their own rows.
- **mailbox** (in agents.db) — the nervous system: core↔agent, agent→agent, core→broadcast, user↔both.
- **bridge** — the only code that opens both DBs: core reads agents, and *moves* (never copies) promoted notes.
- **brain** — maintenance backend (11 jobs incl. `core_helper` + `prune_empty_agents`, snapshots, health). Never imported by core code.
- **dashboard** — human control plane (13 tabs). Same-origin, token-gated, stdlib HTTP.
- **direct** — in-process `DirectMemory` `src/direct/provider.py:1` for Hermes/OpenClaw (no MCP stdio, same 23 tools via `RLock`, `check_same_thread=False`).

## Conventions used across this wiki

- `core.db` / `agents.db` live in `memory/` (created on first run, never hand-edited).
- `?db=core|agents|all` selects the database; `?agent=AshaWeb` / `POST {agent:"…"}` auto-resolves via Logic Engine `src/logic/engine.py:44` (no `agent_ensure` needed).
- Code refs look like `src/core/recall.py:recall`.
- “Locked” means decided in `RULINGS.md` (C1–C26) and not re-litigated here.
- Logic Engine: `src/logic/engine.py` + `src/logic/api_client.py` (`load_dashboard_token` reads `brain/config.json:dashboard_token`).
