"""
PostgreSQL connection layer for DFMS (tbl_dfms_* tables).

SQL in database.py still uses logical table names and ``?`` placeholders;
this module rewrites them for Postgres:
  - table names → tbl_dfms_*
  - ? → %s
  - COLLATE NOCASE → ILIKE / stripped
  - INSERT OR IGNORE → ON CONFLICT DO NOTHING
  - PRAGMA / REINDEX / BEGIN IMMEDIATE → no-op or harmless
"""
from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from app import config

_local = threading.local()

IntegrityError = psycopg2.IntegrityError
OperationalError = psycopg2.OperationalError

_PG_TABLES = {
    "users": "tbl_dfms_users",
    "workers": "tbl_dfms_workers",
    "scripts": "tbl_dfms_scripts",
    "schedules": "tbl_dfms_schedules",
    "jobs": "tbl_dfms_jobs",
    "commands": "tbl_dfms_commands",
    "user_pc_access": "tbl_dfms_user_pc_access",
    "user_pc_access_periods": "tbl_dfms_user_pc_access_periods",
    "user_script_access": "tbl_dfms_user_script_access",
    "schedule_access": "tbl_dfms_schedule_access",
    "schedule_folders": "tbl_dfms_schedule_folders",
    "schedule_folder_items": "tbl_dfms_schedule_folder_items",
    "schedule_folder_runs": "tbl_dfms_schedule_folder_runs",
    "schedule_folder_access": "tbl_dfms_schedule_folder_access",
    "scheduler_view_access": "tbl_dfms_scheduler_view_access",
    "history_log": "tbl_dfms_history_log",
    "worker_file_tree": "tbl_dfms_worker_file_tree",
    "worker_tree_sync": "tbl_dfms_worker_tree_sync",
    "file_history": "tbl_dfms_file_history",
    "user_starred_files": "tbl_dfms_user_starred_files",
    "file_watchlist": "tbl_dfms_file_watchlist",
    "scraper_reports": "tbl_dfms_scraper_reports",
    "scraper_report_errors": "tbl_dfms_scraper_report_errors",
    "scraper_report_files": "tbl_dfms_scraper_report_files",
}

_TABLE_NAMES_DESC = sorted(_PG_TABLES.keys(), key=len, reverse=True)


class _Row:
    """Dict-like row: supports row['col'] and row[0]."""

    __slots__ = ("_data", "_keys")

    def __init__(self, mapping: Any):
        if mapping is None:
            self._data = {}
            self._keys = []
            return
        self._data = dict(mapping)
        self._keys = list(self._data.keys())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._keys[key]]
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __iter__(self):
        for k in self._keys:
            yield self._data[k]

    def __len__(self):
        return len(self._keys)


def adapt_sql(sql: str) -> str:
    """Rewrite logical SQL for PostgreSQL."""
    if not sql:
        return sql

    raw = sql.strip()
    upper = raw.upper()
    if upper.startswith("PRAGMA") or upper.startswith("REINDEX"):
        return ""

    sql = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "SELECT 1", sql, flags=re.IGNORECASE)
    # SQLite char(n) → PostgreSQL chr(n)
    sql = re.sub(r"\bchar\s*\(", "chr(", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"=\s*\?\s+COLLATE\s+NOCASE",
        " ILIKE ?",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"\s+COLLATE\s+NOCASE\b", "", sql, flags=re.IGNORECASE)

    if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", sql, flags=re.IGNORECASE):
        sql = re.sub(
            r"INSERT\s+OR\s+IGNORE\s+INTO",
            "INSERT INTO",
            sql,
            flags=re.IGNORECASE,
        )
        if not re.search(r"\bON\s+CONFLICT\b", sql, flags=re.IGNORECASE):
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    for name in _TABLE_NAMES_DESC:
        sql = re.sub(rf"\b{name}\b", _PG_TABLES[name], sql)

    out = []
    i = 0
    in_single = False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_single and i + 1 < len(sql) and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue
        if not in_single and ch == "?":
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class _PgCursor:
    def __init__(self, real_cur):
        self._cur = real_cur
        self.rowcount = -1

    def execute(self, sql: str, params: Any = None):
        adapted = adapt_sql(sql)
        if not adapted.strip():
            self.rowcount = -1
            return None
        if params is None:
            self._cur.execute(adapted)
        else:
            self._cur.execute(adapted, params)
        self.rowcount = self._cur.rowcount
        return self

    def executemany(self, sql: str, seq_of_params):
        adapted = adapt_sql(sql)
        if not adapted.strip():
            return None
        self._cur.executemany(adapted, seq_of_params)
        self.rowcount = self._cur.rowcount
        return self

    def executescript(self, script: str):
        for stmt in script.split(";"):
            s = stmt.strip()
            if s:
                self.execute(s)
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        return None if row is None else _Row(row)

    def fetchall(self):
        return [_Row(r) for r in self._cur.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany() if size is None else self._cur.fetchmany(size)
        return [_Row(r) for r in rows]

    @property
    def lastrowid(self):
        try:
            self._cur.execute("SELECT LASTVAL()")
            row = self._cur.fetchone()
            if row is None:
                return None
            if isinstance(row, dict):
                return list(row.values())[0]
            return row[0]
        except Exception:
            return None

    def close(self):
        self._cur.close()

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _PgConnection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _PgCursor(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (OperationalError, psycopg2.InterfaceError)):
        return True
    msg = str(exc).lower()
    return "connection already closed" in msg or "server closed the connection" in msg


def _open_connection() -> _PgConnection:
    if not config.PGPASSWORD:
        raise RuntimeError(
            "PGPASSWORD is not set. Add it to .env or the environment "
            "(see .env.example)."
        )
    raw = psycopg2.connect(
        host=config.PGHOST,
        port=config.PGPORT,
        dbname=config.PGDATABASE,
        user=config.PGUSER,
        password=config.PGPASSWORD,
        connect_timeout=int(os.environ.get("PGCONNECT_TIMEOUT", "15")),
        options=f"-c search_path={config.PGSCHEMA},public",
    )
    raw.autocommit = False
    return _PgConnection(raw)


def get_connection():
    """Return a thread-local PostgreSQL connection."""
    conn = getattr(_local, "connection", None)
    if conn is not None:
        try:
            if conn._conn.closed:
                close_connection()
                conn = None
        except Exception:
            close_connection()
            conn = None
    if conn is None:
        _local.connection = _open_connection()
    return _local.connection


def close_connection():
    conn = getattr(_local, "connection", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.connection = None


@contextmanager
def db_cursor():
    last_exc: Optional[BaseException] = None
    for attempt in range(2):
        try:
            conn = get_connection()
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
                return
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and _is_connection_error(exc):
                close_connection()
                continue
            raise
    if last_exc is not None:
        raise last_exc
