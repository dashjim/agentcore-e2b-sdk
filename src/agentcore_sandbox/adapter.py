"""Deterministic, model-free Runtime adapter for the version 1 SDK protocol.

The file API boundary is deliberately narrower than arbitrary shell access.
Directory descriptors and O_NOFOLLOW prevent file API symlink traversal; they
do not isolate hostile commands from other processes in the same container.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import grp
import json
import os
from pathlib import PurePosixPath
import pwd
import stat
import uuid


PROTOCOL_VERSION = 1
MAX_FILE_BYTES = 1024 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_LIST_ENTRIES = 1000
DEFAULT_BOOT_FILE = "/app/run/boot.json"
DEFAULT_WORKSPACE = "/workspace"
CAPABILITIES = [
    "commands", "files.read", "files.write", "files.list",
    "files.make_dir", "files.remove", "probe",
]
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class AdapterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def initialize_boot_file(path: str) -> str:
    """The container entry point calls this exactly once on every start.

    Operators must keep the boot marker outside persisted workspace mounts.
    Repeated init requests do not call this function or reset the workspace.
    """
    boot_id = str(uuid.uuid4())
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    temporary = os.path.join(parent, f".boot-{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"boot_id": boot_id}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return boot_id


def read_boot_identity(path: str) -> tuple[str, int, int]:
    """Read generation and Unix ownership from the same opened marker.

    Runtime's native command process may start as root while the HTTP process
    runs under another account. The helper uses marker ownership to reproduce
    that account; the marker never carries environment variables or secrets.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("invalid boot marker type")
            marker = json.loads(stream.read(4097))
        boot_id = marker["boot_id"]
        if not isinstance(boot_id, str) or str(uuid.UUID(boot_id)) != boot_id:
            raise ValueError("invalid boot ID")
        return boot_id, info.st_uid, info.st_gid
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AdapterError("PROTOCOL_ERROR", "The adapter boot marker is unavailable or invalid") from exc


def read_boot_id(path: str) -> str:
    return read_boot_identity(path)[0]


def _path_string(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value.encode("utf-8")) > 4096:
        raise AdapterError("INVALID_REQUEST", "The adapter requires a nonempty bounded path string")
    if ".." in value.split("/"):
        raise AdapterError("PATH_VIOLATION", "The file API rejects parent traversal")
    return value


def _mapped_os_error(exc: OSError) -> AdapterError:
    code, message = {
        errno.ENOENT: ("NOT_FOUND", "The requested path does not exist"),
        errno.EACCES: ("PERMISSION_DENIED", "The adapter cannot access the requested path"),
        errno.EPERM: ("PERMISSION_DENIED", "The adapter cannot access the requested path"),
        errno.ELOOP: ("PATH_VIOLATION", "The file API rejects symbolic links"),
        errno.ENOTEMPTY: ("NOT_EMPTY", "The adapter cannot remove a nonempty directory"),
        errno.EISDIR: ("IS_DIRECTORY", "The operation requires a regular file"),
        errno.ENOTDIR: ("NOT_DIRECTORY", "A path component is not a directory"),
        errno.ENAMETOOLONG: ("INVALID_REQUEST", "The requested path is too long"),
        errno.EEXIST: ("ALREADY_EXISTS", "A conflicting path already exists"),
    }.get(exc.errno, ("IO_ERROR", "The adapter could not complete the filesystem operation"))
    return AdapterError(code, message)


