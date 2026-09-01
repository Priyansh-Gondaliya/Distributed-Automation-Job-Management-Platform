"""Daily schedule state computation tests."""
from datetime import datetime, timedelta, timezone

from app import create_app, database


def test_daily_state_before_slot():
    app = create_app()
    with app.app_context():
        now = datetime(2026, 8, 28, 9, 0)
        sch = {
            "enabled": 1,
            "schedule_type": "daily",
            "run_time": "10:30",
            "last_run": None,
            "worker_name": "Priyansh",
        }
        st = database.compute_schedule_daily_state(
            sch, now=now, today_job=None, worker_online=True
        )
        assert st["daily_state"] == "scheduled"


def test_daily_state_due_in_grace():
    app = create_app()
    with app.app_context():
        now = datetime(2026, 8, 28, 10, 35)
        sch = {
            "enabled": 1,
            "schedule_type": "daily",
            "run_time": "10:30",
            "last_run": "2026-08-27 06:30:00",
            "worker_name": "Priyansh",
        }
        st = database.compute_schedule_daily_state(
            sch, now=now, today_job=None, worker_online=True
        )
        assert st["daily_state"] == "due"


def test_daily_state_missed_after_grace():
    app = create_app()
    with app.app_context():
        now = datetime(2026, 8, 28, 11, 0)
        sch = {
            "enabled": 1,
            "schedule_type": "daily",
            "run_time": "10:30",
            "last_run": "2026-08-27 06:30:00",
            "worker_name": "Priyansh",
        }
        st = database.compute_schedule_daily_state(
            sch, now=now, today_job=None, worker_online=False
        )
        assert st["daily_state"] == "missed"
        assert "offline" in st["daily_state_reason"].lower()


def test_daily_state_completed_today_job():
    app = create_app()
    with app.app_context():
        now = datetime(2026, 8, 28, 11, 0)
        sch = {
            "enabled": 1,
            "schedule_type": "daily",
            "run_time": "10:30",
            "last_run": "2026-08-27 06:30:00",
        }
        st = database.compute_schedule_daily_state(
            sch,
            now=now,
            today_job={"status": "completed"},
            worker_online=True,
        )
        assert st["daily_state"] == "completed"


def test_pending_job_invalid_after_grace():
    app = create_app()
    with app.app_context():
        now = datetime(2026, 8, 28, 11, 0)
        sch = {"schedule_type": "daily", "run_time": "10:30", "enabled": 1}
        job = {
            "status": "pending",
            "created_at": datetime(2026, 8, 28, 10, 5).replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        assert database._pending_schedule_job_still_valid(sch, job, now) is False


def test_pending_job_valid_in_grace():
    app = create_app()
    with app.app_context():
        now = datetime(2026, 8, 28, 10, 40)
        sch = {"schedule_type": "daily", "run_time": "10:30", "enabled": 1}
        job = {
            "status": "pending",
            "created_at": datetime(2026, 8, 28, 10, 31).replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        assert database._pending_schedule_job_still_valid(sch, job, now) is True


def test_daily_state_pending_offline_shows_due_not_queued():
    app = create_app()
    with app.app_context():
        now = datetime(2026, 8, 28, 10, 40)
        sch = {"enabled": 1, "schedule_type": "daily", "run_time": "10:30"}
        job = {
            "status": "pending",
            "created_at": datetime(2026, 8, 28, 10, 31).replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        st = database.compute_schedule_daily_state(
            sch, now=now, today_job=job, worker_online=False
        )
        assert st["daily_state"] == "due"


if __name__ == "__main__":
    test_daily_state_before_slot()
    test_daily_state_due_in_grace()
    test_daily_state_missed_after_grace()
    test_daily_state_completed_today_job()
    test_pending_job_invalid_after_grace()
    test_pending_job_valid_in_grace()
    test_daily_state_pending_offline_shows_due_not_queued()
    print("ok")
