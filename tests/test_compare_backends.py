"""Offline tests of comparison truthfulness, isolation and lifecycle guarantees."""
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock, patch

import pytest

spec = importlib.util.spec_from_file_location("compare_backends", Path(__file__).resolve().parents[1] / "scripts" / "compare_backends.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class FakeSandbox:
    def __init__(self):
        self.directories = []
        self.files = types.SimpleNamespace(make_dir=self.make_dir, list=lambda p: [])
        self.commands = types.SimpleNamespace(run=self.run)

    def make_dir(self, path):
        self.directories.append(path)
        return True

    def run(self, command, **kwargs):
        if command == "pwd":
            return types.SimpleNamespace(stdout="/home/test\n", stderr="", exit_code=0)
        if command.startswith("python3 --version"):
            return types.SimpleNamespace(stdout="Python 3.12\nx86_64\ntest\n", stderr="", exit_code=0)
        raise RuntimeError("Unexpected command")


class FakeBackend:
    def __init__(self, fail_create=False):
        self.sandbox = FakeSandbox()
        self.cleaned = False
        self.fail_create = fail_create

    def create(self):
        if self.fail_create:
            raise ValueError("PRIVATE: account=123456789012 session=id key=secret")
        return self.sandbox

    def cleanup(self):
        self.cleaned = True
        return {"status": "passed"}


def call_case(function, *, expected="return", category="core", directory="/home/test/root/case"):
    return mod.execute_case(FakeSandbox(), mod.Case("test", function, category, expected), directory, 1)


def raising(exc):
    def invoke(*args, **kwargs):
        raise exc
    return invoke


def test_manifest_separates_boundary_cases_and_has_full_python_flow():
    manifest = {c.name: c for c in mod.CASES}
    assert len(manifest) >= 28
    assert manifest["write_python_execute_read"].category == "core"
    for name in ("bytearray_write", "filelike_write", "files_request_timeout_keyword", "nested_parent_creation"):
        assert manifest[name].category == "boundary"


def test_binary_snapshot_preserves_bytes_vs_bytearray():
    left, right = mod.snapshot(bytes(range(256))), mod.snapshot(bytearray(range(256)))
    assert left["hex"] == right["hex"]
    assert left["python_type"] != right["python_type"]
    assert mod.field_projection(left) == mod.field_projection(right)


def test_exception_snapshot_never_reads_message_dict_or_cause():
    class Sensitive(Exception):
        stdout = "sensitive server response"
        stderr = "sensitive transport diagnostics"
        error = "PRIVATE1234"
        remote_state_unknown = True
        def __str__(self):
            raise AssertionError("must not format exceptions")
    exc = Sensitive("secret body")
    exc.session_id = "secret session"
    captured = mod.exception_snapshot(exc)
    serialized = json.dumps(captured)
    for secret in ("sensitive", "PRIVATE1234", "secret body", "secret session"):
        assert secret not in serialized
    assert captured["fields"]["remote_state_unknown"]["value"] is True


def test_equal_length_omitted_exception_fields_have_distinct_fingerprints():
    left, right = RuntimeError(), RuntimeError()
    left.error, right.error = "first-secret", "other-secret"
    assert len(left.error) == len(right.error)
    assert mod.exception_snapshot(left) != mod.exception_snapshot(right)


def test_normalization_only_changes_allowed_metadata_and_retains_dynamic_values():
    raw = mod.snapshot({"path": "/one/case/file", "stdout": "/one/case/file", "owner": "alice", "modified_time": None})
    dynamic = {}
    normalized = mod.normalize(raw, "/one/case", dynamic)
    assert normalized["fields"]["path"]["value"] == "<CASE_DIR>/file"
    assert normalized["fields"]["stdout"]["value"] == "/one/case/file"
    assert dynamic["owner"]["value"] == "alice"
    assert normalized["fields"]["modified_time"]["python_type"]["name"] == "NoneType"


def test_directory_substring_not_normalized_without_path_boundary():
    raw = mod.snapshot({"path": "/one/cases/file"})
    assert mod.normalize(raw, "/one/case") == raw


def test_equal_values_with_different_python_types_never_strict_equal():
    left = call_case(lambda s, d: bytes([1]))
    right = call_case(lambda s, d: bytearray([1]))
    compared = mod.compare(left, right)
    assert compared["status"] == "different"
    assert compared["field_values_equal_ignoring_python_identity"] is True
    assert compared["type_identity_compatible"] is False


def test_matching_unexpected_failures_are_execution_failed_not_success():
    left = call_case(raising(ValueError("not expected")))
    compared = mod.compare(left, left)
    assert compared["status"] == "execution_failed"
    assert compared["business_outcome"] == "matching_error"
    assert not compared["successful_business_operations"]


def test_matching_expected_errors_are_negative_test_not_business_success():
    class FileNotFoundException(Exception):
        pass
    left = call_case(raising(FileNotFoundException("not present")), expected="raise_missing")
    assert left["status"] == "expected_exception"
    compared = mod.compare(left, left)
    assert compared["status"] == "literal_equal"
    assert not compared["successful_business_operations"]


def test_wrong_nonzero_fields_fail_independent_oracle():
    from agentcore_sandbox import CommandExitException
    wrong = CommandExitException("failure", exit_code=8, stdout="partial-out", stderr="expected-error")
    item = call_case(raising(wrong), expected="raise_command_exit")
    assert item["status"] == "execution_failed"
    assert item["checks"]["exit_code"] is False


def test_correct_nonzero_fields_pass_negative_oracle():
    from agentcore_sandbox import CommandExitException
    exc = CommandExitException("failure", exit_code=7, stdout="partial-out", stderr="expected-error")
    item = call_case(raising(exc), expected="raise_command_exit")
    assert item["status"] == "expected_exception"
    assert item["business_operation_succeeded"] is False


def test_matching_incorrect_return_values_are_not_success():
    left = call_case(lambda s, d: mod.result("wrong", correct_content=False))
    compared = mod.compare(left, left)
    assert compared["status"] == "execution_failed"
    assert not compared["successful_business_operations"]


def test_boundary_transport_failure_is_not_a_capability_observation():
    from agentcore_sandbox import TransportError, UnsupportedCapabilityError
    failed = call_case(raising(TransportError("sensitive")), category="boundary", expected="either")
    unsupported = call_case(raising(UnsupportedCapabilityError("unsupported")), category="boundary", expected="either")
    assert failed["status"] == "execution_failed"
    assert unsupported["status"] == "capability_observed"
    assert not unsupported["business_operation_succeeded"]


def test_bytearray_upstream_invalid_argument_is_matching_rejection_not_business_success():
    from agentcore_sandbox import UnsupportedCapabilityError
    upstream_error = type("InvalidArgumentException", (Exception,), {"__module__": "e2b.exceptions"})
    observations = []
    for exc in (upstream_error("invalid input"), UnsupportedCapabilityError("unsupported")):
        case = mod.Case("bytearray_write", raising(exc), "boundary", "either")
        observations.append(mod.execute_case(FakeSandbox(), case, "/base/case", 1))
    assert all(item["status"] == "capability_observed" for item in observations)
    compared = mod.compare(*observations)
    assert compared["status"] == "different"
    assert compared["business_outcome"] == "matching_error"
    assert not compared["successful_business_operations"]


@pytest.mark.parametrize("case_name,module_name", [
    ("filelike_write", "e2b.exceptions"),
    ("bytearray_write", "other.exceptions"),
])
def test_invalid_argument_reclassification_is_scoped_to_upstream_bytearray_probe(case_name, module_name):
    error = type("InvalidArgumentException", (Exception,), {"__module__": module_name})
    case = mod.Case(case_name, raising(error("invalid input")), "boundary", "either")
    item = mod.execute_case(FakeSandbox(), case, "/base/case", 1)
    assert item["status"] == "execution_failed"


def test_timeout_typeerror_is_execution_failure_not_a_successful_timeout():
    sandbox = FakeSandbox()
    sandbox.commands.run = raising(TypeError("wrong call"))
    with patch.object(mod.time, "sleep") as sleep:
        item = mod.execute_case(sandbox, mod.Case("timeout", mod.timeout_side_effect), "/base/case", 1)
    assert item["status"] == "execution_failed"
    assert not mod.compare(item, item)["successful_business_operations"]
    sleep.assert_called_once_with(3.2)


def test_timeout_captures_remote_side_effect_and_is_negative_business_outcome():
    from agentcore_sandbox import CommandWaitTimeout
    sandbox = FakeSandbox()
    sandbox.commands.run = raising(CommandWaitTimeout("wait", remote_state_unknown=True))
    sandbox.files.list = lambda path: [types.SimpleNamespace(name="timeout-marker")]
    sandbox.files.read = lambda path: "completed"
    with patch.object(mod.time, "sleep"):
        item = mod.execute_case(sandbox, mod.Case("timeout", mod.timeout_side_effect), "/base/case", 1)
    assert item["status"] == "observed"
    assert item["raw"]["fields"]["marker_present_after_wait"]["value"] is True
    assert not mod.compare(item, item)["successful_business_operations"]


def test_case_failure_does_not_prevent_later_case_and_cleanup():
    backend = FakeBackend()
    cases = [mod.Case("fail", raising(ValueError("failure"))), mod.Case("next", lambda s, d: "ok")]
    report = mod.run_backend("fake", backend, 2, cases)
    assert backend.cleaned
    assert len(report["observations"]) == 4
    directories = [x["directory"] for x in report["observations"]]
    assert len(set(directories)) == 4
    assert report["observations"][-1]["status"] == "observed"


def test_setup_failure_still_cleans_up_without_leaking_identifiers():
    backend = FakeBackend(fail_create=True)
    report = mod.run_backend("fake", backend, 1, [])
    assert backend.cleaned
    assert report["setup"]["status"] == "failed"
    assert "PRIVATE" not in json.dumps(report)
    assert "123456789012" not in json.dumps(report)


def test_missing_key_prevents_both_factories():
    with patch.dict(mod.os.environ, {}, clear=True), patch.object(mod.E2BBackend, "create", side_effect=AssertionError("cloud forbidden")):
        report = mod.compare_backends(Path("unused.json"))
    assert report["status"] == "skipped"
    assert report["backends"] == {}


def test_core_failure_is_separate_from_execution_completeness():
    factories = {name: FakeBackend for name in ("e2b", "agentcore")}
    report = mod.compare_backends(Path("unused"), factories=factories,
                                  cases=[mod.Case("bad", lambda s, d: mod.result("wrong", correct=False))])
    assert report["execution_complete"] is True
    assert report["core_acceptance"]["status"] == "failed"
    assert report["status"] == "completed_with_core_failures"


def test_e2b_kill_failure_still_attempts_is_running_verification():
    backend = mod.E2BBackend()
    backend.sandbox = types.SimpleNamespace(kill=Mock(side_effect=RuntimeError("private")),
                                           is_running=Mock(return_value=True))
    result = backend.cleanup()
    assert result["status"] == "failed"
    backend.sandbox.is_running.assert_called_once()
    assert "private" not in json.dumps(result)


def test_agentcore_resume_uses_same_attempt_and_stop_is_not_kill(tmp_path):
    import agentcore_sandbox as sdk
    state = tmp_path / "deployment.json"
    state.write_text(json.dumps(dict(runtime_arn="unused", qualifier="DEFAULT", workspace="/workspace",
                                    boot_file="/app/boot", module_path="/app", region="us-east-1")))
    attempt = object()
    sandbox = types.SimpleNamespace(runtime=types.SimpleNamespace(stop=Mock()), close=Mock())
    client = types.SimpleNamespace(sandboxes=types.SimpleNamespace(create=Mock(side_effect=sdk.CreateOutcomeUnknown("private", attempt=attempt))),
                                   resume_create=Mock(return_value=sandbox), close=Mock())
    with patch.object(sdk, "Client", return_value=client), patch.object(sdk, "RuntimeTarget"), patch.object(mod.time, "sleep"):
        backend = mod.AgentCoreBackend(state)
        assert backend.create() is sandbox
        cleaned = backend.cleanup()
    client.sandboxes.create.assert_called_once()
    client.resume_create.assert_called_once_with(attempt)
    sandbox.runtime.stop.assert_called_once()
    sandbox.close.assert_called_once()
    client.close.assert_called_once()
    assert cleaned["status"] == "stop_request_accepted"


def test_agentcore_stop_retries_and_closes_after_failure():
    backend = mod.AgentCoreBackend(Path("unused"))
    stop = Mock(side_effect=RuntimeError("private"))
    backend.sandbox = types.SimpleNamespace(runtime=types.SimpleNamespace(stop=stop), close=Mock())
    backend.client = types.SimpleNamespace(close=Mock())
    with patch.object(mod.time, "sleep"):
        result = backend.cleanup()
    assert result["status"] == "failed"
    assert stop.call_count == 3
    backend.sandbox.close.assert_called_once()
    backend.client.close.assert_called_once()
    assert "private" not in json.dumps(result)
