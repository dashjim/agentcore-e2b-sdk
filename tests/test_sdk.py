"""SDK contract/fault tests: the test transport, not AWS, injects each failure."""
import base64
from dataclasses import FrozenInstanceError
import io
import json
import shlex
import subprocess
import threading
import time

import pytest

from agentcore_sandbox import (
    AdapterProtocolError, CallbackError, Client, CommandExecutionTimeout,
    CommandExitException, CommandWaitTimeout, ConcurrentOperationError,
    ConfigurationError, CreateAttempt, CreateOutcomeUnknown, FileOperationError,
    FileType, OutputLimitExceededError, RuntimeTarget, SessionClosedError,
    SessionRef, SessionStateLostError, StopOutcomeUnknown, TransportError,
    UnsupportedCapabilityError, WriteOutcomeUnknown,
)
from agentcore_sandbox.client import CAPABILITIES

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/sdk_test-ABC123"


def chunk(name, body):
    return {"chunk": {name: body}}


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __iter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            if callable(event):
                event = event()
            if event is not None:
                yield event

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.boot = "boot-original"
        self.capabilities = sorted(CAPABILITIES)
        self.files = {}
        self.adapter_hook = None
        self.command_hook = None
        self.stop_hook = None
        self.closed = False
        self.last_stream = None

    def invoke_agent_runtime(self, **kwargs):
        request = json.loads(kwargs["payload"])
        self.calls.append(("invoke", kwargs, request))
        if self.adapter_hook:
            response = self.adapter_hook(request)
            if response is not None:
                if isinstance(response, bytes):
                    return dict(runtimeSessionId=kwargs["runtimeSessionId"], response=io.BytesIO(response))
                return response
        op, args = request["op"], request["args"]
        if op in {"init", "probe"}:
            result = dict(workspace="/workspace", capabilities=self.capabilities)
        elif op == "files.write":
            self.files[args["path"]] = args["data"]
            result = dict(name=args["path"].rsplit("/", 1)[-1], path=args["path"], type="file", metadata=None)
        elif op == "files.read":
            result = dict(data=self.files[args["path"]], encoding="base64")
        elif op == "files.list":
            result = [dict(name="a", path="/workspace/a", type="file", metadata=None,
                           size=3, mode=33188, permissions="rw-r--r--", owner="1000", group="1000",
                           modified_time="2026-10-12T00:00:00+00:00")]
        elif op == "files.make_dir":
            result = True
        else:
            result = None
        response = dict(version=1, request_id=request["request_id"], boot_id=self.boot, result=result)
        return dict(runtimeSessionId=kwargs["runtimeSessionId"], statusCode=200,
                    response=io.BytesIO(json.dumps(response).encode()))

    def invoke_agent_runtime_command(self, **kwargs):
        self.calls.append(("command", kwargs))
        data = json.loads(base64.b64decode(kwargs["body"]["command"].split()[-1]))
        header = "ACSBX1 " + json.dumps(dict(version=1, request_id=data["request_id"], boot_id=self.boot, ok=True)) + "\n"
        events = [chunk("contentStart", {}), chunk("contentDelta", {"stdout": header}),
                  chunk("contentDelta", {"stdout": "42\n", "stderr": "warning\n"}),
                  chunk("contentStop", {"status": "COMPLETED", "exitCode": 0})]
        if self.command_hook:
            events = self.command_hook(events, data)
        self.last_stream = Stream(events)
        return dict(runtimeSessionId=kwargs["runtimeSessionId"], statusCode=200, stream=self.last_stream)

    def stop_runtime_session(self, **kwargs):
        self.calls.append(("stop", kwargs))
        if self.stop_hook:
            result = self.stop_hook(kwargs)
            if result is not None:
                return result
        return dict(runtimeSessionId=kwargs["runtimeSessionId"], statusCode=200)

    def close(self):
        self.closed = True


@pytest.fixture
def env():
    transport = FakeTransport()
    client = Client(region="us-west-2", templates={"python": RuntimeTarget(ARN)}, transport=transport)
    sbx = client.sandboxes.create(template="python")
    yield client, sbx, transport
    client.close()


def test_reference_roundtrip_and_immutable(env):
    client, sbx, transport = env
    ref = sbx.export_ref()
    assert SessionRef.from_json(ref.to_json()) == ref
    with pytest.raises(FrozenInstanceError):
        ref.boot_id = "changed"
    before = len(transport.calls)
    connected = client.sandboxes.connect(ref)
    assert len(transport.calls) == before
    connected.commands.run("printf 42")
    assert transport.calls[-2][2]["op"] == "probe"


