"""One-off Postgres smoke test. Run: python postgres/smoke_test.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app import database
from app.db_compat import adapt_sql, close_connection


def main() -> int:
    print("PGHOST=", config.PGHOST, "PGDATABASE=", config.PGDATABASE, "PGUSER=", config.PGUSER)
    print("PGPASSWORD set=", bool(config.PGPASSWORD))
    print("adapt sample:", adapt_sql("SELECT * FROM users WHERE username = ? COLLATE NOCASE"))

    database.init_schema()
    print("init_schema: OK")

    with database.db_cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = ? AND tablename LIKE ? ORDER BY 1",
            ("public", "tbl_dfms_%"),
        )
        tables = [r[0] for r in cur.fetchall()]
        print(f"tbl_dfms_* tables: {len(tables)}")
        assert len(tables) >= 24, tables

        cur.execute("SELECT id, username, role FROM users ORDER BY id")
        users = [dict(r) for r in cur.fetchall()]
        print("users:", users)
        assert any(u["username"] == "admin" for u in users), "admin seed missing"

        cur.execute("DELETE FROM jobs WHERE worker_name = ?", ("_pg_smoke_worker",))
        cur.execute("DELETE FROM scripts WHERE worker_name = ?", ("_pg_smoke_worker",))
        cur.execute("DELETE FROM workers WHERE worker_name = ?", ("_pg_smoke_worker",))

    w = database.register_worker("_pg_smoke_worker", "127.0.0.99", state="idle")
    print("worker:", w and w.get("worker_name"), w and w.get("id"))
    assert w and w["worker_name"] == "_pg_smoke_worker"

    with database.db_cursor() as cur:
        now = database._utc_now()
        cur.execute(
            """
            INSERT INTO scripts (worker_name, script_name, script_path, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(worker_name, script_name) DO UPDATE SET script_path = excluded.script_path
            """,
            ("_pg_smoke_worker", "smoke.py", "smoke.py", now),
        )
        cur.execute(
            "SELECT id FROM scripts WHERE worker_name = ? AND script_name = ?",
            ("_pg_smoke_worker", "smoke.py"),
        )
        script_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO jobs (worker_name, script_id, status, output, created_at, updated_at)
            VALUES (?, ?, 'pending', '', ?, ?)
            """,
            ("_pg_smoke_worker", script_id, now, now),
        )
        print("created job_id=", cur.lastrowid, "script_id=", script_id)

    claimed = database.claim_pending_job("_pg_smoke_worker")
    print("claimed:", claimed and {k: claimed.get(k) for k in ("id", "status", "script_name")})
    assert claimed and claimed["id"], "claim failed"

    database.touch_worker("_pg_smoke_worker", "127.0.0.99", "idle")
    print("touch_worker: OK")

    with database.db_cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE worker_name = ?", ("_pg_smoke_worker",))
        cur.execute("DELETE FROM scripts WHERE worker_name = ?", ("_pg_smoke_worker",))
        cur.execute("DELETE FROM workers WHERE worker_name = ?", ("_pg_smoke_worker",))
    print("cleanup: OK")

    from app import create_app

    app = create_app()
    print("create_app: OK", app.name)
    close_connection()
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("SMOKE TEST FAILED:", type(exc).__name__, exc)
        raise
