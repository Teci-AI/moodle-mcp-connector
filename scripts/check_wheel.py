"""Install the built wheel into a clean virtual environment and smoke-test it.

Usage: python scripts/check_wheel.py [DIST_DIR] [--installer pip|uv]
       DIST_DIR defaults to dist; --installer uv needs uv on PATH.

Proves the wheel is self-contained: it installs with only its declared
dependencies, the `moodle-mcp-connector` command and `python -m
moodle_mcp_connector` start and exit cleanly with no configuration, a full
MCP session works over stdio, and the token never appears in any output.
Everything runs outside the repository, so no .env file or source checkout
can leak in. Exits non-zero with a message on the first failure.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import venv
import zipfile
from pathlib import Path

TIMEOUT = 60
# Tools registered by default (read-only, all toolsets).
EXPECTED_READ_TOOLS = 41


def fail(message: str) -> None:
    print(f"check_wheel: FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def find_wheel(dist: Path) -> Path:
    wheels = sorted(dist.glob("moodle_mcp_connector-*.whl"))
    if len(wheels) != 1:
        fail(f"expected exactly one wheel in {dist}, found {[w.name for w in wheels]}")
    return wheels[0]


def check_contents(wheel: Path) -> str:
    """Check the wheel ships only the package; return its version."""
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = zf.read(meta).decode("utf-8")
    top = {n.split("/", 1)[0] for n in names}
    stray = {t for t in top if t != "moodle_mcp_connector" and not t.endswith(".dist-info")}
    if stray:
        fail(f"wheel contains unexpected top-level entries: {sorted(stray)}")
    for required in ("moodle_mcp_connector/server.py", "moodle_mcp_connector/__main__.py"):
        if required not in names:
            fail(f"wheel is missing {required}")
    if "License-Expression: MIT" not in metadata:
        fail("wheel METADATA has no 'License-Expression: MIT'")
    version = next(line.split(":", 1)[1].strip() for line in metadata.splitlines()
                   if line.startswith("Version:"))
    return version


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOODLE_")}
    env.pop("PYTHONPATH", None)
    env.update(extra)
    return env


def run(cmd: list[str], cwd: Path, env: dict, stdin: bytes = b"") -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, env=env, input=stdin,
                              capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        fail(f"{' '.join(cmd)} did not exit within {TIMEOUT}s after stdin closed")


class Session:
    """Drive the server over stdio the way an MCP client does.

    Requests are sent one at a time and each response is awaited before
    stdin is closed: the server stops (dropping unfinished requests) as soon
    as stdin reaches EOF, exactly as a real client would never let happen.
    """

    def __init__(self, cmd: list[str], cwd: Path, env: dict):
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.lines: queue.Queue[bytes | None] = queue.Queue()
        self.stdout = bytearray()
        self.stderr = bytearray()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:
            self.stdout += line
            self.lines.put(line)
        self.lines.put(None)

    def _pump_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr += line

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, msg_id: int, method: str, params: dict | None = None) -> dict:
        self.send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + TIMEOUT
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                self.kill()
                fail(f"no response to {method} within {TIMEOUT}s")
            if line is None:
                fail(f"server exited before answering {method}: "
                     f"{bytes(self.stderr)[-1000:]!r}")
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg

    def close(self) -> int:
        """Close stdin like a client disconnecting; return the exit code."""
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            self.kill()
            fail(f"server did not exit within {TIMEOUT}s after stdin closed")

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait()


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # keep progress and errors in order
    parser = argparse.ArgumentParser(description="Smoke-test the built wheel in a clean venv.")
    parser.add_argument("dist", nargs="?", default="dist", help="directory holding the wheel")
    parser.add_argument("--installer", choices=("pip", "uv"), default="pip",
                        help="install with the new venv's pip (default) or with uv")
    args = parser.parse_args()
    installer = args.installer
    dist = Path(args.dist).resolve()
    wheel = find_wheel(dist)
    version = check_contents(wheel)
    print(f"check_wheel: {wheel.name} (version {version})")

    with tempfile.TemporaryDirectory(prefix="mcp-wheel-check-") as tmp:
        work = Path(tmp)
        env_dir = work / "venv"
        venv.create(env_dir, with_pip=True)
        bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        command = bindir / ("moodle-mcp-connector.exe" if os.name == "nt" else "moodle-mcp-connector")

        if installer == "uv":
            install_cmd = ["uv", "pip", "install", "--quiet", "--python", str(python), str(wheel)]
        else:
            install_cmd = [str(python), "-m", "pip", "install", "--quiet",
                           "--disable-pip-version-check", "--timeout", "60", "--retries", "5",
                           str(wheel)]
        installed = subprocess.run(install_cmd, cwd=work, env=clean_env(),
                                   capture_output=True, timeout=900)
        if installed.returncode != 0:
            fail(f"{installer} install failed:\n"
                 f"{installed.stderr.decode(errors='replace')[-2000:]}")
        if not command.exists():
            fail(f"console script not installed at {command}")
        print("check_wheel: installed into a clean virtual environment")

        # 1. No configuration at all, stdin closed: must exit 0 silently.
        for cmd in ([str(command)], [str(python), "-m", "moodle_mcp_connector"]):
            r = run(cmd, work, clean_env())
            if r.returncode != 0 or r.stdout or r.stderr:
                fail(f"{cmd[-1]} with no config: exit {r.returncode}, "
                     f"stdout {r.stdout[:200]!r}, stderr {r.stderr[:500]!r}")
        print("check_wheel: command and python -m exit cleanly without configuration")

        # 2. A full MCP session against an unreachable site, with a canary
        #    token that must never be echoed anywhere.
        canary = "canary-" + secrets.token_hex(16)
        env = clean_env(MOODLE_URL="http://127.0.0.1:9", MOODLE_TOKEN=canary)
        session = Session([str(command)], work, env)
        init = session.request(0, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "check_wheel", "version": "0"}})
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = session.request(1, "tools/list")
        called = session.request(2, "tools/call", {"name": "server_info", "arguments": {}})
        code = session.close()
        if code != 0:
            fail(f"MCP session exited {code}: {bytes(session.stderr)[-1000:]!r}")
        if canary.encode() in session.stdout or canary.encode() in session.stderr:
            fail("the token appeared in the server's output")
        if init.get("result", {}).get("serverInfo", {}).get("name") != "moodle-mcp-connector":
            fail(f"bad initialize response: {init}")
        tools = listed.get("result", {}).get("tools", [])
        if len(tools) != EXPECTED_READ_TOOLS:
            fail(f"expected {EXPECTED_READ_TOOLS} tools, got {len(tools)}")
        call = called.get("result", {})
        if call.get("isError") or not call.get("content"):
            fail(f"server_info failed: {called}")
        info = json.loads(call["content"][0]["text"])
        if info.get("server_version") != version:
            fail(f"server_info reports version {info.get('server_version')}, wheel is {version}")
        if info.get("connected") is not False or not info.get("error"):
            fail(f"server_info should report the unreachable site: {info}")
        print(f"check_wheel: MCP session OK ({len(tools)} tools, server_info "
              f"version {version}, token never printed)")

    print("check_wheel: PASS")


if __name__ == "__main__":
    main()
