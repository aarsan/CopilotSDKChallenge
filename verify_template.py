"""Verify the VNet template in the database."""
import asyncio
import json
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.database import init_db, get_backend
    await init_db()
    b = await get_backend()
    rows = await b.execute(
        "SELECT LEN(content) as sz, notes FROM service_artifacts "
        "WHERE service_id = 'Microsoft.Network/virtualNetworks' AND artifact_type = 'template'"
    )
    for r in rows:
        print(f"Size={r['sz']}, Notes={r['notes']}")
    
    # Also get the content to verify no diagnosticSettings
    rows2 = await b.execute(
        "SELECT content FROM service_artifacts "
        "WHERE service_id = 'Microsoft.Network/virtualNetworks' AND artifact_type = 'template'"
    )
    if rows2:
        t = json.loads(rows2[0]['content'])
        resource_types = [r['type'] for r in t.get('resources', [])]
        print(f"Resource types: {resource_types}")
        has_diag = any('diagnosticSettings' in rt for rt in resource_types)
        print(f"Has diagnosticSettings: {has_diag}")
        print(f"Params: {list(t.get('parameters', {}).keys())}")

asyncio.run(main())
