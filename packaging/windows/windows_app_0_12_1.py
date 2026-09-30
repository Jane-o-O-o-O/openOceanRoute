"""Application-local Windows entry point; product code stays in the verified wheel."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "OceanRoute"
DATA.mkdir(parents=True, exist_ok=True)
os.environ["OCEANROUTE_DATA_DIR"] = str(DATA)
INFO = DATA / "running.json"
NAME = "Local\\OceanRoute012_" + hashlib.sha256(str(DATA).casefold().encode()).hexdigest()[:24]
KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL.CreateMutexW.restype = wintypes.HANDLE
KERNEL.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL.OpenMutexW.restype = wintypes.HANDLE
KERNEL.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL.CreateEventW.restype = wintypes.HANDLE
KERNEL.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL.OpenEventW.restype = wintypes.HANDLE
KERNEL.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
KERNEL.WaitForSingleObject.restype = wintypes.DWORD
KERNEL.SetEvent.argtypes = [wintypes.HANDLE]
KERNEL.CloseHandle.argtypes = [wintypes.HANDLE]


def current_url() -> str | None:
    try:
        row = json.loads(INFO.read_text(encoding="utf-8"))
        port = row["port"]
        if type(port) is not int or not 1024 <= port <= 65535:
            return None
        url = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
            health = json.load(response)
        if health.get("product") == "OceanRoute" and health.get("version") in {"0.12.0", "0.12.1"}:
            return url
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def stop() -> int:
    deadline = time.monotonic() + 30
    handle = None
    while time.monotonic() < deadline:
        handle = KERNEL.OpenEventW(2, False, NAME + "_stop")
        if handle:
            break
        instance = KERNEL.OpenMutexW(0x00100000, False, NAME + "_instance")
        if not instance:
            return 0
        KERNEL.CloseHandle(instance)
        time.sleep(0.1)
    if not handle:
        return 1
    try:
        if not KERNEL.SetEvent(handle):
            return 1
        while time.monotonic() < deadline:
            instance = KERNEL.OpenMutexW(0x00100000, False, NAME + "_instance")
            if not instance:
                return 0
            KERNEL.CloseHandle(instance)
            time.sleep(0.1)
        return 1
    finally:
        KERNEL.CloseHandle(handle)


def main() -> int:
    if "--stop" in sys.argv:
        return stop()
    mutex = KERNEL.CreateMutexW(None, False, NAME + "_instance")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    already = ctypes.get_last_error() == 183
    try:
        if already:
            for _ in range(300):
                url = current_url()
                if url:
                    if "--no-browser" not in sys.argv:
                        webbrowser.open(url)
                    return 0
                time.sleep(0.1)
            raise RuntimeError("OceanRoute 正在启动，但尚未响应。请使用“关闭 OceanRoute”后重试。")
        event = KERNEL.CreateEventW(None, True, False, NAME + "_stop")
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            from oceanroute import __version__
            import uvicorn
            if __version__ != "0.12.1":
                raise RuntimeError("安装包版本不一致。请重新安装 OceanRoute 0.12.1。")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                try:
                    listener.bind(("127.0.0.1", 8765))
                except OSError:
                    listener.bind(("127.0.0.1", 0))
                listener.listen(128)
                port = listener.getsockname()[1]
                INFO.write_text(json.dumps({"port": port, "pid": os.getpid(), "version": __version__}), encoding="utf-8")
                config = uvicorn.Config("oceanroute.api:app", host="127.0.0.1", port=port,
                                        log_level="info", access_log=False, use_colors=False)
                server = uvicorn.Server(config)

                def watch_stop():
                    if KERNEL.WaitForSingleObject(event, 0xFFFFFFFF) == 0:
                        server.should_exit = True

                threading.Thread(target=watch_stop, daemon=True).start()

                def open_when_ready():
                    for _ in range(600):
                        if server.should_exit:
                            return
                        if server.started:
                            webbrowser.open(f"http://127.0.0.1:{port}")
                            return
                        time.sleep(0.1)

                if "--no-browser" not in sys.argv:
                    threading.Thread(target=open_when_ready, daemon=True).start()
                try:
                    server.run(sockets=[listener])
                finally:
                    INFO.unlink(missing_ok=True)
                return 0
        finally:
            KERNEL.CloseHandle(event)
    finally:
        KERNEL.CloseHandle(mutex)


if __name__ == "__main__":
    # pythonw has no stdout/stderr. Give logging real UTF-8 streams before imports.
    log = (DATA / "startup.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    logging.basicConfig(level=logging.INFO, stream=log)
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        message = "OceanRoute 启动失败。详细日志：\n" + str(DATA / "startup.log")
        ctypes.windll.user32.MessageBoxW(None, message, "OceanRoute 0.12.1", 0x10)
        raise SystemExit(1)
