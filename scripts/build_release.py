"""Build a wheel containing the interface and a portable source/release archive."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(skip_frontend: bool = False) -> Path:
    if not skip_frontend:
        npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
        if not npm:
            raise RuntimeError("需要 Node.js 编译发布界面")
        subprocess.run([npm, "run", "build"], cwd=ROOT / "web", check=True)
    dist = ROOT / "web/dist"
    if not (dist / "index.html").exists():
        raise RuntimeError("缺少已编译界面")
    packaged_static = ROOT / "oceanroute/static"
    if packaged_static.exists():
        shutil.rmtree(packaged_static)
    shutil.copytree(dist, packaged_static)
    shutil.copyfile(ROOT / "docs/USER_MANUAL.md", ROOT / "oceanroute/manual.md")
    output = ROOT / "outputs/releases"
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(output)], cwd=ROOT, check=True)
    archive = output / "OceanRoute-0.1-portable.zip"
    roots = ["oceanroute", "docs", "examples", "tests", "scripts", "web/src", "web/public", "web/tests", "web/artifacts"]
    files = ["README.md", "pyproject.toml", "launcher.py", "web/package.json", "web/package-lock.json", "web/index.html", "web/tsconfig.json", "web/vite.config.ts", "web/playwright.config.ts"]
    files += ["output/pdf/OceanRoute_用户手册.pdf", "output/pdf/OceanRoute_设计文档.pdf",
              "resources/research/manual_findings.md", "resources/research/website_findings.md",
              "resources/research/web_sources.json"]
    paths = [ROOT / f for f in files]
    for name in roots:
        paths.extend(p for p in (ROOT / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path in sorted(set(paths)):
            package.write(path, "OceanRoute/" + str(path.relative_to(ROOT)))
    print(archive)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-frontend", action="store_true")
    build(parser.parse_args().skip_frontend)
