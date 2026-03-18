"""
epaad – Book Appointment
=========================
Books: 2026-03-20 at 16:45 (15 min / 1 issue / 3 slots)
"""
import sys
# sys.stdout.reconfigure(encoding='utf-8')
import requests, json
from datetime import datetime, timedelta

BASE_URL    = "https://mcv.epaad.ch"
USERNAME    = "onedoc"
PASSWORD    = "XgyUqCWJQ4Xg9pB4"
CALENDAR_ID = 4

# ══════════════════════════════════════════
#  ✏️  EDIT THESE BEFORE RUNNING
# ══════════════════════════════════════════
START_DATETIME = "2026-03-20T16:45:00"
NUM_ISSUES     = 1   # 1 issue = 15 min = 3 slots

PATIENT = {
    "firstName":          "Umar",
    "lastName":           "Test",
    "birthDate":          "1990-05-15T00:00:00",
    "gender":             "male",
    "address": {
        "street":         "Teststrasse",
        "streetNumber":   "1",
        "zipCode":        "4051",
        "city":           "Basel",
        "state":          "BS",
        "country":        "CH",
    },
    "privatePhoneNumber": "",
    "mobilePhoneNumber":  "+41791234567",
    "email":              "umar.test@example.com",
}
# ══════════════════════════════════════════

ISSUE_DURATION = {1: 15, 2: 20, 3: 30}

def get_token():
    r = requests.post(f"{BASE_URL}/api/v1/authenticate",
        data={"username": USERNAME, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
    r.raise_for_status()
    return r.json()["auth_token"]

def auth_headers(token):
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json", "Accept": "application/json"}

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   epaad – Book Appointment                              ║")
    print("╚══════════════════════════════════════════════════════════╝")

    print("\n🔐 Authenticating...", end=" ")
    token = get_token()
    print("✅ OK")

    duration  = ISSUE_DURATION[NUM_ISSUES]
    num_slots = duration // 5
    start_dt  = datetime.fromisoformat(START_DATETIME)
    slots     = [(start_dt + timedelta(minutes=5*i)).strftime("%Y-%m-%dT%H:%M:%S")
                 for i in range(num_slots)]

    print(f"\n{'═'*60}")
    print(f"  📋 BOOKING DETAILS")
    print(f"{'═'*60}")
    print(f"  Doctor   : Dr. Amjad Mallisho (Calendar {CALENDAR_ID})")
    print(f"  Patient  : {PATIENT['firstName']} {PATIENT['lastName']}")
    print(f"  Date     : 2026-03-20 (Friday)")
    print(f"  Time     : {START_DATETIME[11:16]}")
    print(f"  Issues   : {NUM_ISSUES} → {duration} min → {num_slots} slots")
    print(f"  Slots    : {' | '.join(s[11:16] for s in slots)}")

    print(f"\n{'═'*60}")
    print(f"  🚀 BOOKING")
    print(f"{'═'*60}")

    booked_keys = []
    all_ok = True

    for i, slot_time in enumerate(slots, 1):
        payload = {
            "startDateTime": slot_time,
            "comment": f"AI Booking – {NUM_ISSUES} issue(s) – Slot {i}/{num_slots}",
            "patient": PATIENT,
        }
        r = requests.post(f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events",
            headers=auth_headers(token), json=payload, timeout=20)
        try:
            body = r.json()
        except:
            body = {"raw": r.text}

        icon = "✅" if r.status_code in (200, 201) else "❌"
        print(f"\n  {icon} Slot {i} [{slot_time[11:16]}] → Status: {r.status_code}")
        print(f"     {json.dumps(body, ensure_ascii=False)}")

        if r.status_code in (200, 201):
            key = body.get("onedoc_key") or body.get("key")
            if key:
                booked_keys.append((slot_time[11:16], key))
        else:
            all_ok = False

    print(f"\n{'═'*60}")
    print(f"  📊 RESULT")
    print(f"{'═'*60}")
    print(f"  Status : {'✅ ALL SLOTS BOOKED!' if all_ok else '❌ SOME SLOTS FAILED'}")
    print(f"\n  🔑 onedoc_keys (save these for cancellation):")
    print(f"  {'─'*50}")
    for time, key in booked_keys:
        print(f"  [{time}]  {key}")

    # Save keys to file for easy use in cancel script
    with open("booked_keys.json", "w") as f:
        json.dump({"date": "2026-03-20", "slots": booked_keys}, f, indent=2)
    print(f"\n  💾 Keys saved to: booked_keys.json")
    print(f"     (Cancel script will auto-read from this file)")
    print(f"{'═'*60}\n")
