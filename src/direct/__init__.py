"""src.direct - Direct in-process provider for Hermes/OpenClaw.

Re-exports DirectMemory (thread-safe, LogicEngine-aware) without MCP stdio.
Import:  from src.direct import DirectMemory  or  from src.direct.provider import DirectMemory
Usage:
    m = DirectMemory("./memory")  # or DirectMemory() -> <v3root>/memory
    m.remember("hello", label="hi")
    m.recall("hello")
    m.close()
Context manager:  with DirectMemory() as m: ...
"""

from .provider import DirectMemory

__all__ = ["DirectMemory"]
