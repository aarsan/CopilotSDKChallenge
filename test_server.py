"""Minimal server test to isolate the crash."""
import uvicorn
from fastapi import FastAPI

app = FastAPI()

@app.get("/test")
async def test():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8081)
