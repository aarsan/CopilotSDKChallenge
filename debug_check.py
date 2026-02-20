"""Quick debug: check what validation error was saved."""
import asyncio
import os
import json
from dotenv import load_dotenv
load_dotenv()

async def check():
    from src.database import init_db, get_backend
    await init_db()
    backend = await get_backend()

    rows = await backend.execute(
        "SELECT id, name, status, review_notes FROM services WHERE status IN ('validation_failed', 'validating')",
        ()
    )
    if not rows:
        print("No services in validation_failed or validating state.")
    for r in rows:
        print(f"Service: {r['id']}")
        print(f"  Name:   {r['name']}")
        print(f"  Status: {r['status']}")
        notes = r.get('review_notes')
        if notes:
            try:
                parsed = json.loads(notes)
                print(f"  Error:  {json.dumps(parsed, indent=2)}")
            except Exception:
                print(f"  Notes:  {notes}")
        print()

    # Also check what template content was generated
    rows2 = await backend.execute(
        "SELECT service_id, artifact_type, status, content, notes FROM service_artifacts WHERE service_id IN (SELECT id FROM services WHERE status IN ('validation_failed', 'validating'))",
        ()
    )
    for r in rows2:
        print(f"Artifact: {r['service_id']} / {r['artifact_type']} (status={r['status']})")
        print(f"  Notes: {r.get('notes')}")
        content = r.get('content') or ""
        # Show first 500 chars of content
        print(f"  Content (first 500): {content[:500]}")
        print()

asyncio.run(check())
