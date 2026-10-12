"""Tests execute the real file adapter, execution helper, and HTTP handler.

Run pytest with --basetemp under the workspace's tmp directory.
"""

import base64
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

from agentcore_sandbox.adapter import Adapter, MAX_BODY_BYTES, MAX_FILE_BYTES, initialize_boot_file
from agentcore_sandbox.server import handler_for
from agentcore_sandbox.exec_guard import _execution_environment
from agentcore_sandbox.adapter import AdapterError, read_boot_identity


@pytest.fixture
def sandbox_adapter(tmp_path):
    workspace = tmp_path / "workspace"
    boot_file = tmp_path / "run" / "boot.json"
    initialize_boot_file(str(boot_file))
    return Adapter(str(workspace), str(boot_file))


def call(adapter, op, **args):
    return adapter.handle({"protocol": "agentcore-sandbox", "version": 1,
                           "request_id": str(uuid.uuid4()), "expected_boot_id": adapter.boot_id,
                           "op": op, "args": args})


def write(adapter, path, data):
    return call(adapter, "files.write", path=path, data=base64.b64encode(data).decode(), encoding="base64")


def test_init_is_repeatable_without_resetting_workspace(sandbox_adapter):
    adapter = sandbox_adapter
    write(adapter, "keep.txt", b"existing customer data")
    result = call(adapter, "init", workspace=adapter.workspace)
    assert result["result"]["workspace"] == adapter.workspace
    assert "commands" in result["result"]["capabilities"]
    assert call(adapter, "init")["boot_id"] == result["boot_id"]
    assert Path(adapter.workspace, "keep.txt").read_bytes() == b"existing customer data"
    assert call(adapter, "probe")["result"]["capabilities"] == result["result"]["capabilities"]


@pytest.mark.parametrize("payload", [b"", "你好，AgentCore 🌍\n".encode(), bytes(range(256)), b"x" * MAX_FILE_BYTES])
def test_binary_unicode_and_limit_roundtrip(sandbox_adapter, payload):
    adapter = sandbox_adapter
    result = write(adapter, "nested/中文.txt", payload)
    assert result["result"] == {"name": "中文.txt", "type": "file", "path": adapter.workspace + "/nested/中文.txt", "metadata": None}
    result = call(adapter, "files.read", path="nested/中文.txt")
    assert result["result"]["encoding"] == "base64"
    assert base64.b64decode(result["result"]["data"]) == payload
    assert not list(Path(adapter.workspace, "nested").glob(".acsbx-write-*"))


def test_entry_info_matches_real_filesystem(sandbox_adapter):
    adapter = sandbox_adapter
    write(adapter, "hello.txt", b"42\n")
    os.chmod(Path(adapter.workspace, "hello.txt"), 0o640)
    item = call(adapter, "files.list", path=adapter.workspace)["result"][0]
    assert item["size"] == 3
    assert item["mode"] == 0o640
    assert item["permissions"] == "rw-r-----"
    assert item["owner"] and item["group"]
    assert item["modified_time"].endswith("+00:00")
    assert "result" in write(adapter, "hello.txt", b"rewritten")
    assert stat.S_IMODE(Path(adapter.workspace, "hello.txt").stat().st_mode) == 0o640


def test_boot_marker_cannot_live_in_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    marker = workspace / "boot.json"
    initialize_boot_file(str(marker))
    with pytest.raises(ValueError, match="outside"):
        Adapter(str(workspace), str(marker))


def test_boot_identity_uses_real_file_ownership(sandbox_adapter):
    marker = Path(sandbox_adapter.boot_file)
    assert read_boot_identity(str(marker)) == (sandbox_adapter.boot_id, marker.stat().st_uid, marker.stat().st_gid)


def test_root_helper_drops_groups_gid_uid_before_using_account_environment(monkeypatch):
    from agentcore_sandbox import exec_guard
    identity = {"uid": 0, "gid": 0}
    calls = []
    monkeypatch.setattr(exec_guard.os, "geteuid", lambda: identity["uid"])
    monkeypatch.setattr(exec_guard.os, "getegid", lambda: identity["gid"])
    monkeypatch.setattr(exec_guard.os, "setgroups", lambda groups: calls.append(("groups", groups)))
    def set_gid(gid):
        calls.append(("gid", gid))
        identity["gid"] = gid
    def set_uid(uid):
        calls.append(("uid", uid))
        identity["uid"] = uid
    monkeypatch.setattr(exec_guard.os, "setgid", set_gid)
    monkeypatch.setattr(exec_guard.os, "setuid", set_uid)
    monkeypatch.setattr(exec_guard.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="http-user", pw_dir="/home/http-user"))
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("AGENTCORE_SANDBOX_BOOT_FILE", "/dev/shm/runtime/run/boot.json")
    environment = _execution_environment(991, 992, {"TEST_VALUE": "customer"})
    assert calls == [("groups", []), ("gid", 992), ("uid", 991)]
    assert environment["HOME"] == "/home/http-user"
    assert environment["USER"] == environment["LOGNAME"] == "http-user"
    assert environment["AGENTCORE_SANDBOX_BOOT_FILE"] == "/dev/shm/runtime/run/boot.json"
    assert environment["TEST_VALUE"] == "customer"