def test_connect_rejects_string_and_foreign_target(env):
    client, sbx, _ = env
    with pytest.raises(ConfigurationError):
        client.sandboxes.connect(sbx.sandbox_id)
    values = sbx.export_ref().to_dict()
    values["runtime_arn"] = ARN.replace("sdk_test", "foreign")
    with pytest.raises(ConfigurationError):
        client.sandboxes.connect(SessionRef.from_dict(values))


def test_command_result_callbacks_and_structured_envelope(env):
    _, sbx, transport = env
    out, err = [], []
    cmd = "printf '%s' \"$VALUE\"; echo '复杂 ; $(not executed by SDK)'"
    result = sbx.commands.run(cmd, envs={"VALUE": "中文\n' ; $x"}, on_stdout=out.append, on_stderr=err.append)
    assert (result.stdout, result.stderr, result.exit_code, result.error) == ("42\n", "warning\n", 0, None)
    assert "".join(out) == result.stdout and "".join(err) == result.stderr
    body = transport.calls[-1][1]["body"]
    envelope = json.loads(base64.b64decode(body["command"].split()[-1]))
    assert envelope["cmd"] == cmd and envelope["envs"] == {"VALUE": "中文\n' ; $x"}
    assert body["timeout"] == 300


def test_split_protocol_header_and_hidden_header(env):
    _, sbx, transport = env
    def split(events, _):
        header = events[1]["chunk"]["contentDelta"]["stdout"]
        return [events[0], *[chunk("contentDelta", {"stdout": c}) for c in header], *events[2:]]
    transport.command_hook = split
    out = []
    assert sbx.commands.run("ignored", on_stdout=out.append).stdout == "42\n"
    assert "ACSBX1" not in "".join(out)


@pytest.mark.parametrize("code", [1, 17, 126, 255])
def test_nonzero_exit_preserves_result_fields(env, code):
    _, sbx, transport = env
    def fail(events, _):
        events[-1]["chunk"]["contentStop"]["exitCode"] = code
        return events
    transport.command_hook = fail
    with pytest.raises(CommandExitException) as error:
        sbx.commands.run("ignored")
    assert error.value.exit_code == code
    assert error.value.stdout == "42\n" and error.value.stderr == "warning\n"


@pytest.mark.parametrize("mode", ["missing_stop", "duplicate_stop", "after_stop_exception", "io_failure", "unknown_status", "missing_code", "no_header", "wrong_nonce", "wrong_version", "no_start", "duplicate_start"])
def test_bad_stream_never_succeeds_or_replays(env, mode):
    _, sbx, transport = env
    def mutate(events, _):
        if mode == "missing_stop": return events[:-1]
        if mode == "duplicate_stop": return events + [events[-1]]
        if mode == "after_stop_exception": return events + [{"internalServerException": {"message": "injected"}}]
        if mode == "io_failure": return events[:3] + [OSError("injected disconnect")]
        if mode == "unknown_status": events[-1]["chunk"]["contentStop"]["status"] = "UNKNOWN"
        if mode == "missing_code": del events[-1]["chunk"]["contentStop"]["exitCode"]
        if mode == "no_header": return [events[0], *events[2:]]
        if mode in {"wrong_nonce", "wrong_version"}:
            head = json.loads(events[1]["chunk"]["contentDelta"]["stdout"][7:])
            head["request_id" if mode == "wrong_nonce" else "version"] = "wrong"
            events[1] = chunk("contentDelta", {"stdout": "ACSBX1 " + json.dumps(head) + "\n"})
        if mode == "no_start": return events[1:]
        if mode == "duplicate_start": return [events[0], *events]
        return events
    transport.command_hook = mutate
    with pytest.raises((AdapterProtocolError, TransportError)):
        sbx.commands.run("ignored")
    assert sum(x[0] == "command" for x in transport.calls) == 1


def test_service_execution_timeout(env):
    _, sbx, transport = env
    def expire(events, _):
        events[-1] = chunk("contentStop", {"status": "TIMED_OUT"})
        return events
    transport.command_hook = expire
    with pytest.raises(CommandExecutionTimeout) as error:
        sbx.commands.run("ignored")
    assert error.value.stdout == "42\n" and error.value.exit_code is None


