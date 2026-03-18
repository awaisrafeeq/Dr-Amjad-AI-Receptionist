"""
epaad / onedoc – Free Slots Calculator (All Doctors - Fixed)
=============================================================
- Fetches activated calendars from OneDoc API
- Also scans additional calendar IDs (5, 6, etc.) that exist
  but are not yet activated in OneDoc
- Shows correct doctor name for each calendar

Run:
    pip install requests
    python epaad_free_slots.py
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
from datetime import datetime, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL         = "https://mcv.epaad.ch"
USERNAME         = "onedoc"
PASSWORD         = "XgyUqCWJQ4Xg9pB4"

WORK_START_HOUR  = 7
WORK_START_MIN   = 30
WORK_END_HOUR    = 17
WORK_END_MIN     = 30

MIN_SLOT_MINUTES = 15
DAYS_AHEAD       = 14

# Extra calendar IDs to scan (not in OneDoc yet but data exists)
EXTRA_CALENDAR_IDS = [5, 6]

# ── AUTH ──────────────────────────────────────────────────────────────────────
def get_token():
    r = requests.post(
        f"{BASE_URL}/api/v1/authenticate",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Content-Type": "application/json"},
        timeout=20
    )
    r.raise_for_status()
    return r.json()["auth_token"]

# ── FETCH ALL ACTIVATED CALENDARS ─────────────────────────────────────────────
def get_all_calendars(token):
    r = requests.get(
        f"{BASE_URL}/api/v1/onedoc/calendars",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=20
    )
    r.raise_for_status()
    return r.json()

# ── FETCH SINGLE EVENT TO GET DOCTOR INFO ────────────────────────────────────
def get_first_event_info(token, calendar_id, from_dt, until_dt):
    """Try to get at least 1 event to infer calendar info."""
    r = requests.get(
        f"{BASE_URL}/api/v1/onedoc/calendars/{calendar_id}/events",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={
            "fromDateTime":  from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "untilDateTime": until_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        timeout=20
    )
    if r.status_code == 200:
        return r.json()
    return None

# ── FREE SLOT CALCULATOR ──────────────────────────────────────────────────────
def get_free_slots_for_day(events, target_date):
    work_start = datetime.combine(target_date, datetime.min.time()).replace(
        hour=WORK_START_HOUR, minute=WORK_START_MIN, second=0
    )
    work_end = datetime.combine(target_date, datetime.min.time()).replace(
        hour=WORK_END_HOUR, minute=WORK_END_MIN, second=0
    )

    day_events = []
    for e in events:
        start = datetime.fromisoformat(e["startDateTime"])
        end   = datetime.fromisoformat(e["endDateTime"])
        if start.date() == target_date:
            day_events.append((start, end))

    day_events.sort(key=lambda x: x[0])

    free_slots = []
    cursor = work_start

    for start, end in day_events:
        if start > cursor:
            gap = int((start - cursor).total_seconds() / 60)
            if gap >= MIN_SLOT_MINUTES:
                free_slots.append((cursor, start, gap))
        if end > cursor:
            cursor = end

    if cursor < work_end:
        gap = int((work_end - cursor).total_seconds() / 60)
        if gap >= MIN_SLOT_MINUTES:
            free_slots.append((cursor, work_end, gap))

    return free_slots

# ── PRINT CALENDAR SECTION ────────────────────────────────────────────────────
def print_calendar_slots(token, cal_id, doc_name, location, status_tag, today, end_date):
    print("\n╔" + "═" * 60 + "╗")
    print(f"║  👨‍⚕️  {doc_name}")
    print(f"║  📍 {location}")
    print(f"║  🆔 Calendar ID: {cal_id}   {status_tag}")
    print("╚" + "═" * 60 + "╝")

    events = get_first_event_info(token, cal_id, today, end_date)

    if events is None:
        print("  ❌ Could not fetch events (no access or invalid ID)\n")
        return

    print(f"  → {len(events)} events fetched\n")

    total_free = 0
    for i in range(DAYS_AHEAD):
        check_date = (today + timedelta(days=i)).date()
        if check_date.weekday() in (5, 6):
            continue

        free_slots = get_free_slots_for_day(events, check_date)
        day_name   = check_date.strftime("%A")

        if free_slots:
            print(f"  📅 {check_date.strftime('%Y-%m-%d')} ({day_name})")
            for s, e, mins in free_slots:
                print(f"     🟢 {s.strftime('%H:%M')} – {e.strftime('%H:%M')}   ({mins} min)")
                total_free += mins
        else:
            print(f"  📅 {check_date.strftime('%Y-%m-%d')} ({day_name})   🔴 Fully booked")

    print(f"\n  ✅ Total free: {total_free} min ({total_free//60}h {total_free%60}m)")

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║   epaad – Free Slots Calculator (All Doctors - Fixed)      ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    # Authenticate
    print("\n🔐 Authenticating...", end=" ")
    try:
        token = get_token()
        print("✅ OK")
    except Exception as e:
        print(f"❌ {e}")
        exit(1)

    # Get activated calendars
    print("📋 Fetching OneDoc activated calendars...", end=" ")
    try:
        calendars = get_all_calendars(token)
        print(f"✅ {len(calendars)} found")
    except Exception as e:
        print(f"❌ {e}")
        exit(1)

    today    = datetime.now()
    end_date = today + timedelta(days=DAYS_AHEAD)

    print(f"\n  📅 Range : {today.strftime('%Y-%m-%d')} → {end_date.strftime('%Y-%m-%d')}")
    print(f"  ⏱  Min slot: {MIN_SLOT_MINUTES} min | Weekends skipped")

    # Build a dict of known calendar IDs → doctor info
    known_ids = {}
    for cal in calendars:
        doc  = cal.get("professional", {})
        name = f"Dr. {doc.get('firstName', '')} {doc.get('lastName', '')}".strip()
        loc  = cal.get("location", {}).get("name", "Unknown")
        known_ids[cal["id"]] = {"name": name, "location": loc}

    # ── SECTION 1: OneDoc Activated Calendars ────────────────────────────────
    print(f"\n\n{'━'*62}")
    print(f"  ✅ ONEDOC ACTIVATED CALENDARS ({len(calendars)} total)")
    print(f"{'━'*62}")

    for cal in calendars:
        info = known_ids[cal["id"]]
        print_calendar_slots(
            token, cal["id"],
            info["name"], info["location"],
            "✅ OneDoc Active",
            today, end_date
        )

    # ── SECTION 2: Extra Calendar IDs (not in OneDoc yet) ────────────────────
    extra_to_check = [i for i in EXTRA_CALENDAR_IDS if i not in known_ids]

    if extra_to_check:
        print(f"\n\n{'━'*62}")
        print(f"  ⚠️  OTHER CALENDARS (not activated in OneDoc)")
        print(f"{'━'*62}")

        for cal_id in extra_to_check:
            print_calendar_slots(
                token, cal_id,
                f"Unknown Doctor (Calendar {cal_id})",
                "Location unknown — not in OneDoc",
                "⚠️  Not activated",
                today, end_date
            )

    print(f"\n\n{'═'*62}")
    print("  ✔  Done! Ask your client to activate more calendars in OneDoc")
    print(f"{'═'*62}\n")