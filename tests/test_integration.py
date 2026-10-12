"""Real local SDK/Adapter/exec_guard integration, without an AWS service.

The test transport wraps real local subprocess output in synthetic AWS-shaped
messages. These tests establish agreement among our Python components, not AWS
wire compatibility, cloud session isolation, IAM, or actual Runtime lifecycle.
"""
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from agentcore_sandbox import Client, RuntimeTarget, SessionRef
from agentcore_sandbox.adapter import Adapter, initialize_boot_file
from agentcore_sandbox.exceptions import (
    CallbackError, CommandExitException, ConcurrentOperationError, CreateOutcomeUnknown,
    FileOperationError, SessionClosedError, SessionStateLostError, StopOutcomeUnknown,
)


@dataclass
class Binding:
    target: RuntimeTarget
    adapter: Adapter
    boot_file: Path


class LocalEventStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class BotoLikeLocalTransport:
    """Test-only transport; no claims about live AWS service compatibility.

    Each configured target has a separate real directory and boot marker. The
    transport rejects a second session on the same local target rather than
    manufacturing a false proof of AWS per-session container isolation.
    """
    def __init__(self, bindings):
        self.bindings = {(b.target.runtime_arn, b.target.qualifier): b for b in bindings}
        self.sessions = {}
        self.calls = []
        self.closed = False
        self.command_calls = 0
        self.stop_calls = []

    def _binding(self, request):
        key = request['agentRuntimeArn'], request['qualifier']
        binding = self.bindings[key]
        previous = self.sessions.setdefault(key, request['runtimeSessionId'])
        assert previous == request['runtimeSessionId'], 'Use a separate target/workdir for each local session'
        return binding

    def invoke_agent_runtime(self, **request):
        binding = self._binding(request)
        payload = json.loads(request['payload'])
        self.calls.append(payload['op'])
        response = binding.adapter.handle(payload)
        return {'runtimeSessionId': request['runtimeSessionId'], 'statusCode': 200,
                'response': io.BytesIO(json.dumps(response).encode())}

    def invoke_agent_runtime_command(self, **request):
        binding = self._binding(request)
        self.command_calls += 1
        argv = shlex.split(request['body']['command'])
        assert argv[0] == 'env'
        assert argv[-4:-1] == ['python3', '-m', 'agentcore_sandbox.exec_guard']
        argv[-4] = sys.executable
        env = os.environ.copy()
        # Native Runtime commands need not inherit the application's cwd/env.
        for key in ('PYTHONPATH', 'AGENTCORE_SANDBOX_BOOT_FILE', 'AGENTCORE_SANDBOX_WORKSPACE'):
            env.pop(key, None)
        completed = subprocess.run(argv, cwd='/', env=env,
                                   capture_output=True, text=True, timeout=10)
        events = [{'chunk': {'contentStart': {}}}]
        # Emit stderr before the helper handshake to cover SDK pre-header buffering.
        if completed.stderr:
            events.append({'chunk': {'contentDelta': {'stderr': completed.stderr}}})
        # Split actual helper output across arbitrary boundaries, including the header.
        for start in range(0, len(completed.stdout), 13):
            events.append({'chunk': {'contentDelta': {'stdout': completed.stdout[start:start + 13]}}})
        events.append({'chunk': {'contentStop': {'status': 'COMPLETED', 'exitCode': completed.returncode}}})
        return {'runtimeSessionId': request['runtimeSessionId'], 'statusCode': 200,
                'stream': LocalEventStream(events)}

    def stop_runtime_session(self, **request):
        self._binding(request)
        self.stop_calls.append(request)
        # This only models receipt acceptance; it does not simulate container destruction.
        return {'runtimeSessionId': request['runtimeSessionId'], 'statusCode': 200}

    def close(self):
        self.closed = True


@pytest.fixture
def local_stack(tmp_path):
    bindings = []
    for name in ('first', 'second'):
        root = tmp_path / name
        boot_file = root / 'boot.json'
        initialize_boot_file(str(boot_file))
        target = RuntimeTarget(
            f'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test_{name}',
            workspace=str(root / 'workspace'),
            boot_file=str(boot_file),
            module_path=str(Path(__file__).resolve().parents[1] / 'src'),
        )
        bindings.append(Binding(target, Adapter(target.workspace, str(boot_file)), boot_file))
    transport = BotoLikeLocalTransport(bindings)
    client = Client(region='us-west-2', templates={
        'first': bindings[0].target, 'second': bindings[1].target,
    }, transport=transport)
    yield client, transport, bindings
    client.close()


