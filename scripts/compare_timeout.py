#!/usr/bin/env python3
"""Compare bounded timeout observations with shared positive control and markers.

The application executes the same probe on both backends. Finite observations
cannot establish an E2B cancellation guarantee or permanent process termination.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

from compare_backends import (
    AgentCoreBackend, E2BBackend, PINNED_E2B_VERSION, exception_snapshot,
    package_version, redact_configured_secret,
)


COMMAND = "printf started > started; printf started; sleep 2; printf completed > completed; printf completed"
KNOWN_OUTPUTS = {"", "started", "completed", "startedcompleted"}


def controlled_text(value):
    if isinstance(value, str) and value in KNOWN_OUTPUTS:
        return value
    return {"omitted": "unexpected_probe_output", "length": len(value) if isinstance(value, str) else None}


def markers(sandbox, directory):
    """The application reports only declared marker names and controlled text."""
    entries = sandbox.files.list(directory)
    names = {entry.name for entry in entries}
    return {
        marker: {"present": marker in names,
                 "content": controlled_text(sandbox.files.read(directory + "/" + marker)) if marker in names else None}
        for marker in ("started", "completed")
    }


def shared_timeout_probe(sandbox, directory):
    """Identical business code, observer calls, and timing for both backends."""
    control_dir, short_dir = directory + "/control", directory + "/short"
    sandbox.files.make_dir(control_dir)
    sandbox.files.make_dir(short_dir)
    started = time.monotonic()
    control = sandbox.commands.run(COMMAND, cwd=control_dir, timeout=15)
    control_markers = markers(sandbox, control_dir)
    result = {
        "control": {
            "stdout": controlled_text(control.stdout),
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "markers": control_markers,
            "passed": control.stdout == "startedcompleted" and all(
                control_markers[name] == {"present": True, "content": name}
                for name in ("started", "completed")
            ),
        },
        "short_timeout": {},
        "windows": [],
    }
    if not result["control"]["passed"]:
        result["status"] = "control_failed"
        return result

    callbacks = []
    started = time.monotonic()

    def on_stdout(chunk):
        callbacks.append({"elapsed_seconds": round(time.monotonic() - started, 6),
                          "text": controlled_text(chunk)})

    try:
        command = sandbox.commands.run(COMMAND, cwd=short_dir, timeout=0.5, on_stdout=on_stdout)
        result["short_timeout"] = {"outcome": "returned", "stdout": controlled_text(command.stdout)}
    except Exception as exc:
        result["short_timeout"] = {"outcome": "raised", "exception": exception_snapshot(exc)}
    ended = time.monotonic()
    result["short_timeout"]["elapsed_seconds"] = round(ended - started, 6)
    result["short_timeout"]["callbacks_at_return"] = list(callbacks)

    for delay, cumulative in ((3.2, 3.2), (3.0, 6.2)):
        time.sleep(delay)
        window = {"requested_wait_after_command_seconds": cumulative,
                  "actual_elapsed_after_command_seconds": round(time.monotonic() - ended, 6)}
        try:
            window["markers"] = markers(sandbox, short_dir)
            window["status"] = "observed"
        except Exception as exc:
            window.update(status="failed", exception=exception_snapshot(exc))
        window["callbacks"] = list(callbacks)
        result["windows"].append(window)
    result["status"] = "observed" if all(w["status"] == "observed" for w in result["windows"]) else "failed"
    return result


def run_backend(name, backend):
    started = time.monotonic()
    report = {"setup": {"status": "not_started"}, "probe": {"status": "not_started"},
              "cleanup": {"status": "not_started"}, "expected_timeout_exception":
              "TimeoutException" if name == "e2b" else "CommandWaitTimeout"}
    phase = "setup"
    try:
        sandbox = backend.create()
        default = sandbox.commands.run("pwd", timeout=15).stdout.strip()
        if not default.startswith("/") or "\n" in default:
            raise ValueError("invalid default directory")
        directory = default.rstrip("/") + "/timeout-comparison-" + uuid.uuid4().hex[:12]
        sandbox.files.make_dir(directory)
        report["setup"] = {"status": "ready"}
        phase = "probe"
        report["probe"] = shared_timeout_probe(sandbox, directory)
        observed = report["probe"].get("short_timeout", {})
        report["expected_timeout_observed"] = (
            observed.get("outcome") == "raised"
            and observed.get("exception", {}).get("python_type", {}).get("name") == report["expected_timeout_exception"]
        )
        if not report["expected_timeout_observed"]:
            report["probe"]["status"] = "failed"
    except (Exception, KeyboardInterrupt) as exc:
        report[phase] = {"status": "failed", "exception": exception_snapshot(exc)}
    finally:
        try:
            report["cleanup"] = backend.cleanup()
        except (Exception, KeyboardInterrupt) as exc:
            report["cleanup"] = {"status": "failed", "exception": exception_snapshot(exc)}
    report["complete"] = (
        report["setup"]["status"] == "ready" and report["probe"]["status"] == "observed"
        and report["cleanup"]["status"] == ("passed" if name == "e2b" else "stop_request_accepted")
    )
    report["duration_seconds"] = round(time.monotonic() - started, 6)
    return report


def compare_timeouts(state_path, *, factories=None):
    report = {
        "schema_version": 1,
        "execution_subject": "scripts/compare_timeout.py application runner",
        "systems_under_test": {"e2b": "real E2B cloud through upstream e2b SDK",
                               "agentcore": "real AgentCore Runtime through agentcore-e2b-sdk and adapter"},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "versions": {"python": sys.version.split()[0], "e2b": package_version("e2b"),
                     "agentcore-e2b-sdk": package_version("agentcore-e2b-sdk")},
        "shared_probe": {"command": COMMAND, "control_timeout_seconds": 15,
                         "short_timeout_seconds": 0.5, "wait_windows_after_return_seconds": [3.2, 6.2],
                         "description": "The application runs the identical command first in a control directory and then in a fresh short-timeout directory. The application uses identical stdout callbacks and marker reads on both backends."},
        "interpretation_limit": "Even when started exists and completed is absent, the application has observed only two finite windows. These results cannot establish permanent cancellation, process termination, or an E2B cancellation guarantee. Missing started also cannot establish that the command began execution.",
        "cleanup_semantics": "The application kills E2B and verifies is_running=False. The application requests AgentCore Runtime stop; stop_request_accepted confirms request acceptance only, not observed termination or E2B kill equivalence.",
        "completion_semantics": "completed means positive controls, expected timeout exception classes, both observation windows, and cleanup completed. It does not mean the backends have equal behavior.",
        "backends": {}, "status": "not_started",
    }
    if factories is None:
        if not os.environ.get("E2B_API_KEY", "").strip():
            report.update(status="skipped", reason="E2B_API_KEY is missing; neither cloud backend was invoked.")
            return report
        if report["versions"]["e2b"] != PINNED_E2B_VERSION:
            report.update(status="failed", reason="The runner requires e2b==2.51.0; neither cloud backend was invoked.")
            return report
        factories = {"e2b": E2BBackend, "agentcore": lambda: AgentCoreBackend(state_path)}
    for name in ("e2b", "agentcore"):
        report["backends"][name] = run_backend(name, factories[name]())
    report["status"] = "completed" if all(b["complete"] for b in report["backends"].values()) else "incomplete"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="Private AgentCore deployment JSON; identifiers are not copied to the report")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare_timeouts(args.state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(redact_configured_secret(json.dumps(report, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"]}), flush=True)
    return 0 if report["status"] == "completed" else 2 if report["status"] == "skipped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
