from app import database

def run_test():
    # Using proper functions instead of raw SQL
    database.create_user('admin_test', 'hash', '127', 'admin')
    database.create_user('user_owner', 'hash', '127', 'user')
    database.create_user('user_no_perm', 'hash', '127', 'user')
    database.create_user('user_perm', 'hash', '127', 'user')
    
    admin_id = database.get_user_by_username('admin_test')['id']
    owner_id = database.get_user_by_username('user_owner')['id']
    no_perm_id = database.get_user_by_username('user_no_perm')['id']
    perm_id = database.get_user_by_username('user_perm')['id']
    
    with database.db_cursor() as cur:
        try: cur.execute("INSERT INTO workers (worker_name, state) VALUES ('worker1', 'idle')")
        except: pass
        cur.execute("UPDATE workers SET owner_id = ? WHERE worker_name = 'worker1'", (owner_id,))
        
        try:
            cur.execute("INSERT INTO scripts (script_name, script_path, worker_name, owner_id, created_at) VALUES ('test.py', 'test.py', 'worker1', ?, 'now')", (owner_id,))
            script_id = cur.lastrowid
        except:
            cur.execute("SELECT id FROM scripts WHERE script_name='test.py' AND worker_name='worker1'")
            script_id = cur.fetchone()[0]
        
    sch_admin = database.create_schedule(script_id, owner_id, 'worker1', '12:00', 0)['id']
    sch_owner = database.create_schedule(script_id, owner_id, 'worker1', '12:00', 0)['id']
    sch_noperm = database.create_schedule(script_id, owner_id, 'worker1', '12:00', 0)['id']
    sch_perm = database.create_schedule(script_id, owner_id, 'worker1', '12:00', 0)['id']
    
    database.grant_schedule_access(sch_perm, perm_id, admin_id, can_delete=1)
    
    print(f'Created users: Admin={admin_id}, Owner={owner_id}, NoPerm={no_perm_id}, Perm={perm_id}')
    
    def exists(sid):
        with database.db_cursor() as cur:
            cur.execute("SELECT 1 FROM schedules WHERE id = ?", (sid,))
            return cur.fetchone() is not None
            
    def can_delete(uid, sch_id):
        schedules = {s['id']: s for s in database.list_schedules(uid)}
        sch = schedules.get(sch_id)
        if not sch: return False, 'Not in list'
        if sch['user_id'] == uid or database.is_admin(uid) or sch.get('can_delete'): return True, 'Auth'
        return False, 'Unauth'
        
    auth, msg = can_delete(admin_id, sch_admin)
    if auth: database.delete_schedule(sch_admin)
    print(f'Admin delete actual success: {not exists(sch_admin)}')
    
    auth, msg = can_delete(owner_id, sch_owner)
    if auth: database.delete_schedule(sch_owner)
    print(f'Owner delete actual success: {not exists(sch_owner)}')
    
    auth, msg = can_delete(perm_id, sch_perm)
    if auth: database.delete_schedule(sch_perm)
    print(f'Perm user delete actual success: {not exists(sch_perm)}')
    
    auth, msg = can_delete(no_perm_id, sch_noperm)
    if auth: database.delete_schedule(sch_noperm)
    print(f'No-perm user delete actual success: {not exists(sch_noperm)} (Expect False if schedule is retained)')
    
    with database.db_cursor() as cur:
        cur.execute("DELETE FROM users WHERE id IN (?, ?, ?, ?)", (admin_id, owner_id, no_perm_id, perm_id))
        cur.execute("DELETE FROM schedules WHERE id IN (?, ?, ?, ?)", (sch_admin, sch_owner, sch_noperm, sch_perm))
        cur.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
        cur.execute("DELETE FROM workers WHERE worker_name = 'worker1'")

run_test()
