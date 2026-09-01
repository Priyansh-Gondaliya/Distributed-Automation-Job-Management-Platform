from app import database
import time

def run_test():
    start_time = time.time()
    
    # Create 200 Scripts & Schedules
    print('Inserting 200 Scripts and Schedules...')
    with database.db_cursor() as cur:
        for i in range(200):
            try:
                cur.execute(
                    "INSERT INTO scripts (script_name, script_path, worker_name, owner_id, created_at) VALUES (?, ?, ?, 1, 'now')",
                    (f'perf_script_{i}.py', f'/path/perf_script_{i}.py', 'worker1')
                )
                script_id = cur.lastrowid
                
                cur.execute(
                    "INSERT INTO schedules (script_id, user_id, worker_name, run_time, days, enabled, created_at, updated_at) VALUES (?, 1, 'worker1', '12:00', 0, 1, 'now', 'now')",
                    (script_id,)
                )
            except Exception as e:
                pass
            
    # Create 5000 Jobs (File history mock)
    print('Inserting 5000 Job Histories...')
    with database.db_cursor() as cur:
        for i in range(5000):
            try:
                cur.execute(
                    "INSERT INTO jobs (worker_name, script_id, status, created_at) VALUES ('worker1', 1, 'completed', 'now')"
                )
            except:
                pass
            
    insert_time = time.time() - start_time
    print(f'Data inserted in {insert_time:.2f} seconds.')
    
    # Benchmark 1: list_schedules()
    t1 = time.time()
    schedules = database.list_schedules(1)
    t2 = time.time()
    print(f'list_schedules() took: {(t2 - t1) * 1000:.2f} ms')
    
    # Benchmark 2: get_due_schedules()
    t1 = time.time()
    due = database.get_due_schedules()
    t2 = time.time()
    print(f'get_due_schedules() took: {(t2 - t1) * 1000:.2f} ms')
    
    # Benchmark 3: Worker Polling 
    t1 = time.time()
    job = database.claim_pending_job('worker1')
    t2 = time.time()
    print(f'claim_pending_job() took: {(t2 - t1) * 1000:.2f} ms')

run_test()
