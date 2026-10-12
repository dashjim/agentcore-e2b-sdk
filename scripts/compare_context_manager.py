#!/usr/bin/env python3
"""Observe the same with-block on E2B and AgentCore, then clean up both sessions.

The application uses identical business code. Backend-specific observers inspect
the different lifecycle contracts; they are not identical client API calls.
Exit zero means observation and cleanup completed, not lifecycle equivalence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from compare_backends import (
    AgentCoreBackend,
    E2BBackend,
    PINNED_E2B_VERSION,
    exception_snapshot,
    package_version,
    redact_configured_secret,
)


SHARED_BUSINESS_CODE = """with sandbox:
    result = sandbox.commands.run('printf context-ok', timeout=15)
"""


def shared_business_block(sandbox):
    """Both real backends receive exactly this business block."""
    with sandbox:
        result = sandbox.commands.run('printf context-ok', timeout=15)
    return result


def controlled_output(value, expected):
    """Keep only controlled probe text, never unexpected endpoint content."""
    if isinstance(value, str) and value == expected:
        return value
    return {"omitted": "unexpected_probe_output", "length": len(value) if isinstance(value, str) else None}


def run_backend(name, backend):
    started = time.monotonic()
    report = {
        "setup": {"status": "not_started"},
        "business_block": {"status": "not_started"},
        "observer": {"status": "not_started"},
        "cleanup": {"status": "not_started"},
        "expected_behavior": {
            "description": (
                "E2B SDK exits the with-block by killing the cloud sandbox; the original handle reports is_running=False."
                if name == "e2b" else
                "AgentCore SDK exits the with-block by closing the local handle; the application reconnects the same Runtime session and executes a command."
            ),
            "satisfied": False,
        },
    }
    old_handle = ref = None
    phase = "setup"
    try:
        old_handle = backend.create()
        if name == "agentcore":
            # The application retains the reference privately before __exit__.
            ref = old_handle.export_ref()
        report["setup"] = {"status": "ready"}
        phase = "business_block"
        result = shared_business_block(old_handle)
        report["business_block"] = {
            "status": "observed",
            "stdout": controlled_output(result.stdout, "context-ok"),
            "expected_stdout": result.stdout == "context-ok",
        }
        phase = "observer"
        if name == "e2b":
            running = old_handle.is_running(request_timeout=20)
            report["observer"] = {
                "status": "observed",
                "execution_subject": "application using the original E2B handle",
                "call": "old_handle.is_running(request_timeout=20)",
                "is_running": running if type(running) is bool else None,
            }
            report["expected_behavior"]["satisfied"] = running is False
        else:
            old_state = old_handle.state
            resumed = backend.client.sandboxes.connect(ref)
            # cleanup must stop through an open handle, never the closed one.
            backend.sandbox = resumed
            same_session = resumed.export_ref() == ref
            result = resumed.commands.run("printf resumed", timeout=15)
            report["observer"] = {
                "status": "observed",
                "execution_subject": "application using the AgentCore client and reconnected handle",
                "calls": ["old_handle.state", "client.sandboxes.connect(ref)",
                          "resumed.commands.run('printf resumed', timeout=15)"],
                "old_handle_state": old_state if old_state in {"CLOSED", "READY", "STOP_REQUESTED"} else "unrecognized_state",
                "same_session_reference": same_session,
                "resumed_stdout": controlled_output(result.stdout, "resumed"),
                "expected_resumed_stdout": result.stdout == "resumed",
            }
            report["expected_behavior"]["satisfied"] = old_state == "CLOSED" and same_session and result.stdout == "resumed"
    except (Exception, KeyboardInterrupt) as exc:
        report[phase] = {"status": "failed", "exception": exception_snapshot(exc)}
    finally:
        # A failed body or observer can leave the original local handle CLOSED.
        # The application still obtains an open handle for the Runtime stop call.
        if name == "agentcore" and ref is not None and backend.sandbox is old_handle:
            try:
                backend.sandbox = backend.client.sandboxes.connect(ref)
                report["cleanup_handle"] = {"status": "reconnected_same_reference"}
            except (Exception, KeyboardInterrupt) as exc:
                report["cleanup_handle"] = {"status": "failed", "exception": exception_snapshot(exc)}
        try:
            report["cleanup"] = backend.cleanup()
        except (Exception, KeyboardInterrupt) as exc:
            report["cleanup"] = {"status": "failed", "exception": exception_snapshot(exc)}
    report["complete"] = (
        report["setup"]["status"] == "ready"
        and report["business_block"]["status"] == "observed"
        and report["observer"]["status"] == "observed"
        and report["cleanup"]["status"] == ("passed" if name == "e2b" else "stop_request_accepted")
    )
    report["duration_seconds"] = round(time.monotonic() - started, 6)
    return report


def compare_context_managers(state_path, *, factories=None):
    report = {
        "schema_version": 1,
        "execution_subject": "scripts/compare_context_manager.py application runner",
        "systems_under_test": {
            "e2b": "real E2B cloud through upstream e2b SDK",
            "agentcore": "real AgentCore Runtime through agentcore-e2b-sdk and adapter",
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
        "versions": {"python": sys.version.split()[0], "e2b": package_version("e2b"),
                     "agentcore-e2b-sdk": package_version("agentcore-e2b-sdk")},
        "shared_business_code": SHARED_BUSINESS_CODE,
        "scope": "The application executes identical with-block business code. The application uses backend-specific observers and cleanup calls outside that block.",
        "cleanup_semantics": {
            "e2b": "The application calls kill again in finally, then checks is_running=False. A repeated kill return or exception is recorded separately; no_handle does not confirm termination.",
            "agentcore": "The application uses an open handle to request Runtime stop and closes local handles. stop_request_accepted confirms request acceptance only, not observed termination or E2B kill semantics.",
        },
        "completion_semantics": "completed/exit 0 means both observations and cleanup completed. It does not mean zero differences or full SDK compatibility. expected_behavior records each backend's separate lifecycle expectation.",
        "backends": {},
        "status": "not_started",
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
    report["summary"] = {
        "expected_behavior_satisfied": {name: b["expected_behavior"]["satisfied"] for name, b in report["backends"].items()},
        "business_stdout_equal": all(b["business_block"].get("expected_stdout", False) for b in report["backends"].values()),
        "expected_lifecycle_difference": "E2B __exit__ kills the remote sandbox; AgentCore __exit__ closes only the local handle.",
        "lifecycle_difference_observed": all(b["expected_behavior"]["satisfied"] for b in report["backends"].values()),
    }
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="Private AgentCore deployment JSON; identifiers are never copied to report")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare_context_managers(args.state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(redact_configured_secret(json.dumps(report, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "summary": report.get("summary", {})}), flush=True)
    return 0 if report["status"] == "completed" else 2 if report["status"] == "skipped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
