"""SQLite database for cost tracking.

Schema lives at ``~/.config/swm/costs.db`` alongside the TOML config.
The database is created automatically on first access.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from swm.config import CONFIG_DIR

DB_PATH = CONFIG_DIR / "costs.db"

_SCHEMA_VERSION = 1

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_id         TEXT    NOT NULL,
    provider       TEXT    NOT NULL,
    gpu_type       TEXT,
    gpu_count      INTEGER DEFAULT 1,
    cost_per_hr    REAL,
    started_at     TEXT    NOT NULL,
    stopped_at     TEXT,
    estimated_cost REAL,
    workspace      TEXT,
    name           TEXT
);

CREATE TABLE IF NOT EXISTS budgets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL,
    limit_usd  REAL    NOT NULL,
    period     TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_pod    ON sessions (pod_id);
CREATE INDEX IF NOT EXISTS idx_sessions_prov   ON sessions (provider);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions (stopped_at) WHERE stopped_at IS NULL;
"""

_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """Return a module-level connection, creating the schema if needed."""
    global _conn
    if _conn is not None:
        return _conn

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(_SCHEMA)

    cur = _conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if not row:
        _conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        _conn.commit()

    return _conn


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── sessions CRUD ────────────────────────────────────────────────────


def insert_session(
    *,
    pod_id: str,
    provider: str,
    gpu_type: str | None = None,
    gpu_count: int = 1,
    cost_per_hr: float | None = None,
    workspace: str | None = None,
    name: str | None = None,
) -> int:
    """Insert a new billing session. Returns the row id."""
    db = get_db()
    cur = db.execute(
        """
        INSERT INTO sessions
            (pod_id, provider, gpu_type, gpu_count, cost_per_hr,
             started_at, workspace, name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pod_id, provider, gpu_type, gpu_count, cost_per_hr,
         _utcnow(), workspace, name),
    )
    db.commit()
    return cur.lastrowid  # type: ignore[return-value]


def end_session(pod_id: str, provider: str) -> bool:
    """Close the most recent open session for *pod_id*.

    Computes ``estimated_cost`` from ``cost_per_hr × hours``.
    Returns True if a session was closed, False if none was open.
    """
    db = get_db()
    now = _utcnow()
    cur = db.execute(
        """
        SELECT id, started_at, cost_per_hr FROM sessions
        WHERE pod_id = ? AND provider = ? AND stopped_at IS NULL
        ORDER BY started_at DESC LIMIT 1
        """,
        (pod_id, provider),
    )
    row = cur.fetchone()
    if not row:
        return False

    cost: float | None = None
    if row["cost_per_hr"] is not None:
        started = datetime.fromisoformat(row["started_at"])
        stopped = datetime.fromisoformat(now)
        hours = (stopped - started).total_seconds() / 3600
        cost = round(row["cost_per_hr"] * hours, 4)

    db.execute(
        "UPDATE sessions SET stopped_at = ?, estimated_cost = ? WHERE id = ?",
        (now, cost, row["id"]),
    )
    db.commit()
    return True


def active_sessions() -> list[sqlite3.Row]:
    """Return all sessions that have not been stopped."""
    db = get_db()
    return db.execute(
        "SELECT * FROM sessions WHERE stopped_at IS NULL ORDER BY started_at DESC"
    ).fetchall()


def query_sessions(
    *,
    provider: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Flexible session query with optional filters."""
    db = get_db()
    clauses: list[str] = []
    params: list[object] = []

    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if since:
        clauses.append("started_at >= ?")
        params.append(since)
    if until:
        clauses.append("started_at < ?")
        params.append(until)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    lim = f"LIMIT {limit}" if limit else ""

    return db.execute(
        f"SELECT * FROM sessions {where} ORDER BY started_at DESC {lim}",
        params,
    ).fetchall()


# ── budgets CRUD ─────────────────────────────────────────────────────


def set_budget(scope: str, limit_usd: float, period: str) -> int:
    """Insert or replace a budget for *scope*. Returns the row id."""
    db = get_db()
    db.execute("DELETE FROM budgets WHERE scope = ? AND period = ?", (scope, period))
    cur = db.execute(
        "INSERT INTO budgets (scope, limit_usd, period, created_at) VALUES (?, ?, ?, ?)",
        (scope, limit_usd, period, _utcnow()),
    )
    db.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_budgets() -> list[sqlite3.Row]:
    """Return all budget rows."""
    return get_db().execute("SELECT * FROM budgets ORDER BY scope").fetchall()


def delete_budget(scope: str, period: str) -> bool:
    """Remove a budget. Returns True if it existed."""
    db = get_db()
    cur = db.execute(
        "DELETE FROM budgets WHERE scope = ? AND period = ?", (scope, period)
    )
    db.commit()
    return cur.rowcount > 0


def spend_in_period(
    scope: str,
    period: str,
) -> float:
    """Compute total estimated spend matching *scope* within *period*.

    *scope* is ``global``, ``provider:<slug>``, or ``pod:<id>``.
    *period* is ``daily``, ``weekly``, ``monthly``, or ``total``.
    Includes running sessions valued at rate × elapsed so far.
    """
    from datetime import timedelta

    db = get_db()
    now_dt = datetime.now(timezone.utc)

    since: str | None = None
    if period == "daily":
        since = (now_dt - timedelta(days=1)).isoformat()
    elif period == "weekly":
        since = (now_dt - timedelta(weeks=1)).isoformat()
    elif period == "monthly":
        since = (now_dt - timedelta(days=30)).isoformat()

    clauses: list[str] = []
    params: list[object] = []

    if since:
        clauses.append("started_at >= ?")
        params.append(since)

    if scope.startswith("provider:"):
        clauses.append("provider = ?")
        params.append(scope.split(":", 1)[1])
    elif scope.startswith("pod:"):
        clauses.append("pod_id = ?")
        params.append(scope.split(":", 1)[1])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = db.execute(f"SELECT * FROM sessions {where}", params).fetchall()

    total = 0.0
    now_str = now_dt.isoformat()
    for r in rows:
        if r["stopped_at"] is not None and r["estimated_cost"] is not None:
            total += r["estimated_cost"]
        elif r["cost_per_hr"] is not None:
            started = datetime.fromisoformat(r["started_at"])
            elapsed_hrs = (now_dt - started).total_seconds() / 3600
            total += r["cost_per_hr"] * elapsed_hrs

    return round(total, 4)
