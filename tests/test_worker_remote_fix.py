"""Verify scheduler timezone fix and worker path resolution for remote PCs."""
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import database


def test_daily_grace_window():
    from datetime import timedelta

    now = datetime.now().replace(second=0, microsecond=0)
    sch = {
        "run_time": (now - timedelta(minutes=2)).strftime("%H:%M"),
        "schedule_type": "daily",
        "schedule_config": "{}",
        "last_run": None,
    }
    assert database.schedule_is_due(sch, now) is True
    sch["run_time"] = (now - timedelta(minutes=10)).strftime("%H:%M")
    assert database.schedule_is_due(sch, now) is False
    sch["run_time"] = (now + timedelta(minutes=2)).strftime("%H:%M")
    assert database.schedule_is_due(sch, now) is False
    print("PASS daily grace window (2 min late fires, 10 min late does not)")


def test_last_run_aware_datetime():
    now = datetime.now()
    sch = {
        "run_time": now.strftime("%H:%M"),
        "schedule_type": "daily",
        "schedule_config": "{}",
        "last_run": datetime.now(timezone.utc),
    }
    result = database.schedule_is_due(sch, now)
    assert result is False
    print("PASS last_run aware datetime does not crash and counts as already-run today")


def test_schedule_is_due_interval_no_timezone_crash():
    now = datetime.now()
    sch = {
        "run_time": "09:00",
        "schedule_type": "interval",
        "schedule_config": '{"interval_val": "5m"}',
        "last_run": "2026-08-17 09:00:00",
    }
    # Must not raise: can't subtract offset-naive and offset-aware datetimes
    result = database.schedule_is_due(sch, now)
    assert isinstance(result, bool)
    print("PASS schedule_is_due interval (no timezone crash)")


def test_resolve_job_script_path():
    with tempfile.TemporaryDirectory() as tmp:
        scripts_dir = Path(tmp) / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "sleep_test.py"
        script.write_text("import time\nprint('hello')\ntime.sleep(4)\nprint('done')\n", encoding="utf-8")

        os.environ["SCRIPTS_DIR"] = str(scripts_dir)
        os.environ["WORKER_NAME"] = "test_worker"

        # Import after env so SCRIPTS_DIR is picked up
        import importlib
        import worker_agent.worker as w

        importlib.reload(w)

        foreign = r"C:\OtherPC\Automation\scripts\sleep_test.py"
        resolved = w._resolve_job_script_path(foreign, "sleep_test.py")
        assert resolved == str(script.resolve()), resolved
        print("PASS _resolve_job_script_path maps foreign path to local SCRIPTS_DIR")


def test_execute_script_runs_four_seconds():
    with tempfile.TemporaryDirectory() as tmp:
        scripts_dir = Path(tmp) / "scripts"
        scripts_dir.mkdir()
        logs_dir = Path(tmp) / "logs"
        logs_dir.mkdir()
        script = scripts_dir / "sleep_test.py"
        script.write_text(
            "import time\nprint('worker test start')\ntime.sleep(4)\nprint('worker test done')\n",
            encoding="utf-8",
        )

        os.environ["SCRIPTS_DIR"] = str(scripts_dir)
        os.environ["WORKER_NAME"] = "test_worker"

        import importlib
        import worker_agent.worker as w

        w.LOGS_DIR = logs_dir
        importlib.reload(w)
        w.LOGS_DIR = logs_dir

        code, output, duration = w.execute_script(str(script), job_id=99999)
        assert code == 0, (code, output)
        assert duration >= 3.5, f"duration too short: {duration}"
        assert "worker test done" in output
        print(f"PASS execute_script ran {duration:.2f}s (>= 4s expected)")


if __name__ == "__main__":
    test_schedule_is_due_interval_no_timezone_crash()
    test_daily_grace_window()
    test_last_run_aware_datetime()
    test_resolve_job_script_path()
    test_execute_script_runs_four_seconds()
    print("All remote-worker fix tests passed.")