def test_write_python_run_read_and_directory_metadata(local_stack):
    client, transport, bindings = local_stack
    sbx = client.sandboxes.create(template='first')
    script = "from pathlib import Path\nPath('result.txt').write_text('结果🙂', encoding='utf-8')\nprint('你好🙂')\n"
    info = sbx.files.write('job.py', script)
    assert info.path == str(Path(bindings[0].target.workspace) / 'job.py')
    assert sbx.files.read('job.py') == script
    result = sbx.commands.run('python3 job.py')
    assert (result.stdout, result.stderr, result.exit_code) == ('你好🙂\n', '', 0)
    assert sbx.files.read('result.txt') == '结果🙂'
    entries = {entry.name: entry for entry in sbx.files.list('.')}
    assert set(entries) == {'job.py', 'result.txt'}
    assert entries['result.txt'].size == len('结果🙂'.encode())
    assert entries['result.txt'].modified_time.tzinfo is not None
    assert transport.command_calls == 1


def test_binary_files_directory_idempotence_and_remove(local_stack):
    sbx = local_stack[0].sandboxes.create(template='first')
    assert sbx.files.make_dir('nested') is True
    assert sbx.files.make_dir('nested') is False
    payload = bytes(range(256)) + b'\x00\xff'
    sbx.files.write('nested/payload.bin', payload)
    actual = sbx.files.read('nested/payload.bin', format='bytes')
    assert isinstance(actual, bytearray) and actual == payload
    with pytest.raises(FileOperationError) as exc:
        sbx.files.read('nested/payload.bin')
    assert exc.value.code == 'ENCODING_ERROR'
    with pytest.raises(FileOperationError) as exc:
        sbx.files.remove('nested')
    assert exc.value.code == 'NOT_EMPTY'
    sbx.files.remove('nested/payload.bin')
    sbx.files.remove('nested')
    assert sbx.files.list('.') == []


def test_callbacks_receive_user_output_and_nonzero_keeps_fields(local_stack):
    sbx = local_stack[0].sandboxes.create(template='first')
    stdout, stderr = [], []
    with pytest.raises(CommandExitException) as exc:
        sbx.commands.run("printf '结果🙂'; printf 'problem' >&2; exit 7",
                         on_stdout=stdout.append, on_stderr=stderr.append)
    assert (exc.value.stdout, exc.value.stderr, exc.value.exit_code) == ('结果🙂', 'problem', 7)
    assert ''.join(stdout) == '结果🙂'
    assert ''.join(stderr) == 'problem'
    assert 'ACSBX1 ' not in ''.join(stdout)


def test_callback_failure_does_not_replay_command(local_stack):
    client, transport, bindings = local_stack
    sbx = client.sandboxes.create(template='first')
    def fail(_):
        raise RuntimeError('application callback failed')
    with pytest.raises(CallbackError) as exc:
        sbx.commands.run("printf x >> count; printf out", on_stdout=fail)
    assert exc.value.remote_state_unknown is True
    assert transport.command_calls == 1
    assert sbx.files.read('count') == 'x'


def test_cwd_and_envs_reach_actual_shell(local_stack):
    client, _, bindings = local_stack
    sbx = client.sandboxes.create(template='first')
    sbx.files.make_dir('working')
    value = "literal $(touch should-not-exist) ' 中文"
    result = sbx.commands.run('printf "%s\\n" "$SDK_VALUE"; pwd',
                              cwd='working', envs={'SDK_VALUE': value})
    assert result.stdout == value + '\n' + bindings[0].target.workspace + '/working\n'
    assert sbx.files.list('working') == []


def test_serialized_ref_reconnects_to_same_real_workspace(local_stack):
    client, _, _ = local_stack
    original = client.sandboxes.create(template='first')
    original.files.write('persistent.txt', 'same-session')
    ref = SessionRef.from_json(original.export_ref().to_json())
    original.close()
    reconnected = client.sandboxes.connect(ref)
    assert reconnected.files.read('persistent.txt') == 'same-session'
    assert reconnected.commands.run('cat persistent.txt').stdout == 'same-session'


def test_generation_change_rejects_file_and_prevents_command(local_stack):
    client, _, bindings = local_stack
    sbx = client.sandboxes.create(template='first')
    sbx.files.write('preserved.txt', 'before')
    initialize_boot_file(str(bindings[0].boot_file))
    with pytest.raises(SessionStateLostError):
        sbx.files.write('preserved.txt', 'after')
    with pytest.raises(SessionStateLostError):
        sbx.commands.run('touch must-not-run')
    workspace = Path(bindings[0].target.workspace)
    assert (workspace / 'preserved.txt').read_text() == 'before'
    assert not (workspace / 'must-not-run').exists()


