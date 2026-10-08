"""Launch only ephemeral local application processes for the manual-work browser suite."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request


def run_browser(back: Path, front: Path, output: Path, env: dict, reader_dsn: str, writer_dsn: str):
    for port in (8107, 3107):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js required")
    environment = {**env, "PROJECTS_DATABASE_URL": reader_dsn,
        "PROJECTS_WRITE_DATABASE_URL": writer_dsn,
        "BUSINESS_WRITES_ENABLED": "true", "PROJECTS_CREATE_SUBJECTS": "engineer01",
        "PROJECTS_WRITE_ORIGINS": "http://127.0.0.1:3107", "NEXT_PUBLIC_USE_MOCKS": "false",
        "NEXT_PUBLIC_API_BASE_URL": "/api", "BACKEND_URL": "http://127.0.0.1:8107",
        "NEXT_DIST_DIR": "tmp/manual-next", "EE_BASE_URL": "http://127.0.0.1:3107", "EE_MANUAL_OUTPUT": str(output / "browser")}
    processes, logs = [], []
    try:
        for name, args, cwd in (
            ("api", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8107"], back),
            ("web", [node, "node_modules/next/dist/bin/next", "dev", "--hostname", "127.0.0.1", "--port", "3107"], front),
        ):
            log = (output / (name + ".log")).open("wb"); logs.append(log)
            processes.append(subprocess.Popen(args, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags))
        for url in ("http://127.0.0.1:8107/health", "http://127.0.0.1:3107/login"):
            deadline = time.monotonic() + 100
            while True:
                try:
                    with urllib.request.urlopen(url, timeout=10) as response:
                        if response.status == 200: break
                except Exception:
                    if time.monotonic() > deadline: raise RuntimeError("Local test server failed to start: " + url)
                    time.sleep(1)
        suites = ["manual-work"] + (["metadata-edit"] if env.get("EE_METADATA_TEST") == "1" else [])
        for suite in suites:
            log_name = "browser.log" if suite == "manual-work" else "metadata-browser.log"
            with (output / log_name).open("wb") as log:
                result = subprocess.run([node, "tests/browser/" + suite + ".mjs"], cwd=front, env=environment, stdout=log, stderr=subprocess.STDOUT, creationflags=flags, timeout=300)
            print((output / log_name).read_text(encoding="utf8"), flush=True)
            if result.returncode: raise RuntimeError("Browser suite failed; inspect " + str(output / log_name))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                # Process IDs come only from the children created immediately above.
                if os.name == "nt": subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=flags)
                else: process.terminate()
                process.wait(timeout=20)
        for log in logs: log.close()
