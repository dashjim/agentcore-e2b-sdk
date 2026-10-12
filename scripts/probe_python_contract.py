#!/usr/bin/env python3
"""Reproduce Python-level E2B/AgentCore differences without sandbox factories.

The probe instantiates data records and exceptions only. An audit hook blocks
network connections and DNS resolution. Source excerpts describe lifecycle
implementations; they do not establish live backend lifecycle behavior.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import sys
from datetime import datetime, timezone


def block_network(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
        raise RuntimeError(f"Offline contract probe forbids {event}")


sys.addaudithook(block_network)


def qualified(value):
    return f"{value.__module__}.{value.__qualname__}"


def source_record(value):
    path = Path(inspect.getsourcefile(value)).resolve()
    lines, start = inspect.getsourcelines(value)
    return {"path": str(path), "start_line": start,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source": "".join(lines)}


def class_record(cls):
    result = {"qualified_name": qualified(cls), "signature": str(inspect.signature(cls)),
              "mro": [qualified(base) for base in cls.__mro__]}
    if dataclasses.is_dataclass(cls):
        result["frozen"] = cls.__dataclass_params__.frozen
        result["fields"] = [{"name": field.name, "keyword_only": field.kw_only,
                             "required": field.default is dataclasses.MISSING and
                             field.default_factory is dataclasses.MISSING}
                            for field in dataclasses.fields(cls)]
    return result


def assignment_probe(record):
    original = record.stdout
    try:
        record.stdout = "changed"
        return {"assignment_succeeded": True, "value_after": record.stdout,
                "value_before": original}
    except Exception as error:
        return {"assignment_succeeded": False, "exception": qualified(type(error)),
                "message": str(error), "value_after": record.stdout}


def binding_probe(method, args, kwargs=None):
    signature = inspect.signature(method)
    try:
        signature.bind(None, *args, **(kwargs or {}))
        return {"accepted_by_python_signature": True}
    except TypeError as error:
        return {"accepted_by_python_signature": False, "error": str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentcore-source", type=Path,
                        default=Path(__file__).resolve().parents[1] / "src",
                        help="AgentCore SDK source directory (default: repository src)")
    parser.add_argument("--output", type=Path, required=True,
                        help="Explicit output JSON path; source excerpts contain local filesystem paths")
    args = parser.parse_args()
    public_source = args.agentcore_source.resolve()
    if not (public_source / "agentcore_sandbox" / "__init__.py").is_file():
        raise SystemExit("--agentcore-source must contain agentcore_sandbox/__init__.py")
    sys.path.insert(0, str(public_source))

    import agentcore_sandbox as ac
    from agentcore_sandbox.commands import Commands as ACCommands
    from agentcore_sandbox.files import Files as ACFiles
    from e2b import Sandbox as E2BSandbox
    from e2b.sandbox.commands.command_handle import CommandExitException as E2BExit
    from e2b.sandbox.commands.command_handle import CommandResult as E2BResult
    from e2b.sandbox.filesystem.filesystem import EntryInfo as E2BEntry
    from e2b.sandbox.filesystem.filesystem import FileType as E2BType
    from e2b.sandbox.filesystem.filesystem import WriteInfo as E2BWrite
    from e2b.sandbox_sync.commands.command import Commands as E2BCommands
    from e2b.sandbox_sync.filesystem.filesystem import Filesystem as E2BFiles

    version = importlib.metadata.version("e2b")
    if version != "2.51.0":
        raise SystemExit(f"Expected e2b==2.51.0; found {version}")

    pairs = [("CommandResult", E2BResult, ac.CommandResult),
             ("CommandExitException", E2BExit, ac.CommandExitException),
             ("FileType", E2BType, ac.FileType),
             ("WriteInfo", E2BWrite, ac.WriteInfo),
             ("EntryInfo", E2BEntry, ac.EntryInfo)]
    identities = {}
    for name, e2b_cls, ac_cls in pairs:
        identities[name] = {"e2b": class_record(e2b_cls), "agentcore": class_record(ac_cls),
                            "same_class": e2b_cls is ac_cls,
                            "agentcore_is_e2b_subclass": issubclass(ac_cls, e2b_cls)}

    methods = [("commands.run", E2BCommands.run, ACCommands.run, ["printf ok", False]),
               ("files.read", E2BFiles.read, ACFiles.read, ["test.txt", "bytes"]),
               ("files.list", E2BFiles.list, ACFiles.list, [".", 1])]
    signatures = {}
    for name, e2b_method, ac_method, arguments in methods:
        signatures[name] = {"example_positional_arguments": arguments,
                            "e2b": {"signature": str(inspect.signature(e2b_method)),
                                    **binding_probe(e2b_method, arguments)},
                            "agentcore": {"signature": str(inspect.signature(ac_method)),
                                          **binding_probe(ac_method, arguments)}}

    e2b_exit = E2BExit(stdout="out", stderr="err", exit_code=7, error=None)
    ac_exit = ac.CommandExitException("Command exited with code 7", stdout="out", stderr="err", exit_code=7)
    exception_instances = {}
    for backend, error, result_cls in [("e2b", e2b_exit, E2BResult), ("agentcore", ac_exit, ac.CommandResult)]:
        exception_instances[backend] = {"exception_type": qualified(type(error)),
                                       "is_e2b_command_result": isinstance(error, E2BResult),
                                       "is_own_command_result": isinstance(error, result_cls),
                                       "caught_by_e2b_command_exit_exception": isinstance(error, E2BExit),
                                       "stdout": error.stdout, "stderr": error.stderr,
                                       "exit_code": error.exit_code, "error": error.error,
                                       "str": str(error)}

    constructors = {}
    for backend, result_cls in [("e2b", E2BResult), ("agentcore", ac.CommandResult)]:
        record = result_cls("first", "second", 0, None)
        constructors[backend] = {"arguments": ["first", "second", 0, None],
                                 "stdout": record.stdout, "stderr": record.stderr,
                                 "mutation": assignment_probe(record)}

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_subject": "Offline Python contract probe",
        "systems_under_test": ["Installed official E2B Python SDK", "Public AgentCore-style SDK source"],
        "evidence_kind": "Python introspection, local data/exception construction, and source inspection only",
        "limitations": ["No sandbox/client factory was called", "No cloud or backend behavior was tested",
                        "Constructed exception fields are examples, not observations from a real failed command",
                        "Signature binding checks call syntax, not runtime option support"],
        "network_connections_and_dns_blocked": True,
        "versions": {"python": sys.version, "e2b": version, "agentcore_sandbox": ac.__version__},
        "agentcore_source_root": str(public_source),
        "class_identity": identities,
        "file_type": {
            "e2b": {"file_equals_string": E2BType.FILE == "file", "file_is_str": isinstance(E2BType.FILE, str),
                    "members": list(E2BType.__members__)},
            "agentcore": {"file_equals_string": ac.FileType.FILE == "file", "file_is_str": isinstance(ac.FileType.FILE, str),
                          "members": list(ac.FileType.__members__)},
            "cross_backend_file_enum_equal": E2BType.FILE == ac.FileType.FILE},
        "constructed_command_results": constructors,
        "constructed_nonzero_exceptions": exception_instances,
        "positional_argument_binding": signatures,
        "lifecycle_source_only": {
            "e2b_sandbox_exit": source_record(E2BSandbox.__exit__),
            "agentcore_sandbox_exit": source_record(ac.Sandbox.__exit__),
            "agentcore_sandbox_close": source_record(ac.Sandbox.close),
            "interpretation": "E2B Sandbox.__exit__ invokes self.kill(); AgentCore Sandbox.__exit__ invokes self.close(), which marks only the local handle closed. The probe does not execute these methods."},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "e2b": version,
                      "cloud_calls": 0, "class_pairs_checked": len(pairs)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