def test_symlink_file_boundary_is_enforced_by_real_adapter(local_stack, tmp_path):
    client, _, bindings = local_stack
    sbx = client.sandboxes.create(template='first')
    outside = tmp_path / 'outside.txt'
    outside.write_text('preserve')
    (Path(bindings[0].target.workspace) / 'linked.txt').symlink_to(outside)
    for operation in (lambda: sbx.files.read('linked.txt'),
                      lambda: sbx.files.write('linked.txt', 'replacement'),
                      lambda: sbx.files.remove('linked.txt')):
        with pytest.raises(FileOperationError) as exc:
            operation()
        assert exc.value.code == 'PATH_VIOLATION'
    assert outside.read_text() == 'preserve'


def test_local_bindings_have_separate_workspaces_without_claiming_cloud_isolation(local_stack):
    client, _, _ = local_stack
    first = client.sandboxes.create(template='first')
    second = client.sandboxes.create(template='second')
    first.files.write('same-name.txt', 'first')
    second.files.write('same-name.txt', 'second')
    assert first.commands.run('cat same-name.txt').stdout == 'first'
    assert second.commands.run('cat same-name.txt').stdout == 'second'
    assert first.export_ref().boot_id != second.export_ref().boot_id


def test_stop_receipt_blocks_all_shared_handles_close_is_local_only(local_stack):
    client, transport, _ = local_stack
    first = client.sandboxes.create(template='first')
    shared = client.sandboxes.connect(first.export_ref())
    receipt = first.stop()
    assert receipt.state == 'STOP_REQUESTED'
    assert first.state == shared.state == 'STOP_REQUESTED'
    with pytest.raises(SessionClosedError):
        shared.files.read('anything.txt')
    assert len(transport.stop_calls) == 1
    first.close()
    shared.close()
    client.close()
    assert transport.closed is True
    assert len(transport.stop_calls) == 1


def test_closing_unused_handle_and_client_never_stops_remote(local_stack):
    client, transport, _ = local_stack
    sbx = client.sandboxes.create(template='first')
    sbx.close()
    with pytest.raises(SessionClosedError):
        sbx.commands.run('echo never')
    assert transport.command_calls == 0
    client.close()
    assert transport.stop_calls == []


def test_real_initialization_response_loss_resumes_original_attempt(local_stack, monkeypatch):
    client, transport, bindings = local_stack
    preserved = Path(bindings[0].target.workspace) / 'before-init.txt'
    preserved.write_text('keep')
    original_invoke = transport.invoke_agent_runtime
    addresses = []

    def lose_first_response(**request):
        addresses.append(request['runtimeSessionId'])
        response = original_invoke(**request)
        if len(addresses) == 1:
            response['response'].close()
            raise ConnectionError('Response lost after Adapter accepted init')
        return response

    monkeypatch.setattr(transport, 'invoke_agent_runtime', lose_first_response)
    with pytest.raises(CreateOutcomeUnknown) as exc:
        client.sandboxes.create(template='first')
    attempt = exc.value.attempt
    sbx = client.resume_create(attempt)
    assert addresses == [attempt.runtime_session_id, attempt.runtime_session_id]
    assert sbx.sandbox_id == attempt.runtime_session_id
    assert sbx.files.read('before-init.txt') == 'keep'


def test_stop_lost_response_reuses_token_and_blocks_operations(local_stack, monkeypatch):
    client, transport, _ = local_stack
    sbx = client.sandboxes.create(template='first')
    original_stop = transport.stop_runtime_session

    def lose_first_response(**request):
        response = original_stop(**request)
        if len(transport.stop_calls) == 1:
            raise ConnectionError('Stop receipt lost')
        return response

    monkeypatch.setattr(transport, 'stop_runtime_session', lose_first_response)
    with pytest.raises(StopOutcomeUnknown) as exc:
        sbx.stop()
    assert sbx.state == 'STOP_UNKNOWN'
    with pytest.raises(SessionClosedError):
        sbx.commands.run('touch must-not-run')
    receipt = sbx.stop()
    assert receipt.state == 'STOP_REQUESTED'
    assert len(transport.stop_calls) == 2
    assert {call['clientToken'] for call in transport.stop_calls} == {exc.value.client_token}
    assert transport.command_calls == 0


def test_callback_reentry_rejects_same_session_mutation(local_stack):
    client, transport, _ = local_stack
    sbx = client.sandboxes.create(template='first')
    shared = client.sandboxes.connect(sbx.export_ref())
    reentry_errors = []

    def try_reentry(_):
        try:
            shared.files.write('must-not-write', 'blocked')
        except ConcurrentOperationError as exc:
            reentry_errors.append(exc)

    assert sbx.commands.run('printf out', on_stdout=try_reentry).stdout == 'out'
    assert reentry_errors
    assert 'files.write' not in transport.calls
    assert sbx.files.list('.') == []
