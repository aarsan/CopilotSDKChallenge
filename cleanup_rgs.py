"""Clean up all infraforge-val-* resource groups from previous validation runs."""
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

async def main():
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.resource import ResourceManagementClient

    cred = DefaultAzureCredential()
    sub = os.environ["AZURE_SUBSCRIPTION_ID"]
    client = ResourceManagementClient(cred, sub)

    # List all infraforge-val RGs
    rgs = [rg for rg in client.resource_groups.list() if rg.name.startswith("infraforge-val")]
    print(f"Found {len(rgs)} infraforge-val resource groups:")
    for rg in rgs:
        print(f"  {rg.name}  (location={rg.location}, state={rg.properties.provisioning_state})")

    if not rgs:
        print("Nothing to clean up!")
        return

    # Delete them all
    for rg in rgs:
        if rg.properties.provisioning_state == "Deleting":
            print(f"  ⏳ {rg.name} already deleting, skipping")
            continue
        print(f"  🗑️  Deleting {rg.name}...")
        try:
            poller = client.resource_groups.begin_delete(rg.name)
            # Don't wait — just initiate deletion
            print(f"  ✅ Delete initiated for {rg.name}")
        except Exception as e:
            print(f"  ❌ Failed to delete {rg.name}: {e}")

    print("\nDone. RG deletions are running in the background.")

asyncio.run(main())
