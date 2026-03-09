# InfraForge Web UI — Quick Start
# Run this file to launch the web-based interface

import os
import signal
import sys

import uvicorn
from src.config import WEB_HOST, WEB_PORT


def kill_existing(port: int) -> None:
    """Kill any process currently listening on the given port."""
    if sys.platform == "win32":
        import subprocess
        # netstat to find PIDs bound to the port
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True,
        )
        pids: set[int] = set()
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                try:
                    pid = int(parts[-1])
                    if pid != 0:
                        pids.add(pid)
                except (ValueError, IndexError):
                    continue
        my_pid = os.getpid()
        for pid in pids:
            if pid == my_pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"  Killed existing process on port {port} (PID {pid})")
            except OSError:
                pass
    else:
        import subprocess
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
        )
        my_pid = os.getpid()
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line)
                if pid != my_pid:
                    os.kill(pid, signal.SIGTERM)
                    print(f"  Killed existing process on port {port} (PID {pid})")
            except (ValueError, OSError):
                continue

# Ensure UTF-8 output (avoids cp1252 crashes with emoji on Windows)
if sys.platform == "win32":
    if not os.environ.get("PYTHONIOENCODING"):
        os.environ["PYTHONIOENCODING"] = "utf-8"
    # Reconfigure stdout/stderr for this process (env var only affects subprocesses)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if __name__ == "__main__":
    kill_existing(WEB_PORT)
    print(f"⚒️  InfraForge Web UI starting on http://localhost:{WEB_PORT}")
    print(f"   Open your browser to http://localhost:{WEB_PORT}")
    print()
    uvicorn.run(
        "src.web:app",
        host=WEB_HOST,
        port=WEB_PORT,
        reload=False,
        log_level="info",
    )
