"""Explicit clients, template allowlists and adapter transport."""
from contextlib import contextmanager
import json
import threading
from types import MappingProxyType
import uuid

from .exceptions import (AdapterProtocolError, ConfigurationError, ConcurrentOperationError,
    CreateOutcomeUnknown, FileOperationError, SessionClosedError, SessionStateLostError,
    StopOutcomeUnknown, TransportError, UnsupportedCapabilityError, WriteOutcomeUnknown)
from .models import CreateAttempt, RuntimeTarget, SessionRef, StopReceipt, validate_target

CAPABILITIES = frozenset({"commands", "files.read", "files.write", "files.list", "files.make_dir", "files.remove", "probe"})
MAX_RESPONSE = 2 * 1024 * 1024


def reject_options(options):
    if options:
        raise UnsupportedCapabilityError("Unsupported parameters: " + ", ".join(sorted(options)))


class _SessionState:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "READY"
        self.stop_token = None


class Client:
    """IAM/SigV4 client; injected transports obey boto3's public method shapes.

    SDK never retries commands or file mutations. The default botocore client
    uses a 3,660-second socket read timeout and one total attempt. `close()` only
    closes local connections, including an injected transport.
    """

    def __init__(self, *, region, templates, transport=None, max_output_bytes=8 * 1024 * 1024,
                 max_file_bytes=1024 * 1024, connect_timeout=10, read_timeout=3660):
        if not isinstance(region, str) or not region:
            raise ConfigurationError("region is required")
        if not templates or not isinstance(templates, dict):
            raise ConfigurationError("templates must be a nonempty mapping")
        for key, value in templates.items():
            if not isinstance(key, str) or not key or not isinstance(value, RuntimeTarget):
                raise ConfigurationError("templates must map names to RuntimeTarget values")
            validate_target(region, value.runtime_arn, value.qualifier, value.workspace)
        # A CreateAttempt intentionally omits deployment paths; each target has
        # exactly one configured deployment so resuming it is unambiguous.
        locations = {}
        for value in templates.values():
            key = value.runtime_arn, value.qualifier
            location = value.workspace, value.boot_file, value.module_path
            if key in locations and locations[key] != location:
                raise ConfigurationError("A Runtime Endpoint must have one configured workspace and deployment")
            locations[key] = location
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ConfigurationError("max_output_bytes must be a positive integer")
        if type(max_file_bytes) is not int or not 1 <= max_file_bytes <= 1024 * 1024:
            raise ConfigurationError("max_file_bytes must be 1–1048576")
        self.region = region
        self.templates = MappingProxyType(dict(templates))
        self.max_output_bytes = max_output_bytes
        self.max_file_bytes = max_file_bytes
        if transport is None:
            import boto3
            from botocore.config import Config
            transport = boto3.client("bedrock-agentcore", region_name=region,
                config=Config(connect_timeout=connect_timeout, read_timeout=read_timeout,
                              retries={"total_max_attempts": 1, "mode": "standard"}))
        self._transport = transport
        self._closed = False
        self._sessions = {}
        self._registry_lock = threading.Lock()
        self.sandboxes = Sandboxes(self)

    def _ensure_open(self):
        if self._closed:
            raise SessionClosedError("Client is closed")

    def _state(self, record):
        key = (record.runtime_arn, record.qualifier, record.runtime_session_id)
        with self._registry_lock:
            return self._sessions.setdefault(key, _SessionState())

    def _target(self, record):
        if record.region != self.region:
            raise ConfigurationError("Reference region is outside this client")
        for target in self.templates.values():
            if (target.runtime_arn, target.qualifier) == (record.runtime_arn, record.qualifier):
                if isinstance(record, SessionRef) and (target.workspace, target.boot_file, target.module_path) != (record.workspace, record.boot_file, record.module_path):
                    raise ConfigurationError("Reference workspace/deployment does not match configured target")
                return target
        raise ConfigurationError("Reference target is outside the configured template allowlist")

    @contextmanager
    def _claim(self, record, *, allow_stop=False):
        self._ensure_open()
        state = self._state(record)
        if not state.lock.acquire(blocking=False):
            raise ConcurrentOperationError("This client already has an active operation for the session")
        try:
            if not allow_stop and state.state != "READY":
                raise SessionClosedError(f"Session handle state is {state.state}; normal operations are disabled")
            yield state
        finally:
            state.lock.release()

    @staticmethod
    def _address(record):
        return dict(agentRuntimeArn=record.runtime_arn, qualifier=record.qualifier,
                    runtimeSessionId=record.runtime_session_id)

    def _request(self, record, op, args):
        try:
            return self._request_once(record, op, args)
        except (FileOperationError, SessionStateLostError):
            raise
        except Exception as exc:
            if op == "files.write":
                raise WriteOutcomeUnknown("SDK could not confirm file write; read back before retrying",
                                          remote_state_unknown=True) from exc
            if op in {"files.remove", "files.make_dir"} and isinstance(exc, (TransportError, AdapterProtocolError)):
                exc.remote_state_unknown = True
            raise

    def _request_once(self, record, op, args):
        request_id = str(uuid.uuid4())
        request = dict(protocol="agentcore-sandbox", version=1, request_id=request_id, op=op, args=args)
        if isinstance(record, SessionRef):
            request["expected_boot_id"] = record.boot_id
        body = None
        try:
            response = self._transport.invoke_agent_runtime(**self._address(record),
                contentType="application/json", accept="application/json",
                payload=json.dumps(request, ensure_ascii=False).encode("utf-8"))
            self._check_response(response, record)
            body = response["response"]
            data = body.read(MAX_RESPONSE + 1) if hasattr(body, "read") else body
            if not isinstance(data, (str, bytes, bytearray)) or len(data) > MAX_RESPONSE:
                raise AdapterProtocolError("Adapter response is invalid or exceeds 2 MiB")
            document = json.loads(data)
        except (AdapterProtocolError, SessionStateLostError):
            raise
        except Exception as exc:
            error = WriteOutcomeUnknown if op == "files.write" else TransportError
            raise error(f"SDK did not receive a valid {op} response", remote_state_unknown=True) from exc
        finally:
            if hasattr(body, "close"):
                body.close()
        if (not isinstance(document, dict) or type(document.get("version")) is not int or document.get("version") != 1 or
                document.get("request_id") != request_id or
                not isinstance(document.get("boot_id"), str) or not document["boot_id"] or
                ("result" in document) == ("error" in document)):
            raise AdapterProtocolError("Adapter response envelope is invalid")
        if isinstance(record, SessionRef) and document["boot_id"] != record.boot_id:
            raise SessionStateLostError("Runtime boot_id changed; SDK refused the old session reference")
        if "error" in document:
            error = document["error"]
            if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                raise AdapterProtocolError("Adapter error envelope is invalid")
            if error["code"] in {"SESSION_STATE_LOST", "BOOT_ID_MISMATCH"}:
                raise SessionStateLostError(error["message"], code=error["code"])
            raise FileOperationError(error["message"], code=error["code"])
        return document

    @staticmethod
    def _check_response(response, record):
        if not isinstance(response, dict):
            raise AdapterProtocolError("AWS response must be an object")
        # AWS marks this response header optional in all three service models.
        # An absent echo is valid; an explicit contradictory echo is not.
        if "runtimeSessionId" in response and response["runtimeSessionId"] != record.runtime_session_id:
            raise AdapterProtocolError("AWS returned an unexpected runtimeSessionId")
        status = response.get("statusCode", 200)
        if type(status) is not int or not 200 <= status < 300:
            raise TransportError(f"AWS returned HTTP status {status}", remote_state_unknown=True)

    def _initialize(self, attempt):
        target = self._target(attempt)
        with self._claim(attempt):
            try:
                doc = self._request(attempt, "init", {"workspace": target.workspace})
                result = doc["result"]
                if (not isinstance(result, dict) or result.get("workspace") != target.workspace or
                        not isinstance(result.get("capabilities"), list) or
                        any(not isinstance(x, str) for x in result["capabilities"])):
                    raise AdapterProtocolError("Adapter initialization result is invalid")
                capabilities = CAPABILITIES.intersection(result["capabilities"])
                ref = SessionRef(region=attempt.region, runtime_arn=attempt.runtime_arn,
                    qualifier=attempt.qualifier, runtime_session_id=attempt.runtime_session_id,
                    boot_id=doc["boot_id"], workspace=target.workspace,
                    boot_file=target.boot_file, module_path=target.module_path)
            except Exception as exc:
                raise CreateOutcomeUnknown("Initialization failed; resume or stop the original CreateAttempt", attempt=attempt) from exc
        from .sandbox import Sandbox
        return Sandbox(self, ref, capabilities)

    def resume_create(self, attempt):
        if not isinstance(attempt, CreateAttempt):
            raise ConfigurationError("resume_create requires a CreateAttempt")
        return self._initialize(attempt)

    def stop_attempt(self, attempt):
        if not isinstance(attempt, CreateAttempt):
            raise ConfigurationError("stop_attempt requires a CreateAttempt")
        self._target(attempt)
        return self._stop(attempt)

    def _stop(self, record):
        with self._claim(record, allow_stop=True) as state:
            if state.stop_token is None:
                state.stop_token = str(uuid.uuid4())
            try:
                response = self._transport.stop_runtime_session(**self._address(record), clientToken=state.stop_token)
                self._check_response(response, record)
            except Exception as exc:
                state.state = "STOP_UNKNOWN"
                raise StopOutcomeUnknown("Stop outcome is unknown; repeat stop() to reuse this token",
                                         client_token=state.stop_token) from exc
            state.state = "STOP_REQUESTED"
            return StopReceipt(record.runtime_session_id, state.stop_token, response.get("statusCode", 200))

    def close(self):
        if not self._closed:
            self._closed = True
            close = getattr(self._transport, "close", None)
            if close:
                close()

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, *_):
        self.close()


class Sandboxes:
    def __init__(self, client):
        self._client = client

    def create(self, *, template, timeout=None, **kwargs):
        reject_options(kwargs)
        if timeout is not None:
            raise UnsupportedCapabilityError("SDK does not implement per-session TTL")
        self._client._ensure_open()
        target = self._client.templates.get(template)
        if target is None:
            raise ConfigurationError(f"Unknown template: {template}")
        attempt = CreateAttempt(self._client.region, target.runtime_arn, target.qualifier, str(uuid.uuid4()))
        return self._client._initialize(attempt)

    def connect(self, ref):
        self._client._ensure_open()
        if not isinstance(ref, SessionRef):
            raise ConfigurationError("connect requires a complete SessionRef, not a sandbox ID")
        self._client._target(ref)
        from .sandbox import Sandbox
        return Sandbox(self._client, ref, None)

    def list(self, **kwargs):
        raise UnsupportedCapabilityError("SDK does not implement a global session directory")
