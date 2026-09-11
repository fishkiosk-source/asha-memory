"""src.agents — agents.db store + bridge + mailbox.

store.py   — owns agents.db schema (full schema + agent_id column, NOT v2's
               dead AGENT_SCHEMA_V2) + agents identity registry.
bridge.py  — ONLY module that opens both DBs (read/promote helpers).
mailbox.py — mailbox + receipts tables, send/read/ack/check_inbox.
"""
