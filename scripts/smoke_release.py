"""Verify a built wheel outside the checkout, including its packaged web UI."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def smoke(wheel: Path) -> None:
    wheel = wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="oceanroute-wheel-") as directory:
        destination = Path(directory)
        with zipfile.ZipFile(wheel) as package:
            package.extractall(destination)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(destination)
        environment["OCEANROUTE_DATA_DIR"] = str(destination / "data")
        code = r'''
import json, pathlib, re
import oceanroute
from fastapi.testclient import TestClient
from oceanroute.api import create_app
from oceanroute.storage import ProjectStore
root = pathlib.Path.cwd()
assert pathlib.Path(oceanroute.__file__).is_relative_to(root), oceanroute.__file__
client = TestClient(create_app(ProjectStore(root / "test.sqlite3")))
page = client.get("/")
assert page.status_code == 200 and 'id="root"' in page.text
assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text)
assert assets, page.text
for path in assets:
    assert client.get(path).status_code == 200, path
manual = client.get("/api/manual")
assert manual.status_code == 200 and "用户手册" in manual.json()["text"]
project = client.get("/api/sample").json()
analysis = client.post("/api/analyze", json=project)
assert analysis.status_code == 200, analysis.text
assert analysis.json()["summary"]["surface_length_m"] > 0
saved = client.post("/api/projects", json=project)
assert saved.status_code == 200, saved.text
conflict = client.post("/api/projects", json=project)
assert conflict.status_code in (409, 422), conflict.text
json.dumps(analysis.json(), allow_nan=False)
print(json.dumps({"wheel_module":str(oceanroute.__file__), "assets":len(assets),
                 "manual":True, "analysis":True, "revision_guard":True}))
'''
        subprocess.run([sys.executable, "-c", code], cwd=destination, env=environment, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    smoke(parser.parse_args().wheel)
