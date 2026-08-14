import paramiko, time, json, sys, os
from datetime import datetime

LOG_FILE = r"C:\Users\Surya\Desktop\maruthi suziki\call_report.json"

def collect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('187.127.177.149', username='root', password='xOv,n;HhC+1KQE2C', timeout=10)
    
    data = {}
    
    # Total calls per role
    for role in ['sales_1', 'sales_2']:
        stdin, stdout, stderr = ssh.exec_command(f"journalctl -u data-edge --no-pager --since '00:00' 2>&1 | grep -c 'role={role}' | head -1", timeout=10)
        raw = stdout.read().decode(errors='replace').strip()
        try:
            data[f'{role}_total'] = int(raw)
        except:
            data[f'{role}_total'] = raw
    
    # Connected per role
    for role in ['sales_1', 'sales_2']:
        stdin, stdout, stderr = ssh.exec_command(f"journalctl -u data-edge --no-pager --since '00:00' 2>&1 | grep -c 'Vobiz WS.*client connected.*role={role}'", timeout=10)
        raw = stdout.read().decode(errors='replace').strip()
        try:
            data[f'{role}_connected'] = int(raw)
        except:
            data[f'{role}_connected'] = raw
    
    # No answer total
    stdin, stdout, stderr = ssh.exec_command("journalctl -u data-edge --no-pager --since '00:00' 2>&1 | grep -c 'No answer'", timeout=10)
    data['no_answer'] = stdout.read().decode(errors='replace').strip()
    
    # Failed
    stdin, stdout, stderr = ssh.exec_command("journalctl -u data-edge --no-pager --since '00:00' 2>&1 | grep -c 'marking failed'", timeout=10)
    data['failed'] = stdout.read().decode(errors='replace').strip()
    
    # Retries
    stdin, stdout, stderr = ssh.exec_command("journalctl -u data-edge --no-pager --since '00:00' 2>&1 | grep -c 'Scheduled failed-call retry'", timeout=10)
    data['retries'] = stdout.read().decode(errors='replace').strip()
    
    # WS connects total
    stdin, stdout, stderr = ssh.exec_command("journalctl -u data-edge --no-pager --since '00:00' 2>&1 | grep -c 'Vobiz WS.*client connected'", timeout=10)
    data['ws_connects'] = stdout.read().decode(errors='replace').strip()
    
    # Health
    stdin, stdout, stderr = ssh.exec_command("curl -m 5 -s https://surya.187.127.177.149.nip.io/health", timeout=10)
    data['health'] = stdout.read().decode(errors='replace').strip()
    
    # Service status
    stdin, stdout, stderr = ssh.exec_command("systemctl is-active data-edge", timeout=10)
    data['service'] = stdout.read().decode(errors='replace').strip()
    
    # Calls made file
    stdin, stdout, stderr = ssh.exec_command("cat /opt/data-edge/Data-Edge/backend/data/calls_made.json 2>/dev/null", timeout=10)
    data['calls_made'] = stdout.read().decode(errors='replace').strip()
    
    # Timestamp
    data['timestamp'] = datetime.now().isoformat()
    
    ssh.close()
    return data

# Collect every 10 minutes in background
print("Starting background data collection...")
print(f"Will collect every 10 minutes until 18:30 IST")
print(f"Results saved to: {LOG_FILE}")

results = []
start_time = time.time()
end_time = start_time + (3.2 * 3600)  # 3.2 hours from now

while time.time() < end_time:
    try:
        data = collect()
        results.append(data)
        
        # Save to file
        with open(LOG_FILE, 'w') as f:
            json.dump(results, f, indent=2)
        
        s1 = data.get('sales_1_total', '?')
        s2 = data.get('sales_2_total', '?')
        c1 = data.get('sales_1_connected', '?')
        c2 = data.get('sales_2_connected', '?')
        now = datetime.now().strftime('%H:%M')
        print(f"[{now}] sales1={s1}(c:{c1}) sales2={s2}(c:{c2}) retries={data.get('retries','?')} service={data.get('service','?')}")
        
    except Exception as e:
        print(f"Error: {e}")
    
    time.sleep(600)  # 10 minutes

# Final collection
try:
    data = collect()
    results.append(data)
    with open(LOG_FILE, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nFINAL: {json.dumps(data, indent=2)}")
except Exception as e:
    print(f"Final error: {e}")

print("Collection complete.")
