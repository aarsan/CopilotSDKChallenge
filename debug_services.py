"""Direct DB query to check service statuses."""
import os, struct, pyodbc
from azure.identity import AzureCliCredential

server = os.environ.get("AZURE_SQL_SERVER", "infraforgesql.database.windows.net")
database = os.environ.get("AZURE_SQL_DATABASE", "infraforgedb")

cred = AzureCliCredential()
token = cred.get_token("https://database.windows.net/.default")
token_bytes = token.token.encode("utf-16-le")
token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

conn_str = (
    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
    f"SERVER={server};DATABASE={database};"
    "Encrypt=yes;TrustServerCertificate=no;"
)
conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
cursor = conn.cursor()

print("=== SERVICES TABLE COLUMNS ===")
cursor.execute("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='services' ORDER BY ORDINAL_POSITION")
for r in cursor.fetchall():
    print(f"  {r[0]} ({r[1]})")

print("\n=== ALL SERVICES ===")
cursor.execute("SELECT * FROM services ORDER BY name")
cols = [desc[0] for desc in cursor.description]
print(f"Columns: {cols}")
rows = cursor.fetchall()
print(f"Total: {len(rows)}")
for r in rows:
    d = dict(zip(cols, r))
    status = d.get("status", "?")
    marker = "✅" if status == "approved" else "⬚"
    print(f"  {marker} {d}")

print("\n=== SERVICE_VERSIONS TABLE COLUMNS ===")
cursor.execute("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='service_versions' ORDER BY ORDINAL_POSITION")
for r in cursor.fetchall():
    print(f"  {r[0]} ({r[1]})")

print("\n=== VERSIONS FOR APPROVED SERVICES ===")
cursor.execute("""
    SELECT sv.service_id, sv.version, sv.status,
           CASE WHEN sv.arm_template IS NOT NULL AND LEN(CAST(sv.arm_template AS NVARCHAR(MAX))) > 10 THEN 'YES' ELSE 'NO' END as has_arm
    FROM service_versions sv
    INNER JOIN services s ON sv.service_id = s.id
    WHERE s.status = 'approved'
""")
rows = cursor.fetchall()
print(f"Versions for approved: {len(rows)}")
for r in rows:
    print(f"  svc_id={r[0]}, ver={r[1]}, status={r[2]}, has_arm={r[3]}")

conn.close()
print("\nDone.")
