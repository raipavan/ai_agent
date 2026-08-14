import sqlite3
conn = sqlite3.connect('/opt/data-edge/Data-Edge/backend/data/vernika.db')
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in c.fetchall()]
print('Tables:', tables)
if 'incoming_calls' in tables:
    c.execute('PRAGMA table_info(incoming_calls)')
    cols = c.fetchall()
    print('incoming_calls columns:', [col[1] for col in cols])
    c.execute('SELECT COUNT(*) FROM incoming_calls')
    print('Rows:', c.fetchone()[0])
else:
    print('incoming_calls table NOT found')
conn.close()
