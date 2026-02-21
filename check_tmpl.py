import asyncio
from src.database import init_db, get_backend

async def check():
    await init_db()
    b = await get_backend()
    rows = await b.execute("SELECT id, name, status, updated_at FROM catalog_templates")
    print("=== PARENT TEMPLATES ===")
    for r in rows:
        print(f"  id={r['id']}  name={r['name']}  status={r['status']}  updated={r['updated_at']}")
    rows2 = await b.execute("SELECT id, template_id, version, status, validation_status FROM template_versions")
    print()
    print("=== TEMPLATE VERSIONS ===")
    for r in rows2:
        print(f"  id={r['id']}  tmpl_id={r['template_id']}  ver={r['version']}  status={r['status']}  val_status={r['validation_status']}")

asyncio.run(check())
