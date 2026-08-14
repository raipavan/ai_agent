import sqlite3
conn = sqlite3.connect("/opt/data-edge/Data-Edge/backend/data/vernika.db")
cols = conn.execute("PRAGMA table_info(role_state)").fetchall()
print("role_state columns:")
for c in cols:
    print(f"  {c[1]} ({c[2]})")
rows = conn.execute("SELECT * FROM role_state").fetchall()
for r in rows:
    print(f"\nRow: {r[0]}")
    for i, c in enumerate(cols):
        val = str(r[i])[:120] if r[i] else "NULL"
        print(f"  {c[1]}: {val}")
conn.close()
