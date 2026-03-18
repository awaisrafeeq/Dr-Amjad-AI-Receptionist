import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
import json
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL = "https://mcv.epaad.ch"
USERNAME = "onedoc"
PASSWORD = "XgyUqCWJQ4Xg9pB4"

def verify():
    print("🔐 Authenticating...")
    r = requests.post(f"{BASE_URL}/api/v1/authenticate", json={"username": USERNAME, "password": PASSWORD})
    r.raise_for_status()
    token = r.json()["auth_token"]
    print("✅ Auth Success")

    print(f"\n🔍 Searching for changes between 4140 and 4152...")
    r = requests.get(f"{BASE_URL}/api/v1/onedoc/events/changes",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"afterChangeId": 4140})
    
    if r.status_code == 200:
        changes = r.json()
        print(f"Total: {len(changes)} changes found.")
        for ch in changes:
            ev = ch.get("event", {})
            print(f"🌟 Change {ch.get('changeId')}: {ch.get('changeType')} - Start: {ev.get('startDateTime')} - Summary: {ev.get('summary')}")
    else:
        print(f"❌ Error: {r.status_code} {r.text}")

    print("\n✅ Verification complete.")

if __name__ == "__main__":
    verify()
