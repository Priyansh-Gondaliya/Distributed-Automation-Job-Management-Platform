from app import database

def test():
    # Register script with days = None
    database.register_script("test_worker", "no_days.py", "/path/to/no_days.py", None)
    
    # Query it back
    with database.db_cursor() as cur:
        cur.execute("SELECT days FROM scripts WHERE script_name = 'no_days.py'")
        row = cur.fetchone()
        print(f"Days in DB for no_days.py: {row['days']}")

if __name__ == '__main__':
    test()
