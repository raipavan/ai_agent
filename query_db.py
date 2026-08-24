import psycopg2
import os
from dotenv import load_dotenv
load_dotenv('backend/.env')

dsn = os.getenv('DATABASE_URL')
conn = psycopg2.connect(dsn)
cur = conn.cursor()

# Check sample data from each table
for table in ['manual_calls', 'incoming_calls', 'leads']:
    try:
        cur.execute(f'SELECT * FROM {table} LIMIT 5')
        rows = cur.fetchall()
        print(f'{table}: {len(rows)} rows')
        if rows:
            cols = [desc[0] for desc in cur.description]
            for r in rows:
                d = dict(zip(cols, r))
                phone_fields = {k:v for k,v in d.items() if 'phone' in k.lower()}
                print(f'  id={d.get("id")}: phones={phone_fields}')
    except Exception as e:
        print(f'{table}: error - {e}')

# Also search with different formats
formats = ['9769660799', '+919769660799', '919769660799', '09769660799']
for fmt in formats:
    for table, col in [('manual_calls', 'to_phone'), ('incoming_calls', 'from_phone'), ('leads', 'phone')]:
        try:
            cur.execute(f'SELECT id, {col} FROM {table} WHERE {col} ILIKE %s LIMIT 3', (f'%{fmt}%',))
            rows = cur.fetchall()
            if rows:
                print(f'Match in {table}.{col} for {fmt}: {rows}')
        except Exception as e:
            pass

cur.close()
conn.close()