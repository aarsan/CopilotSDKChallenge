"""Reset validation_failed services back to validating for retest."""
import asyncio
from dotenv import load_dotenv
load_dotenv()

async def reset():
    from src.database import init_db, get_backend
    await init_db()
    backend = await get_backend()
    rows = await backend.execute_write(
        "UPDATE services SET status = 'validating', review_notes = NULL WHERE status IN ('validation_failed', 'approved', 'validating') AND id = 'Microsoft.Network/virtualNetworks'",
        ()
    )
    print(f"Reset {rows} services -> validating")

asyncio.run(reset())
