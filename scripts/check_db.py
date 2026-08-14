import sqlite3, json
conn = sqlite3.connect("/opt/data-edge/Data-Edge/backend/data/vernika.db")
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute("SELECT id, caller_name, from_phone, status, summary FROM incoming_calls WHERE role='sales_1' ORDER BY id DESC LIMIT 5")
for r in c.fetchall():
    d = dict(r)
    d["callee_name"] = d.get("caller_name") or ""
    print(json.dumps({k: v for k, v in d.items() if k in ("id","caller_name","callee_name","from_phone","status")}, indent=2))
conn.close()
