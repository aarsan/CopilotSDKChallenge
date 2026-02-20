"""Wrapper to run the InfraForge web server with signal protection."""
import signal
import sys
import os

# Ignore SIGINT/SIGBREAK so the server doesn't die from stray signals
if sys.platform == "win32":
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)

# Now start uvicorn
import uvicorn
from src.config import WEB_HOST, WEB_PORT

if __name__ == "__main__":
    print(f"⚒️  InfraForge Web UI starting on http://localhost:{WEB_PORT}")
    uvicorn.run(
        "src.web:app",
        host=WEB_HOST,
        port=WEB_PORT,
        reload=False,
        log_level="info",
        timeout_keep_alive=120,
    )
