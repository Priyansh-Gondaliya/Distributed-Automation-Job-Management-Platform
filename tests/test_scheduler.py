from app import database
import time
from datetime import datetime

def run_test():
    # Check if a dummy worker script exists, otherwise log one
    # Note: Using direct SQL for testing since create_worker is not a dedicated func
    # Create a user first
    database.create_user('test_sched_user', 'hash', '127', 'admin')
    user_id = database.get_user_by_username('test_sched_user')['id']
    
    with database.db_cursor() as cur:
        try: cur.execute("INSERT INTO workers (worker_name, state) VALUES ('test_worker', 'idle')")
        except: pass
        cur.execute("UPDATE workers SET owner_id = ? WHERE worker_name = 'test_worker'", (user_id,))
        
        try: cur.execute("INSERT INTO scripts (script_name, script_path, worker_name, owner_id, created_at) VALUES ('test_script.py', 'test_script.py', 'test_worker', ?, 'now')", (user_id,))
        except: pass
        
        cur.execute("SELECT id FROM scripts WHERE script_name='test_script.py'")
        script_id = cur.fetchone()[0]

    print("Phase 3: Verify Scheduler Pipeline")
    print(f"Using Script ID: {script_id}")

    # Create a schedule that is past due TODAY to trigger it immediately
    local_now = datetime.now()
    past_time = f"{local_now.hour:02d}:{local_now.minute - 1 if local_now.minute > 0 else 59:02d}"
    
    # By default, create_schedule initializes last_run to now if run_time < current_time!
    # Let's bypass it for the test
    now_utc = database._utc_now()
    with database.db_cursor() as cur:
        cur.execute("INSERT INTO schedules (script_id, user_id, worker_name, run_time, days, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (script_id, user_id, 'test_worker', past_time, 0, now_utc, now_utc))
        sch_id = cur.lastrowid
        
    print(f"Created Schedule ID: {sch_id} with run_time: {past_time}")

    # Check due schedules
    due = database.get_due_schedules()
    is_due = any(s['id'] == sch_id for s in due)
    print(f"Is Schedule Due? {is_due}")
    
    if is_due:
        # Simulate Job Creation (What scheduler.py does)
        database.mark_schedule_run(sch_id)
        job_id = database.create_job('test_worker', script_id, sch_id)['id']
        print(f"Created Job ID: {job_id}")
        
        # Simulate Worker Poll
        job = database.claim_pending_job('test_worker')
        print(f"Worker Poll - Pending Jobs Found: {job is not None}")
        
        if job and job['id'] == job_id:
            print("Worker Successfully found job. Proceeding to update status.")
            database.update_job_status(job_id, 'running')
            time.sleep(1)
            database.update_job_status(job_id, 'completed')
            print("Job Completed.")
            
    # Cleanup
    with database.db_cursor() as cur:
        cur.execute("DELETE FROM schedules WHERE id = ?", (sch_id,))
        cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
        cur.execute("DELETE FROM workers WHERE worker_name = 'test_worker'")
        cur.execute("DELETE FROM scripts WHERE id = ?", (script_id,))

if __name__ == '__main__':
    run_test()
