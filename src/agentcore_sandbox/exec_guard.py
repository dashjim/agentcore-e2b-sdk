"""Versioned pre-exec handshake used by InvokeAgentRuntimeCommand.

Runtime transports this helper's stdout/stderr; the SDK removes only the first
protocol line. The helper never reserves a customer command's exit code.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import pwd
import re
import sys
import uuid

from .adapter import (
    Adapter, AdapterError, DEFAULT_BOOT_FILE, DEFAULT_WORKSPACE,
    _mapped_os_error, read_boot_identity,
)


HEADER_PREFIX = "ACSBX1 "
MAX_ENVELOPE_BYTES = 64 * 1024
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _header(value: dict) -> None:
    sys.stdout.write(HEADER_PREFIX + json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _execution_environment(owner_uid: int, owner_gid: int, supplied_env: dict[str, str]) -> dict[str, str]:
    """Match the HTTP application's account before opening cwd or executing.

    This adapts Runtime's default launch identity; arbitrary customer shell
    access in this same container remains outside this helper's trust boundary.
    Explicit envs retain their public API meaning after account defaults apply.
    """
    current_uid = os.geteuid()
    if current_uid != owner_uid and current_uid != 0:
        raise AdapterError("PERMISSION_DENIED", "The execution helper cannot match the HTTP application's Unix user")
    try:
        account = pwd.getpwuid(owner_uid)
    except KeyError as exc:
        raise AdapterError("PERMISSION_DENIED", "The HTTP application's Unix account is unavailable") from exc
    if current_uid == 0 and owner_uid != 0:
        os.setgroups([])
        os.setgid(owner_gid)
        os.setuid(owner_uid)
        if os.geteuid() != owner_uid or os.getegid() != owner_gid:
            raise AdapterError("PERMISSION_DENIED", "The execution helper could not match the HTTP application's Unix identity")
    environment = os.environ.copy()
    environment.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    environment.update(supplied_env)
    return environment


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    header = {"version": 1, "request_id": None, "boot_id": None, "ok": False}
    try:
        if len(arguments) != 1 or len(arguments[0]) > MAX_ENVELOPE_BYTES:
            raise AdapterError("INVALID_REQUEST", "The execution helper requires one bounded Base64 envelope")
        try:
            request = json.loads(base64.b64decode(arguments[0], validate=True))
        except (binascii.Error, ValueError, UnicodeError) as exc:
            raise AdapterError("INVALID_REQUEST", "The execution helper could not decode its envelope") from exc
        if not isinstance(request, dict):
            raise AdapterError("INVALID_REQUEST", "The execution helper requires an object envelope")
        if isinstance(request.get("request_id"), str) and len(request["request_id"]) <= 128:
            header["request_id"] = request["request_id"]
        if type(request.get("version")) is not int or request["version"] != 1:
            raise AdapterError("PROTOCOL_ERROR", "The execution helper requires protocol version 1")
        try:
            if not header["request_id"] or str(uuid.UUID(header["request_id"])) != header["request_id"]:
                raise ValueError("invalid request ID")
        except ValueError as exc:
            raise AdapterError("INVALID_REQUEST", "The execution helper requires a canonical UUID request ID") from exc
        if set(request) - {"version", "request_id", "expected_boot_id", "cmd", "cwd", "envs"}:
            raise AdapterError("INVALID_REQUEST", "The execution helper rejects unsupported arguments")
        boot_file = os.environ.get("AGENTCORE_SANDBOX_BOOT_FILE", DEFAULT_BOOT_FILE)
        boot_id, owner_uid, owner_gid = read_boot_identity(boot_file)
        header["boot_id"] = boot_id
        if request.get("expected_boot_id") != boot_id:
            raise AdapterError("SESSION_STATE_LOST", "The container generation no longer matches the session reference")
        cmd = request.get("cmd")
        if not isinstance(cmd, str) or "\0" in cmd:
            raise AdapterError("INVALID_REQUEST", "The execution helper requires a command string without NUL bytes")
        supplied_env = request.get("envs")
        if supplied_env is None:
            supplied_env = {}
        if not isinstance(supplied_env, dict) or any(
            not isinstance(key, str) or not _ENV_NAME.fullmatch(key)
            or not isinstance(value, str) or "\0" in value
            for key, value in supplied_env.items()
        ):
            raise AdapterError("INVALID_REQUEST", "The execution helper requires valid environment names and string values")
        workspace = os.environ.get("AGENTCORE_SANDBOX_WORKSPACE", DEFAULT_WORKSPACE)
        environment = _execution_environment(owner_uid, owner_gid, supplied_env)
        adapter = Adapter(workspace, boot_file)
        cwd = request.get("cwd")
        if cwd is None:
            cwd = workspace
        with adapter.open_directory(cwd) as descriptor:
            os.fchdir(descriptor)
    except AdapterError as exc:
        header["error"] = {"code": exc.code, "message": exc.message}
        _header(header)
        return 125
    except OSError as exc:
        mapped = _mapped_os_error(exc)
        header["error"] = {"code": mapped.code, "message": mapped.message}
        _header(header)
        return 125
    except (TypeError, ValueError, UnicodeError):
        header["error"] = {"code": "INVALID_REQUEST", "message": "The execution helper rejected its envelope"}
        _header(header)
        return 125
    header["ok"] = True
    _header(header)
    try:
        os.execvpe("/bin/bash", ["/bin/bash", "-l", "-c", cmd], environment)
    except OSError:
        # The handshake has already been emitted. Do not emit another header or
        # any request contents; Runtime still reports the actual launch failure.
        sys.stderr.write("The execution helper could not start /bin/bash\n")
        return 127
    return 127  # exec replaces the process; this branch is unreachable.


if __name__ == "__main__":
    raise SystemExit(main())
