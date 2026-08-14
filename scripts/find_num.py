import sqlite3, sys

db = "/opt/data-edge/Data-Edge/backend/data/vernika.db"
pattern = sys.argv[1] if len(sys.argv) > 1 else "720495"

conn = sqlite3.connect(db)
c = conn.cursor()

# Check leads
c.execute("SELECT id, name, phone FROM leads WHERE phone LIKE ?", (f"%{pattern}%",))
for r in c.fetchall():
    print(f"LEAD: id={r[0]} name={r[1]} phone={r[2]}")

# Check leads.extra JSON
c.execute("SELECT id, name, extra FROM leads WHERE extra LIKE ?", (f"%{pattern}%",))
for r in c.fetchall():
    print(f"LEAD_EXTRA: id={r[0]} name={r[1]} extra has pattern")

# Check role_state vobiz_config
c.execute("SELECT role, vobiz_config FROM role_state")
for r in c.fetchall():
    if pattern in (r[1] or ""):
        print(f"VOBIZ_CFG: role={r[0]} config contains pattern")

# Check .env file
import os
env_paths = ["/opt/data-edge/.env", "/opt/data-edge/Data-Edge/.env"]
for p in env_paths:
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and pattern in line:
                    print(f"ENV ({p}): {line}")

print(f"\nSearched for: {pattern}")
conn.close()
