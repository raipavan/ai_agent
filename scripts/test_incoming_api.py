import urllib.request, json

# Test API
req = urllib.request.Request(
    'http://127.0.0.1:8000/api/login',
    data=json.dumps({"password": "Maruti@123"}).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
resp = urllib.request.urlopen(req)
token = json.loads(resp.read())['access_token']
print('Token:', token[:20] + '...')

# Test incoming calls endpoint
req2 = urllib.request.Request(
    'http://127.0.0.1:8000/api/incoming/calls/recent?role=sales_1&limit=5',
    headers={'Authorization': f'Bearer {token}'}
)
resp2 = urllib.request.urlopen(req2)
data = json.loads(resp2.read())
print('Items:', len(data.get('items', [])))
for item in data.get('items', []):
    print(f"  id={item['id']} status={item['status']} name={item.get('callee_name','')} phone={item.get('from_phone','')}")
