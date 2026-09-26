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


def build(skip_frontend: bool = False, archive_only: bool = False, *, frontend_directory: Path | None = None, pdf_suffix: str | None = None) -> Path:
    dist = (frontend_directory or ROOT / "web/dist").resolve()
    version = re.search(r'^version = "(\d+\.\d+\.\d+)"$', (ROOT/"pyproject.toml").read_text(), re.M).group(1)
    label = version.removesuffix(".0")
    # Later releases must not silently bundle the unversioned frozen 0.4 PDFs.
    modern_documents = tuple(map(int, version.split("."))) >= (0, 5, 0)
    if pdf_suffix is None:
        pdf_suffix = label if modern_documents else ""
    if modern_documents and pdf_suffix != label:
        raise ValueError(f"Release {version} requires explicitly versioned PDFs with suffix {label}")
    if pdf_suffix and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", pdf_suffix):
        raise ValueError("PDF suffix must contain 1 to 64 letters, digits, dots, underscores or hyphens")
    suffix = "_" + pdf_suffix if pdf_suffix else ""
    documents = [f"output/pdf/OceanRoute_用户手册{suffix}.pdf", f"output/pdf/OceanRoute_设计文档{suffix}.pdf"]
    if any(not (ROOT / name).is_file() for name in documents):
        raise RuntimeError("Missing the explicitly selected manual/design PDFs")
    if not skip_frontend and not archive_only:
        npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
        if not npm:
            raise RuntimeError("需要 Node.js 编译发布界面")
        command = [npm, "run", "build"]
        if frontend_directory is not None:
            command += ["--", "--outDir", str(dist)]
        subprocess.run(command, cwd=ROOT / "web", check=True)
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
    wheel = output / f"oceanroute-{version}-py3-none-any.whl"
    expected = {str(p.relative_to(ROOT)): p for p in (ROOT / "oceanroute").rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    with zipfile.ZipFile(wheel) as package:
        members = {name for name in package.namelist() if name.startswith("oceanroute/")}
        if members != set(expected):
            raise RuntimeError(f"Wheel package mismatch: extra={sorted(members-set(expected))}; missing={sorted(set(expected)-members)}")
        if any(package.read(name) != path.read_bytes() for name, path in expected.items()):
            raise RuntimeError("Wheel package bytes differ from current source/static/manual")
    archive = output / f"OceanRoute-{label}-portable.zip"
    # Ship this release's reviewed screenshots; older releases retain their
    # own evidence without duplicating every historical image in each bundle.
    roots = ["oceanroute", "docs", "examples", "tests", "scripts", "web/src", "web/public", "web/tests", f"web/artifacts/release-{label}", "resources/validation"]
    files = ["README.md", "pyproject.toml", "launcher.py", "web/package.json", "web/package-lock.json", "web/index.html", "web/tsconfig.json", "web/vite.config.ts", "web/playwright.config.ts"]
    files += documents
    files += ["resources/research/manual_findings.md", "resources/research/website_findings.md",
              "resources/research/web_sources.json"]
    if tuple(map(int, version.split("."))) >= (0, 8, 0):
        files += ["resources/research/s57_sources.json"]
    if tuple(map(int, version.split("."))) >= (0, 9, 0):
        files += ["resources/run_0_9_backend_gate.py", "resources/run_0_9_browser_gate.py",
                  "resources/run_0_9_ui_build.py", "resources/validate_0_9_pdf_structure.py"]
    if tuple(map(int, version.split("."))) >= (0, 10, 0):
        files += ["resources/run_0_10_backend_gate.py", "resources/run_0_10_browser_gate.py",
                  "resources/validate_0_10_pdf_structure.py",
                  "resources/documentation_0_10_automatic_rules_contract.md"]
    if tuple(map(int, version.split("."))) >= (0, 11, 0):
        files += ["resources/run_0_11_backend_gate.py", "resources/run_0_11_browser_gate.py",
                  "resources/validate_0_11_pdf_structure.py", "resources/research/arc_sources.json"]
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
    parser.add_argument("--frontend-directory", type=Path, help="Use a separately verified frontend without replacing a frozen build")
    parser.add_argument("--pdf-suffix", help="Select versioned PDF filenames; defaults to the current release label from 0.5 onward")
    options = parser.parse_args()
    build(options.skip_frontend, options.archive_only, frontend_directory=options.frontend_directory, pdf_suffix=options.pdf_suffix)
