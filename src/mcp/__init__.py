"""src.mcp — MCP JSON-RPC stdio server (thin).

server.py — stdio loop, tools/list|call (port of v2 asha_mcp.py minus skills bloat).
tools.py  — 23-tool surface (Idea.md rev8 §8) + dispatch + inbox-injection wrapper.
            The wrapper is the ONLY caller of mailbox.check_inbox (envelope-only, C5).
"""
