"""src.logic.api_client — Direct API client for dashboard HTTP.

Agents don't have the user token; LogicEngine reads brain/config.json
dashboard_token and auto-injects X-Api-Token so agent/core Direct API
calls pass auth on 127.0.0.1 or 0.0.0.0 binds.

Stdlib only: urllib, json, pathlib.
No new DB connections — reuses BrainEngine.config or file read.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


def _default_brain_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "brain"


def load_dashboard_token(brain_dir: Optional[Path] = None) -> str:
    """Read dashboard_token from brain/config.json (empty -> no auth)."""
    b = Path(brain_dir) if brain_dir else _default_brain_dir()
    cfg_path = b / "config.json"
    if not cfg_path.exists():
        # fallback: try memory/ parent brain
        alt = Path("brain/config.json")
        if alt.exists():
            cfg_path = alt
        else:
            return ""
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        tok = str(data.get("dashboard_token") or "").strip()
        return tok
    except Exception:
        return ""


class DashboardClient:
    """HTTP client for dashboard Direct API with auto token injection.

    Usage:
        client = DashboardClient(brain_dir="brain", base_url="http://127.0.0.1:8500")
        # token auto-loaded from brain/config.json, header added to every request
        data = client.get("/api/nodes", params={"db":"core", "agent":"AshaWeb", "q":"budget"})
        data = client.post("/api/node_add", json={"content":"hi", "agent":"AshaWeb"})

    Agents/jobs pass agent hint; client forwards to server where LogicEngine
    re-resolves (second verification). Token never exposed to agent prompt.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8500",
                 brain_dir: Optional[Path] = None,
                 token: Optional[str] = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.brain_dir = Path(brain_dir) if brain_dir else _default_brain_dir()
        # explicit token wins, else load from brain config, else "" (open)
        if token is not None:
            self.token = str(token)
        else:
            self.token = load_dashboard_token(self.brain_dir)
        self.timeout = timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            h["X-Api-Token"] = self.token
        if extra:
            h.update(extra)
        return h

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if qs:
                url += "?" + qs
        return url

    def request(self, method: str, path: str,
                params: Optional[Dict[str, Any]] = None,
                json_body: Optional[Dict[str, Any]] = None) -> Any:
        url = self._url(path, params)
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method.upper())
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                ctype = r.headers.get_content_type() if hasattr(r.headers, 'get_content_type') else r.headers.get("Content-Type", "")
                if "json" in ctype:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                # try json anyway
                try:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    return raw.decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            # surface JSON error if present
            try:
                j = json.loads(body)
                raise RuntimeError(f"HTTP {e.code} {path}: {j.get('error') or j.get('message') or body}") from None
            except (ValueError, json.JSONDecodeError):
                raise RuntimeError(f"HTTP {e.code} {path}: {body}") from None

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, json: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("POST", path, params=params, json_body=json or {})

    # ── convenience wrappers (mirror unified MCP) ──

    def remember(self, content: str, agent: Optional[str] = None,
                 label: Optional[str] = None, node_type: Optional[str] = None,
                 **kwargs) -> Any:
        """Unified remember via Direct API (agent auto-resolves server-side)."""
        payload: Dict[str, Any] = {"content": content}
        if label is not None:
            payload["label"] = label
        if node_type is not None:
            payload["node_type"] = node_type
        if agent is not None:
            payload["agent"] = agent  # unified new field; server also accepts agent_id
            payload["agent_id"] = agent
        payload.update(kwargs)
        # server's node_add now supports agent param; we try new unified endpoint first
        try:
            return self.post("/api/remember", json=payload)
        except RuntimeError as e:
            if "404" in str(e):
                # fallback to legacy db-based endpoint with LogicEngine agent inference
                # resolve locally to pick db
                from .engine import LogicEngine
                # we need a temp conn to resolve; fallback uses core db
                payload["db"] = "agents" if agent else "core"
                return self.post("/api/node_add", json=payload)
            raise

    def recall(self, query: str, agent: Optional[str] = None, **kwargs) -> Any:
        params: Dict[str, Any] = {"q": query}
        if agent:
            params["agent"] = agent
        params.update(kwargs)
        return self.get("/api/nodes", params=params)

    def health(self) -> Any:
        # health/ping are open (no token needed) but we still send token if present
        return self.get("/api/health")
