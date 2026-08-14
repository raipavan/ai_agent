import sqlite3, os, glob

db_path = "/opt/data-edge/Data-Edge/backend/data/vernika.db"
conn = sqlite3.connect(db_path)

# Check for any JSONL files with PITCHXAI content
print("=== Searching for PITCHXAI in transcript files ===")
base = "/opt/data-edge/Data-Edge/backend/data"
found = 0
for root, dirs, files in os.walk(base):
    for f in files:
        if f.endswith(".jsonl"):
            fpath = os.path.join(root, f)
            try:
                with open(fpath, "r") as fh:
                    content = fh.read()
                if "PITCHXAI" in content or "Maruti Suzuki" in content or "pitchxai" in content.lower():
                    print(f"  FOUND: {fpath}")
                    print(f"  Content preview: {content[:300]}")
                    found += 1
                    if found >= 3:
                        break
            except:
                pass
    if found >= 3:
        break

if found == 0:
    print("  No PITCHXAI/Maruti Suzuki found in transcript files")

# Check conversation_logs for the specific log_id
log_id = "camp-971408c1-2ea-20260617T14215"
print(f"\n=== Checking conversation_logs for {log_id} ===")
for day_dir in glob.glob(f"{base}/conversation_logs/*"):
    fpath = f"{day_dir}/{log_id}.jsonl"
    if os.path.exists(fpath):
        with open(fpath) as f:
            content = f.read()
        print(f"  FOUND: {fpath}")
        print(f"  Content (first 500): {content[:500]}")

# Check all transcript dirs for this log_id
print(f"\n=== Checking all log dirs for {log_id} ===")
for role in ["sales_1", "maruti", "sales_2"]:
    for day_dir in glob.glob(f"{base}/{role}/logs/*"):
        for ext in ["jsonl", "txt"]:
            fpath = f"{day_dir}/{log_id}.{ext}"
            if os.path.exists(fpath):
                with open(fpath) as f:
                    content = f.read()
                print(f"  FOUND: {fpath}")
                print(f"  Content (first 500): {content[:500]}")

# Also check legacy paths
print(f"\n=== Checking legacy paths ===")
for legacy in ["/root/vernika/backend/data", "/root/vernika/agent/data", "/root/DataEdge/backend/data"]:
    for day_dir in glob.glob(f"{legacy}/conversation_logs/*") if os.path.exists(legacy) else []:
        fpath = f"{day_dir}/{log_id}.jsonl"
        if os.path.exists(fpath):
            with open(fpath) as f:
                content = f.read()
            print(f"  FOUND: {fpath}")
            print(f"  Content (first 500): {content[:500]}")

conn.close()
