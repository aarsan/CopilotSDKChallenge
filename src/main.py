"""
InfraForge — AI-Powered IaC & Pipeline Generator
Main entry point with interactive CLI using the GitHub Copilot SDK.
"""

import asyncio
import os
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from copilot import CopilotClient

from src.config import (
    APP_NAME,
    APP_VERSION,
    APP_DESCRIPTION,
    COPILOT_MODEL,
    COPILOT_LOG_LEVEL,
    OUTPUT_DIR,
    SYSTEM_MESSAGE,
)
from src.tools import get_all_tools
from src.utils import ensure_output_dir, save_generated_file

console = Console()


def print_banner():
    """Print the InfraForge welcome banner."""
    banner_text = Text()
    banner_text.append("⚒️  InfraForge", style="bold cyan")
    banner_text.append(f" v{APP_VERSION}\n", style="dim")
    banner_text.append(APP_DESCRIPTION, style="white")

    console.print(
        Panel(
            banner_text,
            title="[bold green]Infrastructure-as-Code Generator[/bold green]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()
    console.print("[dim]Commands:[/dim]")
    console.print("  [cyan]exit[/cyan] / [cyan]quit[/cyan]  — Exit InfraForge")
    console.print("  [cyan]clear[/cyan]         — Clear the screen")
    console.print("  [cyan]save[/cyan]          — Save last generated output to file")
    console.print()
    console.print("[dim]Example prompts:[/dim]")
    console.print(
        '  [italic]"Create a 3-tier web app in Azure with a SQL backend"[/italic]'
    )
    console.print(
        '  [italic]"Generate a GitHub Actions pipeline for a Python app with staging and prod"[/italic]'
    )
    console.print(
        '  [italic]"Estimate the monthly cost for 3 App Services and a SQL Database"[/italic]'
    )
    console.print()


async def main():
    """Main application loop."""
    print_banner()
    ensure_output_dir(OUTPUT_DIR)

    # ── Initialize Copilot SDK ───────────────────────────────
    console.print("[dim]Initializing Copilot SDK...[/dim]")

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

    console.print("[green]✓ Ready![/green]\n")

    last_response = ""

    # ── Interactive Loop ─────────────────────────────────────
    while True:
        try:
            user_input = console.input("[bold cyan]You:[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            break

        if user_input.lower() == "clear":
            console.clear()
            print_banner()
            continue

        if user_input.lower() == "save":
            if last_response:
                filepath = save_generated_file(last_response, OUTPUT_DIR)
                console.print(f"[green]✓ Saved to {filepath}[/green]\n")
            else:
                console.print("[yellow]Nothing to save yet.[/yellow]\n")
            continue

        # ── Stream the response ──────────────────────────────
        console.print()
        response_chunks = []

        done = asyncio.Event()

        def on_event(event):
            nonlocal last_response
            if event.type.value == "assistant.message_delta":
                delta = event.data.delta_content or ""
                response_chunks.append(delta)
                console.print(delta, end="", highlight=False)
            elif event.type.value == "assistant.message":
                last_response = event.data.content or ""
            elif event.type.value == "session.idle":
                done.set()

        unsubscribe = session.on(on_event)

        try:
            await session.send({"prompt": user_input})
            await done.wait()
        finally:
            unsubscribe()

        console.print("\n")

    # ── Cleanup ──────────────────────────────────────────────
    console.print("\n[dim]Shutting down...[/dim]")
    await session.destroy()
    await client.stop()
    console.print("[green]Goodbye! 👋[/green]")


if __name__ == "__main__":
    asyncio.run(main())
