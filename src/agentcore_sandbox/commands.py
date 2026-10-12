"""Foreground commands, bounded streaming and an end-to-end wait deadline."""
import base64
import json
import math
import queue
import re
import shlex
import threading
import time
import uuid

from .client import reject_options
from .exceptions import (AdapterProtocolError, CallbackError, CommandExecutionTimeout,
    CommandExitException, CommandWaitTimeout, OutputLimitExceededError, SandboxError,
    SessionStateLostError, TransportError, UnsupportedCapabilityError)
from .models import CommandResult

HEADER_PREFIX = "ACSBX1 "
MAX_HEADER_BYTES = 8192


def _close_async(stream):
    # Closing a socket or a caller-supplied stream may block. Deadline reporting
    # must not wait for that cleanup; a daemon also closes late invoke results.
    def close():
        try:
            stream.close()
        except Exception:
            pass
    if hasattr(stream, "close"):
        threading.Thread(target=close, daemon=True, name="agentcore-stream-close").start()


class Commands:
    def __init__(self, sandbox):
        self._sandbox = sandbox

    def run(self, cmd, *, cwd=None, envs=None, on_stdout=None, on_stderr=None,
            background=False, user=None, stdin=False, timeout=60,
            request_timeout=None, execution_timeout=300, **kwargs):
        """Run Bash in the Runtime. `request_timeout` is compatibility-only.

        `timeout` is the caller's total wait budget; None/0 disables that budget.
        Runtime separately enforces execution_timeout (1–3600 seconds). A caller
        timeout or callback failure does not prove remote process cancellation.
        Callbacks run synchronously; SDK cannot interrupt arbitrary Python
        callback code. Applications must keep callbacks nonblocking.
        """
        reject_options(kwargs)
        if background or stdin or user is not None:
            raise UnsupportedCapabilityError("SDK supports foreground commands without custom user/stdin only")
        if not isinstance(cmd, str) or not cmd or "\x00" in cmd:
            raise ValueError("cmd must be a nonempty string without NUL")
        if type(execution_timeout) is not int or not 1 <= execution_timeout <= 3600:
            raise ValueError("execution_timeout must be an integer between 1 and 3600")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                                    not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be nonnegative, finite, or None")
        for callback in (on_stdout, on_stderr):
            if callback is not None and not callable(callback):
                raise ValueError("Output callbacks must be callable")
        if envs is None:
            envs = {}
        if not isinstance(envs, dict) or any(not isinstance(k, str) or
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or
                not isinstance(v, str) or "\x00" in v for k, v in envs.items()):
            raise ValueError("envs must map shell variable names to strings without NUL")
        sbx = self._sandbox
        cwd = sbx.files._path(cwd or sbx._ref.workspace)
        request_id = str(uuid.uuid4())
        envelope = dict(version=1, request_id=request_id, expected_boot_id=sbx._ref.boot_id,
                        cmd=cmd, cwd=cwd, envs=envs)
        encoded = base64.b64encode(json.dumps(envelope, ensure_ascii=False).encode()).decode("ascii")
        # Native command invocations can start with a different cwd/env from the
        # Runtime application (notably CodeZip). Pass explicit deployment paths.
        argv = ["env", "AGENTCORE_SANDBOX_WORKSPACE=" + sbx._ref.workspace,
                "AGENTCORE_SANDBOX_BOOT_FILE=" + sbx._ref.boot_file]
        if sbx._ref.module_path is not None:
            argv.append("PYTHONPATH=" + sbx._ref.module_path)
        argv.extend(["python3", "-m", "agentcore_sandbox.exec_guard", encoded])
        command = " ".join(shlex.quote(arg) for arg in argv)
        if len(command.encode("utf-8")) > 64 * 1024:
            raise ValueError("Encoded command exceeds the SDK's 64 KiB limit")
        with sbx._operation("commands", negotiate=False):
            return self._stream(command, request_id, execution_timeout, timeout, on_stdout, on_stderr)

    def _stream(self, command, request_id, execution_timeout, timeout, on_stdout, on_stderr):
        client = self._sandbox._client
        ref = self._sandbox._ref
        deadline = time.monotonic() + timeout if timeout else None
        messages = queue.Queue(maxsize=8)
        cancelled = threading.Event()
        holder = []

        def put(item):
            while not cancelled.is_set():
                try:
                    messages.put(item, timeout=0.05)
                    return
                except queue.Full:
                    pass

        def receive():
            stream = None
            try:
                # A daemon may first get CPU after its caller already timed out.
                # Do not initiate a probe or command in that delayed worker.
                if cancelled.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    return
                if self._sandbox._capabilities is None:
                    self._sandbox._negotiate()
                if "commands" not in self._sandbox._capabilities:
                    raise UnsupportedCapabilityError("Adapter does not advertise commands")
                if cancelled.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    return
                response = client._transport.invoke_agent_runtime_command(**client._address(ref),
                    contentType="application/json", accept="application/vnd.amazon.eventstream",
                    body={"command": command, "timeout": execution_timeout})
                stream = response.get("stream") if isinstance(response, dict) else None
                if stream is not None:
                    holder.append(stream)
                if cancelled.is_set():
                    return
                client._check_response(response, ref)
                if stream is None:
                    raise AdapterProtocolError("AWS command response has no stream")
                for event in stream:
                    if cancelled.is_set():
                        return
                    put(("event", event))
                put(("end", None))
            except Exception as exc:
                put(("error", exc))
            finally:
                if stream is not None:
                    _close_async(stream)

        threading.Thread(target=receive, daemon=True, name="agentcore-command-stream").start()
        stdout, stderr = [], []
        header_buffer = ""
        pending_stderr = []
        pending_stderr_size = 0
        header_valid = False
        output_size = 0
        stop = None
        started = False

        def output(kind, value):
            nonlocal output_size
            if not value:
                return
            size = len(value.encode("utf-8"))
            if output_size + size > client.max_output_bytes:
                raise OutputLimitExceededError("Command output exceeds the configured limit", remote_state_unknown=True)
            output_size += size
            (stdout if kind == "stdout" else stderr).append(value)
            callback = on_stdout if kind == "stdout" else on_stderr
            if callback:
                try:
                    callback(value)
                except Exception as exc:
                    raise CallbackError("Output callback failed; SDK did not cancel the remote command",
                                        remote_state_unknown=True) from exc

        try:
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise CommandWaitTimeout("SDK wait deadline expired; remote state is unknown", remote_state_unknown=True)
                try:
                    kind, value = messages.get(timeout=remaining)
                except queue.Empty:
                    raise CommandWaitTimeout("SDK wait deadline expired; remote state is unknown", remote_state_unknown=True) from None
                if kind == "error":
                    if isinstance(value, SandboxError):
                        raise value
                    raise TransportError("Command transport failed; SDK did not replay the command", remote_state_unknown=True) from value
                if kind == "end":
                    break
                if not isinstance(value, dict) or len(value) != 1 or "chunk" not in value:
                    raise TransportError("AWS command stream contains an exception or unknown event", remote_state_unknown=True)
                chunk = value["chunk"]
                if not isinstance(chunk, dict) or len(chunk) != 1:
                    raise AdapterProtocolError("Command chunk must contain exactly one event", remote_state_unknown=True)
                event, data = next(iter(chunk.items()))
                if not isinstance(data, dict):
                    raise AdapterProtocolError("Command event data must be an object", remote_state_unknown=True)
                if stop is not None:
                    raise AdapterProtocolError("SDK received an event after contentStop", remote_state_unknown=True)
                if event == "contentStart":
                    if started:
                        raise AdapterProtocolError("SDK received duplicate contentStart", remote_state_unknown=True)
                    started = True
                elif event == "contentDelta":
                    if not started:
                        raise AdapterProtocolError("contentDelta preceded contentStart", remote_state_unknown=True)
                    if any(k not in {"stdout", "stderr"} or not isinstance(v, str) for k, v in data.items()):
                        raise AdapterProtocolError("Command output contains invalid fields", remote_state_unknown=True)
                    out, err = data.get("stdout", ""), data.get("stderr", "")
                    if not header_valid:
                        header_buffer += out
                        if err:
                            encoded_err = err.encode("utf-8")
                            available = MAX_HEADER_BYTES - pending_stderr_size
                            if len(encoded_err) > available:
                                pending_stderr.append(encoded_err[:available].decode("utf-8", errors="ignore"))
                                raise AdapterProtocolError("Command helper pre-header stderr exceeds 8 KiB", remote_state_unknown=True)
                            pending_stderr.append(err)
                            pending_stderr_size += len(encoded_err)
                        line, sep, remainder = header_buffer.partition("\n")
                        if len(line.encode("utf-8")) > MAX_HEADER_BYTES or pending_stderr_size > MAX_HEADER_BYTES:
                            raise AdapterProtocolError("Command helper protocol header exceeds 8 KiB", remote_state_unknown=True)
                        if not sep:
                            continue
                        try:
                            head = json.loads(line[len(HEADER_PREFIX):]) if line.startswith(HEADER_PREFIX) else None
                        except ValueError as exc:
                            raise AdapterProtocolError("Command helper header is not JSON", remote_state_unknown=True) from exc
                        if (not isinstance(head, dict) or type(head.get("version")) is not int or head.get("version") != 1 or
                                head.get("request_id") != request_id or not isinstance(head.get("ok"), bool)):
                            raise AdapterProtocolError("Command helper header is invalid", remote_state_unknown=True)
                        if head.get("boot_id") != ref.boot_id:
                            raise SessionStateLostError("Command helper rejected a changed boot_id")
                        if not head["ok"]:
                            error = head.get("error", {})
                            if not isinstance(error, dict):
                                raise AdapterProtocolError("Command helper error is invalid")
                            if error.get("code") in {"SESSION_STATE_LOST", "BOOT_ID_MISMATCH"}:
                                raise SessionStateLostError(error.get("message", "Command helper rejected boot_id"))
                            raise AdapterProtocolError(error.get("message", "Command helper rejected the command"), code=error.get("code"))
                        header_valid = True
                        output("stdout", remainder)
                        for part in pending_stderr:
                            output("stderr", part)
                        header_buffer = ""
                        pending_stderr = []
                    else:
                        output("stdout", out)
                        output("stderr", err)
                elif event == "contentStop":
                    if not started:
                        raise AdapterProtocolError("contentStop preceded contentStart", remote_state_unknown=True)
                    stop = data
                else:
                    raise AdapterProtocolError("Unknown command event", remote_state_unknown=True)
            if not header_valid or stop is None:
                raise AdapterProtocolError("Command stream ended without a valid helper header and terminal event", remote_state_unknown=True)
            status, code = stop.get("status"), stop.get("exitCode")
            if code is not None and type(code) is not int:
                raise AdapterProtocolError("Command exitCode must be an integer", remote_state_unknown=True)
            if status == "TIMED_OUT":
                raise CommandExecutionTimeout("Runtime command execution timed out", exit_code=code)
            if status != "COMPLETED" or code is None:
                raise AdapterProtocolError("Command stream has an invalid terminal status or exitCode", remote_state_unknown=True)
            if code != 0:
                raise CommandExitException(f"Command exited with code {code}", exit_code=code)
            return CommandResult("".join(stdout), "".join(stderr), code)
        except SandboxError as exc:
            exc.stdout = "".join(stdout)
            # A missing helper/header can itself be diagnosed by Python/shell
            # stderr. Preserve that bounded diagnostic in the exception without
            # invoking callbacks or exposing a partial protocol header as stdout.
            exc.stderr = "".join(stderr) + ("".join(pending_stderr) if not header_valid else "")
            raise
        finally:
            cancelled.set()
            if holder:
                _close_async(holder[0])

    def connect(self, *args, **kwargs):
        raise UnsupportedCapabilityError("SDK does not implement background process handles")

    def list(self, **kwargs):
        raise UnsupportedCapabilityError("SDK does not implement process listing")
