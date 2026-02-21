"""Test the approved-for-templates endpoint logic using raw SQL."""
import os, struct, json, pyodbc
from azure.identity import AzureCliCredential

server = os.environ.get("AZURE_SQL_SERVER", "infraforgesql.database.windows.net")
database = os.environ.get("AZURE_SQL_DATABASE", "infraforgedb")
cred = AzureCliCredential()
token = cred.get_token("https://database.windows.net/.default")
token_bytes = token.token.encode("utf-16-le")
token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server};DATABASE={database};Encrypt=yes;TrustServerCertificate=no;"
conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
cursor = conn.cursor()

# Get approved services
cursor.execute("SELECT id, name, category, status, active_version, risk_tier FROM services WHERE status = 'approved'")
cols = [d[0] for d in cursor.description]
approved = [dict(zip(cols, r)) for r in cursor.fetchall()]
print(f"Approved services: {len(approved)}")

for svc in approved:
    sid = svc["id"]
    av = svc["active_version"]
    print(f"\n--- {sid} (active_version={av}) ---")
    
    if av is None:
        print("  ⚠ No active_version set!")
        continue
    
    # Get the active version's ARM template
    cursor.execute(
        "SELECT version, status, CAST(arm_template AS NVARCHAR(MAX)) as arm_template FROM service_versions WHERE service_id = ? AND version = ?",
        (sid, av)
    )
    ver_row = cursor.fetchone()
    if not ver_row:
        print(f"  ⚠ Version {av} not found in service_versions!")
        continue
    
    ver_cols = [d[0] for d in cursor.description]
    ver = dict(zip(ver_cols, ver_row))
    print(f"  Version {ver['version']}, status={ver['status']}")
    
    arm = ver.get("arm_template")
    if not arm:
        print("  ⚠ No ARM template!")
        continue
    
    try:
        tpl = json.loads(arm)
        params = tpl.get("parameters", {})
        print(f"  ✅ ARM template parsed, {len(params)} parameters: {list(params.keys())}")
    except Exception as e:
        print(f"  ❌ Failed to parse ARM: {e}")

# Now simulate what the endpoint does
print("\n\n=== SIMULATED ENDPOINT RESULT ===")
STANDARD_PARAMS = {"resourceName", "location", "environment", "projectName", "ownerEmail", "costCenter"}
result = []
for svc in approved:
    sid = svc["id"]
    av = svc["active_version"]
    if av is None:
        continue
    
    cursor.execute(
        "SELECT CAST(arm_template AS NVARCHAR(MAX)) as arm_template FROM service_versions WHERE service_id = ? AND version = ?",
        (sid, av)
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        continue
    
    try:
        tpl = json.loads(row[0])
        all_params = tpl.get("parameters", {})
    except:
        continue
    
    extra_params = []
    for pname, pdef in all_params.items():
        meta = pdef.get("metadata", {})
        extra_params.append({
            "name": pname,
            "type": pdef.get("type", "string"),
            "is_standard": pname in STANDARD_PARAMS,
        })
    
    result.append({
        "id": sid,
        "name": svc["name"],
        "category": svc["category"],
        "active_version": av,
        "param_count": len(extra_params),
    })

print(json.dumps({"services": result, "total": len(result)}, indent=2))
conn.close()
