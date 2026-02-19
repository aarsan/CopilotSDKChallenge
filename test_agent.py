"""
InfraForge — Non-interactive test script.
Tests the agent with a single prompt and prints the response.
"""

import asyncio
import sys

from copilot import CopilotClient

# Add project root to path
sys.path.insert(0, ".")

from src.config import COPILOT_MODEL, COPILOT_LOG_LEVEL, SYSTEM_MESSAGE
from src.tools import get_all_tools


async def test_agent(prompt: str):
    """Run a single prompt through the InfraForge agent."""
    print(f"\n{'='*60}")
    print(f"PROMPT: {prompt}")
    print(f"{'='*60}\n")

    client = CopilotClient({"log_level": COPILOT_LOG_LEVEL})
    await client.start()

    tools = get_all_tools()

    session = await client.create_session(
        {
            "model": COPILOT_MODEL,
            "streaming": True,
            "tools": tools,
            "system_message": {"content": SYSTEM_MESSAGE},
        }
    )

    done = asyncio.Event()
    full_response = []

    def on_event(event):
        if event.type.value == "assistant.message_delta":
            delta = event.data.delta_content or ""
            full_response.append(delta)
            print(delta, end="", flush=True)
        elif event.type.value == "session.idle":
            done.set()

    session.on(on_event)

    await session.send({"prompt": prompt})
    await done.wait()

    print(f"\n\n{'='*60}")
    print(f"Response length: {len(''.join(full_response))} chars")
    print(f"{'='*60}")

    await session.destroy()
    await client.stop()


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else (
        "Estimate the monthly cost for 2 App Services S1, "
        "a SQL Database S1, Redis C1, and Key Vault in production"
    )
    asyncio.run(test_agent(prompt))
