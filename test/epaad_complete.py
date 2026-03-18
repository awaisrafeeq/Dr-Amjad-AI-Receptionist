"""
epaad / onedoc API – Complete Test Suite
==========================================
Based on official API spec (26.2.2026)

Endpoints covered:
  POST   api/v1/authenticate                              → Get Bearer Token
  GET    api/v1/onedoc/calendars                          → List Calendars
  GET    api/v1/onedoc/calendars/{id}/events              → List Events
  GET    api/v1/onedoc/calendars/{id}/events/{event_id}   → Get Single Event
  POST   api/v1/onedoc/calendars/{id}/events              → Create Event
  DELETE api/v1/onedoc/calendars/{id}/events/{onedoc_key} → Delete Event
  GET    api/v1/onedoc/events/changes                     → Get Changes
  GET    api/v1/onedoc/events/last-change-id              → Get Latest Change ID

Run:
    pip install requests
    python epaad_complete.py
"""

import requests
import json
from datetime import datetime, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://mcv.epaad.ch"
USERNAME    = "onedoc"
PASSWORD    = "XgyUqCWJQ4Xg9pB4"
CALENDAR_ID = 4

# ── GLOBALS ───────────────────────────────────────────────────────────────────
TOKEN = None

def headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

def pretty(data):
    if isinstance(data, (dict, list)):
        return json.dumps(data, indent=2, ensure_ascii=False)
    return str(data)[:1000]

def print_section(title):
    print(f"\n{'═'*60}")
    print(f"  {title}")
    print(f"{'═'*60}")

def print_result(label, r):
    sc = r.status_code
    icon = "✅" if sc in (200, 201, 204) else "❌"
    print(f"\n{icon}  {label}")
    print(f"   Status : {sc}")
    try:
        body = r.json()
        print(f"   Body   :\n{pretty(body)}")
    except:
        if r.text:
            print(f"   Body   : {r.text[:500]}")
        else:
            print(f"   Body   : (empty)")
    return sc

# ══════════════════════════════════════════════════════════════════════════════
# 1. AUTHENTICATE — Get Bearer Token
# ══════════════════════════════════════════════════════════════════════════════
def authenticate():
    global TOKEN
    print_section("STEP 1 — Authenticate (Get Bearer Token)")

    url = f"{BASE_URL}/api/v1/authenticate"
    print(f"   POST {url}")
    print(f"   Params: username={USERNAME}, password=***")

    r = requests.post(
        url,
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20
    )

    sc = print_result("Authenticate", r)

    if sc in (200, 201):
        body = r.json()
        # Try common token field names
        for key in ["auth_token", "token", "access_token", "accessToken", "jwt", "Authorization", "bearer"]:
            if key in body:
                TOKEN = body[key]
                print(f"\n   🎯 Token found (key='{key}'): {TOKEN[:40]}...")
                return True
        # If token is the whole response (some APIs return just the token string)
        if isinstance(body, str):
            TOKEN = body
            print(f"\n   🎯 Token (raw string): {TOKEN[:40]}...")
            return True
        print(f"\n   ⚠️  200 received but token key not found. Full body above ↑")
        print(f"   → Manually copy the token value and set TOKEN variable below")
        return False
    else:
        print(f"\n   ❌ Authentication failed!")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# 2. GET CALENDARS
# ══════════════════════════════════════════════════════════════════════════════
def get_calendars():
    print_section("STEP 2 — Get Calendars")
    url = f"{BASE_URL}/api/v1/onedoc/calendars"
    print(f"   GET {url}")
    r = requests.get(url, headers=headers(), timeout=20)
    print_result("GetCalendars", r)
    return r

