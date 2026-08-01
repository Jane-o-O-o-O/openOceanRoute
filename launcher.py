"""Cross-platform source/release launcher using the user's Python installation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main() -> int:
    if sys.version_info < (3, 10):
        print("OceanRoute 需要 Python 3.10 或更高版本。")
        return 1
    environment = ROOT / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        print("创建本地 Python 环境……", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    fingerprint = hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()
    stamp = environment / "oceanroute-installed.txt"
    if not stamp.exists() or stamp.read_text().strip() != fingerprint:
        print("安装 OceanRoute 与地形依赖（首次需要联网）……", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "-e", str(ROOT) + "[terrain]"], check=True, cwd=ROOT)
        stamp.write_text(fingerprint)
    if not (ROOT / "web/dist/index.html").exists() and not (ROOT / "oceanroute/static/index.html").exists():
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            print("此源码未编译界面。请安装 Node.js 后重试，或使用含编译界面的发布包。")
            return 1
        subprocess.run([npm, "ci"], check=True, cwd=ROOT / "web")
        subprocess.run([npm, "run", "build"], check=True, cwd=ROOT / "web")
    environment_vars = os.environ.copy()
    environment_vars.setdefault("OCEANROUTE_DATA_DIR", str(ROOT / ".oceanroute"))
    print("浏览器将打开本地工作空间（默认端口8765）；按 Ctrl+C 结束。", flush=True)
    return subprocess.call([str(python), "-m", "oceanroute", "--open", *sys.argv[1:]], cwd=ROOT, env=environment_vars)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nOceanRoute 已关闭。")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
