"""Check the VNet policy content in the DB."""
import asyncio, json
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.database import init_db, get_backend
    await init_db()
    b = await get_backend()
    rows = await b.execute(
        "SELECT content, status FROM service_artifacts "
        "WHERE service_id = 'Microsoft.Network/virtualNetworks' AND artifact_type = 'policy'"
    )
    if not rows:
        print("No policy found!")
        return
    content = rows[0]["content"]
    print(f"Status: {rows[0]['status']}")
    print(f"Content ({len(content)} chars):")
    try:
        p = json.loads(content)
        print(json.dumps(p, indent=2))
    except Exception:
        print(content[:1000])

asyncio.run(main())
