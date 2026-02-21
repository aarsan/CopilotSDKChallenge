"""Quick test to debug the versions endpoint 500 error."""
import asyncio
import traceback

async def test():
    try:
        from src.database import init_db, get_service, get_service_versions
        await init_db()
        print("DB initialized")

        svc = await get_service("Microsoft.CognitiveServices/accounts")
        print(f"Service found: {svc is not None}")
        if svc:
            print(f"  active_version: {svc.get('active_version')}")

        versions = await get_service_versions("Microsoft.CognitiveServices/accounts")
        print(f"Versions count: {len(versions)}")
        for v in versions:
            print(f"  v{v.get('version')}: status={v.get('status')}, keys={sorted(v.keys())}")

        # Simulate what the endpoint does
        versions_summary = []
        for v in versions:
            vs = {k: v2 for k, v2 in v.items() if k != "arm_template"}
            vs["template_size_bytes"] = len(v.get("arm_template") or "") if v.get("arm_template") else 0
            versions_summary.append(vs)
        print(f"Summary built OK: {len(versions_summary)} items")

    except Exception:
        traceback.print_exc()

asyncio.run(test())
