"""src.core.store — SQLite open/PRAGMAs for memory/core.db.

THE INVARIANT (Idea.md rev8, concurrency): connect() below is the ONLY place
that opens core.db. Every connection gets:
  journal_mode=WAL, synchronous=NORMAL, foreign_keys=ON,
  busy_timeout=30000, cache_size=<sqlite_cache_size>.
No raw sqlite3.connect anywhere else (lint: grep). Schema DDL lives in
schema.json + migrate.py (Phase 2 fills them).
"""

import sqlite3
from pathlib import Path
from typing import Optional, Union

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MEMORY_DIR = REPO_ROOT / "memory"
CORE_DB_NAME = "core.db"

USER_VERSION_V3 = 4
BUSY_TIMEOUT_MS = 8000
DEFAULT_CACHE_SIZE = -64000  # KB (negative = KB), from v2 DEFAULT_CONFIG


def memory_dir(base: Optional[Union[Path, str]] = None) -> Path:
    """Resolve the memory/ data dir (created on first run, never hand-edited)."""
    return Path(base) if base else DEFAULT_MEMORY_DIR


def core_db_path(base: Optional[Union[Path, str]] = None) -> Path:
    return memory_dir(base) / CORE_DB_NAME


def connect(db_path: Union[Path, str],
            cache_size: int = DEFAULT_CACHE_SIZE,
            timeout_s: float = 30.0) -> sqlite3.Connection:
    """Open core.db with the v3 PRAGMA invariant. Caller owns close()."""
    conn = sqlite3.connect(str(db_path), timeout=timeout_s, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA cache_size={int(cache_size)}")
    return conn
