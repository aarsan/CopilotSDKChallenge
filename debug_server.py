"""Debug server starter — runs uvicorn inline to capture all errors."""
import sys
import traceback
import logging

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")

try:
    import uvicorn
    uvicorn.run(
        "src.web:app",
        host="0.0.0.0",
        port=8080,
        log_level="warning",
        timeout_keep_alive=120,
    )
except KeyboardInterrupt:
    print("Server stopped by user")
except Exception:
    traceback.print_exc()
    sys.exit(1)