@pytest.mark.parametrize("stage", ["invoke", "silent_stream", "connect_probe"])
def test_wait_deadline_covers_blocked_io(env, stage):
    client, sbx, transport = env
    blocker = threading.Event()
    if stage == "invoke":
        def hook(events, _):
            blocker.wait(2)
            return events
        transport.command_hook = hook
    elif stage == "silent_stream":
        def hook(events, _):
            return [*events[:2], lambda: blocker.wait(2), *events[2:]]
        transport.command_hook = hook
    else:
        sbx = client.sandboxes.connect(sbx.export_ref())
        def probe(request):
            if request["op"] == "probe": blocker.wait(2)
        transport.adapter_hook = probe
    started = time.monotonic()
    try:
        with pytest.raises(CommandWaitTimeout) as error:
            sbx.commands.run("ignored", timeout=0.05)
        assert time.monotonic() - started < 0.7
        assert error.value.remote_state_unknown
    finally:
        blocker.set()


def test_callback_failure_preserves_cause_and_partial_output(env):
    _, sbx, _ = env
    def callback(_):
        raise LookupError("customer callback")
    with pytest.raises(CallbackError) as error:
        sbx.commands.run("ignored", on_stdout=callback)
    assert isinstance(error.value.__cause__, LookupError)
    assert error.value.stdout == "42\n" and error.value.remote_state_unknown


def test_output_limit_returns_partial_output_not_success(env):
    client, sbx, _ = env
    client.max_output_bytes = 4
    with pytest.raises(OutputLimitExceededError) as error:
        sbx.commands.run("ignored")
    assert error.value.stdout == "42\n" and error.value.remote_state_unknown


def test_same_session_reentrant_callback_is_rejected(env):
    client, sbx, _ = env
    second = client.sandboxes.connect(sbx.export_ref())
    errors = []
    def callback(_):
        try:
            second.probe()
        except ConcurrentOperationError as error:
            errors.append(error)
    sbx.commands.run("ignored", on_stdout=callback)
    assert len(errors) == 1


def test_different_sessions_can_operate_in_callback(env):
    client, sbx, _ = env
    other = client.sandboxes.create(template="python")
    results = []
    sbx.commands.run("ignored", on_stdout=lambda _: results.append(other.probe()))
    assert len(results) == 1


def test_boot_mismatch_blocks_file_and_command(env):
    _, sbx, transport = env
    transport.boot = "boot-restarted"
    with pytest.raises(SessionStateLostError): sbx.probe()
    with pytest.raises(SessionStateLostError): sbx.files.write("a", "value")
    with pytest.raises(SessionStateLostError): sbx.commands.run("ignored")


def test_lost_create_can_resume_same_id():
    transport = FakeTransport()
    client = Client(region="us-west-2", templates={"python": RuntimeTarget(ARN)}, transport=transport)
    def fail(_): raise OSError("lost response")
    transport.adapter_hook = fail
    with pytest.raises(CreateOutcomeUnknown) as error:
        client.sandboxes.create(template="python")
    attempt = error.value.attempt
    assert CreateAttempt.from_json(attempt.to_json()) == attempt
    with pytest.raises(ConfigurationError): client.sandboxes.connect(attempt)
    transport.adapter_hook = None
    sbx = client.resume_create(attempt)
    assert sbx.sandbox_id == attempt.runtime_session_id
    assert transport.calls[0][1]["runtimeSessionId"] == transport.calls[1][1]["runtimeSessionId"]
    client.close()


def test_lost_stop_reuses_token_and_blocks_other_handles(env):
    client, sbx, transport = env
    second = client.sandboxes.connect(sbx.export_ref())
    def fail(_): raise OSError("lost stop response")
    transport.stop_hook = fail
    with pytest.raises(StopOutcomeUnknown) as error: sbx.stop()
    assert sbx.state == "STOP_UNKNOWN" and error.value.client_token
    with pytest.raises(SessionClosedError): second.commands.run("ignored")
    transport.stop_hook = None
    receipt = sbx.stop()
    assert receipt.client_token == error.value.client_token and receipt.state == "STOP_REQUESTED"
    stop_calls = [x for x in transport.calls if x[0] == "stop"]
    assert stop_calls[0][1]["clientToken"] == stop_calls[1][1]["clientToken"]


