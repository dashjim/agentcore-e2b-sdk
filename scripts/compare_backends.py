#!/usr/bin/env python3
"""Run identical client calls against real E2B and AgentCore, without credentials in reports.

The application executes cases sequentially. Only factories/lifecycle differ.
Expected negative tests are observations, not successful business operations.
Strict comparisons preserve Python class/module identities; a separate field
projection identifies equal business values without claiming type compatibility.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
import time
from typing import Callable
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PINNED_E2B_VERSION = "2.51.0"
FIELDS = ("stdout", "stderr", "exit_code", "error", "name", "type", "path",
          "metadata", "size", "mode", "permissions", "owner", "group",
          "modified_time", "symlink_target", "pid")
DYNAMIC_FIELDS = {"modified_time", "pid", "owner", "group"}
NORMALIZATION_RULES = [
    "Replace only this runner's random directory prefix in file metadata path/symlink_target; retain actual command stdout/cwd paths.",
    "Separate modified_time, pid, owner and group values from comparisons, retaining their field presence and Python types.",
    "Preserve list ordering, callback chunk count/content, exceptions, enum classes, all other values and Python class/module identities.",
]


def type_id(value):
    return {"module": type(value).__module__, "name": type(value).__name__}


def snapshot(value, depth=0):
    """Select explicit public fields; never dump __dict__, repr or exception text."""
    identity = {"python_type": type_id(value)}
    if depth > 12:
        return {**identity, "omitted": "depth_limit"}
    if isinstance(value, Enum):
        return {**identity, "enum_name": value.name, "value": snapshot(value.value, depth + 1)}
    if value is None or type(value) in (str, int, float, bool):
        return {**identity, "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {**identity, "hex": bytes(value).hex(), "length": len(value)}
    if isinstance(value, datetime):
        return {**identity, "value": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return {**identity, "items": [snapshot(item, depth + 1) for item in value]}
    if isinstance(value, dict):
        return {**identity, "fields": {str(k): snapshot(v, depth + 1) for k, v in value.items()}}
    return {**identity, "fields": {field: snapshot(getattr(value, field), depth + 1)
                                    for field in FIELDS if hasattr(value, field)}}


def exception_snapshot(exc):
    """No messages/bodies/causes. Command output is accepted only from controlled probes."""
    fields = {}
    for field in ("code", "exit_code", "stdout", "stderr", "error", "remote_state_unknown"):
        if not hasattr(exc, field):
            continue
        value = getattr(exc, field)
        if value is None or type(value) in (int, bool):
            fields[field] = snapshot(value)
        elif isinstance(value, str):
            if field == "code" and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                fields[field] = snapshot(value)
            elif field in ("stdout", "stderr") and value in ("", "partial-out", "expected-error"):
                fields[field] = snapshot(value)
            else:
                fields[field] = {"python_type": type_id(value), "omitted": "untrusted_exception_field", "length": len(value),
                                 "redacted_sha256": hashlib.sha256(redact_configured_secret(value).encode()).hexdigest()}
        else:
            fields[field] = {"python_type": type_id(value), "omitted": "non_scalar_exception_field"}
    return {"python_type": type_id(exc), "fields": fields}


def normalize(node, directory, dynamic=None, trail=()):
    """Predeclared metadata-only normalization. stdout absolute paths remain literal."""
    if dynamic is None:
        dynamic = {}
    if isinstance(node, list):
        return [normalize(v, directory, dynamic, trail + (str(i),)) for i, v in enumerate(node)]
    if not isinstance(node, dict):
        return node
    result = {}
    for key, value in node.items():
        if key == "fields":
            fields = {}
            for field, item in value.items():
                path = trail + (field,)
                if field in DYNAMIC_FIELDS:
                    dynamic["/".join(path)] = item
                    fields[field] = {"python_type": item.get("python_type"), "separated_dynamic_value": True}
                elif field in {"path", "symlink_target"} and isinstance(item.get("value"), str):
                    text = item["value"]
                    if text == directory or text.startswith(directory + "/"):
                        text = "<CASE_DIR>" + text[len(directory):]
                    fields[field] = {**item, "value": text}
                else:
                    fields[field] = normalize(item, directory, dynamic, path)
            result[key] = fields
        else:
            result[key] = normalize(value, directory, dynamic, trail + (key,))
    return result


def field_projection(node):
    """Diagnostic only: drop Python identities, preserving fields and enum names.

    This projection MUST NOT determine strict comparison status. It intentionally
    cannot certify isinstance compatibility or bytes/bytearray interchangeability.
    """
    if isinstance(node, list):
        return [field_projection(x) for x in node]
    if isinstance(node, dict):
        return {k: field_projection(v) for k, v in node.items() if k != "python_type"}
    return node


def differing_paths(left, right, trail="", limit=30):
    if type(left) is not type(right):
        return [trail or "/"]
    if isinstance(left, dict):
        paths = []
        for key in sorted(left.keys() | right.keys()):
            path = trail + "/" + key
            paths.extend([path] if key not in left or key not in right else differing_paths(left[key], right[key], path, limit))
            if len(paths) >= limit:
                break
        return paths[:limit]
    if isinstance(left, list):
        paths = [trail + "/length"] if len(left) != len(right) else []
        for i, (a, b) in enumerate(zip(left, right)):
            paths.extend(differing_paths(a, b, trail + f"/{i}", limit))
        return paths[:limit]
    return [] if left == right else [trail or "/"]


@dataclass(frozen=True)
class Case:
    name: str
    function: Callable
    category: str = "core"
    expected: str = "return"
    description: str = ""


@dataclass
class Observed:
    values: object
    checks: dict
    business_success: bool = True


def result(values, **checks):
    return Observed(values, checks)


def command_streams(s, d):
    r = s.commands.run("printf 'hello-out'; printf 'hello-err' >&2", cwd=d, timeout=15)
    return result(r, output=(r.stdout == "hello-out" and r.stderr == "hello-err" and r.exit_code == 0))


def nonzero(s, d):
    return s.commands.run("printf 'partial-out'; printf 'expected-error' >&2; exit 7", cwd=d, timeout=15)


def callbacks(s, d):
    out, err = [], []
    r = s.commands.run("printf first; sleep 0.05; printf second; printf problem >&2", cwd=d,
                       on_stdout=out.append, on_stderr=err.append, timeout=15)
    return result({"result": r, "stdout_chunks": out, "stderr_chunks": err,
                   "stdout_joined": "".join(out), "stderr_joined": "".join(err),
                   "stdout_count": len(out), "stderr_count": len(err)},
                  callback_content=("".join(out) == r.stdout == "firstsecond" and "".join(err) == r.stderr == "problem"))


def unicode_file(s, d):
    text = "你好，AgentCore 与 E2B！\n🙂 café\n"
    s.files.write(d + "/中文.txt", text)
    value = s.files.read(d + "/中文.txt")
    return result(value, exact_content=value == text, text_type=type(value) is str)


def binary_file(s, d):
    data = bytes(range(256)) + b"\x00\xff\x80\x00"
    s.files.write(d + "/binary.bin", data)
    value = s.files.read(d + "/binary.bin", format="bytes")
    return result(value, exact_content=bytes(value) == data, bytes_type=type(value) is bytearray)


def write_info(s, d):
    value = s.files.write(d + "/info.txt", "content")
    return result(value, name=value.name == "info.txt", path=value.path == d + "/info.txt", file_type=value.type.value == "file")


def list_metadata(s, d):
    s.files.write(d + "/a.txt", "abc")
    s.files.make_dir(d + "/child")
    entries = s.files.list(d)
    entry = next((v for v in entries if v.name == "a.txt"), None)
    return result(entries, names={v.name for v in entries} == {"a.txt", "child"},
                  file_size=entry is not None and entry.size == 3,
                  metadata_types=all(type(v.mode) is int and isinstance(v.modified_time, datetime) for v in entries))


def mkdir_idempotent(s, d):
    first, second = s.files.make_dir(d + "/new"), s.files.make_dir(d + "/new")
    return result({"first": first, "second": second}, first=first is True, second=second is False)


def remove_existing(s, d):
    s.files.write(d + "/gone", "x")
    value = s.files.remove(d + "/gone")
    return result(value, returns_none=value is None, removed=s.files.list(d) == [])


def remove_missing(s, d):
    return s.files.remove(d + "/missing")


def read_missing(s, d):
    return s.files.read(d + "/missing")


def cwd_env(s, d):
    quoted = d + "/space ' 中文"
    s.files.make_dir(quoted)
    value = "space ' \" $HOME $(printf UNSAFE) ; 中文"
    r = s.commands.run("printf '%s\\n' \"$COMPARE_VALUE\"; pwd", cwd=quoted, envs={"COMPARE_VALUE": value}, timeout=15)
    return result(r, literal_env_and_cwd=r.stdout == value + "\n" + quoted + "\n")


def chinese_command(s, d):
    r = s.commands.run("printf '中文命令🙂\\n'", cwd=d, timeout=15)
    return result(r, unicode_stdout=r.stdout == "中文命令🙂\n")


def command_file(s, d):
    s.commands.run("printf 'command-file' > created.txt", cwd=d, timeout=15)
    value = s.files.read(d + "/created.txt")
    return result(value, same_filesystem=value == "command-file")


def write_python_execute_read(s, d):
    source = "import json\nprint(json.dumps({'message': '你好', 'value': 6 * 7}, ensure_ascii=False))\n"
    written = s.files.write(d + "/script.py", source)
    command = s.commands.run("python3 script.py", cwd=d, timeout=15)
    content = s.files.read(d + "/script.py")
    return result({"write": written, "command": command, "source": content},
                  source_preserved=content == source,
                  executed=command.exit_code == 0 and json.loads(command.stdout) == {"message": "你好", "value": 42})


def overwrite_permissions(s, d):
    path = d + "/run.sh"
    s.files.write(path, "old")
    s.commands.run("chmod 750 run.sh", cwd=d, timeout=15)
    written = s.files.write(path, "new")
    entry = next(v for v in s.files.list(d) if v.name == "run.sh")
    content = s.files.read(path)
    return result({"write": written, "entry": entry, "content": content}, overwrite=content == "new", mode_preserved=entry.mode & 0o777 == 0o750)


def nested_parents(s, d):
    value = s.files.write(d + "/one/two/file", "nested")
    content = s.files.read(d + "/one/two/file")
    return result({"write": value, "content": content}, auto_parent=content == "nested")


def bytearray_write(s, d):
    value = s.files.write(d + "/buffer", bytearray(b"bytearray"))
    content = s.files.read(d + "/buffer", format="bytes")
    return result({"write": value, "content": content}, roundtrip=bytes(content) == b"bytearray")


def filelike_write(s, d):
    with io.BytesIO(b"file-like") as stream:
        value = s.files.write(d + "/stream", stream)
    content = s.files.read(d + "/stream", format="bytes")
    return result({"write": value, "content": content}, roundtrip=bytes(content) == b"file-like")


def file_request_timeout(s, d):
    value = s.files.write(d + "/timeout-option", "request", request_timeout=15)
    content = s.files.read(d + "/timeout-option", request_timeout=15)
    return result({"write": value, "content": content}, roundtrip=content == "request")


def command_request_timeout(s, d):
    r = s.commands.run("printf request-option", cwd=d, request_timeout=15, timeout=15)
    return result(r, accepted=r.stdout == "request-option")


def depth_two(s, d):
    s.files.make_dir(d + "/child")
    s.files.write(d + "/child/nested", "x")
    entries = s.files.list(d, depth=2)
    return result(entries, nested_visible=any(v.name == "nested" for v in entries))


def nonempty_remove(s, d):
    s.files.make_dir(d + "/child")
    s.files.write(d + "/child/nested", "x")
    value = s.files.remove(d + "/child")
    return result(value, removed=s.files.list(d) == [])


def symlink_read(s, d):
    s.files.write(d + "/target", "linked")
    s.commands.run("ln -s target link", cwd=d, timeout=15)
    value = s.files.read(d + "/link")
    return result(value, follows_link=value == "linked")


def relative_default(s, d):
    path = str(PurePosixPath(d).relative_to(PurePosixPath(d).parent.parent)) + "/relative.txt"
    written = s.files.write(path, "relative")
    command = s.commands.run("cat " + shlex.quote(path), timeout=15)
    content = s.files.read(path)
    return result({"write": written, "command": command, "content": content},
                  shared_default=command.stdout == content == "relative")


def timeout_side_effect(s, d):
    started = time.monotonic()
    try:
        r = s.commands.run("sleep 2; printf completed > timeout-marker", cwd=d, timeout=0.2)
        outcome = {"outcome": "returned", "value": snapshot(r)}
    except Exception as exc:
        if type(exc).__name__ not in {"TimeoutException", "CommandWaitTimeout"}:
            time.sleep(3.2)
            raise
        outcome = {"outcome": "raised", "exception": exception_snapshot(exc)}
    elapsed = time.monotonic() - started
    # Timeout is a wait deadline; do not assume that the backend cancelled work.
    # Always allow this <= 3 second remote command to finish before next case.
    time.sleep(3.2)
    entries = s.files.list(d)
    present = any(v.name == "timeout-marker" for v in entries)
    marker = s.files.read(d + "/timeout-marker") if present else None
    return Observed({"command_observation": outcome, "marker_present_after_wait": present,
                     "marker_content": marker},
                    {"timeout_raised": outcome["outcome"] == "raised", "wait_bounded": elapsed < 10},
                    business_success=False)


def timeout_zero(s, d):
    r = s.commands.run("sleep 0.2; printf timeout-zero", cwd=d, timeout=0)
    return result(r, zero_disables_deadline=r.stdout == "timeout-zero")


def invalid_utf8(s, d):
    s.files.write(d + "/invalid", b"\xff\xfe")
    return s.files.read(d + "/invalid")


CASES = [
    Case("stdout_stderr_exit_zero", command_streams),
    Case("nonzero_exception", nonzero, expected="raise_command_exit"),
    Case("callbacks_chunks_and_values", callbacks),
    Case("unicode_text_roundtrip", unicode_file),
    Case("all_bytes_roundtrip_and_type", binary_file),
    Case("write_return_fields", write_info),
    Case("list_metadata", list_metadata),
    Case("mkdir_idempotence", mkdir_idempotent),
    Case("remove_existing", remove_existing),
    Case("remove_missing", remove_missing, "boundary", "either"),
    Case("read_missing", read_missing, expected="raise_missing"),
    Case("quoted_cwd_and_env", cwd_env),
    Case("chinese_command", chinese_command),
    Case("command_write_sdk_read", command_file),
    Case("write_python_execute_read", write_python_execute_read),
    Case("overwrite_permission_preservation", overwrite_permissions, "boundary", "either"),
    Case("nested_parent_creation", nested_parents, "boundary", "either"),
    Case("bytearray_write", bytearray_write, "boundary", "either"),
    Case("filelike_write", filelike_write, "boundary", "either"),
    Case("files_request_timeout_keyword", file_request_timeout, "boundary", "either"),
    Case("commands_request_timeout_keyword", command_request_timeout),
    Case("list_depth_two", depth_two, "boundary", "either"),
    Case("remove_nonempty_directory", nonempty_remove, "boundary", "either"),
    Case("symlink_file_read", symlink_read, "boundary", "either"),
    Case("relative_path_default_directory", relative_default),
    Case("timeout_remote_side_effect", timeout_side_effect),
    Case("timeout_zero", timeout_zero),
    Case("invalid_utf8_text_read", invalid_utf8, "boundary", "either"),
]


def execute_case(sandbox, case, directory, round_number):
    observation = {"name": case.name, "category": case.category, "expected": case.expected,
                   "round": round_number, "directory": directory, "execution_subject": "comparison runner"}
    start = time.monotonic()
    stage = "isolation_directory"
    try:
        sandbox.files.make_dir(directory)
        stage = "case_function"
        value = case.function(sandbox, directory)
        checks = value.checks if isinstance(value, Observed) else {}
        business_success = value.business_success if isinstance(value, Observed) else True
        value = value.values if isinstance(value, Observed) else value
        if case.expected.startswith("raise"):
            checks["expected_exception_raised"] = False
        observation.update(outcome="returned", raw=snapshot(value), checks=checks,
                           business_operation_succeeded=business_success and all(checks.values()),
                           status="observed" if all(checks.values()) else "failed_expectation")
    except Exception as exc:
        expected = False
        checks = {}
        if stage == "case_function" and case.expected == "raise_command_exit":
            checks = {"command_exit_class": type(exc).__name__ == "CommandExitException",
                      "exit_code": getattr(exc, "exit_code", None) == 7,
                      "stdout": getattr(exc, "stdout", None) == "partial-out",
                      "stderr": getattr(exc, "stderr", None) == "expected-error"}
            expected = all(checks.values())
        elif stage == "case_function" and case.expected == "raise_missing":
            # E2B and AgentCore expose distinct exception classes/codes; retain both.
            expected = type(exc).__name__ in {"NotFoundException", "FileNotFoundException", "FileOperationError"}
            if type(exc).__name__ == "FileOperationError":
                expected = getattr(exc, "code", None) == "NOT_FOUND"
            checks = {"missing_file_exception": expected}
        boundary = (stage == "case_function" and case.expected == "either" and
                    (type(exc).__name__ in {"UnsupportedCapabilityError", "FileNotFoundException", "NotFoundException"} or
                     type(exc).__name__ == "FileOperationError" and getattr(exc, "code", None) in
                     {"PATH_VIOLATION", "NOT_FOUND", "NOT_EMPTY", "ENCODING_ERROR", "PERMISSION_DENIED"} or
                     # Upstream E2B rejects bytearray as an invalid input type.
                     # Keep this observed rejection specific to this probe.
                     case.name == "bytearray_write" and type_id(exc) == {
                         "module": "e2b.exceptions", "name": "InvalidArgumentException"}))
        observation.update(outcome="raised", raw=exception_snapshot(exc), checks=checks,
                           business_operation_succeeded=False,
                           status="expected_exception" if expected else "capability_observed" if boundary else "execution_failed",
                           failure_stage=stage)
    observation["duration_seconds"] = round(time.monotonic() - start, 6)
    dynamic = {}
    observation["normalized"] = normalize(observation["raw"], directory, dynamic)
    observation["dynamic_values"] = dynamic
    print(f"{sandbox_label(sandbox)} r{round_number} {case.name}: {observation['status']}", flush=True)
    return observation


# The runner does not need to put backend names/IDs onto SDK objects.
_LABELS = {}


def sandbox_label(sandbox):
    return _LABELS.get(id(sandbox), "backend")


def compare(left, right):
    usable = {"observed", "expected_exception", "capability_observed"}
    failure = left["status"] not in usable or right["status"] not in usable
    same_outcome = left["outcome"] == right["outcome"]
    literal = same_outcome and left["raw"] == right["raw"]
    normalized_same = same_outcome and left["normalized"] == right["normalized"]
    fields_same = same_outcome and field_projection(left["normalized"]) == field_projection(right["normalized"])
    return {"name": left["name"], "round": left["round"], "category": left["category"],
            "status": "execution_failed" if failure else "literal_equal" if literal else "normalized_same" if normalized_same else "different",
            "business_outcome": "matching_error" if same_outcome and left["outcome"] == "raised" else "both_returned" if same_outcome else "return_vs_exception",
            "literal_equal": literal, "normalized_same": normalized_same,
            "field_values_equal_ignoring_python_identity": fields_same,
            "type_identity_compatible": same_outcome and not any("python_type" in p for p in differing_paths(left["normalized"], right["normalized"], limit=1000)),
            "difference_paths": differing_paths(left["normalized"], right["normalized"]),
            "both_contract_checks_passed": all(left["checks"].values()) and all(right["checks"].values()) and not failure,
            "successful_business_operations": left.get("business_operation_succeeded", False) and right.get("business_operation_succeeded", False) and not failure}


class E2BBackend:
    def __init__(self):
        self.sandbox = None

    def create(self):
        from e2b import Sandbox
        self.sandbox = Sandbox.create(timeout=600, request_timeout=30,
                                      metadata={"purpose": "agentcore-e2b-real-comparison"})
        return self.sandbox

    def cleanup(self):
        if self.sandbox is None:
            return {"status": "no_handle", "fallback": "Any unconfirmed creation has timeout at most 600 seconds."}
        outcome = {"action": "E2B Sandbox.kill followed by is_running", "status": "failed"}
        try:
            outcome["kill_return"] = snapshot(self.sandbox.kill(request_timeout=20))
        except Exception as exc:
            outcome["kill_exception"] = exception_snapshot(exc)
        try:
            running = self.sandbox.is_running(request_timeout=20)
            outcome["is_running"] = running
            outcome["status"] = "passed" if running is False else "failed"
        except Exception as exc:
            outcome["verification_exception"] = exception_snapshot(exc)
        return outcome


class AgentCoreBackend:
    def __init__(self, state_path):
        self.state_path = state_path
        self.client = self.sandbox = self.attempt = None

    def create(self):
        from agentcore_sandbox import Client, RuntimeTarget, CreateOutcomeUnknown
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        target = RuntimeTarget(state["runtime_arn"], state["qualifier"], state["workspace"],
                               boot_file=state["boot_file"], module_path=state["module_path"])
        self.client = Client(region=state["region"], templates={"comparison": target}, read_timeout=90)
        for attempt_index in range(3):
            try:
                self.sandbox = self.client.resume_create(self.attempt) if self.attempt is not None else self.client.sandboxes.create(template="comparison")
                return self.sandbox
            except CreateOutcomeUnknown as exc:
                self.attempt = exc.attempt
                if attempt_index == 2:
                    raise
                time.sleep(2)
        raise RuntimeError("unreachable")

    def cleanup(self):
        outcome = {"status": "no_handle", "action": "AgentCore Runtime stop request (not E2B kill); local handles close"}
        try:
            if self.sandbox is not None or self.attempt is not None:
                for attempt_index in range(3):
                    try:
                        if self.sandbox is not None:
                            self.sandbox.runtime.stop()
                        else:
                            self.client.stop_attempt(self.attempt)
                        outcome["status"] = "stop_request_accepted"
                        break
                    except Exception as exc:
                        outcome["status"] = "failed"
                        outcome["exception"] = exception_snapshot(exc)
                        if attempt_index < 2:
                            time.sleep(2)
            return outcome
        finally:
            for handle in (self.sandbox, self.client):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception as exc:
                        outcome["status"] = "failed"
                        outcome["close_exception"] = exception_snapshot(exc)


def package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def run_backend(name, backend, rounds, cases=CASES):
    start = time.monotonic()
    report = {"setup": {"status": "not_started"}, "cleanup": {"status": "not_started"}, "observations": []}
    try:
        sandbox = backend.create()
        _LABELS[id(sandbox)] = name
        default = sandbox.commands.run("pwd", timeout=20).stdout.strip()
        if not default.startswith("/") or "\n" in default:
            raise ValueError("default directory is not one absolute path")
        environment = sandbox.commands.run("python3 --version; uname -m; id -un", timeout=20)
        prefix = default.rstrip("/") + "/backend-compare-" + uuid.uuid4().hex[:12]
        sandbox.files.make_dir(prefix)
        report["setup"] = {"status": "ready", "default_directory": default, "test_directory": prefix,
                           "environment_fingerprint": snapshot(environment),
                           "template": "E2B default template" if name == "e2b" else "deployment state Runtime target"}
        for round_number in range(1, rounds + 1):
            for case in cases:
                directory = prefix + f"/r{round_number}-" + case.name
                report["observations"].append(execute_case(sandbox, case, directory, round_number))
    except (Exception, KeyboardInterrupt) as exc:
        report["setup"] = {"status": "failed", "exception": exception_snapshot(exc)}
    finally:
        try:
            report["cleanup"] = backend.cleanup()
        except Exception as exc:
            report["cleanup"] = {"status": "failed", "exception": exception_snapshot(exc)}
        if getattr(backend, "sandbox", None) is not None:
            _LABELS.pop(id(backend.sandbox), None)
    report["duration_seconds"] = round(time.monotonic() - start, 6)
    return report


def compare_backends(state_path, rounds=1, *, factories=None, cases=CASES):
    start = time.monotonic()
    report = {"schema_version": 1, "execution_subject": "scripts/compare_backends.py application runner",
              "systems_under_test": {"e2b": "real E2B cloud through upstream e2b SDK",
                                     "agentcore": "real AgentCore Runtime through agentcore-e2b-sdk and adapter"},
              "started_at": datetime.now(timezone.utc).isoformat(), "rounds": rounds,
              "versions": {"python": sys.version.split()[0], "e2b": package_version("e2b"),
                           "agentcore-e2b-sdk": package_version("agentcore-e2b-sdk")},
              "normalization_rules": NORMALIZATION_RULES,
              "field_projection_note": "Diagnostic field comparison removes Python identities only; it does not determine strict status or certify type compatibility.",
              "capture_scope": "literal_equal compares the selected captured projection, not entire objects/exception contracts. Untrusted exception strings are omitted and compared through redacted SHA256 fingerprints. Callback chunks are retained for strict comparison; content checks independently verify callback semantics.",
              "case_manifest": [{"name": c.name, "category": c.category, "expected": c.expected} for c in cases],
              "backends": {}, "comparisons": [], "status": "not_started"}
    if not 1 <= rounds <= 5:
        raise ValueError("rounds must be between 1 and 5")
    if factories is None:
        if not os.environ.get("E2B_API_KEY", "").strip():
            report.update(status="skipped", reason="E2B_API_KEY is missing; neither cloud backend was invoked.")
            return report
        if report["versions"]["e2b"] != PINNED_E2B_VERSION:
            report.update(status="failed", reason="The runner requires e2b==2.51.0; neither backend was invoked.")
            return report
        factories = {"e2b": E2BBackend, "agentcore": lambda: AgentCoreBackend(state_path)}
    for name in ("e2b", "agentcore"):
        report["backends"][name] = run_backend(name, factories[name](), rounds, cases)
    left = {(x["round"], x["name"]): x for x in report["backends"]["e2b"]["observations"]}
    right = {(x["round"], x["name"]): x for x in report["backends"]["agentcore"]["observations"]}
    for key in left.keys() | right.keys():
        if key in left and key in right:
            report["comparisons"].append(compare(left[key], right[key]))
    report["comparisons"].sort(key=lambda c: (c["round"], [x.name for x in cases].index(c["name"])))
    statuses = [x["status"] for x in report["comparisons"]]
    report["summary"] = {status: statuses.count(status) for status in ("literal_equal", "normalized_same", "different", "execution_failed")}
    report["summary"]["field_values_equal"] = sum(c["field_values_equal_ignoring_python_identity"] for c in report["comparisons"])
    complete = all(b["setup"]["status"] == "ready" and b["cleanup"]["status"] in {"passed", "stop_request_accepted"}
                   and len(b["observations"]) == rounds * len(cases) for b in report["backends"].values())
    core_failures = [{"backend": name, "round": obs["round"], "case": obs["name"], "status": obs["status"]}
                     for name, backend in report["backends"].items() for obs in backend["observations"]
                     if obs["category"] == "core" and obs["status"] not in {"observed", "expected_exception"}]
    report["execution_complete"] = complete
    report["core_acceptance"] = {"status": "failed" if core_failures or not complete else "passed", "failures": core_failures}
    report["status"] = "completed_with_core_failures" if complete and core_failures else "completed" if complete else "incomplete"
    report["duration_seconds"] = round(time.monotonic() - start, 6)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def redact_configured_secret(text):
    key = os.environ.get("E2B_API_KEY", "")
    return text.replace(key, "<REDACTED>") if key else text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="Private AgentCore deployment JSON; identifiers are never copied to report")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=1, choices=range(1, 6))
    args = parser.parse_args()
    report = compare_backends(args.state, args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(redact_configured_secret(json.dumps(report, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "summary": report.get("summary", {})}), flush=True)
    return 0 if report["status"] == "completed" else 2 if report["status"] == "skipped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
