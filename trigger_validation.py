"""Trigger VNet validation and capture the streaming NDJSON response."""
import requests
import json
import sys

url = "http://localhost:8080/api/services/Microsoft.Network%2FvirtualNetworks/validate-deployment"
print(f"POST {url}")
print("=" * 70)

try:
    resp = requests.post(url, json={}, stream=True, timeout=600)
    resp.raise_for_status()
except Exception as e:
    print(f"Request error: {e}")
    sys.exit(1)

for line in resp.iter_lines(decode_unicode=True):
    if not line or not line.strip():
        continue
    try:
        evt = json.loads(line)
        phase = evt.get("phase", "?")
        detail = evt.get("detail", "")
        attempt = evt.get("attempt", "")
        progress = evt.get("progress", 0)
        etype = evt.get("type", "?")

        prefix = f"[{etype:15s}] [{phase:25s}]"
        if attempt:
            prefix += f" (#{attempt})"
        print(f"{prefix} {detail[:200]}")

        if etype == "error":
            print(f"\n{'='*70}\nFAILED: {detail}\n{'='*70}")
        elif etype == "done":
            print(f"\n{'='*70}\nSUCCESS: {detail}\n{'='*70}")
    except json.JSONDecodeError:
        print(f"  [raw] {line[:200]}")

print("\nDone.")