def test_close_is_local_only(env):
    client, sbx, transport = env
    sbx.close()
    with pytest.raises(SessionClosedError): sbx.commands.run("ignored")
    assert not any(c[0] == "stop" for c in transport.calls)
    client.close()
    assert transport.closed


def test_file_unicode_bytes_typed_metadata_and_directories(env):
    _, sbx, _ = env
    info = sbx.files.write("hello.txt", "中文\n")
    assert info.type is FileType.FILE and info.metadata is None
    assert sbx.files.read("hello.txt") == "中文\n"
    raw = bytes([0, 128, 255])
    sbx.files.write("binary", raw)
    assert sbx.files.read("binary", format="bytes") == bytearray(raw)
    with pytest.raises(FileOperationError) as error: sbx.files.read("binary")
    assert error.value.code == "ENCODING_ERROR"
    entry = sbx.files.list(".")[0]
    assert entry.modified_time.tzinfo is not None and entry.type is FileType.FILE
    assert sbx.files.make_dir("nested") is True
    assert sbx.files.remove("nested") is None


@pytest.mark.parametrize("path", ["../bad", "/etc/passwd", "a/../../bad", "", "a\x00b"])
def test_local_path_rejection_does_not_invoke(env, path):
    _, sbx, transport = env
    before = len(transport.calls)
    with pytest.raises(FileOperationError): sbx.files.write(path, "x")
    assert len(transport.calls) == before


@pytest.mark.parametrize("bad", [b"not json", b'{}'])
def test_write_bad_ack_is_unknown_without_replay(env, bad):
    _, sbx, transport = env
    transport.adapter_hook = lambda _: bad
    with pytest.raises(WriteOutcomeUnknown) as error: sbx.files.write("a", "x")
    assert error.value.remote_state_unknown
    assert sum(c[0] == "invoke" and c[2]["op"] == "files.write" for c in transport.calls) == 1


def test_file_size_preflight(env):
    _, sbx, transport = env
    before = len(transport.calls)
    with pytest.raises(FileOperationError) as error: sbx.files.write("a", b"x" * (1024 * 1024 + 1))
    assert error.value.code == "FILE_TOO_LARGE" and len(transport.calls) == before


@pytest.mark.parametrize("operation", [
    lambda c,s: s.commands.run("x", background=True),
    lambda c,s: s.commands.run("x", stdin=True),
    lambda c,s: s.commands.run("x", user="root"),
    lambda c,s: s.commands.run("x", typo=True),
    lambda c,s: c.sandboxes.create(template="python", timeout=60),
    lambda c,s: c.sandboxes.create(template="python", envs={"X":"1"}),
    lambda c,s: s.files.list(".", depth=2),
    lambda c,s: s.files.write("a", bytearray(b"x")),
    lambda c,s: s.kill(), lambda c,s: s.pause(), lambda c,s: s.pty.create(),
    lambda c,s: s.set_timeout(100), lambda c,s: s.run_code("1+1"),
    lambda c,s: c.sandboxes.list(),
])
def test_unsupported_options_rejected_before_network(env, operation):
    client, sbx, transport = env
    before = len(transport.calls)
    with pytest.raises(UnsupportedCapabilityError): operation(client, sbx)
    assert len(transport.calls) == before


def test_connected_session_negotiates_missing_capability(env):
    client, sbx, transport = env
    connected = client.sandboxes.connect(sbx.export_ref())
    transport.capabilities.remove("commands")
    with pytest.raises(UnsupportedCapabilityError): connected.commands.run("ignored")
    assert not any(c[0] == "command" for c in transport.calls)


@pytest.mark.parametrize("value", [0, None])
def test_zero_or_none_disables_wait_not_execution_limit(env, value):
    _, sbx, transport = env
    sbx.commands.run("ignored", timeout=value, execution_timeout=120, request_timeout=0.001)
    assert transport.calls[-1][1]["body"]["timeout"] == 120


@pytest.mark.parametrize("kwargs", [{"execution_timeout":0},{"execution_timeout":3601},{"timeout":-1},{"timeout":float('nan')},{"envs":{"BAD-KEY":"x"}},{"cmd":"x"*65536}])
def test_invalid_command_preflight(env, kwargs):
    _, sbx, transport = env
    options = {"cmd": "ignored", **kwargs}
    before = len(transport.calls)
    with pytest.raises(ValueError): sbx.commands.run(**options)
    assert len(transport.calls) == before