def _reject_symlink(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise AdapterError("PATH_VIOLATION", "The file API rejects symbolic links")
    return info


class Adapter:
    """File/probe handler; the HTTP server owns boot marker generation."""

    def __init__(self, workspace: str, boot_file: str):
        workspace = _path_string(os.fspath(workspace))
        if not workspace.startswith("/") or str(PurePosixPath(workspace)) == "/":
            raise ValueError("workspace must be an absolute directory other than /")
        self.workspace = str(PurePosixPath(workspace))
        self.boot_file = os.fspath(boot_file)
        if os.path.commonpath([os.path.realpath(self.workspace), os.path.realpath(self.boot_file)]) == os.path.realpath(self.workspace):
            raise ValueError("the boot marker must be outside the potentially persisted workspace")
        self.boot_id = read_boot_id(self.boot_file)
        with self._directory([], create=True):
            pass

    def _parts(self, path: object) -> list[str]:
        value = _path_string(path)
        parsed = PurePosixPath(value)
        if parsed.is_absolute():
            try:
                parsed = parsed.relative_to(PurePosixPath(self.workspace))
            except ValueError as exc:
                raise AdapterError("PATH_VIOLATION", "The file API only accepts paths inside its workspace") from exc
        return [part for part in parsed.parts if part not in ("", ".")]

    @contextmanager
    def _directory(self, parts: list[str], *, create: bool = False):
        # Walk from / so even configured workspace ancestors cannot be links.
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        try:
            for name in [*PurePosixPath(self.workspace).parts[1:], *parts]:
                _reject_symlink(descriptor, name)
                if create:
                    try:
                        os.mkdir(name, mode=0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def open_directory(self, path: str):
        """Execution guard opens cwd through the same no-follow path walker."""
        with self._directory(self._parts(path)) as descriptor:
            yield descriptor

    def _write_info(self, parts: list[str], kind: str) -> dict:
        path = str(PurePosixPath(self.workspace).joinpath(*parts))
        return {"name": PurePosixPath(path).name, "type": kind, "path": path, "metadata": None}

    def _entry_info(self, parts: list[str], info: os.stat_result) -> dict:
        if stat.S_ISDIR(info.st_mode):
            kind = "dir"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            raise AdapterError("PATH_VIOLATION", "The file API only lists regular files and directories")
        try:
            owner = pwd.getpwuid(info.st_uid).pw_name
        except KeyError:
            owner = str(info.st_uid)
        try:
            group = grp.getgrgid(info.st_gid).gr_name
        except KeyError:
            group = str(info.st_gid)
        return {
            **self._write_info(parts, kind), "size": info.st_size,
            "mode": stat.S_IMODE(info.st_mode), "permissions": stat.filemode(info.st_mode)[1:],
            "owner": owner, "group": group,
            "modified_time": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        }

    def handle(self, request: object) -> dict:
        request_id = request.get("request_id") if isinstance(request, dict) else None
        response = {"version": PROTOCOL_VERSION, "request_id": request_id if isinstance(request_id, str) else None, "boot_id": self.boot_id}
        try:
            # Check the live marker too: no existing Adapter instance can accept
            # an old generation after the entry point has replaced the marker.
            self.boot_id = read_boot_id(self.boot_file)
            response["boot_id"] = self.boot_id
            result = self._dispatch(request)
            response["result"] = result
            if len(json.dumps(response, ensure_ascii=True).encode("utf-8")) > MAX_BODY_BYTES:
                raise AdapterError("RESPONSE_TOO_LARGE", "The adapter response exceeds its size limit")
        except AdapterError as exc:
            response.pop("result", None)
            response["error"] = {"code": exc.code, "message": exc.message}
        except OSError as exc:
            mapped = _mapped_os_error(exc)
            response.pop("result", None)
            response["error"] = {"code": mapped.code, "message": mapped.message}
        except (ValueError, TypeError, UnicodeError):
            response.pop("result", None)
            response["error"] = {"code": "INVALID_REQUEST", "message": "The adapter could not decode the request"}
        return response

    def _dispatch(self, request: object):
        if not isinstance(request, dict) or request.get("protocol") != "agentcore-sandbox" or type(request.get("version")) is not int or request["version"] != PROTOCOL_VERSION:
            raise AdapterError("PROTOCOL_ERROR", "The adapter requires agentcore-sandbox protocol version 1")
        request_id = request.get("request_id")
        try:
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError("invalid request ID")
        except ValueError as exc:
            raise AdapterError("INVALID_REQUEST", "The adapter requires a canonical UUID request ID") from exc
        op, args = request.get("op"), request.get("args")
        if not isinstance(args, dict):
            raise AdapterError("INVALID_REQUEST", "The adapter requires an args object")
        if (op != "init" or "expected_boot_id" in request) and request.get("expected_boot_id") != self.boot_id:
            raise AdapterError("SESSION_STATE_LOST", "The container generation no longer matches the session reference")
        if op == "init":
            if set(args) - {"workspace"} or args.get("workspace", self.workspace) != self.workspace:
                raise AdapterError("INVALID_REQUEST", "The requested workspace does not match the configured workspace")
            return {"workspace": self.workspace, "capabilities": CAPABILITIES.copy(), "shell": "/bin/bash", "user": pwd.getpwuid(os.getuid()).pw_name}
        if op == "probe":
            if args:
                raise AdapterError("INVALID_REQUEST", "Probe takes no arguments")
            return {"workspace": self.workspace, "capabilities": CAPABILITIES.copy()}
        expected_args = {
            "files.read": {"path"}, "files.write": {"path", "data", "encoding"},
            "files.list": {"path", "depth"}, "files.make_dir": {"path"}, "files.remove": {"path"},
        }
        if op not in expected_args:
            raise AdapterError("UNSUPPORTED_CAPABILITY", "The adapter does not support the requested operation")
        if set(args) - expected_args[op]:
            raise AdapterError("INVALID_REQUEST", "The adapter rejects unsupported file arguments")
        parts = self._parts(args.get("path"))
        if op == "files.read":
            return self._read(parts)
        if op == "files.write":
            return self._write(parts, args)
        if op == "files.list":
            if type(args.get("depth", 1)) is not int or args.get("depth", 1) != 1:
                raise AdapterError("UNSUPPORTED_CAPABILITY", "The adapter only lists depth 1")
            return self._list(parts)
        if op == "files.make_dir":
            return self._make_dir(parts)
        return self._remove(parts)

    def _read(self, parts: list[str]) -> dict:
        if not parts:
            raise AdapterError("IS_DIRECTORY", "The operation requires a regular file")
        with self._directory(parts[:-1]) as parent:
            _reject_symlink(parent, parts[-1])
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if stat.S_ISDIR(info.st_mode):
                    raise AdapterError("IS_DIRECTORY", "The operation requires a regular file")
                if not stat.S_ISREG(info.st_mode):
                    raise AdapterError("PATH_VIOLATION", "The file API only reads regular files")
                if info.st_size > MAX_FILE_BYTES:
                    raise AdapterError("FILE_TOO_LARGE", "The file exceeds the 1 MiB limit")
                data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise AdapterError("FILE_TOO_LARGE", "The file exceeds the 1 MiB limit")
        return {"data": base64.b64encode(data).decode("ascii"), "encoding": "base64"}

    def _write(self, parts: list[str], args: dict) -> dict:
        if not parts:
            raise AdapterError("IS_DIRECTORY", "The operation requires a regular file")
        if args.get("encoding") != "base64" or not isinstance(args.get("data"), str):
            raise AdapterError("INVALID_REQUEST", "The file writer requires Base64 data")
        if len(args["data"]) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise AdapterError("FILE_TOO_LARGE", "The file exceeds the 1 MiB limit")
        try:
            data = base64.b64decode(args["data"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AdapterError("INVALID_REQUEST", "The file writer requires valid Base64 data") from exc
        if len(data) > MAX_FILE_BYTES:
            raise AdapterError("FILE_TOO_LARGE", "The file exceeds the 1 MiB limit")
        with self._directory(parts[:-1], create=True) as parent:
            existing = _reject_symlink(parent, parts[-1])
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                code = "IS_DIRECTORY" if stat.S_ISDIR(existing.st_mode) else "PATH_VIOLATION"
                raise AdapterError(code, "The operation requires a regular file")
            temporary = f".acsbx-write-{uuid.uuid4().hex}"
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=parent)
                with os.fdopen(descriptor, "wb") as stream:
                    if existing is not None:
                        os.fchmod(stream.fileno(), stat.S_IMODE(existing.st_mode))
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                _reject_symlink(parent, parts[-1])
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return self._write_info(parts, "file")

    def _list(self, parts: list[str]) -> list[dict]:
        result = []
        with self._directory(parts) as descriptor:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if len(result) >= MAX_LIST_ENTRIES:
                        raise AdapterError("DIRECTORY_TOO_LARGE", "The directory exceeds the 1000 entry limit")
                    if entry.is_symlink():
                        raise AdapterError("PATH_VIOLATION", "The file API rejects symbolic links")
                    result.append(self._entry_info([*parts, entry.name], entry.stat(follow_symlinks=False)))
        return sorted(result, key=lambda entry: entry["name"])

    def _make_dir(self, parts: list[str]) -> bool:
        if not parts:
            return False
        with self._directory(parts[:-1], create=True) as parent:
            existing = _reject_symlink(parent, parts[-1])
            if existing is not None:
                if not stat.S_ISDIR(existing.st_mode):
                    raise AdapterError("NOT_DIRECTORY", "A file already occupies the requested directory path")
                return False
            try:
                os.mkdir(parts[-1], mode=0o755, dir_fd=parent)
                return True
            except FileExistsError:
                existing = _reject_symlink(parent, parts[-1])
                if existing is None or not stat.S_ISDIR(existing.st_mode):
                    raise AdapterError("NOT_DIRECTORY", "A conflicting path already exists")
                return False

    def _remove(self, parts: list[str]) -> None:
        if not parts:
            raise AdapterError("PATH_VIOLATION", "The adapter cannot remove its workspace root")
        with self._directory(parts[:-1]) as parent:
            info = _reject_symlink(parent, parts[-1])
            if info is None:
                raise AdapterError("NOT_FOUND", "The requested path does not exist")
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(parts[-1], dir_fd=parent)
            elif stat.S_ISREG(info.st_mode):
                os.unlink(parts[-1], dir_fd=parent)
            else:
                raise AdapterError("PATH_VIOLATION", "The file API only removes regular files and empty directories")
        return None
