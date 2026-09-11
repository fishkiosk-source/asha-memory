"""src.direct.provider - Direct in-process memory provider (no MCP)."""

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
import sys
_v3root = Path(__file__).resolve().parents[2]
if str(_v3root) not in sys.path:
    sys.path.insert(0, str(_v3root))
try:
    from ..mcp import tools as _tools
except ImportError:
    from src.mcp import tools as _tools

class DirectMemory:
    def __init__(self, memory_dir: Optional[str] = None):
        self._lock = threading.RLock()
        self._ctx = _tools.open_context(memory_dir)
        self.memory_dir = self._ctx["memory_dir"]
        self.clock = self._ctx["clock"]
        self.cache = self._ctx["cache"]
    def close(self):
        with self._lock:
            try:
                _tools.close_context(self._ctx)
            except:
                pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        self.close()
        return False
    def call(self, tool: str, args: Dict[str, Any]):
        # dispatch handles ctx["_lock"] per-attempt and releases during backoff,
        # so we don't hold self._lock across the whole retry (would block other threads)
        return _tools.dispatch(tool, args, self._ctx)
    def definitions(self):
        return _tools.definitions()
    def remember(self, content: str, node_type=None, label=None, source=None, trust=None, importance=None, agent=None, agent_id=None, metadata=None, attention_state=None):
        args = {"content": content}
        if node_type is not None: args["node_type"] = node_type
        if label is not None: args["label"] = label
        if source is not None: args["source"] = source
        if trust is not None: args["trust"] = trust
        if importance is not None: args["importance"] = importance
        if agent is not None: args["agent"] = agent
        if agent_id is not None: args["agent_id"] = agent_id
        if metadata is not None: args["metadata"] = metadata
        if attention_state is not None: args["attention_state"] = attention_state
        return self.call("remember", args)
    def recall(self, query: str, mode="RELATED", bound=10, limit=None, offset=0, include_agent_notes=False, node_type=None, agent=None, agent_id=None):
        args = {"query": query, "mode": mode, "bound": bound, "offset": offset, "include_agent_notes": include_agent_notes}
        if limit is not None: args["limit"] = limit
        if node_type is not None: args["node_type"] = node_type
        if agent is not None: args["agent"] = agent
        if agent_id is not None: args["agent_id"] = agent_id
        return self.call("recall", args)
    def relate(self, from_id, to_id, edge_type, weight=1.0, agent=None, agent_id=None, fuzzy=False):
        args = {"from_id": from_id, "to_id": to_id, "edge_type": edge_type, "weight": weight, "fuzzy": fuzzy}
        if agent is not None: args["agent"] = agent
        if agent_id is not None: args["agent_id"] = agent_id
        return self.call("relate", args)
    def get_node(self, node_id, agent=None, agent_id=None):
        args = {"node_id": node_id}
        if agent is not None: args["agent"] = agent
        if agent_id is not None: args["agent_id"] = agent_id
        return self.call("get_node", args)
    def update_node(self, node_id, label=None, content=None, trust_level=None, importance=None, source=None, metadata=None, agent=None, agent_id=None):
        args = {"node_id": node_id}
        if label is not None: args["label"] = label
        if content is not None: args["content"] = content
        if trust_level is not None: args["trust_level"] = trust_level
        if importance is not None: args["importance"] = importance
        if source is not None: args["source"] = source
        if metadata is not None: args["metadata"] = metadata
        if agent is not None: args["agent"] = agent
        if agent_id is not None: args["agent_id"] = agent_id
        return self.call("update_node", args)
    def delete_node(self, node_id, agent=None, agent_id=None):
        args = {"node_id": node_id}
        if agent is not None: args["agent"] = agent
        if agent_id is not None: args["agent_id"] = agent_id
        return self.call("delete_node", args)
    def agent_ensure(self, job_hint, preferred_id=None):
        args = {"job_hint": job_hint}
        if preferred_id is not None: args["preferred_id"] = preferred_id
        return self.call("agent_ensure", args)
    def agent_list(self):
        return self.call("agent_list", {})
    def agent_remember(self, agent_id, content, label=None, attention_state="agent_private", metadata=None):
        args = {"agent_id": agent_id, "content": content, "attention_state": attention_state}
        if label is not None: args["label"] = label
        if metadata is not None: args["metadata"] = metadata
        return self.call("agent_remember", args)
    def agent_recall(self, agent_id, query, mode="RELATED", bound=10, limit=None, offset=0):
        args = {"agent_id": agent_id, "query": query, "mode": mode, "bound": bound, "offset": offset}
        if limit is not None: args["limit"] = limit
        return self.call("agent_recall", args)
    def find_across_agents(self, query, min_confidence=0.15, bound=10):
        return self.call("find_across_agents", {"query": query, "min_confidence": min_confidence, "bound": bound})
    def promote_to_core(self, agent_id, agent_node_id, new_type="FACT", new_label=None):
        args = {"agent_id": agent_id, "agent_node_id": agent_node_id, "new_type": new_type}
        if new_label is not None: args["new_label"] = new_label
        return self.call("promote_to_core", args)
    def review_queue(self, bound=20, limit=None):
        args = {"bound": bound}
        if limit is not None: args["limit"] = limit
        return self.call("review_queue", args)
    def set_attention(self, agent_id, agent_node_id, attention_state):
        return self.call("set_attention", {"agent_id": agent_id, "agent_node_id": agent_node_id, "attention_state": attention_state})
    def mailbox_send(self, from_scope, to_scope, body, metadata=None):
        args = {"from": from_scope, "to": to_scope, "body": body}
        if metadata is not None: args["metadata"] = metadata
        return self.call("mailbox.send", args)
    def mailbox_read(self, scope="core", state="pending", limit=50, offset=0):
        return self.call("mailbox.read", {"scope": scope, "state": state, "limit": limit, "offset": offset})
    def mailbox_ack(self, scope="core", msg_ids=None):
        args = {"scope": scope}
        if msg_ids is not None: args["msg_ids"] = msg_ids
        return self.call("mailbox.ack", args)
    def profile(self):
        return self.call("profile", {})
    def health(self):
        return self.call("health", {})
    def stats(self, db="all"):
        return self.call("stats", {"db": db})
    def export(self, path):
        return self.call("export", {"path": path})
    def vacuum(self):
        return self.call("vacuum", {})
    def compact(self, keep_last=3, max_age_days=7, db="all"):
        return self.call("compact", {"keep_last": keep_last, "max_age_days": max_age_days, "db": db})
    def register_skill(self, name, description, level="ASSIGNABLE", tags=""):
        return self.call("register_skill", {"name": name, "description": description, "level": level, "tags": tags})
    def ensure_agent(self, *a, **kw):
        return self.agent_ensure(*a, **kw)