@pytest.mark.parametrize("operation", ["files.write", "files.make_dir", "files.remove"])
def test_invalid_mutation_ack_is_an_unknown_outcome(env, operation):
    _, sbx, transport = env
    def bad(request):
        return json.dumps(dict(version=1, request_id=request["request_id"], boot_id=transport.boot,
                               result={"unexpected": "response"})).encode()
    transport.adapter_hook = bad
    with pytest.raises((WriteOutcomeUnknown, AdapterProtocolError)) as error:
        if operation == "files.write": sbx.files.write("a", "x")
        elif operation == "files.make_dir": sbx.files.make_dir("a")
        else: sbx.files.remove("a")
    assert error.value.remote_state_unknown


def test_aws_response_session_mismatch_is_rejected(env):
    _, sbx, transport = env
    transport.adapter_hook = lambda _: dict(runtimeSessionId="different", response=io.BytesIO(b"{}"))
    with pytest.raises(AdapterProtocolError): sbx.probe()


def test_stop_attempt_after_lost_create_retains_original_session():
    transport = FakeTransport()
    client = Client(region="us-west-2", templates={"python": RuntimeTarget(ARN)}, transport=transport)
    def fail(_): raise OSError("lost init response")
    transport.adapter_hook = fail
    with pytest.raises(CreateOutcomeUnknown) as error: client.sandboxes.create(template="python")
    receipt = client.stop_attempt(error.value.attempt)
    assert receipt.runtime_session_id == error.value.attempt.runtime_session_id
    client.close()


def test_aws_optional_session_echo_may_be_absent_in_all_operations():
    class OptionalEchoTransport(FakeTransport):
        def invoke_agent_runtime(self, **kwargs):
            response = super().invoke_agent_runtime(**kwargs)
            response.pop("runtimeSessionId")
            return response

        def invoke_agent_runtime_command(self, **kwargs):
            response = super().invoke_agent_runtime_command(**kwargs)
            response.pop("runtimeSessionId")
            return response

        def stop_runtime_session(self, **kwargs):
            response = super().stop_runtime_session(**kwargs)
            response.pop("runtimeSessionId")
            return response

    transport = OptionalEchoTransport()
    with Client(region="us-west-2", templates={"python": RuntimeTarget(ARN)}, transport=transport) as client:
        sbx = client.sandboxes.create(template="python")
        assert sbx.commands.run("ignored").stdout == "42\n"
        assert sbx.stop().state == "STOP_REQUESTED"


@pytest.mark.parametrize("connected", [False, True])
def test_delayed_worker_does_not_send_after_caller_timeout(env, monkeypatch, connected):
    """A real thread starts after cancellation; neither probe nor command runs."""
    import agentcore_sandbox.commands as command_module
    client, sbx, transport = env
    if connected:
        sbx = client.sandboxes.connect(sbx.export_ref())
    gate = threading.Event()
    finished = threading.Event()
    original_thread = threading.Thread

    def delayed_thread(*args, **kwargs):
        original_target = kwargs["target"]
        if kwargs.get("name") == "agentcore-command-stream":
            def target():
                gate.wait(2)
                try:
                    original_target()
                finally:
                    finished.set()
            kwargs["target"] = target
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(command_module.threading, "Thread", delayed_thread)
    before = len(transport.calls)
    try:
        with pytest.raises(CommandWaitTimeout): sbx.commands.run("ignored", timeout=0.02)
    finally:
        gate.set()
    assert finished.wait(1)
    assert len(transport.calls) == before


def test_stream_cleanup_waits_for_final_events_to_be_enqueued(env):
    """Queue backpressure and early producer cleanup preserve all final output."""
    _, sbx, transport = env
    def many(events, _):
        return [*events[:2], *[chunk("contentDelta", {"stdout": f"{i},"}) for i in range(128)], events[-1]]
    transport.command_hook = many
    result = sbx.commands.run("ignored")
    assert result.stdout == "".join(f"{i}," for i in range(128))


def test_empty_deltas_before_header_remain_valid_and_bounded(env):
    _, sbx, transport = env
    def empty(events, _):
        return [events[0], *[chunk("contentDelta", {}) for _ in range(1000)], *events[1:]]
    transport.command_hook = empty
    assert sbx.commands.run("ignored").stdout == "42\n"


