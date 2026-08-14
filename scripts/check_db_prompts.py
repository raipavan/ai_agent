import sqlite3, sys
db_path = "/opt/data-edge/Data-Edge/backend/data/vernika.db"
conn = sqlite3.connect(db_path)

print("=== ROLE STATES (prompt preview) ===")
rows = conn.execute("SELECT id, role, SUBSTR(prompt,1,120) as preview FROM role_states").fetchall()
for r in rows:
    print(f"  id={r[0]} role={r[1]} prompt={r[2]}")

print("\n=== LEADS with _log_id (sample) ===")
rows = conn.execute("SELECT id, role, phone, name, _log_id, status FROM leads WHERE _log_id IS NOT NULL AND _log_id != '' ORDER BY id DESC LIMIT 5").fetchall()
for r in rows:
    print(f"  id={r[0]} role={r[1]} phone={r[2]} name={r[3]} log_id={r[4]} status={r[5]}")

print("\n=== LEADS without _log_id (sample) ===")
rows = conn.execute("SELECT id, role, phone, name, status FROM leads WHERE (_log_id IS NULL OR _log_id = '') AND status != 'pending' ORDER BY id DESC LIMIT 5").fetchall()
for r in rows:
    print(f"  id={r[0]} role={r[1]} phone={r[2]} name={r[3]} status={r[4]}")

print("\n=== MANUAL CALLS (sample) ===")
rows = conn.execute("SELECT id, role, to_phone, callee_name, log_id, status FROM manual_calls ORDER BY id DESC LIMIT 5").fetchall()
for r in rows:
    print(f"  id={r[0]} role={r[1]} phone={r[2]} name={r[3]} log_id={r[4]} status={r[5]}")

print("\n=== LEAD COUNTS ===")
for role in ["maruti", "sales_1", "sales_2"]:
    total = conn.execute("SELECT COUNT(*) FROM leads WHERE role=?", (role,)).fetchone()[0]
    called = conn.execute("SELECT COUNT(*) FROM leads WHERE role=? AND (status='completed' OR status='interested' OR status='not_interested' OR status='callback_scheduled')", (role,)).fetchone()[0]
    print(f"  {role}: total={total}, called={called}")

conn.close()
