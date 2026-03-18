import sys
sys.stdout.reconfigure(encoding='utf-8')
import asyncio
import json
from utils.epaad_client import epaad_client

async def verify():
    print("🔍 Fetching events for 2026-03-20 to verify booking...")
    calendar_id = 4
    from_dt = "2026-03-20T00:00:00"
    until_dt = "2026-03-20T23:59:59"
    
    try:
        events = await epaad_client.get_events(calendar_id, from_dt, until_dt)
        print(f"✅ Successfully fetched {len(events)} events.")
        
        target_time = "16:45:00"
        found = False
        
        print("\n--- Events on 2026-03-20 ---")
        for ev in events:
            start = ev.get("startDateTime", "")
            summary = ev.get("summary", "N/A")
            ev_id = ev.get("id", "N/A")
            
            marker = ""
            if target_time in start:
                marker = "  ◄🎯 MATCH FOUND!"
                found = True
            
            print(f"[{start[11:16]}] ID: {ev_id} | {summary}{marker}")
            
        if found:
            print("\n✅ VERIFICATION SUCCESS: The appointment for 16:45 is present on the EPaaD platform!")
        else:
            print("\n❌ VERIFICATION FAILED: The appointment for 16:45 was NOT found on the EPaaD platform.")
            
    except Exception as e:
        print(f"❌ Error during verification: {e}")

if __name__ == "__main__":
    asyncio.run(verify())
