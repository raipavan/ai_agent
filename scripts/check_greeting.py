import sqlite3
conn = sqlite3.connect('/opt/data-edge/Data-Edge/backend/data/vernika.db')
c = conn.cursor()
c.execute("SELECT role, greeting_text FROM role_state WHERE role IN ('sales_1', 'maruti')")
for r in c.fetchall():
    print('role:', r[0])
    print('greeting_text:', repr(r[1]))
    print()
conn.close()
