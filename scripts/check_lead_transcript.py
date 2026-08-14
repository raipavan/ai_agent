import sqlite3, os, glob

db_path = "/opt/data-edge/Data-Edge/backend/data/vernika.db"
conn = sqlite3.connect(db_path)

# Check lead 2252 (Dr. Giridhara Kaje from screenshots)
row = conn.execute("SELECT id, role, phone, name, _log_id, status, analysis FROM leads WHERE id=2252").fetchone()
if row:
    print(f"=== LEAD {row[0]} ===")
    print(f"  name: {row[3]}")
    print(f"  phone: {row[2]}")
    print(f"  _log_id: {row[4]}")
    print(f"  status: {row[5]}")
    analysis = row[6] or ""
    print(f"  analysis (first 500): {analysis[:500]}")

    # Try to find the transcript
    log_id = row[4]
    if log_id:
        # Check conversation logs
        base = "/opt/data-edge/Data-Edge/backend/data"
        for role_dir in ["sales_1", "maruti", "sales_2", "logs"]:
            for day_dir in glob.glob(f"{base}/{role_dir}/logs/*"):
                for ext in ["jsonl", "txt"]:
                    fpath = f"{day_dir}/{log_id}.{ext}"
                    if os.path.exists(fpath):
                        print(f"\n  TRANSCRIPT FOUND: {fpath}")
                        with open(fpath) as f:
                            content = f.read()
                        print(f"  Content (first 500): {content[:500]}")
                        break

        # Check conversation_logs
        for day_dir in glob.glob(f"{base}/conversation_logs/*"):
            fpath = f"{day_dir}/{log_id}.jsonl"
            if os.path.exists(fpath):
                print(f"\n  CONVERSATION LOG: {fpath}")
                with open(fpath) as f:
                    content = f.read()
                print(f"  Content (first 500): {content[:500]}")

        # Check recording exists
        rec_base = "/opt/data-edge/Data-Edge/backend/data/call_recordings"
        for day_dir in glob.glob(f"{rec_base}/*"):
            for ext in ["mp3", "wav"]:
                fpath = f"{day_dir}/{log_id}_mixed.{ext}"
                if os.path.exists(fpath):
                    size = os.path.getsize(fpath)
                    print(f"\n  RECORDING: {fpath} ({size} bytes)")

conn.close()
