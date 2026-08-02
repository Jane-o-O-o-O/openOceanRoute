"""Run the local desktop/browser application."""

import argparse
import threading
import webbrowser

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="OceanRoute local engineering workspace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--open", action="store_true", help="Open the workspace in the default browser")
    parser.add_argument("--reload", action="store_true", help="Development reload")
    options = parser.parse_args()
    if options.open:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{options.host}:{options.port}")).start()
    uvicorn.run("oceanroute.api:app", host=options.host, port=options.port, reload=options.reload)


if __name__ == "__main__":
    main()
