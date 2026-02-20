"""
Reset a service status back to not_approved so we can re-test onboarding.
"""
import asyncio
import os
import sys

# Load .env
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

SERVICE_ID = "Microsoft.Network/virtualNetworks"

async def main():
    from src.database import get_backend, init_db
    await init_db()
    backend = await get_backend()

    # Reset service status
    await backend.execute_write(
        "UPDATE services SET status = 'not_approved', review_notes = NULL, active_version = NULL WHERE id = ?",
        (SERVICE_ID,),
    )
    print(f"✓ Reset {SERVICE_ID} to not_approved")

    # Delete all versions for this service
    rows = await backend.execute(
        "SELECT COUNT(*) as cnt FROM service_versions WHERE service_id = ?",
        (SERVICE_ID,),
    )
    count = dict(rows[0]).get("cnt", 0) if rows else 0
    await backend.execute_write(
        "DELETE FROM service_versions WHERE service_id = ?",
        (SERVICE_ID,),
    )
    print(f"✓ Deleted {count} version(s)")

if __name__ == "__main__":
    asyncio.run(main())
