import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:8000/api/login",
    data=json.dumps({"email": "admin@test.com", "password": "nimda"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
token = json.loads(resp.read())["token"]
print("Got token")

req2 = urllib.request.Request(
    "http://127.0.0.1:8000/api/incoming/calls/recent?role=sales_1&limit=5",
    headers={"Authorization": f"Bearer {token}"}
)
resp2 = urllib.request.urlopen(req2)
data = json.loads(resp2.read())
items = data.get("items", [])
print(f"Items: {len(items)}")
for item in items:
    print(f"  id={item.get('id')} name={item.get('callee_name')} phone={item.get('from_phone')} status={item.get('status')}")
