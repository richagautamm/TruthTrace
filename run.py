"""Start TruthTrace:  python run.py   then open http://127.0.0.1:8000"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"TruthTrace running at http://127.0.0.1:{port}")
    uvicorn.run("backend.app:app", host="127.0.0.1", port=port)
