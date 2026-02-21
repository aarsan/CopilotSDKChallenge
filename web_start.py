# InfraForge Web UI — Quick Start
# Run this file to launch the web-based interface

import os
import sys
import uvicorn
from src.config import WEB_HOST, WEB_PORT

# Ensure UTF-8 output (avoids cp1252 crashes with emoji on Windows)
if sys.platform == "win32" and not os.environ.get("PYTHONIOENCODING"):
    os.environ["PYTHONIOENCODING"] = "utf-8"

if __name__ == "__main__":
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
