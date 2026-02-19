"""Quick test: initialize the database and verify governance seed data."""
import asyncio
from src.database import (
    init_db,
    get_all_services,
    get_security_standards,
    get_compliance_frameworks,
    get_governance_policies,
    get_governance_policies_as_dict,
)


async def main():
    print("=" * 60)
    print("InfraForge — Governance Database Test")
    print("=" * 60)

    await init_db()
    print("\n✅ Database initialized and seeded.\n")

    # Services
    svcs = await get_all_services()
    print(f"📦 Services: {len(svcs)}")
    for svc in svcs[:3]:
        print(f"   • {svc['name']} ({svc['id']})")
        print(f"     Status: {svc['status']}, Risk: {svc.get('risk_tier', 'N/A')}")
        print(f"     SKUs: {svc.get('approved_skus', [])}")
        print(f"     Regions: {svc.get('approved_regions', [])}")
        print(f"     Policies: {len(svc.get('policies', []))}")

    # Security Standards
    stds = await get_security_standards()
    print(f"\n🔒 Security Standards: {len(stds)}")
    for std in stds[:3]:
        print(f"   • {std['id']}: {std['name']} [{std['severity']}]")
        print(f"     Key: {std['validation_key']} = {std.get('validation_value', '')}")

    # Compliance Frameworks
    fws = await get_compliance_frameworks()
    print(f"\n📋 Compliance Frameworks: {len(fws)}")
    total_controls = 0
    for fw in fws:
        controls = fw.get("controls", [])
        total_controls += len(controls)
        print(f"   • {fw['name']} ({fw['id']}) — {len(controls)} controls")
        for ctrl in controls[:2]:
            std_ids = ctrl.get("security_standard_ids", [])
            print(f"     - {ctrl['control_id']}: {ctrl['name']} → {std_ids}")
    print(f"   Total controls: {total_controls}")

    # Governance Policies
    pols = await get_governance_policies()
    print(f"\n🏛️ Governance Policies: {len(pols)}")
    for pol in pols:
        print(f"   • {pol['id']}: {pol['name']} [{pol['enforcement']}]")
        print(f"     Rule: {pol['rule_key']} = {pol.get('rule_value', '')}")

    # Test the flat dict helper
    pol_dict = await get_governance_policies_as_dict()
    print(f"\n📝 Policy Dict (for checker): {len(pol_dict)} keys")
    for k, v in pol_dict.items():
        print(f"   {k} = {v}")

    # Filter tests
    compute_svcs = await get_all_services(category="compute")
    approved_svcs = await get_all_services(status="approved")
    print(f"\n🔍 Filter tests:")
    print(f"   Compute services: {len(compute_svcs)}")
    print(f"   Approved services: {len(approved_svcs)}")

    # Idempotency: run init again — should not duplicate
    await init_db()
    svcs2 = await get_all_services()
    assert len(svcs2) == len(svcs), f"Idempotency check failed: {len(svcs2)} vs {len(svcs)}"
    print(f"\n✅ Idempotency check passed: {len(svcs2)} services after second init.")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