def test_deployment_defaults_preserve_original_constructor_and_ref(env):
    _, sbx, _ = env
    target = RuntimeTarget(ARN)
    assert target.boot_file == "/app/run/boot.json" and target.module_path is None
    value = sbx.export_ref().to_dict()
    value.pop("boot_file")
    value.pop("module_path")
    ref = SessionRef.from_dict(value)
    assert ref.boot_file == target.boot_file and ref.module_path is None


def test_command_deployment_env_is_shell_quoted_and_roundtrips(tmp_path):
    marker = tmp_path / "must-not-be-created"
    boot_file = "/run/boot '$(touch " + str(marker) + ")'.json"
    module_path = "/var/task'; touch " + str(marker) + "; #"
    target = RuntimeTarget(ARN, boot_file=boot_file, module_path=module_path)
    transport = FakeTransport()
    with Client(region="us-west-2", templates={"python": target}, transport=transport) as client:
        sbx = client.sandboxes.create(template="python")
        ref = SessionRef.from_json(sbx.export_ref().to_json())
        assert (ref.boot_file, ref.module_path) == (boot_file, module_path)
        sbx.commands.run("ignored")
        command = transport.calls[-1][1]["body"]["command"]
        argv = shlex.split(command)
        assert argv[:4] == ["env", "AGENTCORE_SANDBOX_WORKSPACE=/workspace",
                           "AGENTCORE_SANDBOX_BOOT_FILE=" + boot_file, "PYTHONPATH=" + module_path]
        # Exercise the actual shell parser on the generated prefix; the test
        # replaces only the helper executable with a harmless env reporter.
        prefix = command.rsplit(" python3 -m agentcore_sandbox.exec_guard ", 1)[0]
        script = "import os,json;print(json.dumps([os.environ['AGENTCORE_SANDBOX_BOOT_FILE'],os.environ['PYTHONPATH']]))"
        result = subprocess.run(prefix + " python3 -c " + shlex.quote(script), shell=True,
                                check=True, capture_output=True, text=True)
        assert json.loads(result.stdout) == [boot_file, module_path]
        assert not marker.exists()


@pytest.mark.parametrize("field,new_value", [("boot_file", "/run/other-boot.json"), ("module_path", "/var/task")])
def test_reference_cannot_mix_deployment_configuration(env, field, new_value):
    client, sbx, transport = env
    record = sbx.export_ref().to_dict()
    record[field] = new_value
    before = len(transport.calls)
    with pytest.raises(ConfigurationError): client.sandboxes.connect(SessionRef.from_dict(record))
    assert len(transport.calls) == before


@pytest.mark.parametrize("kwargs", [{"boot_file": "relative.json"}, {"module_path": "relative"},
    {"boot_file": "/workspace/boot.json"}, {"boot_file": "/workspace"},
    {"boot_file": "/run/../boot.json"}, {"module_path": "/var/task\x00oops"}])
def test_invalid_deployment_paths_rejected_locally(kwargs):
    with pytest.raises(ConfigurationError): RuntimeTarget(ARN, **kwargs)


def test_same_endpoint_cannot_have_conflicting_deployment_paths():
    transport = FakeTransport()
    with pytest.raises(ConfigurationError):
        Client(region="us-west-2", templates={"a": RuntimeTarget(ARN),
            "b": RuntimeTarget(ARN, module_path="/var/task")}, transport=transport)
    assert not transport.calls


def test_missing_helper_header_preserves_stderr_without_callbacks(env):
    _, sbx, transport = env
    diagnostic = "ModuleNotFoundError: No module named 'agentcore_sandbox'\n"
    transport.command_hook = lambda events, _: [events[0],
        chunk("contentDelta", {"stderr": diagnostic}),
        chunk("contentStop", {"status": "COMPLETED", "exitCode": 1})]
    out, err = [], []
    with pytest.raises(AdapterProtocolError) as error:
        sbx.commands.run("ignored", on_stdout=out.append, on_stderr=err.append)
    assert error.value.stderr == diagnostic
    assert error.value.stdout == "" and out == [] and err == []


def test_missing_helper_header_stderr_diagnostic_remains_bounded(env):
    _, sbx, transport = env
    transport.command_hook = lambda events, _: [events[0],
        chunk("contentDelta", {"stderr": "错" * 10000}), events[-1]]
    with pytest.raises(AdapterProtocolError) as error: sbx.commands.run("ignored")
    assert 0 < len(error.value.stderr.encode("utf-8")) <= 8192
    assert error.value.stdout == ""
