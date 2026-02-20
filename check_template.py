"""Check what template is currently in the DB and re-fix if healer changed it."""
import asyncio
import json
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.database import init_db, get_backend
    await init_db()
    b = await get_backend()

    rows = await b.execute(
        "SELECT content FROM service_artifacts "
        "WHERE service_id = 'Microsoft.Network/virtualNetworks' AND artifact_type = 'template'"
    )
    if not rows:
        print("No template found!")
        return

    t = json.loads(rows[0]["content"])
    resources = t.get("resources", [])
    print(f"Resources: {len(resources)}")
    for r in resources:
        rtype = r.get("type", "?")
        loc = r.get("location", "?")
        print(f"  {rtype} — location={loc}")

    params = t.get("parameters", {})
    loc_param = params.get("location", {})
    print(f"\nLocation parameter: default={loc_param.get('defaultValue', 'MISSING')}")

    # Check if the healer changed anything
    content = rows[0]["content"]
    if "centralus" in content:
        print("\n⚠️  Template contains 'centralus' — healer corrupted it!")
    else:
        print("\n✅ Template does NOT contain 'centralus'")

    if "diagnosticSettings" in content:
        print("⚠️  Template still has diagnosticSettings!")
    else:
        print("✅ No diagnosticSettings")

asyncio.run(main())
