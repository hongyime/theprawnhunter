"""
TheprawnHunter post-startup cleanup.
Runs after docker compose up to clear stale session leases
that were left from the previous session.
"""
import sys
import time

import requests

# Load env
env = {}
try:
    with open(r"C:\theprawnhunter\.env") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v
except Exception as e:
    print(f"ERROR reading .env: {e}")
    sys.exit(1)

url = env.get("SUPABASE_URL")
svc = env.get("SUPABASE_SERVICE_ROLE_KEY")
if not url or not svc:
    print("ERROR: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    sys.exit(1)

H = {
    "apikey": svc,
    "Authorization": f"Bearer {svc}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# Wait for API to be healthy (up to 60s)
api_url = env.get("API_URL", "http://localhost:8011")
print(f"Waiting for API at {api_url}...")
for i in range(12):
    try:
        r = requests.get(f"{api_url}/health", timeout=5)
        if r.status_code == 200:
            print(f"API healthy after {i*5}s")
            break
    except Exception:
        pass
    time.sleep(5)
else:
    print("WARNING: API not healthy after 60s — proceeding anyway")

# Clear stale session leases
print("Clearing stale session leases...")
r = requests.patch(
    f"{url}/rest/v1/telegram_accounts",
    headers=H,
    params={"status": "eq.active"},
    json={"locked_by": None, "locked_until": None}
)
if r.status_code == 200:
    cleared = len(r.json())
    print(f"Cleared {cleared} stale lease(s)")
else:
    print(f"WARNING: Lease clear failed: {r.status_code} {r.text[:100]}")

print("Post-startup cleanup done.")
