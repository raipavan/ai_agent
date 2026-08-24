import sqlite3
import os

# Check all sqlite databases
for root, dirs, files in os.walk('backend/data'):
    for f in files:
        if f.endswith('.db'):
            path = os.path.join(root, f)
            try:
                conn = sqlite3.connect(path)
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = cur.fetchall()
                if tables:
                    print(f'{path}: {tables}')
                    for t in tables:
                        tname = t[0]
                        try:
                            cur.execute(f"SELECT * FROM {tname} LIMIT 3")
                            rows = cur.fetchall()
                            if rows:
                                print(f'  Sample {tname}: {rows[0]}')
                        except:
                            pass
                conn.close()
            except Exception as e:
                print(f'{path}: error - {e}')