def test_nonroot_helper_rejects_other_marker_owner(monkeypatch):
    from agentcore_sandbox import exec_guard
    monkeypatch.setattr(exec_guard.os, "geteuid", lambda: 10001)
    with pytest.raises(AdapterError) as caught:
        _execution_environment(991, 991, {})
    assert caught.value.code == "PERMISSION_DENIED"


def test_directory_create_remove_contract(sandbox_adapter):
    adapter = sandbox_adapter
    assert call(adapter, "files.make_dir", path="a/b")["result"] is True
    assert call(adapter, "files.make_dir", path="a/b")["result"] is False
    assert call(adapter, "files.remove", path="a")["error"]["code"] == "NOT_EMPTY"
    assert call(adapter, "files.remove", path="a/b")["result"] is None
    assert call(adapter, "files.remove", path="a")["result"] is None
    assert call(adapter, "files.remove", path=adapter.workspace)["error"]["code"] == "PATH_VIOLATION"


@pytest.mark.parametrize("op,args,code", [
    ("files.read", {"path": "missing"}, "NOT_FOUND"),
    ("files.read", {"path": "."}, "IS_DIRECTORY"),
    ("files.write", {"path": "a", "encoding": "base64", "data": "???"}, "INVALID_REQUEST"),
    ("files.write", {"path": "a", "encoding": "utf8", "data": "text"}, "INVALID_REQUEST"),
    ("files.list", {"path": ".", "depth": 2}, "UNSUPPORTED_CAPABILITY"),
    ("files.list", {"path": ".", "depth": True}, "UNSUPPORTED_CAPABILITY"),
    ("files.remove", {"path": "missing"}, "NOT_FOUND"),
    ("files.make_dir", {"path": "../outside"}, "PATH_VIOLATION"),
    ("files.read", {"path": "/etc/passwd"}, "PATH_VIOLATION"),
    ("files.read", {"path": "a/../../b"}, "PATH_VIOLATION"),
    ("files.read", {"path": "bad\0path"}, "INVALID_REQUEST"),
    ("files.read", {"path": []}, "INVALID_REQUEST"),
])
def test_invalid_file_requests(sandbox_adapter, op, args, code):
    assert call(sandbox_adapter, op, **args)["error"]["code"] == code


def test_size_limits_reject_read_and_write(sandbox_adapter):
    adapter = sandbox_adapter
    assert write(adapter, "too-big", b"x" * (MAX_FILE_BYTES + 1))["error"]["code"] == "FILE_TOO_LARGE"
    Path(adapter.workspace, "too-big").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    assert call(adapter, "files.read", path="too-big")["error"]["code"] == "FILE_TOO_LARGE"


@pytest.mark.parametrize("op", ["files.read", "files.write", "files.list", "files.make_dir", "files.remove"])
def test_all_operations_reject_symlink_components(sandbox_adapter, tmp_path, op):
    adapter = sandbox_adapter
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret").write_bytes(b"do not modify")
    Path(adapter.workspace, "link").symlink_to(external, target_is_directory=True)
    args = {"path": "link/secret"}
    if op == "files.write":
        args.update(data="eA==", encoding="base64")
    assert call(adapter, op, **args)["error"]["code"] == "PATH_VIOLATION"
    assert (external / "secret").read_bytes() == b"do not modify"


def test_direct_symlink_and_fifo_rejected(sandbox_adapter, tmp_path):
    adapter = sandbox_adapter
    external = tmp_path / "external"
    external.write_bytes(b"secret")
    Path(adapter.workspace, "link").symlink_to(external)
    assert call(adapter, "files.read", path="link")["error"]["code"] == "PATH_VIOLATION"
    assert write(adapter, "link", b"changed")["error"]["code"] == "PATH_VIOLATION"
    assert call(adapter, "files.list", path=".")["error"]["code"] == "PATH_VIOLATION"
    os.mkfifo(Path(adapter.workspace, "fifo"))
    assert call(adapter, "files.read", path="fifo")["error"]["code"] == "PATH_VIOLATION"


