"""
epaad – Cancel Appointments by onedoc_key
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
import json

BASE_URL    = "https://mcv.epaad.ch"
USERNAME    = "onedoc"
PASSWORD    = "XgyUqCWJQ4Xg9pB4"
CALENDAR_ID = 4

# ── Keys to cancel ────────────────────────────────────────────────────────────
KEYS_TO_CANCEL = [
    "d3c621e2-247f-4af9-a9ea-f55e37a982bb",  # 12:00
    "bf97235c-6f72-45f0-81fb-6a00dfe645b3",  # 12:05
    "3efb3df0-5c69-41ef-ae85-6371b3334203",  # 12:10
    "e5707d33-f19c-4148-b5d9-f8da1b7e16c8",  # 12:15
]

def get_token():
    r = requests.post(
        f"{BASE_URL}/api/v1/authenticate",
        data={"username": USERNAME, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20
    )
    r.raise_for_status()
    return r.json()["auth_token"]

def cancel_appointment(token, onedoc_key):
    r = requests.delete(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events/{onedoc_key}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=20
    )
    return r.status_code, r.text

def verify(token):
    r = requests.get(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={"fromDateTime": "2026-03-16T11:50:00", "untilDateTime": "2026-03-16T12:30:00"},
        timeout=20
    )
    return r.json() if r.status_code == 200 else []

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   epaad – Cancel Test Appointments                      ║")
    print("╚══════════════════════════════════════════════════════════╝")

    print("\n🔐 Authenticating...", end=" ")
    token = get_token()
    print("✅ OK")

    print(f"\n{'═'*60}")
    print(f"  🗑️  CANCELLING {len(KEYS_TO_CANCEL)} SLOTS")
    print(f"{'═'*60}")

    times  = ["12:00", "12:05", "12:10", "12:15"]
    all_ok = True

    for i, key in enumerate(KEYS_TO_CANCEL):
        status, body = cancel_appointment(token, key)
        icon = "✅" if status == 204 else "❌"
        print(f"\n  {icon} Slot {i+1} [{times[i]}] → Status: {status}")
        if body:
            print(f"     Response: {body[:100]}")
        if status != 204:
            all_ok = False

    print(f"\n{'═'*60}")
    print(f"  🔍 VERIFYING — Checking 12:00–12:30 on 2026-03-16")
    print(f"{'═'*60}")

    events = verify(token)
    if events:
        print(f"\n  ⚠️  {len(events)} events still found in this window:")
        for e in events:
            print(f"     {e['startDateTime'][11:16]}  {e.get('type','')}  {e.get('summary','')}")
    else:
        print(f"\n  ✅ No events found in 12:00–12:30 window — all cancelled!")

    print(f"\n{'═'*60}")
    print(f"  Status: {'✅ ALL CANCELLED SUCCESSFULLY' if all_ok else '❌ SOME FAILED'}")
    print(f"{'═'*60}\n")
