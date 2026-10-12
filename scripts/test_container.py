#!/usr/bin/env python3
"""Test the built image as its non-root user, without network or host ports.

The host test runner launches one disposable container. The container test
runner starts the real HTTP server and executes the installed Bash helper.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import uuid


CONTAINER_TEST = r'''
import base64
import json
import os
from pathlib import Path
import platform
import pwd
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import uuid

checks = []
server = None
root_helper = os.environ.get("ACSBX_TEST_ROOT_HELPER") == "1"

def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append({"name": name, "status": "passed"})

def start_server():
    identity = {"user": 10001, "group": 10001, "extra_groups": []} if root_helper else {}
    process = subprocess.Popen(
        [sys.executable, "-m", "agentcore_sandbox.server", "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        **identity,
    )
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("The installed HTTP server exited during startup: " + process.stderr.read()[:2000])
        try:
            with urlopen("http://127.0.0.1:8080/ping", timeout=0.2) as response:
                if json.load(response)["status"] == "Healthy":
                    return process
        except OSError:
            time.sleep(0.05)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("The installed HTTP server did not become healthy")

def stop_server(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    process.stderr.close()

def invoke(op, args, boot_id=None):
    request = {"protocol": "agentcore-sandbox", "version": 1,
               "request_id": str(uuid.uuid4()), "op": op, "args": args}
    if boot_id is not None:
        request["expected_boot_id"] = boot_id
    req = Request("http://127.0.0.1:8080/invocations", data=json.dumps(request).encode(),
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=5) as response:
        result = json.load(response)
    check("HTTP request correlation: " + op, result["request_id"] == request["request_id"])
    return result

def execute(cmd, boot_id):
    request = {"version": 1, "request_id": str(uuid.uuid4()), "expected_boot_id": boot_id,
               "cmd": cmd, "cwd": "/workspace", "envs": {}}
    encoded = base64.b64encode(json.dumps(request).encode()).decode()
    result = subprocess.run([sys.executable, "-m", "agentcore_sandbox.exec_guard", encoded],
                            capture_output=True, text=True, timeout=10)
    line, output = result.stdout.split("\n", 1)
    check("Execution helper protocol header", line.startswith("ACSBX1 "))
    header = json.loads(line[len("ACSBX1 "):])
    check("Execution helper request correlation", header["request_id"] == request["request_id"])
    return result, header, output

try:
    if root_helper:
        check("Container test runner simulates a native Runtime command launched as root", os.getuid() == 0)
        os.environ.update(HOME="/root", USER="root", LOGNAME="root")
    else:
        check("Container runs as the configured non-root sandbox user", os.getuid() == 10001 and pwd.getpwuid(os.getuid()).pw_name == "sandbox")
    check("Container uses the ARM64 image", platform.machine() == "aarch64")
    check("Fresh container has no inherited boot marker", not Path("/app/run/boot.json").exists())
    server = start_server()
    initialized = invoke("init", {"workspace": "/workspace"})
    boot_id = initialized["boot_id"]
    check("HTTP entry point creates a shared boot marker", json.loads(Path("/app/run/boot.json").read_text())["boot_id"] == boot_id)
    check("HTTP process creates the boot marker as the non-root sandbox user", Path("/app/run/boot.json").stat().st_uid == 10001)
    check("HTTP adapter advertises commands and files", {"commands", "files.write", "files.read"}.issubset(initialized["result"]["capabilities"]))
    content = "你好，AgentCore container!\n"
    written = invoke("files.write", {"path": "smoke/hello.txt", "encoding": "base64", "data": base64.b64encode(content.encode()).decode()}, boot_id)
    check("HTTP adapter writes a file as the non-root user", written["result"]["path"] == "/workspace/smoke/hello.txt" and Path(written["result"]["path"]).stat().st_uid == 10001)
    result, header, output = execute("cat /workspace/smoke/hello.txt", boot_id)
    check("Installed Bash helper prints the HTTP-written Unicode file", result.returncode == 0 and header["ok"] is True and output == content)
    check("Login Bash starts without stderr errors", result.stderr == "")
    result, header, output = execute('id -u; id -g; printf "%s\\n%s\\n%s\\n" "$HOME" "$USER" "$LOGNAME"', boot_id)
    check("Helper runs Bash with the HTTP user's uid, gid, HOME, USER and LOGNAME", result.returncode == 0 and header["ok"] is True and output == "10001\n10001\n/home/sandbox\nsandbox\nsandbox\n")
    result, header, output = execute("printf normal; printf error >&2; exit 37", boot_id)
    check("Installed Bash helper preserves nonzero exit and both output streams", result.returncode == 37 and header["ok"] is True and output == "normal" and result.stderr == "error")
    stop_server(server)
    server = None
    server = start_server()
    new_boot = invoke("init", {"workspace": "/workspace"})["boot_id"]
    check("HTTP entry point rotates the boot marker after restart", new_boot != boot_id)
    check("HTTP entry point preserves existing workspace files after restart", Path("/workspace/smoke/hello.txt").read_text() == content)
    stale_write = invoke("files.write", {"path": "never-file", "encoding": "base64", "data": "eA=="}, boot_id)
    check("HTTP adapter rejects stale boot before modifying files", stale_write["error"]["code"] == "SESSION_STATE_LOST" and not Path("/workspace/never-file").exists())
    result, header, output = execute("touch /workspace/never-command", boot_id)
    check("Installed Bash helper rejects stale boot before executing customer code", result.returncode != 0 and header["ok"] is False and header["error"]["code"] == "SESSION_STATE_LOST" and output == "" and not Path("/workspace/never-command").exists())
    print(json.dumps({"status": "passed", "execution_subject": "Container test runner (uid %s)" % os.getuid(),
                      "system_under_test": "Installed HTTP adapter and exec_guard in the ARM64 Docker image",
                      "architecture": platform.machine(), "uid": os.getuid(), "checks": checks}))
finally:
    if server is not None:
        stop_server(server)
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="agentcore-e2b-sdk:0.1.0")
    parser.add_argument("--root-helper", action="store_true", help="Start HTTP as uid 10001 and the helper as root to verify identity adaptation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        name = "container-root-helper-results.json" if args.root_helper else "container-results.json"
        args.output = Path(__file__).resolve().parents[1] / "docs" / name
    container_name = "acsbx-smoke-" + uuid.uuid4().hex[:12]
    started = time.monotonic()
    report = {
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "image": args.image,
        "host_test_runner": "scripts/test_container.py",
        "docker_network": "none",
        "published_host_ports": [],
        "container_auto_remove": True,
        "root_helper_simulation": args.root_helper,
    }
    try:
        command = ["docker", "run", "--rm", "--network", "none", "--name", container_name]
        if args.root_helper:
            command.extend(["--user", "0", "--env", "ACSBX_TEST_ROOT_HELPER=1"])
        command.extend(["-i", args.image, "python3", "-"])
        process = subprocess.run(
            command,
            input=CONTAINER_TEST, capture_output=True, text=True, timeout=60,
        )
        if process.returncode:
            report.update(status="failed", returncode=process.returncode,
                          error=process.stderr[-4000:], stdout=process.stdout[-4000:])
        else:
            report.update(json.loads(process.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report.update(status="failed", error=str(exc))
    finally:
        # --rm removes normal runs. Explicit cleanup also covers an interrupted
        # Docker client, which otherwise could leave its container running.
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=15)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    inspection = subprocess.run(["docker", "inspect", container_name], capture_output=True, timeout=15)
    report["container_removed"] = inspection.returncode != 0
    if not report["container_removed"]:
        report["status"] = "failed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": len(report.get("checks", [])), "report": str(args.output.resolve())}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
