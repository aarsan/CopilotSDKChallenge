"""
Test script: trigger Virtual Network onboarding and stream results.
Captures the full NDJSON event stream so we can see exactly what fails.
"""
import requests
import json
import sys
import time

BASE = "http://localhost:8080"
SERVICE_ID = "Microsoft.Network/virtualNetworks"

def main():
    # 1. Check server is up
    try:
        r = requests.get(f"{BASE}/api/catalog/services", timeout=5)
        r.raise_for_status()
        services = r.json().get("services", [])
        vnet = next((s for s in services if s["id"] == SERVICE_ID), None)
        if not vnet:
            print(f"ERROR: Service '{SERVICE_ID}' not found in catalog.")
            print(f"Available: {[s['id'] for s in services[:10]]}")
            return
        print(f"✓ Found service: {vnet['name']}  status={vnet['status']}")
    except Exception as e:
        print(f"ERROR: Cannot reach server: {e}")
        return

    # 2. Trigger onboarding (streaming NDJSON)
    print(f"\n{'='*70}")
    print(f"TRIGGERING ONBOARDING FOR: {SERVICE_ID}")
    print(f"{'='*70}\n")

    try:
        resp = requests.post(
            f"{BASE}/api/services/{SERVICE_ID}/onboard",
            headers={"Accept": "application/x-ndjson"},
            stream=True,
            timeout=300,
        )
        if resp.status_code != 200:
            print(f"ERROR: HTTP {resp.status_code}")
            print(resp.text[:2000])
            return

        events = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                print(f"  [raw] {line}")
                continue

            events.append(evt)
            etype = evt.get("type", "?")
            phase = evt.get("phase", "")
            detail = evt.get("detail", "")
            attempt = evt.get("attempt", "")
            progress = evt.get("progress", "")

            # Color-code output
            prefix = ""
            if etype == "error":
                prefix = "❌ ERROR"
            elif etype == "done":
                prefix = "✅ DONE"
            elif etype == "healing":
                prefix = "🤖 HEAL"
            elif etype == "healing_done":
                prefix = "🔧 FIXED"
            elif etype == "debug":
                # Print debug events in full
                print(f"\n{'─'*50}")
                print(f"DEBUG [{phase}]:")
                print(detail[:3000])
                print(f"{'─'*50}\n")
                continue
            else:
                prefix = f"   {phase}"

            progress_str = f" [{int(float(progress)*100)}%]" if progress else ""
            attempt_str = f" (attempt {attempt})" if attempt else ""
            print(f"{prefix}{attempt_str}{progress_str}: {detail[:200]}")

            # If error, dump the full event for diagnosis
            if etype == "error":
                print(f"\n{'─'*50}")
                print("FULL ERROR EVENT:")
                print(json.dumps(evt, indent=2))
                print(f"{'─'*50}\n")

    except requests.exceptions.Timeout:
        print("TIMEOUT: Onboarding took too long (>300s)")
    except Exception as e:
        print(f"ERROR during streaming: {e}")

    # 3. Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"Total events: {len(events)}")

    errors = [e for e in events if e.get("type") == "error"]
    heals = [e for e in events if e.get("type") == "healing"]
    done = [e for e in events if e.get("type") == "done"]

    print(f"Errors: {len(errors)}")
    print(f"Healing attempts: {len(heals)}")
    print(f"Success: {'YES' if done else 'NO'}")

    if errors:
        print(f"\nLAST ERROR DETAIL:")
        print(json.dumps(errors[-1], indent=2))

    # 4. Check final status
    try:
        r2 = requests.get(f"{BASE}/api/services/{SERVICE_ID}/versions", timeout=10)
        if r2.ok:
            versions = r2.json().get("versions", [])
            if versions:
                latest = versions[0]
                print(f"\nLatest version: v{latest['version']} status={latest['status']}")
                if latest.get("validation_result"):
                    vr = latest["validation_result"]
                    if "error" in vr:
                        print(f"Validation error: {json.dumps(vr['error'], indent=2)[:1000]}")
                    if "phase" in vr:
                        print(f"Failed phase: {vr['phase']}")
    except:
        pass

if __name__ == "__main__":
    main()
