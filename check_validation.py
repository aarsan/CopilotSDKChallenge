"""Quick check: did VM onboarding actually deploy to Azure?"""
import asyncio
import json
from src.database import init_db, get_service_versions


async def check():
    await init_db()
    versions = await get_service_versions("Microsoft.Compute/virtualMachines")
    for v in versions:
        vr = v.get("validation_result", {})
        print(f"\n=== Version {v['version']} ===")
        print(f"Status: {v['status']}")
        print(f"Validated at: {v.get('validated_at', 'N/A')}")
        print(f"Attempts: {vr.get('attempts', '?')}")

        # What-If
        wif = vr.get("what_if", {})
        print(f"What-If status: {wif.get('status', 'MISSING')}")
        print(f"What-If changes: {wif.get('change_counts', 'NONE')}")

        # Deployed resources
        deployed = vr.get("deployed_resources", [])
        print(f"Deployed resource count: {len(deployed)}")
        for dr in deployed:
            print(f"  - {dr.get('type')}/{dr.get('name')} @ {dr.get('location')}")

        # Policy compliance
        print(f"All policy compliant: {vr.get('all_policy_compliant', 'MISSING')}")
        print(f"Has runtime policy: {vr.get('has_runtime_policy', 'MISSING')}")
        pc = vr.get("policy_compliance", [])
        print(f"Policy compliance results: {len(pc)}")
        for p in pc:
            print(f"  - {p.get('resource_name')}: compliant={p.get('compliant')} reason={p.get('reason')}")


asyncio.run(check())
