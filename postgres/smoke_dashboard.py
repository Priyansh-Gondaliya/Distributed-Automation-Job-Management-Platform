"""Exercise common dashboard DB paths against Postgres."""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import database
from app.db_compat import close_connection


def run(label, fn):
    try:
        result = fn()
        n = len(result) if isinstance(result, list) else type(result).__name__
        print(f"OK  {label}: {n}")
        return True
    except Exception as e:
        print(f"FAIL {label}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def main() -> int:
    database.init_schema()
    admin = database.get_user_by_username("admin")
    assert admin, "admin missing"
    uid = admin["id"]

    checks = [
        ("refresh_worker_statuses", lambda: database.refresh_worker_statuses(30) or True),
        ("list_accessible_workers", lambda: database.list_accessible_workers(uid)),
        ("list_scripts", lambda: database.list_scripts()),
        ("list_accessible_scripts", lambda: database.list_accessible_scripts(uid)),
        ("list_schedules", lambda: database.list_schedules(uid)),
        ("list_jobs", lambda: database.list_jobs(limit=20)),
        ("get_job_counts", lambda: database.get_job_counts(uid, [])),
        ("list_starred_scripts", lambda: database.list_starred_scripts_for_dashboard(uid)),
        ("get_history", lambda: database.get_history(limit=20, user_id=uid)),
        ("list_scripts_by_ids empty", lambda: database.list_scripts_by_ids([], uid)),
    ]

    # If any scripts exist, also test by-ids path
    scripts = database.list_scripts()
    if scripts:
        ids = [s["id"] for s in scripts[:5]]
        checks.append(("list_scripts_by_ids", lambda ids=ids: database.list_scripts_by_ids(ids, uid)))
        wn = scripts[0].get("worker_name")
        if wn:
            checks.append(("list_scripts worker", lambda wn=wn: database.list_scripts(wn)))
            checks.append(("list_jobs_for_worker", lambda wn=wn: database.list_jobs_for_worker(wn, limit=20)))

    # Reports helpers (empty DB should still succeed)
    checks.extend(
        [
            ("get_paginated_reports", lambda: database.get_paginated_reports(None, None, None, "", "", "", 10, 0, uid)),
            ("get_report_analytics", lambda: database.get_report_analytics(user_id=uid)),
            ("get_report_summary_cards", lambda: database.get_report_summary_cards(None, None, None, "", "", uid)),
            ("get_report_folders", lambda: database.get_report_folders(uid)),
            ("get_user_watchlist_entries", lambda: database.get_user_watchlist_entries(uid)),
            ("get_enriched_watchlist", lambda: database.get_enriched_watchlist(uid)),
        ]
    )

    ok = 0
    fail = 0
    for label, fn in checks:
        if run(label, fn):
            ok += 1
        else:
            fail += 1

    close_connection()
    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
