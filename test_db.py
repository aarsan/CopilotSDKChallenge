"""Quick smoke test for the database layer."""
import asyncio
from src.database import (
    init_db, save_session, get_session, delete_session,
    save_chat_message, get_chat_history,
    log_usage, get_usage_stats,
    save_approval_request, get_approval_requests,
    create_project, get_project, list_projects,
)


async def main():
    # 1. Init
    await init_db()
    print("✅ DB initialized")

    # 2. Sessions
    await save_session("test-token", {
        "user_id": "u1",
        "display_name": "Test User",
        "email": "test@contoso.com",
        "department": "Engineering",
        "cost_center": "CC-1234",
    })
    print("✅ Session saved")

    s = await get_session("test-token")
    assert s is not None
    assert s["display_name"] == "Test User"
    print(f"✅ Session retrieved: {s['display_name']} ({s['email']})")

    # 3. Chat
    await save_chat_message("test-token", "user", "Hello InfraForge")
    await save_chat_message("test-token", "assistant", "Hi! How can I help?")
    history = await get_chat_history("test-token")
    assert len(history) >= 2
    print(f"✅ Chat messages: {len(history)}")

    # 4. Usage
    await log_usage({
        "user": "test@contoso.com",
        "department": "Engineering",
        "cost_center": "CC-1234",
        "prompt": "deploy a 3-tier web app",
        "resource_types": ["App Service", "SQL Database"],
        "estimated_cost": 450.0,
        "from_catalog": True,
    })
    stats = await get_usage_stats()
    assert stats["totalRequests"] >= 1
    print(f"✅ Usage stats: {stats['totalRequests']} requests, reuse rate {stats['catalogReuseRate']}%")

    # 5. Approval requests
    req_id = await save_approval_request({
        "service_name": "Azure OpenAI",
        "business_justification": "Need GPT-4 for code generation",
        "project_name": "InfraForge",
        "requestor": {"name": "Test User", "email": "test@contoso.com"},
    })
    reqs = await get_approval_requests()
    assert len(reqs) >= 1
    print(f"✅ Approval request: {req_id} ({len(reqs)} total)")

    # 6. Projects
    proj_id = await create_project({
        "name": "InfraForge Platform",
        "description": "Self-service infrastructure platform",
        "owner_email": "test@contoso.com",
        "department": "Engineering",
    })
    proj = await get_project(proj_id)
    assert proj is not None
    projects = await list_projects()
    print(f"✅ Project: {proj_id} ({len(projects)} total)")

    # Cleanup
    await delete_session("test-token")
    print(f"✅ Session cleaned up")

    print("\n🎉 ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
