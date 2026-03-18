"""
epaad – Cancel Appointment
============================
Reads onedoc_keys from booked_keys.json and cancels them.
OR manually paste keys below.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests, json, os

BASE_URL    = "https://mcv.epaad.ch"
USERNAME    = "onedoc"
PASSWORD    = "XgyUqCWJQ4Xg9pB4"
CALENDAR_ID = 4

# ══════════════════════════════════════════
#  ✏️  OPTION A: Auto-read from booked_keys.json (recommended)
#  OPTION B: Manually paste keys here
MANUAL_KEYS = []   # e.g. ["key1", "key2", "key3"]
# ══════════════════════════════════════════

def get_token():
    r = requests.post(f"{BASE_URL}/api/v1/authenticate",
        data={"username": USERNAME, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
    r.raise_for_status()
    return r.json()["auth_token"]

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   epaad – Cancel Appointment                            ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # Load keys
    keys_with_time = []
    if MANUAL_KEYS:
        keys_with_time = [("--:--", k) for k in MANUAL_KEYS]
        print(f"\n  📋 Using {len(MANUAL_KEYS)} manually provided keys")
    elif os.path.exists("booked_keys.json"):
        with open("booked_keys.json") as f:
            data = json.load(f)
        keys_with_time = data.get("slots", [])
        print(f"\n  📋 Loaded {len(keys_with_time)} keys from booked_keys.json")
        print(f"  📅 Date: {data.get('date', 'unknown')}")
    else:
        print("\n  ❌ No keys found!")
        print("  → Either run epaad_book.py first (creates booked_keys.json)")
        print("  → Or paste keys manually in MANUAL_KEYS list at top of script")
        exit(1)

    print(f"\n🔐 Authenticating...", end=" ")
    token = get_token()
    print("✅ OK")

    print(f"\n{'═'*60}")
    print(f"  🗑️  CANCELLING {len(keys_with_time)} SLOTS")
    print(f"{'═'*60}")

    all_ok = True
    for i, (time, key) in enumerate(keys_with_time, 1):
        r = requests.delete(
            f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events/{key}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=20
        )
        icon = "✅" if r.status_code == 204 else "❌"
        print(f"\n  {icon} Slot {i} [{time}] → Status: {r.status_code}")
        if r.text:
            print(f"     {r.text[:100]}")
        if r.status_code != 204:
            all_ok = False

    # Verify
    if keys_with_time:
        date_str = "2026-03-20"
        print(f"\n{'═'*60}")
        print(f"  🔍 VERIFYING — Checking 16:45–17:00 on {date_str}")
        print(f"{'═'*60}")
        r = requests.get(
            f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"fromDateTime": f"{date_str}T16:45:00",
                    "untilDateTime": f"{date_str}T17:00:00"},
            timeout=20
        )
        if r.status_code == 200:
            events = r.json()
            if not events:
                print(f"\n  ✅ Slot is empty — cancellation confirmed!")
            else:
                print(f"\n  ⚠️  {len(events)} event(s) still showing:")
                for e in events:
                    print(f"     {e['startDateTime'][11:16]}  {e.get('type','')}  {e.get('summary','')}")

    # Cleanup json file
    if os.path.exists("booked_keys.json") and all_ok:
        os.remove("booked_keys.json")
        print(f"\n  🧹 booked_keys.json deleted (cleanup)")

    print(f"\n{'═'*60}")
    print(f"  Status: {'✅ ALL CANCELLED!' if all_ok else '❌ SOME FAILED'}")
    print(f"{'═'*60}\n")
