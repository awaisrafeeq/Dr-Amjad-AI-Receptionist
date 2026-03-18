"""
epaad / onedoc – Create Appointment (Multi-Slot)
==================================================
Based on confirmed working API spec:
  - No "event" wrapper — send JSON directly
  - birthDate format: YYYY-MM-DDT00:00:00
  - 5-minute base slots
  - 1 issue → 15 min (3 slots)
  - 2 issues → 20 min (4 slots)
  - 3 issues → 30 min (6 slots)

Run:
    python epaad_create_appointment.py
"""

import requests
import json
from datetime import datetime, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://mcv.epaad.ch"
USERNAME    = "onedoc"
PASSWORD    = "XgyUqCWJQ4Xg9pB4"
CALENDAR_ID = 4

# ── BOOKING RULES (5-min base slots) ─────────────────────────────────────────
ISSUE_DURATION = {
    1: 15,   # 1 issue  → 15 min → 3 slots
    2: 20,   # 2 issues → 20 min → 4 slots
    3: 30,   # 3 issues → 30 min → 6 slots
}

# ── TEST PATIENT ──────────────────────────────────────────────────────────────
# Best free slot: Monday 2026-03-16, 12:00–13:50 (110 min free)
TEST_PATIENT = {
    "firstName":          "Umar",
    "lastName":           "Test",
    "birthDate":          "1990-05-15T00:00:00",   # ← correct format!
    "gender":             "male",
    "address": {
        "street":         "Teststrasse",
        "streetNumber":   "1",
        "zipCode":        "4051",
        "city":           "Basel",
        "state":          "BS",
        "country":        "CH",
    },
    "privatePhoneNumber": "",                       # ← must exist even if empty
    "mobilePhoneNumber":  "+41791234567",
    "email":              "umar.test@example.com",
}

# ── AUTH ──────────────────────────────────────────────────────────────────────
def get_token():
    r = requests.post(
        f"{BASE_URL}/api/v1/authenticate",
        data={"username": USERNAME, "password": PASSWORD},  # x-www-form-urlencoded
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20
    )
    r.raise_for_status()
    return r.json()["auth_token"]

def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

def pretty(data):
    return json.dumps(data, indent=2, ensure_ascii=False)

# ── SLOT GENERATOR ────────────────────────────────────────────────────────────
def generate_slots(start_datetime_str: str, num_issues: int):
    """
    Generate list of start datetimes for consecutive 5-min slots.
    e.g. start=12:00, issues=2 → [12:00, 12:05, 12:10, 12:15]
    """
    duration_min = ISSUE_DURATION.get(num_issues, 15)
    num_slots    = duration_min // 5
    start_dt     = datetime.fromisoformat(start_datetime_str)

    slots = []
    for i in range(num_slots):
        slot_time = start_dt + timedelta(minutes=5 * i)
        slots.append(slot_time.strftime("%Y-%m-%dT%H:%M:%S"))

    return slots, duration_min

# ── CREATE SINGLE SLOT ────────────────────────────────────────────────────────
def create_slot(token, start_datetime: str, comment: str, patient: dict):
    payload = {
        "startDateTime": start_datetime,
        "comment":       comment,
        "patient":       patient,
    }

    r = requests.post(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events",
        headers=auth_headers(token),
        json=payload,
        timeout=20
    )

    try:
        body = r.json()
    except:
        body = {"raw": r.text}

    return r.status_code, body

# ── VERIFY BOOKING ────────────────────────────────────────────────────────────
def verify_booking(token, date_str: str):
    r = requests.get(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events",
        headers=auth_headers(token),
        params={
            "fromDateTime":  f"{date_str}T00:00:00",
            "untilDateTime": f"{date_str}T23:59:59",
        },
        timeout=20
    )
    if r.status_code == 200:
        return r.json()
    return []

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   epaad – Multi-Slot Appointment Booking Test           ║")
    print("║   Dr. Amjad Mallisho | Calendar 4 | MedCenter Volta    ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # ── Auth
    print("\n🔐 Authenticating...", end=" ")
    try:
        token = get_token()
        print("✅ OK")
    except Exception as e:
        print(f"❌ {e}")
        exit(1)

    # ── Booking config
    START_DATETIME = "2026-03-16T12:00:00"   # Monday 12:00 (110 min free)
    NUM_ISSUES     = 2                        # 2 issues → 20 min → 4 slots

    slots, duration = generate_slots(START_DATETIME, NUM_ISSUES)

    print(f"\n{'═'*60}")
    print(f"  📋 BOOKING PLAN")
    print(f"{'═'*60}")
    print(f"  Patient     : {TEST_PATIENT['firstName']} {TEST_PATIENT['lastName']}")
    print(f"  Date        : Monday, 2026-03-16")
    print(f"  Start Time  : {START_DATETIME[11:16]}")
    print(f"  Issues      : {NUM_ISSUES} → {duration} minutes → {len(slots)} slots")
    print(f"  Phone       : {TEST_PATIENT['mobilePhoneNumber']}")
    print(f"\n  Slots to book:")
    for i, s in enumerate(slots, 1):
        print(f"    Slot {i}: {s[11:16]}")

    # ── Book each slot
    print(f"\n{'═'*60}")
    print(f"  🚀 BOOKING SLOTS")
    print(f"{'═'*60}")

    booked_keys = []
    all_success = True

    for i, slot_time in enumerate(slots, 1):
        comment = f"AI Booking – {NUM_ISSUES} issues – Slot {i}/{len(slots)} – TEST bitte löschen"
        status, body = create_slot(token, slot_time, comment, TEST_PATIENT)

        icon = "✅" if status in (200, 201) else "❌"
        print(f"\n  {icon} Slot {i} [{slot_time[11:16]}] → Status: {status}")
        print(f"     {pretty(body)}")

        if status in (200, 201):
            key = body.get("onedoc_key") or body.get("key")
            if key:
                booked_keys.append(key)
                print(f"     🔑 onedoc_key: {key}")
        else:
            all_success = False

    # ── Verify
    print(f"\n{'═'*60}")
    print(f"  🔍 VERIFYING — Events on 2026-03-16")
    print(f"{'═'*60}")

    events = verify_booking(token, "2026-03-16")
    print(f"\n  Total events found: {len(events)}\n")
    print(f"  {'Time':<10} {'Type':<22} {'Summary'}")
    print(f"  {'─'*50}")
    for e in sorted(events, key=lambda x: x["startDateTime"]):
        time    = e["startDateTime"][11:16]
        etype   = e.get("type", "")
        summary = e.get("summary", "") or ""
        marker  = " ◄ OUR BOOKING" if time in [s[11:16] for s in slots] else ""
        print(f"  {time:<10} {etype:<22} {summary}{marker}")

    # ── Summary
    print(f"\n{'═'*60}")
    print(f"  📊 FINAL SUMMARY")
    print(f"{'═'*60}")
    print(f"  Patient   : {TEST_PATIENT['firstName']} {TEST_PATIENT['lastName']}")
    print(f"  Date/Time : Monday 2026-03-16 {START_DATETIME[11:16]}")
    print(f"  Duration  : {duration} min ({NUM_ISSUES} issues, {len(slots)} slots)")
    print(f"  Status    : {'✅ ALL SLOTS BOOKED' if all_success else '❌ SOME SLOTS FAILED'}")
    if booked_keys:
        print(f"  Keys      :")
        for k in booked_keys:
            print(f"    → {k}")
    print(f"\n  ➡️  Portal: https://mcv.epaad.ch")
    print(f"{'═'*60}\n")
