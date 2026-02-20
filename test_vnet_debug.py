"""
Debug script: trigger Virtual Network onboarding and capture the FULL
generated policy and resource data for diagnosis.
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
            print(f"ERROR: Service '{SERVICE_ID}' not found.")
            return
        print(f"✓ Found: {vnet['name']}  status={vnet['status']}")
    except Exception as e:
        print(f"ERROR: Cannot reach server: {e}")
        return

    # 2. Trigger onboarding
    print(f"\nTriggering onboarding...\n")

    events = []
    policy_events = []
    resource_events = []

    try:
        resp = requests.post(
            f"{BASE}/api/services/{SERVICE_ID}/onboard",
            headers={"Accept": "application/x-ndjson"},
            stream=True,
            timeout=300,
        )
        if resp.status_code != 200:
            print(f"ERROR: HTTP {resp.status_code}: {resp.text[:500]}")
            return

        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                evt = json.loads(line)
            except:
                continue
            events.append(evt)

            etype = evt.get("type", "")
            phase = evt.get("phase", "")
            detail = evt.get("detail", "")
            attempt = evt.get("attempt", "")

            # Capture policy-related events
            if "policy" in phase.lower() or "policy" in detail.lower():
                policy_events.append(evt)

            # Capture resource events
            if phase in ("resource_check_complete", "resource_check"):
                resource_events.append(evt)

            # Short progress display
            if etype == "error":
                print(f"❌ {detail[:150]}")
            elif etype == "done":
                print(f"✅ {detail[:150]}")
            elif etype == "healing":
                print(f"🤖 {detail[:150]}")
            elif phase in ("policy_testing", "policy_failed", "policy_testing_complete"):
                print(f"🛡️  {detail[:150]}")
            elif "policy_result" in etype or "policy_result" in phase:
                print(f"📋 {detail[:150]}")
            elif phase in ("deploy_complete", "deploy_failed"):
                print(f"{'✓' if 'complete' in phase else '✗'} {detail[:150]}")
            elif phase == "generated":
                print(f"📦 {detail[:150]}")

    except Exception as e:
        print(f"ERROR: {e}")

    # 3. Analysis
    print(f"\n{'='*70}")
    print("DIAGNOSIS")
    print(f"{'='*70}")

    # Find policy_result events with full data
    policy_results = [e for e in events if e.get("type") == "policy_result"]
    if policy_results:
        print(f"\nPolicy Results ({len(policy_results)}):")
        for pr in policy_results:
            print(f"  compliant={pr.get('compliant')} | {pr.get('detail', '')[:120]}")
            if pr.get("resource"):
                print(f"    resource: {json.dumps(pr['resource'], indent=2)[:500]}")

    # Summary event (has full policy + resource info)
    done_events = [e for e in events if e.get("type") == "done"]
    if done_events:
        print(f"\n✅ SUCCESS: {done_events[0].get('detail')}")
        if done_events[0].get("summary"):
            print(f"Summary: {json.dumps(done_events[0]['summary'], indent=2)[:1000]}")

    errors = [e for e in events if e.get("type") == "error"]
    if errors:
        print(f"\n❌ FINAL ERROR: {errors[-1].get('detail')}")

    # Check final versions
    try:
        r2 = requests.get(f"{BASE}/api/services/{SERVICE_ID}/versions", timeout=10)
        if r2.ok:
            versions = r2.json().get("versions", [])
            if versions:
                latest = versions[0]
                print(f"\nLatest version: v{latest['version']} status={latest['status']}")
                vr = latest.get("validation_result", {})
                if vr:
                    print(f"Validation result: {json.dumps(vr, indent=2)[:1000]}")
                # Print the ARM template
                if latest.get("arm_template"):
                    print(f"\n--- ARM Template ({len(latest['arm_template'])} chars) ---")
                    print(latest["arm_template"][:3000])
                    print("--- end ---")
    except:
        pass

if __name__ == "__main__":
    main()
