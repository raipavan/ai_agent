import sqlite3
db_path = "/opt/data-edge/Data-Edge/backend/data/vernika.db"
conn = sqlite3.connect(db_path)

print("=== TABLES ===")
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for t in tables:
    print(f"  {t[0]}")

print("\n=== LEADS with _log_id (sample 5) ===")
try:
    rows = conn.execute("SELECT id, role, phone, name, _log_id, status FROM leads WHERE _log_id IS NOT NULL AND _log_id != '' ORDER BY id DESC LIMIT 5").fetchall()
    for r in rows:
        print(f"  id={r[0]} role={r[1]} phone={r[2]} name={r[3]} log_id={r[4]} status={r[5]}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== LEADS without _log_id (sample 5) ===")
try:
    rows = conn.execute("SELECT id, role, phone, name, status FROM leads WHERE (_log_id IS NULL OR _log_id = '') AND status != 'pending' ORDER BY id DESC LIMIT 5").fetchall()
    for r in rows:
        print(f"  id={r[0]} role={r[1]} phone={r[2]} name={r[3]} status={r[4]}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== MANUAL CALLS (sample 5) ===")
try:
    rows = conn.execute("SELECT id, role, to_phone, callee_name, log_id, status FROM manual_calls ORDER BY id DESC LIMIT 5").fetchall()
    for r in rows:
        print(f"  id={r[0]} role={r[1]} phone={r[2]} name={r[3]} log_id={r[4]} status={r[5]}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== LEAD COUNTS ===")
try:
    for role in ["maruti", "sales_1", "sales_2"]:
        total = conn.execute("SELECT COUNT(*) FROM leads WHERE role=?", (role,)).fetchone()[0]
        print(f"  {role}: total={total}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== LEAD COLUMNS ===")
try:
    cols = conn.execute("PRAGMA table_info(leads)").fetchall()
    for c in cols:
        print(f"  {c[1]} ({c[2]})")
except Exception as e:
    print(f"  Error: {e}")

conn.close()
