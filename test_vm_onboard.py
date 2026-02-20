"""Test script: trigger Virtual Machine onboarding and stream results."""
import requests, json, sys

BASE = "http://localhost:8080"
SERVICE_ID = "Microsoft.Compute/virtualMachines"

def main():
    try:
        r = requests.get(f"{BASE}/api/catalog/services", timeout=5)
        r.raise_for_status()
        services = r.json().get("services", [])
        vm = next((s for s in services if s["id"] == SERVICE_ID), None)
        if not vm:
            print(f"ERROR: '{SERVICE_ID}' not found"); return
        print(f"✓ Found: {vm['name']}  status={vm['status']}")
    except Exception as e:
        print(f"ERROR: Cannot reach server: {e}"); return

    print(f"\n{'='*70}\nTRIGGERING ONBOARDING FOR: {SERVICE_ID}\n{'='*70}\n")

    try:
        resp = requests.post(f"{BASE}/api/services/{SERVICE_ID}/onboard",
            headers={"Accept": "application/x-ndjson"}, stream=True, timeout=600)
        if resp.status_code != 200:
            print(f"ERROR: HTTP {resp.status_code}\n{resp.text[:2000]}"); return

        events = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line: continue
            try: evt = json.loads(line)
            except: print(f"  [raw] {line}"); continue

            events.append(evt)
            etype = evt.get("type","?")
            phase = evt.get("phase","")
            detail = evt.get("detail","")
            attempt = evt.get("attempt","")
            progress = evt.get("progress","")

            if etype == "debug":
                print(f"\n{'─'*50}\nDEBUG [{phase}]:\n{detail[:3000]}\n{'─'*50}\n")
                continue

            prefix = {"error":"❌ ERROR","done":"✅ DONE","healing":"🤖 HEAL","healing_done":"🔧 FIXED"}.get(etype, f"   {phase}")
            ps = f" [{int(float(progress)*100)}%]" if progress else ""
            at = f" (attempt {attempt})" if attempt else ""
            print(f"{prefix}{at}{ps}: {detail[:200]}")

            if etype == "error":
                print(f"\n{'─'*50}\nFULL ERROR:\n{json.dumps(evt, indent=2)}\n{'─'*50}\n")

    except requests.exceptions.Timeout:
        print("TIMEOUT: >600s")
    except Exception as e:
        print(f"ERROR: {e}")

    print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
    print(f"Total events: {len(events)}")
    errors = [e for e in events if e.get("type") == "error"]
    heals = [e for e in events if e.get("type") == "healing"]
    done = [e for e in events if e.get("type") == "done"]
    print(f"Errors: {len(errors)}, Healing: {len(heals)}, Success: {'YES' if done else 'NO'}")

    try:
        r2 = requests.get(f"{BASE}/api/services/{SERVICE_ID}/versions", timeout=10)
        if r2.ok:
            versions = r2.json().get("versions", [])
            if versions:
                v = versions[0]
                print(f"\nLatest: v{v['version']} status={v['status']}")
                vr = v.get("validation_result", {})
                if "error" in vr: print(f"Error: {json.dumps(vr['error'])[:500]}")
                if "phase" in vr: print(f"Phase: {vr['phase']}")
    except: pass

if __name__ == "__main__":
    main()
