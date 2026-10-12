#!/usr/bin/env python3
"""Check the real E2B service using the pinned upstream SDK.

Default: authenticate with one read-only list request. --live: also create one
short-lived sandbox, exercise its public APIs, and always attempt to kill it.
Only E2B_API_KEY from the process environment is used. No .env is auto-loaded.
Missing credentials produce status=skipped, never a false passing live result.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import sys
import uuid

PINNED_E2B_VERSION = "2.51.0"


def check_environment(*, live: bool = False, timeout: int = 120) -> dict:
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "execution_subject": "scripts/check_e2b.py",
        "system_under_test": "E2B cloud via upstream e2b SDK",
        "mode": "live" if live else "read_only",
        "status": "skipped",
        "api_key_present": bool(os.environ.get("E2B_API_KEY", "").strip()),
        "expected_sdk_version": PINNED_E2B_VERSION,
        "checks": [],
        "cleanup": {"status": "not_needed"},
    }
    try:
        report["sdk_version"] = version("e2b")
    except PackageNotFoundError:
        report["sdk_version"] = None
    if not report["api_key_present"]:
        report["reason"] = "E2B_API_KEY is not configured; no E2B API requests made."
        return report
    if report["sdk_version"] != PINNED_E2B_VERSION:
        report["status"] = "failed"
        report["reason"] = f"Install the required upstream SDK: e2b=={PINNED_E2B_VERSION}."
        return report
    if not 1 <= timeout <= 120:
        report["status"] = "failed"
        report["reason"] = "Sandbox timeout must be between 1 and 120 seconds."
        return report

    from e2b import CommandExitException, Sandbox

    sandbox = None
    stage = "authentication_list"
    try:
        # Force a request: constructing the paginator alone does not authenticate.
        page = Sandbox.list(limit=1, request_timeout=20).next_items()
        report["checks"].append({"name": stage, "status": "passed"})
        del page  # Do not report existing sandbox IDs or their metadata.
        if live:
            stage = "create"
            sandbox = Sandbox.create(
                timeout=timeout,
                metadata={"purpose": "agentcore-e2b-sdk-contract-smoke"},
                request_timeout=20,
            )
            report["sandbox_timeout_seconds"] = timeout
            report["created_sandbox_id"] = sandbox.sandbox_id
            report["checks"].append({"name": stage, "status": "passed"})

            stage = "stdout_stderr_exit_zero"
            result = sandbox.commands.run(
                "printf 'hello-out'; printf 'hello-err' >&2", timeout=15
            )
            if (result.stdout, result.stderr, result.exit_code) != (
                "hello-out", "hello-err", 0
            ):
                raise AssertionError("command output mismatch")
            report["checks"].append({"name": stage, "status": "passed"})

            prefix = f"/home/user/e2b-sdk-smoke-{uuid.uuid4().hex}"
            stage = "unicode_file_roundtrip"
            content = "你好，AgentCore 与 E2B！\n🙂 café\n"
            sandbox.files.write(prefix + ".txt", content, request_timeout=20)
            if sandbox.files.read(prefix + ".txt", request_timeout=20) != content:
                raise AssertionError("unicode file mismatch")
            report["checks"].append({"name": stage, "status": "passed"})

            stage = "binary_file_roundtrip"
            data = bytes(range(256)) + b"\x00\xff\x80\x00"
            sandbox.files.write(prefix + ".bin", data, request_timeout=20)
            actual = sandbox.files.read(prefix + ".bin", format="bytes", request_timeout=20)
            if bytes(actual) != data:
                raise AssertionError("binary file mismatch")
            report["checks"].append({"name": stage, "status": "passed"})

            stage = "nonzero_command_exception"
            try:
                sandbox.commands.run(
                    "printf 'partial-out'; printf 'expected-error' >&2; exit 7", timeout=15
                )
            except CommandExitException as exc:
                if (exc.exit_code, exc.stdout, exc.stderr) != (
                    7, "partial-out", "expected-error"
                ):
                    raise AssertionError("nonzero exception fields mismatch")
            else:
                raise AssertionError("missing CommandExitException")
            report["checks"].append({"name": stage, "status": "passed"})
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        # Server exception text may contain request data. Keep only its class.
        report["checks"].append({
            "name": stage, "status": "failed", "error_type": type(exc).__name__
        })
    finally:
        if sandbox is not None:
            try:
                killed = sandbox.kill(request_timeout=20)
                if sandbox.is_running(request_timeout=20):
                    raise RuntimeError("sandbox still running")
                report["cleanup"] = {
                    "status": "passed", "kill_returned": killed,
                    "verified_not_running": True,
                }
            except Exception as exc:
                report["status"] = "failed"
                report["cleanup"] = {
                    "status": "failed", "error_type": type(exc).__name__,
                    "fallback": "E2B sandbox timeout remains at most 120 seconds.",
                }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Create and clean up one E2B sandbox")
    parser.add_argument("--timeout", type=int, default=120, choices=range(1, 121), metavar="1..120")
    parser.add_argument("--output", type=Path, help="Also save the non-secret JSON result")
    parser.add_argument("--require-config", action="store_true", help="Exit 2 on skipped for CI")
    args = parser.parse_args()
    report = check_environment(live=args.live, timeout=args.timeout)
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    sys.stdout.write(output)
    if report["status"] == "failed":
        return 1
    if report["status"] == "skipped" and args.require_config:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
