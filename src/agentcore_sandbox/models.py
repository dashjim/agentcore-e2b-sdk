"""Immutable public data contracts; references contain no credentials."""
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import json
import posixpath
import re

from .exceptions import ConfigurationError


def validate_target(region, arn, qualifier, workspace):
    if not isinstance(arn, str):
        raise ConfigurationError("runtime_arn must be an AgentCore Runtime ARN")
    match = re.fullmatch(r"arn:([^:]+):bedrock-agentcore:([^:]+):(\d{12}):runtime/([A-Za-z0-9_-]+)", arn)
    if not match or (region is not None and match[2] != region):
        raise ConfigurationError("Runtime ARN must match the configured region")
    if not isinstance(qualifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", qualifier):
        raise ConfigurationError("qualifier must be an Endpoint name")
    if (not isinstance(workspace, str) or not workspace.startswith("/") or
            "\x00" in workspace or workspace == "/" or posixpath.normpath(workspace) != workspace):
        raise ConfigurationError("workspace must be a normalized absolute directory other than /")


def validate_deployment(workspace, boot_file, module_path):
    for name, value in (("boot_file", boot_file), ("module_path", module_path)):
        if name == "module_path" and value is None:
            continue
        if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value or
                posixpath.normpath(value) != value or (name == "boot_file" and value == "/")):
            raise ConfigurationError(f"{name} must be a normalized absolute path")
    if boot_file == workspace or boot_file.startswith(workspace + "/"):
        raise ConfigurationError("boot_file must lie outside the configured workspace")


@dataclass(frozen=True)
class RuntimeTarget:
    runtime_arn: str
    qualifier: str = "DEFAULT"
    workspace: str = "/workspace"
    boot_file: str = "/app/run/boot.json"
    module_path: str | None = None

    def __post_init__(self):
        validate_target(None, self.runtime_arn, self.qualifier, self.workspace)
        validate_deployment(self.workspace, self.boot_file, self.module_path)


class _JsonRecord:
    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, value):
        try:
            return cls(**value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid {cls.__name__}") from exc

    @classmethod
    def from_json(cls, value):
        try:
            return cls.from_dict(json.loads(value))
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(f"Invalid {cls.__name__} JSON") from exc


@dataclass(frozen=True)
class SessionRef(_JsonRecord):
    region: str
    runtime_arn: str
    qualifier: str
    runtime_session_id: str
    boot_id: str
    workspace: str = "/workspace"
    schema_version: int = 1
    adapter_protocol: int = 1
    boot_file: str = "/app/run/boot.json"
    module_path: str | None = None

    def __post_init__(self):
        validate_target(self.region, self.runtime_arn, self.qualifier, self.workspace)
        validate_deployment(self.workspace, self.boot_file, self.module_path)
        if type(self.schema_version) is not int or type(self.adapter_protocol) is not int or self.schema_version != 1 or self.adapter_protocol != 1:
            raise ConfigurationError("Unsupported reference/protocol version")
        _validate_session(self.runtime_session_id)
        if not isinstance(self.boot_id, str) or not self.boot_id or len(self.boot_id) > 256:
            raise ConfigurationError("boot_id must be a nonempty string of at most 256 characters")


def _validate_session(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{33,256}", value):
        raise ConfigurationError("runtime_session_id must contain 33–256 safe characters")


@dataclass(frozen=True)
class CreateAttempt(_JsonRecord):
    region: str
    runtime_arn: str
    qualifier: str
    runtime_session_id: str

    def __post_init__(self):
        validate_target(self.region, self.runtime_arn, self.qualifier, "/workspace")
        _validate_session(self.runtime_session_id)


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None


class FileType(str, Enum):
    FILE = "file"
    DIR = "dir"


@dataclass(frozen=True)
class WriteInfo:
    name: str
    type: FileType
    path: str
    metadata: dict | None = None


@dataclass(frozen=True)
class EntryInfo(WriteInfo):
    size: int | None = None
    mode: int | None = None
    permissions: str | None = None
    owner: str | None = None
    group: str | None = None
    modified_time: datetime | None = None
    symlink_target: str | None = None


@dataclass(frozen=True)
class StopReceipt:
    runtime_session_id: str
    client_token: str
    status_code: int
    state: str = "STOP_REQUESTED"
