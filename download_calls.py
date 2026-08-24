import sqlite3
import os
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

# Connect to database
conn = sqlite3.connect('backend/data/app.db')
cursor = conn.cursor()

# Check tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print('Tables:', tables)

# Check counts
for table in ['manual_calls', 'incoming_calls', 'leads']:
    try:
        cursor.execute(f'SELECT COUNT(*) FROM {table}')
        print(f'{table} count:', cursor.fetchone()[0])
    except Exception as e:
        print(f'{table}: error - {e}')

conn.close()

# Find recent audio files (last 7 days)
call_dir = Path('backend/data/call_recordings/sales_1')
cutoff = datetime.now() - timedelta(days=7)

recent_files = []
for f in call_dir.glob('*.mp3'):
    mtime = datetime.fromtimestamp(f.stat().st_mtime)
    if mtime >= cutoff:
        recent_files.append(f)

for f in call_dir.glob('*.wav'):
    mtime = datetime.fromtimestamp(f.stat().st_mtime)
    if mtime >= cutoff:
        recent_files.append(f)

print(f'\nRecent audio files (last 7 days): {len(recent_files)}')
for f in sorted(recent_files, key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
    print(f'  {f.name} - {datetime.fromtimestamp(f.stat().st_mtime)}')

# Create zip
zip_path = 'recent_call_logs.zip'
with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
    for f in recent_files:
        arcname = f'call_recordings/{f.name}'
        zf.write(f, arcname)
    
    # Add a metadata file
    meta = f"Call Logs Export\nGenerated: {datetime.now()}\nTotal files: {len(recent_files)}\nDate range: Last 7 days\n"
    zf.writestr('call_recordings/README.txt', meta)

print(f'\nCreated: {zip_path}')
print(f'Size: {os.path.getsize(zip_path) / 1024 / 1024:.2f} MB')