# ══════════════════════════════════════════════════════════════════════════════
# 3. GET EVENTS (with fromDateTime / untilDateTime)
# ══════════════════════════════════════════════════════════════════════════════
def get_events():
    print_section("STEP 3 — Get Events (next 30 days)")

    now   = datetime.now()
    frm   = now.strftime("%Y-%m-%dT00:00:00")
    until = (now + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59")

    url    = f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events"
    params = {"fromDateTime": frm, "untilDateTime": until}
    print(f"   GET {url}")
    print(f"   Params: {params}")

    r = requests.get(url, headers=headers(), params=params, timeout=20)
    print_result("GetEvents", r)
    return r

# ══════════════════════════════════════════════════════════════════════════════
# 4. GET SINGLE EVENT
# ══════════════════════════════════════════════════════════════════════════════
def get_single_event(event_id, event_type="appointment"):
    print_section(f"STEP 4 — Get Single Event (id={event_id}, type={event_type})")

    url    = f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events/{event_id}"
    params = {"type": event_type}
    print(f"   GET {url}")
    print(f"   Params: {params}")

    r = requests.get(url, headers=headers(), params=params, timeout=20)
    print_result("GetEvent", r)
    return r

# ══════════════════════════════════════════════════════════════════════════════
# 5. CREATE EVENT (Test Appointment)
# ══════════════════════════════════════════════════════════════════════════════
def create_test_event():
    print_section("STEP 5 — Create Test Event (Appointment Request)")

    tomorrow = (datetime.now() + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%dT%H:%M:%S")

    payload = {
        "event": {
            "startDateTime": tomorrow,
            "comment": "TEST – bitte löschen / please delete",
            "patient": {
                "firstName":         "Test",
                "lastName":          "Patient",
                "gender":            "male",
                "birthDate":         "1990-01-01",
                "email":             "test.patient@example.com",
                "mobilePhoneNumber": "+41791234567",
                "address": {
                    "street":       "Teststrasse",
                    "streetNumber": "1",
                    "zipCode":      "8001",
                    "city":         "Zürich",
                    "state":        "ZH",
                    "country":      "CH",
                },
            },
        }
    }

    url = f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events"
    print(f"   POST {url}")
    print(f"   Payload:\n{pretty(payload)}")

    r = requests.post(url, headers=headers(), json=payload, timeout=20)
    print_result("CreateEvent", r)

    # Extract onedoc_key for potential delete test
    try:
        body = r.json()
        onedoc_key = body.get("onedoc_key") or body.get("onedocKey") or body.get("key")
        if onedoc_key:
            print(f"\n   🔑 onedoc_key: {onedoc_key}")
            return onedoc_key
    except:
        pass
    return None

# ══════════════════════════════════════════════════════════════════════════════
# 6. GET EVENT CHANGES
# ══════════════════════════════════════════════════════════════════════════════
# def get_event_changes(after_change_id=None):
#     print_section("STEP 6 — Get Event Changes")

#     url    = f"{BASE_URL}/api/v1/onedoc/events/changes"
#     params = {}
#     if after_change_id:
#         params["afterChangeId"] = after_change_id
#     print(f"   GET {url}")
#     print(f"   Params: {params}")

#     r = requests.get(url, headers=headers(), params=params, timeout=20)
#     print_result("GetEventChanges", r)
#     return r

# # ══════════════════════════════════════════════════════════════════════════════
# # 7. GET LATEST CHANGE ID
# # ══════════════════════════════════════════════════════════════════════════════
# def get_last_change_id():
#     print_section("STEP 7 — Get Latest Event Change ID")

#     url = f"{BASE_URL}/api/v1/onedoc/events/last-change-id"
#     print(f"   GET {url}")

#     r = requests.get(url, headers=headers(), timeout=20)
#     print_result("GetLatestEventChangeId", r)
#     return r

# ══════════════════════════════════════════════════════════════════════════════
# 8. DELETE EVENT (using onedoc_key)
# ══════════════════════════════════════════════════════════════════════════════
# def delete_event(onedoc_key):
#     print_section(f"STEP 8 — Delete Event (onedoc_key={onedoc_key})")

#     url = f"{BASE_URL}/api/v1/onedoc/calendars/{CALENDAR_ID}/events/{onedoc_key}"
#     print(f"   DELETE {url}")

#     r = requests.delete(url, headers=headers(), timeout=20)
#     sc = r.status_code
#     icon = "✅" if sc == 204 else "❌"
#     print(f"\n{icon}  DeleteEvent → Status: {sc}")
#     if r.text:
#         print(f"   Body: {r.text[:300]}")
#     return r


# ══════════════════════════════════════════════════════════════════════════════
# MAIN — Run all tests
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   epaad / onedoc – Complete API Test Suite              ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Base URL   : {BASE_URL}")
    print(f"  Calendar ID: {CALENDAR_ID}")
    print(f"  Username   : {USERNAME}")

    # Step 1 — Must authenticate first
    auth_ok = authenticate()

    if not auth_ok:
        print("\n\n❌ Cannot continue without a valid token.")
        print("   → Check username/password")
        print("   → Or manually set TOKEN variable in this script")
        exit(1)

    # Step 2 — Calendars
    get_calendars()

    # Step 3 — Events list
    get_events()

    # Step 4 — Single event (use a known event_id if you have one)
    # get_single_event(event_id=123, event_type="appointment")

    # Step 5 — Create test event → get onedoc_key
    onedoc_key = create_test_event()

    # Step 6 — Event changes
    # get_event_changes()

    # Step 7 — Last change ID
    # get_last_change_id()

    # Step 8 — Delete the test event (only if key was returned)
    # if onedoc_key:
    #     # delete_event(onedoc_key)
    # else:
    #     print("\n⚠️  Skipping delete — no onedoc_key returned from create.")

    print(f"\n{'═'*60}")
    print("  ✔  All tests done! Paste output back to Claude.")
    print(f"{'═'*60}\n")
