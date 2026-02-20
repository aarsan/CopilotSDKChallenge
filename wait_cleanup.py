"""Wait for infraforge-val RGs to be cleaned up."""
import time, os
from dotenv import load_dotenv
load_dotenv()
from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient

cred = DefaultAzureCredential()
client = ResourceManagementClient(cred, os.environ["AZURE_SUBSCRIPTION_ID"])

print("Waiting for RG cleanup...")
for i in range(18):
    rgs = [r for r in client.resource_groups.list() if r.name.startswith("infraforge-val")]
    states = [f"{r.name}({r.properties.provisioning_state})" for r in rgs]
    print(f"  [{i*10}s] {len(rgs)} RGs: {states}")
    if not rgs:
        print("All RGs cleaned up!")
        break
    time.sleep(10)
else:
    print(f"Still {len(rgs)} RGs remaining")
