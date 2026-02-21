"""Test approved-for-templates endpoint logic directly."""
import asyncio, json, sys

async def test():
    from src.database import init_db, get_all_services, get_active_service_version
    
    print("Initializing DB...")
    await init_db()
    
    print("Fetching all services...")
    services = await get_all_services()
    print(f"Total services: {len(services)}")
    
    approved = [s for s in services if s.get("status") == "approved"]
    print(f"Approved services: {len(approved)}")
    
    for svc in approved:
        sid = svc["id"]
        print(f"\n--- {sid} ({svc.get('name')}) ---")
        print(f"  active_version: {svc.get('active_version')}")
        
        active = await get_active_service_version(sid)
        print(f"  get_active_service_version returned: {type(active)}")
        if active:
            has_arm = bool(active.get("arm_template"))
            print(f"  has arm_template: {has_arm}")
            if has_arm:
                try:
                    tpl = json.loads(active["arm_template"])
                    params = tpl.get("parameters", {})
                    print(f"  parameters: {list(params.keys())}")
                except Exception as e:
                    print(f"  ERROR parsing ARM: {e}")
        else:
            print("  No active version found!")
            # Try has_builtin_skeleton
            try:
                from src.tools.arm_generator import has_builtin_skeleton
                print(f"  has_builtin_skeleton: {has_builtin_skeleton(sid)}")
            except Exception as e:
                print(f"  ERROR importing arm_generator: {e}")

asyncio.run(test())
