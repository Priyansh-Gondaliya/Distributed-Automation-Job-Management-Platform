"""
Lightweight daily scheduler — runs as a daemon thread inside the Flask process.

Checks every 15 seconds for due schedules and creates jobs through the
existing controller/job system. No external queue or service required.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from app import database


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[Scheduler {ts}] {msg}", flush=True)


def _ist_now(now_utc: datetime) -> datetime:
    return now_utc + timedelta(hours=5, minutes=30)


def _parallel_folder_loop(app) -> None:
    """Dedicated loop for parallel folder launches — runs every 5s independently."""
    _log("Parallel folder launch thread started (5s check interval)")
    while True:
        try:
            with app.app_context():
                from app.services import schedule_folders as sf

                def _create_for_parallel(worker_name, script_id, schedule_id, folder_run_id):
                    return database.create_job(
                        worker_name, script_id, schedule_id=schedule_id, folder_run_id=folder_run_id
                    )

                n_launch = sf.process_pending_folder_launches(create_job_fn=_create_for_parallel)
                if n_launch:
                    _log(f"Parallel folder launch(es): {n_launch}")
        except Exception as exc:
            _log(f"Parallel folder launch error: {exc}")
        time.sleep(5)


def _scheduler_loop(app) -> None:
    """Main scheduler loop — runs inside a daemon thread."""
    _log("Scheduler thread started (15s check interval)")
    last_reset_date = None
    last_midnight_reset_date = None

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            ist = _ist_now(now_utc)

            # 00:00 IST — reset daily schedule tracking state to pending
            if ist.hour == 0 and ist.minute < 2:
                if last_midnight_reset_date != ist.date():
                    with app.app_context():
                        count = database.reset_daily_schedule_states()
                        _log(f"Midnight IST: reset {count} schedule states to pending")
                    last_midnight_reset_date = ist.date()

            # 13:30 UTC = 7:00 PM IST daily reset (script days variable)
            if now_utc.hour == 13 and now_utc.minute >= 30:
                if last_reset_date != now_utc.date():
                    with app.app_context():
                        count = database.reset_all_schedules_days()
                        if count > 0:
                            _log(f"Reset {count} scheduler days to 0 at 7:00 PM IST")
                    last_reset_date = now_utc.date()
            with app.app_context():
                expired = database.expire_stale_schedule_pending_jobs()
                if expired:
                    _log(f"Expired {len(expired)} stale schedule pending job(s): {expired}")

                due = database.get_due_schedules()
                for sch in due:
                    try:
                        worker_name = sch["worker_name"]
                        worker_online = database.is_worker_online(worker_name)
                        job = database.create_job(
                            sch["worker_name"], sch["script_id"], schedule_id=sch["id"]
                        )
                        if worker_online:
                            log_msg = (
                                f"Triggered job #{job['id']} at "
                                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
                            )
                            database.mark_schedule_run(sch["id"], log_msg)
                            _log(
                                f"Schedule #{sch['id']} triggered → Job #{job['id']} "
                                f"({sch['script_name']} on {sch['worker_name']})"
                            )
                        else:
                            _log(
                                f"Schedule #{sch['id']} queued (worker '{worker_name}' offline) "
                                f"→ Job #{job['id']} — 30 min grace, no catch-up after"
                            )
                    except Exception as exc:
                        # Do not mark last_run — otherwise this schedule is skipped until tomorrow.
                        _log(f"Failed to create job for schedule #{sch['id']}: {exc}")

                # Folder sequential runs (same due rules as regular schedules)
                try:
                    from app.services import schedule_folders as sf

                    def _create(worker_name, script_id, schedule_id, folder_run_id):
                        return database.create_job(
                            worker_name, script_id, schedule_id=schedule_id, folder_run_id=folder_run_id
                        )

                    for folder in sf.get_due_folders():
                        result = sf.start_folder_run(folder["id"], create_job_fn=_create)
                        if result.get("error"):
                            _log(f"Folder #{folder['id']} skip: {result['error']}")
                        else:
                            _log(
                                f"Folder #{folder['id']} '{folder.get('name')}' started "
                                f"→ run #{result.get('run_id')} ({result.get('total')} scripts)"
                            )
                except Exception as exc:
                    _log(f"Folder scheduler error: {exc}")
        except Exception as exc:
            _log(f"Scheduler loop error: {exc}")
        time.sleep(15)


def start_scheduler(app) -> None:
    """Start the scheduler as a daemon thread. Call after create_app()."""
    t = threading.Thread(target=_scheduler_loop, args=(app,), daemon=True)
    t.start()
    p = threading.Thread(target=_parallel_folder_loop, args=(app,), daemon=True)
    p.start()
    _log("Scheduler background thread launched")
