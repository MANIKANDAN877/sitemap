import os

os.environ['USE_MOCK_MONGO'] = '1'
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
payload = {
    'emp_code': 'EMP1001',
    'name': 'Test Employee',
    'email': 'test@example.com',
    'department': 'Engineering',
    'shift_start': '09:30',
    'shift_end': '18:30',
    'joined_on': '2026-10-01',
}
create = client.post('/employees', json=payload)
print('create', create.status_code, create.json())

punch = client.post('/attendance/punch-in', json={'emp_code': 'EMP1001', 'punched_at': 1760000000000, 'status': 'PRESENT'})
print('punch', punch.status_code, punch.json())

out = client.post('/attendance/punch-out', json={'emp_code': 'EMP1001', 'punched_at': 1760000000000 + 9*3600*1000})
print('out', out.status_code, out.json())

summary = client.get('/analytics/employees/EMP1001/monthly', params={'month': '2026-10'})
print('summary', summary.status_code, summary.json())
