"""
epaad – Reschedule Appointment
================================
Steps:
  1. Cancel old slots (by onedoc_keys)
  2. Book new slots at new datetime
  3. Verify both old and new slots

Run:
    python epaad_reschedule.py
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

# ══════════════════════════════════════════════════════════════════════════════
#  ✏️  EDIT THESE VALUES BEFORE RUNNING
# ══════════════════════════════════════════════════════════════════════════════

# Old booking — keys to cancel
OLD_ONEDOC_KEYS = [
    "d3c621e2-247f-4af9-a9ea-f55e37a982bb",  # 12:00
    "bf97235c-6f72-45f0-81fb-6a00dfe645b3",  # 12:05
    "3efb3df0-5c69-41ef-ae85-6371b3334203",  # 12:10
    "e5707d33-f19c-4148-b5d9-f8da1b7e16c8",  # 12:15
]

# New booking — new datetime + same patient
NEW_START_DATETIME = "2026-03-20T12:05:00"   # Friday 12:05 (85 min free)
NEW_NUM_ISSUES     = 2                        # 2 issues → 20 min → 4 slots

NEW_PATIENT = {
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

# ══════════════════════════════════════════════════════════════════════════════

def get_token():
    r = requests.post(
        f"{BASE_URL}/api/v1/authenticate",
        data={"username": USERNAME, "password": PASSWORD},
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

def generate_slots(start_str, num_issues):
    duration = ISSUE_DURATION.get(num_issues, 15)
    num_slots = duration // 5
    start_dt = datetime.fromisoformat(start_str)
    slots = [
        (start_dt + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(num_slots)
    ]
    return slots, duration

def cancel_slot(token, onedoc_key):
    r = requests.delete(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events/{onedoc_key}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=20
    )
    return r.status_code, r.text

def book_slot(token, start_datetime, comment, patient):
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

def get_events_window(token, date_str, start_time, end_time):
    r = requests.get(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={
            "fromDateTime":  f"{date_str}T{start_time}",
            "untilDateTime": f"{date_str}T{end_time}",
        },
        timeout=20
    )
    return r.json() if r.status_code == 200 else []


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   epaad – Reschedule Appointment                        ║")
    print("║   Dr. Amjad Mallisho | Calendar 4 | MedCenter Volta    ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # Auth
    print("\n🔐 Authenticating...", end=" ")
    token = get_token()
    print("✅ OK")

    new_slots, duration = generate_slots(NEW_START_DATETIME, NEW_NUM_ISSUES)
    old_date = "2026-03-16"
    new_date = NEW_START_DATETIME[:10]

    # ── Plan ─────────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  📋 RESCHEDULE PLAN")
    print(f"{'═'*60}")
    print(f"  Patient      : {NEW_PATIENT['firstName']} {NEW_PATIENT['lastName']}")
    print(f"\n  ❌ OLD Booking (will be cancelled):")
    print(f"     Date  : Monday 2026-03-16")
    print(f"     Slots : 12:00 / 12:05 / 12:10 / 12:15")
    print(f"\n  ✅ NEW Booking (will be created):")
    print(f"     Date  : {new_date}  {NEW_START_DATETIME[11:16]}")
    print(f"     Issues: {NEW_NUM_ISSUES} → {duration} min → {len(new_slots)} slots")
    for i, s in enumerate(new_slots, 1):
        print(f"     Slot {i}: {s[11:16]}")

    # ── STEP 1: Cancel old slots ──────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  🗑️  STEP 1 — Cancelling {len(OLD_ONEDOC_KEYS)} Old Slots")
    print(f"{'═'*60}")

    cancel_ok = True
    for i, key in enumerate(OLD_ONEDOC_KEYS, 1):
        status, body = cancel_slot(token, key)
        icon = "✅" if status == 204 else "❌"
        print(f"\n  {icon} Slot {i} → Status: {status}  Key: {key[:18]}...")
        if body:
            print(f"     {body[:80]}")
        if status != 204:
            cancel_ok = False

    if cancel_ok:
        print(f"\n  ✅ All old slots cancelled!")
    else:
        print(f"\n  ⚠️  Some slots may already be cancelled or keys expired.")

    # ── STEP 2: Book new slots ────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  🚀 STEP 2 — Booking {len(new_slots)} New Slots")
    print(f"{'═'*60}")

    new_keys  = []
    booking_ok = True

    for i, slot_time in enumerate(new_slots, 1):
        comment = f"RESCHEDULE – {NEW_NUM_ISSUES} issues – Slot {i}/{len(new_slots)} – TEST bitte löschen"
        status, body = book_slot(token, slot_time, comment, NEW_PATIENT)
        icon = "✅" if status in (200, 201) else "❌"
        print(f"\n  {icon} Slot {i} [{slot_time[11:16]}] → Status: {status}")
        print(f"     {pretty(body)}")
        if status in (200, 201):
            key = body.get("onedoc_key") or body.get("key")
            if key:
                new_keys.append(key)
                print(f"     🔑 onedoc_key: {key}")
        else:
            booking_ok = False

    # ── STEP 3: Verify ────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  🔍 STEP 3 — Verifying")
    print(f"{'═'*60}")

    # Old slot should be empty
    print(f"\n  Old slot (2026-03-16 12:00–12:20) — should be EMPTY:")
    old_events = get_events_window(token, old_date, "12:00:00", "12:20:00")
    if not old_events:
        print(f"  ✅ Empty — cancellation confirmed!")
    else:
        print(f"  ⚠️  {len(old_events)} events still there:")
        for e in old_events:
            print(f"     {e['startDateTime'][11:16]}  {e.get('type','')}  {e.get('summary','')}")

    # New slot should have our booking
    new_end = (datetime.fromisoformat(NEW_START_DATETIME) + timedelta(minutes=duration)).strftime("%H:%M:%S")
    print(f"\n  New slot ({new_date} {NEW_START_DATETIME[11:16]}–{new_end}) — should show bookings:")
    new_events = get_events_window(token, new_date, f"{NEW_START_DATETIME[11:16]}:00", new_end)
    if new_events:
        print(f"  ✅ {len(new_events)} events found:")
        for e in new_events:
            print(f"     {e['startDateTime'][11:16]}  {e.get('type','')}  {e.get('summary','')}")
    else:
        print(f"  ⚠️  No events found — may be pending as appointment request")

    # ── Final Summary ─────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  📊 FINAL SUMMARY")
    print(f"{'═'*60}")
    print(f"  Patient      : {NEW_PATIENT['firstName']} {NEW_PATIENT['lastName']}")
    print(f"  Old Booking  : 2026-03-16 12:00  {'✅ Cancelled' if cancel_ok else '❌ Failed'}")
    print(f"  New Booking  : {new_date} {NEW_START_DATETIME[11:16]}  {'✅ Booked' if booking_ok else '❌ Failed'}")
    print(f"  Duration     : {duration} min ({NEW_NUM_ISSUES} issues, {len(new_slots)} slots)")
    if new_keys:
        print(f"  New Keys     :")
        for k in new_keys:
            print(f"    → {k}")
    print(f"\n  ➡️  Portal: https://mcv.epaad.ch")
    print(f"{'═'*60}\n")
