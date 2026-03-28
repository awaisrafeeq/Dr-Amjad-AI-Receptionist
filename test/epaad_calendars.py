"""
epaad – View All Calendars & Doctors
"""
import requests, json

BASE_URL = "https://mcv.epaad.ch"
USERNAME = "onedoc"
PASSWORD = "XgyUqCWJQ4Xg9pB4"

def get_token():
    r = requests.post(f"{BASE_URL}/api/v1/authenticate",
        data={"username": USERNAME, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
    r.raise_for_status()
    return r.json()["auth_token"]

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   epaad – All Calendars & Doctors                       ║")
    print("╚══════════════════════════════════════════════════════════╝")

    print("\n🔐 Authenticating...", end=" ")
    token = get_token()
    print("✅ OK")

    r = requests.get(f"{BASE_URL}/api/v1/onedoc/calendars",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=20)

    calendars = r.json()
    print(f"\n📋 Total Calendars Found: {len(calendars)}\n")
    print(f"{'═'*60}")

    for cal in calendars:
        doc  = cal.get("professional", {})
        addr = cal.get("location", {}).get("address", {})
        print(f"\n  🆔 Calendar ID   : {cal.get('id')}")
        print(f"  👨‍⚕️  Doctor Name   : Dr. {doc.get('firstName','')} {doc.get('lastName','')}")
        print(f"  🔬 GLN Number    : {doc.get('gln', 'N/A')}")
        print(f"  🏥 Location      : {cal.get('location', {}).get('name', 'N/A')}")
        print(f"  📍 Address       : {addr.get('street','')} {addr.get('streetNumber','')}, {addr.get('zipCode','')} {addr.get('city','')}, {addr.get('country','')}")
        print(f"  📅 Activated At  : {cal.get('activatedAt', 'N/A')}")
        print(f"  {'─'*56}")

    print(f"\n  💡 Note: Only OneDoc-activated calendars appear above.")
    print(f"     Raw JSON:\n")
    print(json.dumps(calendars, indent=2, ensure_ascii=False))
    print(f"\n{'═'*60}\n")