def test_generation_mismatch_prevents_file_side_effect(sandbox_adapter):
    adapter = sandbox_adapter
    request = {"protocol": "agentcore-sandbox", "version": 1, "request_id": str(uuid.uuid4()),
               "expected_boot_id": adapter.boot_id, "op": "files.write",
               "args": {"path": "never-created", "encoding": "base64", "data": "eA=="}}
    new_boot = initialize_boot_file(adapter.boot_file)
    response = adapter.handle(request)
    assert response["boot_id"] == new_boot
    assert response["error"]["code"] == "SESSION_STATE_LOST"
    assert not Path(adapter.workspace, "never-created").exists()


def execute_guard(adapter, request):
    environment = os.environ.copy()
    environment["AGENTCORE_SANDBOX_BOOT_FILE"] = adapter.boot_file
    environment["AGENTCORE_SANDBOX_WORKSPACE"] = adapter.workspace
    # Keep subprocess imports stable when pytest itself was invoked via PYTHONPATH.
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    encoded = base64.b64encode(json.dumps(request).encode()).decode()
    completed = subprocess.run([sys.executable, "-m", "agentcore_sandbox.exec_guard", encoded], env=environment, capture_output=True, text=True, timeout=10)
    line, stdout = completed.stdout.split("\n", 1)
    assert line.startswith("ACSBX1 ")
    return completed, json.loads(line[7:]), stdout


def guard_request(adapter, **changes):
    return {"version": 1, "request_id": str(uuid.uuid4()), "expected_boot_id": adapter.boot_id,
            "cmd": "printf output", "cwd": adapter.workspace, "envs": {}, **changes}


def test_exec_preserves_exit_code_stdout_and_stderr(sandbox_adapter):
    result, header, stdout = execute_guard(sandbox_adapter, guard_request(sandbox_adapter, cmd="printf hello; printf error >&2; exit 37"))
    assert header["ok"] is True
    assert header["boot_id"] == sandbox_adapter.boot_id
    assert stdout == "hello"
    assert result.stderr == "error"
    assert result.returncode == 37


def test_exec_does_not_interpolate_environment_values(sandbox_adapter):
    value = '$(touch SHOULD_NOT_EXIST); "quoted"\n中文'
    command = 'printf "%s" "$SDK_TEST_VALUE"; printf "\\n%s" "$PWD"'
    result, header, stdout = execute_guard(sandbox_adapter, guard_request(sandbox_adapter, cmd=command, envs={"SDK_TEST_VALUE": value}))
    assert header["ok"] is True
    assert result.returncode == 0
    assert stdout == value + "\n" + sandbox_adapter.workspace
    assert not Path(sandbox_adapter.workspace, "SHOULD_NOT_EXIST").exists()


@pytest.mark.parametrize("changes,code", [
    ({"expected_boot_id": str(uuid.uuid4())}, "SESSION_STATE_LOST"),
    ({"cwd": "/etc"}, "PATH_VIOLATION"),
    ({"envs": {"BAD=NAME": "x"}}, "INVALID_REQUEST"),
    ({"envs": {"GOOD": 3}}, "INVALID_REQUEST"),
    ({"version": 2}, "PROTOCOL_ERROR"),
])
def test_exec_rejects_before_customer_command(sandbox_adapter, changes, code):
    request = guard_request(sandbox_adapter, cmd="touch NEVER_EXECUTED", **changes)
    result, header, stdout = execute_guard(sandbox_adapter, request)
    assert header["ok"] is False
    assert header["error"]["code"] == code
    assert stdout == ""
    assert result.returncode != 0
    assert not Path(sandbox_adapter.workspace, "NEVER_EXECUTED").exists()


def test_http_ping_invocation_and_bounded_error(sandbox_adapter):
    adapter = sandbox_adapter
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(adapter))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        assert request("GET", "/ping")[1]["status"] == "Healthy"
        data = {"protocol": "agentcore-sandbox", "version": 1, "request_id": str(uuid.uuid4()), "op": "init", "args": {"workspace": adapter.workspace}}
        status, response = request("POST", "/invocations", json.dumps(data), {"Content-Type": "application/json"})
        assert status == 200 and response["request_id"] == data["request_id"]
        status, response = request("POST", "/invocations", "CUSTOMER_SECRET_BAD_JSON", {"Content-Type": "application/json"})
        assert status == 400
        assert "CUSTOMER_SECRET" not in json.dumps(response)
        status, response = request("POST", "/invocations", "", {"Content-Type": "application/json", "Content-Length": str(MAX_BODY_BYTES + 1)})
        assert status == 413
        assert response["error"]["code"] == "REQUEST_TOO_LARGE"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
