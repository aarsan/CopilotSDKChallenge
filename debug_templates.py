"""Debug: check why approved-for-templates returns empty."""
import asyncio
import json

async def check():
    from src.database import init_db, get_all_services, get_active_service_version

    await init_db()
    
    services = await get_all_services()
    print(f"Total services: {len(services)}")
    
    approved_count = 0
    for svc in services:
        status = svc.get("status")
        active_ver = svc.get("active_version")
        svc_id = svc["id"]
        name = svc.get("name", "?")
        
        if status == "approved":
            approved_count += 1
            active = await get_active_service_version(svc_id)
            has_template = bool(active and active.get("arm_template"))
            print(f"  ✅ {svc_id} ({name}): active_ver={active_ver}, has_template={has_template}")
        else:
            print(f"  ⬚ {svc_id} ({name}): status={status}, active_ver={active_ver}")

    print(f"\nApproved: {approved_count}/{len(services)}")

asyncio.run(check())
