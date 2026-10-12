"""Stable public failures. Unknown outcomes never imply remote cancellation."""


class SandboxError(Exception):
    def __init__(self, message, *, code=None, stdout="", stderr="", exit_code=None,
                 error=None, remote_state_unknown=False):
        super().__init__(message)
        self.code = code or type(self).__name__
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.error = error or message
        self.remote_state_unknown = remote_state_unknown


class UnsupportedCapabilityError(SandboxError):
    pass


class ConfigurationError(SandboxError):
    pass


class AdapterProtocolError(SandboxError):
    pass


class SessionStateLostError(SandboxError):
    pass


class SessionClosedError(SandboxError):
    pass


class ConcurrentOperationError(SandboxError):
    pass


class TransportError(SandboxError):
    pass


class CommandExitException(SandboxError):
    pass


class CommandExecutionTimeout(SandboxError):
    pass


class CommandWaitTimeout(SandboxError):
    pass


class OutputLimitExceededError(SandboxError):
    pass


class CallbackError(SandboxError):
    pass


class FileOperationError(SandboxError):
    pass


class WriteOutcomeUnknown(TransportError):
    pass


class CreateOutcomeUnknown(TransportError):
    def __init__(self, message, *, attempt):
        super().__init__(message, remote_state_unknown=True)
        self.attempt = attempt


class StopOutcomeUnknown(TransportError):
    def __init__(self, message, *, client_token):
        super().__init__(message, remote_state_unknown=True)
        self.client_token = client_token
