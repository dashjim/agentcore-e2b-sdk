#!/usr/bin/env python3
"""Run acceptance operations through the real SDK and a dedicated Runtime."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore_sandbox import (Client, RuntimeTarget, CommandExitException,
    CommandExecutionTimeout, CommandWaitTimeout, CreateOutcomeUnknown,
    SessionStateLostError, FileOperationError)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=ROOT / "docs" / "deployment.local.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "agentcore-live-results.json")
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    target = RuntimeTarget(state["runtime_arn"], state["qualifier"], state["workspace"],
                           boot_file=state["boot_file"], module_path=state["module_path"])
    client = Client(region=state["region"], templates={"test": target}, read_timeout=90)
    results = {"system_under_test": "real AgentCore Runtime + SDK + adapter", "cases": [],
               "runtime_arn": state["runtime_arn"], "started_at": time.time(), "cleanup": "not_started"}
    sandbox = None
    attempt = None

    def record(name, function):
        started = time.monotonic()
        try:
            function()
            item = {"name": name, "status": "passed"}
        except Exception as exc:
            item = {"name": name, "status": "failed", "error_type": type(exc).__name__,
                    "message": str(exc)[:350]}
            cause = exc.__cause__
            if cause is not None:
                item["cause"] = f"{type(cause).__name__}: {str(cause)[:350]}"
        item["seconds"] = round(time.monotonic() - started, 3)
        results["cases"].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    def expect(error_type, function):
        try:
            function()
        except error_type:
            return
        raise AssertionError(f"Expected {error_type.__name__}")

    try:
        for index in range(5):
            try:
                sandbox = client.resume_create(attempt) if attempt else client.sandboxes.create(template="test")
                break
            except CreateOutcomeUnknown as exc:
                attempt = exc.attempt
                if index == 4:
                    raise
                time.sleep(2)
        results["session_id"] = sandbox.runtime.export_ref().runtime_session_id
        results["cases"].append({"name": "create_real_runtime_session", "status": "passed"})

        def file_command():
            info = sandbox.files.write("hello.py", "print('你好，AgentCore')\n")
            assert info.name == "hello.py"
            result = sandbox.commands.run("python3 hello.py")
            assert result.exit_code == 0 and result.stdout == "你好，AgentCore\n"
            assert sandbox.files.read("hello.py") == "print('你好，AgentCore')\n"
        record("write_execute_read_unicode", file_command)

        def binary():
            sandbox.files.write("bytes.bin", b"\x00\xff\x80\n")
            value = sandbox.files.read("bytes.bin", format="bytes")
            assert isinstance(value, bytearray) and value == b"\x00\xff\x80\n"
        record("binary_roundtrip", binary)

        def list_files():
            entries = sandbox.files.list(".")
            assert {"hello.py", "bytes.bin"}.issubset({e.name for e in entries})
            assert all(e.modified_time is not None for e in entries)
        record("directory_metadata", list_files)

        def executable_and_identity():
            sandbox.files.write("run.sh", "#!/bin/bash\nprintf executable\n")
            assert sandbox.commands.run("chmod +x run.sh && ./run.sh").stdout == "executable"
            owner = next(e.owner for e in sandbox.files.list(".") if e.name == "run.sh")
            assert sandbox.commands.run("id -un").stdout.strip() == owner
            sandbox.commands.run("printf command-file > command.txt")
            assert sandbox.files.read("command.txt") == "command-file"
        record("executable_workspace_and_same_identity", executable_and_identity)

        def callback():
            stdout, stderr = [], []
            result = sandbox.commands.run("printf out; printf err >&2", on_stdout=stdout.append, on_stderr=stderr.append)
            assert "".join(stdout) == result.stdout == "out"
            assert "".join(stderr) == result.stderr == "err"
        record("stdout_stderr_callbacks", callback)

        def nonzero():
            try:
                sandbox.commands.run("printf failure >&2; exit 7")
            except CommandExitException as exc:
                assert exc.exit_code == 7 and exc.stderr == "failure"
                return
            raise AssertionError("Nonzero exit was accepted")
        record("nonzero_exit_contract", nonzero)

        def directories():
            assert sandbox.files.make_dir("nested") is True
            assert sandbox.files.make_dir("nested") is False
            assert sandbox.files.remove("nested") is None
        record("mkdir_remove", directories)
        record("execution_timeout", lambda: expect(CommandExecutionTimeout,
            lambda: sandbox.commands.run("sleep 3", timeout=20, execution_timeout=1)))
        record("wait_timeout", lambda: expect(CommandWaitTimeout,
            lambda: sandbox.commands.run("sleep 1", timeout=0.05, execution_timeout=5)))
        # A local deadline does not cancel the remote command. Allow this bounded
        # test command to finish before the next shared-workspace operation.
        time.sleep(2)
        invalid_ref = replace(sandbox.runtime.export_ref(), boot_id=str(uuid.uuid4()))
        invalid = client.sandboxes.connect(invalid_ref)
        record("stale_boot_ref_rejected", lambda: expect(SessionStateLostError,
            lambda: invalid.commands.run("printf must-not-run")))
        invalid.close()
    except (Exception, KeyboardInterrupt) as exc:
        item = {"name": "test_setup", "status": "failed", "error_type": type(exc).__name__, "message": str(exc)[:350]}
        if exc.__cause__ is not None:
            item["cause"] = f"{type(exc.__cause__).__name__}: {str(exc.__cause__)[:500]}"
        results["cases"].append(item)
        print(json.dumps(item), flush=True)
    finally:
        for index in range(5):
            try:
                if sandbox is not None:
                    sandbox.runtime.stop()
                elif attempt is not None:
                    client.stop_attempt(attempt)
                results["cleanup"] = "stop_request_accepted" if sandbox or attempt else "no_session"
                break
            except Exception as exc:
                results["cleanup"] = f"failed:{type(exc).__name__}"
                if index < 4:
                    time.sleep(2)
        if sandbox is not None:
            sandbox.close()
        client.close()
        results["finished_at"] = time.time()
        results["passed"] = sum(c["status"] == "passed" for c in results["cases"])
        results["failed"] = sum(c["status"] == "failed" for c in results["cases"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: results[k] for k in ["passed", "failed", "cleanup"]}), flush=True)
    return 1 if results["failed"] or results["cleanup"].startswith("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
