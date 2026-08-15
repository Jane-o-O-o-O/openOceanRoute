"""Build a wheel containing the interface and a portable source/release archive."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(skip_frontend: bool = False, archive_only: bool = False) -> Path:
    if not skip_frontend and not archive_only:
        npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
        if not npm:
            raise RuntimeError("需要 Node.js 编译发布界面")
        subprocess.run([npm, "run", "build"], cwd=ROOT / "web", check=True)
    dist = ROOT / "web/dist"
    if not (dist / "index.html").exists():
        raise RuntimeError("缺少已编译界面")
    packaged_static = ROOT / "oceanroute/static"
    if not archive_only:
        if packaged_static.exists():
            shutil.rmtree(packaged_static)
        shutil.copytree(dist, packaged_static)
        shutil.copyfile(ROOT / "docs/USER_MANUAL.md", ROOT / "oceanroute/manual.md")
    elif (ROOT / "oceanroute/manual.md").read_bytes() != (ROOT / "docs/USER_MANUAL.md").read_bytes():
        raise RuntimeError("Archive-only packaging requires the verified manual to match current documentation")
    output = ROOT / "outputs/releases"
    output.mkdir(parents=True, exist_ok=True)
    # Setuptools keeps files removed from package_data in an old build/lib.
    # Remove only this package's generated cache before compiling a new wheel.
    cached_package = ROOT / "build/lib/oceanroute"
    if not archive_only:
        if cached_package.exists():
            shutil.rmtree(cached_package)
        subprocess.run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(output)], cwd=ROOT, check=True)
    version = re.search(r'^version = "(\d+\.\d+\.\d+)"$', (ROOT/"pyproject.toml").read_text(), re.M).group(1)
    wheel = output / f"oceanroute-{version}-py3-none-any.whl"
    expected = {str(p.relative_to(ROOT)): p for p in (ROOT / "oceanroute").rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    with zipfile.ZipFile(wheel) as package:
        members = {name for name in package.namelist() if name.startswith("oceanroute/")}
        if members != set(expected):
            raise RuntimeError(f"Wheel package mismatch: extra={sorted(members-set(expected))}; missing={sorted(set(expected)-members)}")
        if any(package.read(name) != path.read_bytes() for name, path in expected.items()):
            raise RuntimeError("Wheel package bytes differ from current source/static/manual")
    label = version.removesuffix(".0")
    archive = output / f"OceanRoute-{label}-portable.zip"
    roots = ["oceanroute", "docs", "examples", "tests", "scripts", "web/src", "web/public", "web/tests", "web/artifacts", "resources/validation"]
    files = ["README.md", "pyproject.toml", "launcher.py", "web/package.json", "web/package-lock.json", "web/index.html", "web/tsconfig.json", "web/vite.config.ts", "web/playwright.config.ts"]
    files += ["output/pdf/OceanRoute_用户手册.pdf", "output/pdf/OceanRoute_设计文档.pdf",
              "resources/research/manual_findings.md", "resources/research/website_findings.md",
              "resources/research/web_sources.json"]
    files += ["resources/build_product_documents.py", "resources/validation/voyage_1800s.json"]
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
    parser.add_argument("--archive-only", action="store_true", help="refresh documentation/reports in ZIP while preserving the verified wheel")
    options = parser.parse_args()
    build(options.skip_frontend, options.archive_only)
