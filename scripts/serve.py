"""Lance le serveur web : python scripts/serve.py [--port 8000] [--no-reload]"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Serveur web Skills Hub")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-reload", action="store_true")
    args = p.parse_args()
    uvicorn.run(
        "skillshub.web.app:app", host=args.host, port=args.port,
        reload=not args.no_reload, reload_dirs=[str(ROOT / "skillshub")], app_dir=str(ROOT),
    )
