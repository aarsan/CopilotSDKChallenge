# InfraForge Web UI — Quick Start
# Run this file to launch the web-based interface

import uvicorn
from src.config import WEB_HOST, WEB_PORT

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
