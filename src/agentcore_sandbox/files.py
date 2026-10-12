"""Small file operations with local preflight and typed result conversion."""
import base64
from datetime import datetime
import posixpath

from .client import reject_options
from .exceptions import AdapterProtocolError, FileOperationError, UnsupportedCapabilityError, WriteOutcomeUnknown
from .models import EntryInfo, FileType, WriteInfo


class Files:
    def __init__(self, sandbox):
        self._sandbox = sandbox

    def _path(self, path):
        if not isinstance(path, str) or not path or "\x00" in path or ".." in path.split("/"):
            raise FileOperationError("Path must be nonempty and contain no NUL or parent traversal", code="PATH_VIOLATION")
        workspace = self._sandbox._ref.workspace
        path = posixpath.normpath(path if path.startswith("/") else posixpath.join(workspace, path))
        if path != workspace and not path.startswith(workspace + "/"):
            raise FileOperationError("Path lies outside the configured workspace", code="PATH_VIOLATION")
        if len(path.encode("utf-8")) > 4096:
            raise FileOperationError("Path exceeds 4096 bytes", code="PATH_VIOLATION")
        return path

    def _invoke(self, op, args):
        with self._sandbox._operation(op):
            return self._sandbox._client._request(self._sandbox._ref, op, args)["result"]

    def read(self, path, *, format="text", **kwargs):
        reject_options(kwargs)
        if format not in {"text", "bytes"}:
            raise UnsupportedCapabilityError("SDK supports file read format text or bytes only")
        result = self._invoke("files.read", {"path": self._path(path)})
        if not isinstance(result, dict) or result.get("encoding") != "base64" or not isinstance(result.get("data"), str):
            raise AdapterProtocolError("File read result is invalid")
        try:
            data = base64.b64decode(result["data"], validate=True)
        except ValueError as exc:
            raise AdapterProtocolError("File read result contains invalid Base64") from exc
        if len(data) > self._sandbox._client.max_file_bytes:
            raise FileOperationError("File exceeds the client's maximum size", code="FILE_TOO_LARGE")
        if format == "bytes":
            return bytearray(data)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileOperationError("File does not contain valid UTF-8", code="ENCODING_ERROR") from exc

    def write(self, path, data, **kwargs):
        reject_options(kwargs)
        path = self._path(path)
        if not isinstance(data, (str, bytes)):
            raise UnsupportedCapabilityError("SDK file write accepts str or bytes only")
        data = data.encode("utf-8") if isinstance(data, str) else data
        if len(data) > self._sandbox._client.max_file_bytes:
            raise FileOperationError("File exceeds the client's maximum size", code="FILE_TOO_LARGE")
        result = self._invoke("files.write", {"path": path, "encoding": "base64", "data": base64.b64encode(data).decode("ascii")})
        try:
            info = self._info(result, WriteInfo)
            if info.path != path or info.type is not FileType.FILE:
                raise AdapterProtocolError("Adapter write metadata does not identify the requested file")
            return info
        except AdapterProtocolError as exc:
            raise WriteOutcomeUnknown("Adapter did not confirm valid write metadata", remote_state_unknown=True) from exc

    @staticmethod
    def _info(result, cls):
        if not isinstance(result, dict):
            raise AdapterProtocolError("File info must be an object")
        try:
            data = dict(result)
            data["type"] = FileType(data["type"])
            for field in ("name", "path"):
                if not isinstance(data.get(field), str):
                    raise ValueError(field)
            if "modified_time" in data and data["modified_time"] is not None:
                data["modified_time"] = datetime.fromisoformat(data["modified_time"].replace("Z", "+00:00"))
                if data["modified_time"].tzinfo is None:
                    raise ValueError("modified_time must include a timezone")
            for field in ("size", "mode"):
                if data.get(field) is not None and (type(data[field]) is not int or data[field] < 0):
                    raise ValueError(field)
            for field in ("permissions", "owner", "group"):
                if data.get(field) is not None and not isinstance(data[field], str):
                    raise ValueError(field)
            return cls(**data)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise AdapterProtocolError("File info fields are invalid") from exc

    def list(self, path, *, depth=1, **kwargs):
        reject_options(kwargs)
        if type(depth) is not int or depth != 1:
            raise UnsupportedCapabilityError("SDK directory list supports depth=1 only")
        result = self._invoke("files.list", {"path": self._path(path), "depth": depth})
        if not isinstance(result, list):
            raise AdapterProtocolError("File list result is invalid")
        return [self._info(entry, EntryInfo) for entry in result]

    def make_dir(self, path, **kwargs):
        reject_options(kwargs)
        result = self._invoke("files.make_dir", {"path": self._path(path)})
        if type(result) is not bool:
            raise AdapterProtocolError("make_dir result must be bool", remote_state_unknown=True)
        return result

    def remove(self, path, **kwargs):
        reject_options(kwargs)
        result = self._invoke("files.remove", {"path": self._path(path)})
        if result is not None:
            raise AdapterProtocolError("remove result must be null", remote_state_unknown=True)
