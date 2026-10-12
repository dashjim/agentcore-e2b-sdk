"""Local sandbox handles and explicit Runtime lifecycle methods."""
from contextlib import contextmanager

from .client import CAPABILITIES
from .exceptions import AdapterProtocolError, SessionClosedError, UnsupportedCapabilityError


class _UnsupportedNamespace:
    def __getattr__(self, name):
        def unsupported(*args, **kwargs):
            raise UnsupportedCapabilityError(f"SDK does not implement pty.{name}")
        return unsupported


class Sandbox:
    def __init__(self, client, ref, capabilities=None):
        from .commands import Commands
        from .files import Files
        self._client = client
        self._ref = ref
        self._capabilities = capabilities
        self._closed = False
        self.commands = Commands(self)
        self.files = Files(self)
        self.runtime = Runtime(self)
        self.pty = _UnsupportedNamespace()

    @property
    def sandbox_id(self):
        return self._ref.runtime_session_id

    @property
    def state(self):
        return "CLOSED" if self._closed else self._client._state(self._ref).state

    def _ensure_open(self):
        if self._closed:
            raise SessionClosedError("Sandbox handle is closed")
        self._client._ensure_open()

    def _negotiate(self):
        doc = self._client._request(self._ref, "probe", {})
        result = doc["result"]
        if (not isinstance(result, dict) or not isinstance(result.get("capabilities"), list) or
                any(not isinstance(x, str) for x in result["capabilities"])):
            raise AdapterProtocolError("Adapter probe must return a capabilities list")
        self._capabilities = CAPABILITIES.intersection(result["capabilities"])
        return result

    @contextmanager
    def _operation(self, capability, *, negotiate=True):
        self._ensure_open()
        with self._client._claim(self._ref):
            if self._capabilities is None and negotiate:
                self._negotiate()
            if self._capabilities is not None and capability not in self._capabilities:
                raise UnsupportedCapabilityError(f"Adapter does not advertise {capability}")
            yield

    def export_ref(self):
        return self.runtime.export_ref()

    def probe(self):
        return self.runtime.probe()

    def stop(self):
        return self.runtime.stop()

    def close(self):
        self._closed = True

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, *_):
        self.close()

    def _unsupported(self, *args, **kwargs):
        raise UnsupportedCapabilityError("SDK does not implement this E2B lifecycle/capability")

    kill = set_timeout = get_info = is_running = pause = fork = get_host = run_code = _unsupported


class Runtime:
    def __init__(self, sandbox):
        self._sandbox = sandbox

    def export_ref(self):
        return self._sandbox._ref

    def probe(self):
        sbx = self._sandbox
        sbx._ensure_open()
        with sbx._client._claim(sbx._ref):
            return sbx._negotiate()

    def stop(self):
        sbx = self._sandbox
        sbx._ensure_open()
        return sbx._client._stop(sbx._ref)
