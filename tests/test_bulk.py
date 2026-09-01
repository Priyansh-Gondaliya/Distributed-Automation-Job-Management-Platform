from run import app
from app.database import get_db_connection

def test():
    with app.test_client() as client:
        # First login as admin
        client.post('/login', data={'username': 'admin', 'password': 'password'}) # assuming admin password
        
        # Or mock session?
        with client.session_transaction() as sess:
            sess['user_id'] = 3 # admin
            sess['role'] = 'admin'

        response = client.post('/bulk-update-schedules', data={
            'action': 'delete',
            'schedule_ids': '36'
        })
        print("Status code:", response.status_code)
        
if __name__ == '__main__':
    test()
