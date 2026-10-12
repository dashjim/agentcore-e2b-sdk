"""An explicit E2B-style subset backed by Amazon Bedrock AgentCore Runtime."""
from .client import Client
from .models import (RuntimeTarget, SessionRef, CreateAttempt, CommandResult, WriteInfo,
                     EntryInfo, FileType, StopReceipt)
from .sandbox import Sandbox
from .exceptions import (SandboxError, UnsupportedCapabilityError, ConfigurationError,
    AdapterProtocolError, SessionStateLostError, SessionClosedError, ConcurrentOperationError,
    TransportError, CommandExitException, CommandExecutionTimeout, CommandWaitTimeout,
    OutputLimitExceededError, CallbackError, FileOperationError, WriteOutcomeUnknown,
    CreateOutcomeUnknown, StopOutcomeUnknown)

__version__ = "0.1.0"

__all__ = ["Client", "RuntimeTarget", "SessionRef", "CreateAttempt", "CommandResult",
    "WriteInfo", "EntryInfo", "FileType", "StopReceipt", "Sandbox", "SandboxError",
    "UnsupportedCapabilityError", "ConfigurationError", "AdapterProtocolError",
    "SessionStateLostError", "SessionClosedError", "ConcurrentOperationError", "TransportError",
    "CommandExitException", "CommandExecutionTimeout", "CommandWaitTimeout", "OutputLimitExceededError",
    "CallbackError", "FileOperationError", "WriteOutcomeUnknown", "CreateOutcomeUnknown", "StopOutcomeUnknown"]
