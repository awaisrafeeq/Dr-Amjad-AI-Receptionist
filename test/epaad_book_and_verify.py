"""
epaad – Book Test Appointment & Verify
========================================
1. Creates a test appointment for Dr. Amjad Mallisho
2. Immediately fetches the event to verify it was booked
3. Shows full booking details

Run:
    python epaad_book_and_verify.py
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
import json
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://mcv.epaad.ch"
USERNAME    = "onedoc"
PASSWORD    = "XgyUqCWJQ4Xg9pB4"
CALENDAR_ID = 4

# ── TEST APPOINTMENT DETAILS ──────────────────────────────────────────────────
TEST_EVENT = {
    "startDateTime": "2026-03-19T09:00:00",   # Thursday 9:00 AM
    "comment": "TEST BUCHUNG",
    "patient": {
        "firstName":         "Ali",
        "lastName":          "Khan",
        "gender":            "male",
        "birthDate":         "1985-10-20",
        "email":             "ali.khan.test@example.com",
        "mobilePhoneNumber": "+41765432109",
        "privatePhoneNumber": "+41441234567",
        "address": {
            "street":        "Teststrasse",
            "streetNumber":  "1",
            "zipCode":       "8001",
            "city":          "Zürich",
            "state":         "ZH",
            "country":       "CH",
        },
    },
}

# ── HELPERS ───────────────────────────────────────────────────────────────────
def get_token():
    r = requests.post(
        f"{BASE_URL}/api/v1/authenticate",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Content-Type": "application/json"},
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

# ── STEP 1: CREATE EVENT ──────────────────────────────────────────────────────
def create_event(token):
    print("\n" + "═"*60)
    print("  📝 STEP 1 — Creating Test Appointment")
    print("═"*60)
    print(f"\n  Doctor   : Dr. Amjad Mallisho")
    print(f"  Calendar : {CALENDAR_ID}")
    print(f"  DateTime : {TEST_EVENT['startDateTime']}")
    print(f"  Patient  : {TEST_EVENT['patient']['firstName']} {TEST_EVENT['patient']['lastName']}")
    print(f"  Email    : {TEST_EVENT['patient']['email']}")
    print(f"  Phone    : {TEST_EVENT['patient']['mobilePhoneNumber']}")
    print(f"\n  Sending request...")

    r = requests.post(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events",
        headers=auth_headers(token),
        json=TEST_EVENT,
        timeout=20
    )

    print(f"\n  Status Code : {r.status_code}")

    try:
        body = r.json()
        print(f"  Response    :\n{pretty(body)}")
    except:
        print(f"  Response    : {r.text}")
        body = {}

    if r.status_code in (200, 201):
        print("\n  ✅ BOOKING SUCCESS!")
        onedoc_key = (body.get("onedoc_key") or
                      body.get("onedocKey") or
                      body.get("key") or
                      body.get("id"))
        if onedoc_key:
            print(f"  🔑 onedoc_key : {onedoc_key}")
        return True, onedoc_key, body
    else:
        print(f"\n  ❌ BOOKING FAILED — Status {r.status_code}")
        return False, None, body

# ── STEP 2: VERIFY — GET EVENTS FOR THAT DAY ─────────────────────────────────
def verify_booking(token):
    print("\n" + "═"*60)
    print("  🔍 STEP 2 — Verifying: Fetching events for 2026-03-19")
    print("═"*60)

    r = requests.get(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events",
        headers=auth_headers(token),
        params={
            "fromDateTime":  "2026-03-19T00:00:00",
            "untilDateTime": "2026-03-19T23:59:59",
        },
        timeout=20
    )

    print(f"\n  Status Code : {r.status_code}")

    if r.status_code != 200:
        print(f"  ❌ Could not fetch events: {r.text}")
        return

    events = r.json()
    print(f"  Total events on 2026-03-19: {len(events)}\n")

    print(f"  {'─'*56}")
    print(f"  {'ID':<10} {'Time':<20} {'Type':<20} {'Summary'}")
    print(f"  {'─'*56}")

    test_found = False
    for e in events:
        start = e.get("startDateTime", "")
        etype = e.get("type", "")
        summary = e.get("summary", "") or e.get("description", "") or ""

        # Highlight our test appointment
        marker = ""
        if "09:00" in start or "Ali" in str(e) or "ali.khan" in str(e):
            marker = "  ◄ TEST APPOINTMENT"
            test_found = True

        print(f"  {str(e.get('id','')):<10} {start[11:16]:<20} {etype:<20} {summary}{marker}")

    print(f"  {'─'*56}")

    if test_found:
        print("\n  ✅ TEST APPOINTMENT FOUND IN CALENDAR!")
    else:
        print("\n  ⚠️  Test appointment NOT found in event list")
        print("  → This is normal if API creates 'appointment request'")
        print("  → Check epaad portal manually at: https://mcv.epaad.ch")

# ── STEP 3: GET SINGLE EVENT BY ONEDOC_KEY ────────────────────────────────────
def get_event_by_key(token, onedoc_key):
    if not onedoc_key:
        return
    print("\n" + "═"*60)
    print(f"  🔎 STEP 3 — Fetching event by onedoc_key: {onedoc_key}")
    print("═"*60)

    r = requests.get(
        f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events/{onedoc_key}",
        headers=auth_headers(token),
        params={"type": "appointment"},
        timeout=20
    )

    print(f"\n  Status Code : {r.status_code}")
    try:
        print(f"  Response    :\n{pretty(r.json())}")
    except:
        print(f"  Response    : {r.text}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   epaad – Book & Verify Test Appointment                ║")
    print("║   Dr. Amjad Mallisho | MedCenter Volta | Calendar 4    ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # Auth
    print("\n🔐 Authenticating...", end=" ")
    try:
        token = get_token()
        print("✅ OK")
    except Exception as e:
        print(f"❌ {e}")
        exit(1)

    # Step 1 — Create
    success, onedoc_key, response_body = create_event(token)

    # Step 2 — Verify via events list
    verify_booking(token)

    # Step 3 — Fetch by key if available
    if onedoc_key:
        get_event_by_key(token, onedoc_key)

    # Final Summary
    print("\n" + "═"*60)
    print("  📋 FINAL SUMMARY")
    print("═"*60)
    print(f"  Patient   : Ali Khan")
    print(f"  Date/Time : Thursday 2026-03-19 at 09:00")
    print(f"  Doctor    : Dr. Amjad Mallisho")
    print(f"  Location  : MedCenter Volta, Basel")
    print(f"  Booking   : {'✅ SUCCESS' if success else '❌ FAILED'}")
    if onedoc_key:
        print(f"  Key       : {onedoc_key}")
    print("═"*60)
    print("\n  ➡️  Portal check: https://mcv.epaad.ch")
    print("  ➡️  Paste this output back to Claude for full analysis!\n")
