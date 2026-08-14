import sqlite3, sys
db_path = '/opt/data-edge/Data-Edge/backend/data/vernika.db'
conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS incoming_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_phone TEXT NOT NULL,
    to_phone TEXT,
    callee_name TEXT,
    lead_name TEXT,
    preferred_location TEXT,
    preferred_budget TEXT,
    status TEXT DEFAULT 'ringing',
    transcript TEXT,
    summary TEXT,
    analysis_json TEXT,
    error TEXT,
    recording_available INTEGER DEFAULT 0,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)''')
conn.commit()
c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='incoming_calls'")
print('Table exists:', c.fetchone() is not None)
conn.close()
print('Done.')
