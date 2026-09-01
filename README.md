# asha-memory

A lightweight, zero-dependency, local-first graph memory engine designed for autonomous AI agents. Built entirely with the Python standard library and embedded SQLite, AshaMemory provides deterministic context recall, temporal metadata, strict scope isolation, and self-healing graph maintenance without cloud vector databases or third-party package bloat.

Key Features

 - Zero-Dependency Core: Runs out of the box using pure Python (sqlite3, http.server) — no complex setup, API keys, or external vector DBs.

 - Deterministic Dual-Scope Graph: Strictly isolates core identity context from ephemeral sub-agent execution traces (AGENT_NOTE vs. CORE) to prevent hallucination drift and context contamination.

 - Autonomous Self-Healing: Independent maintenance engine (AshaBrain) handles background TF-IDF deduplication, age-based decay, telemetry log compaction, and automatic SQLite freelist compaction (VACUUM).

 - MCP & Harness Native: Includes a Model Context Protocol (asha_mcp.py) interface and direct harness integration for single-pass temporal recall (_clock) and semantic graph traversals.

 - Built-in Web Dashboard: Features a clean, dark-mode management UI for live graph inspection, configuration tuning, snapshot rollbacks, and manual curation.
