"""Serve the next frontend build without replacing frozen release assets."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, default=ROOT / "web" / "dist-next")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    frontend = args.frontend.resolve()
    if not (frontend / "index.html").is_file():
        parser.error(f"Missing built frontend: {frontend}")

    import uvicorn
    from fastapi.staticfiles import StaticFiles
    from starlette.routing import Mount
    from oceanroute.api import app

    app.router.routes[:] = [
        route for route in app.router.routes
        if not (isinstance(route, Mount) and route.path == "")
        and not (getattr(route, "path", None) == "/" and getattr(route, "name", None) == "index")
    ]
    app.mount("/", StaticFiles(directory=frontend, html=True), name="development-application")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
