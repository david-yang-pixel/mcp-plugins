"""Operation classification and input checks applied before difyctl runs."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .settings import LEVELS

OPERATION = re.compile(r"^[a-z0-9_]+([. ][a-z0-9_]+)*$")
READ_OVERRIDES = {"check.console_app.dsl", "knowledge_fs.diff"}
DESTRUCTIVE_EXACT = {"restore.console_app.version"}
DESTRUCTIVE_PREFIXES = ("reset.", "delete.", "remove.", "revoke.")
FORBIDDEN = {"delete.account.current_session": "Use difyctl_logout to end the Dify session."}
TOOL_FOR_LEVEL = {0: "difyctl_read", 1: "difyctl_write", 2: "difyctl_destructive"}
# Local difyctl commands are not catalog operations; only these have an MCP equivalent.
LOCAL_COMMANDS = {
    "login": "Not available: login is done by MCP OAuth authorization.",
    "logout": "difyctl_logout",
    "use workspace": "difyctl_use_workspace",
    "refresh cache": "difyctl_refresh_catalog",
    "version": "difyctl_status",
    "install skills": "difyctl_guide (skills are preinstalled)",
    "get config": "Not available on this server.",
    "set config": "Not available on this server.",
    "unset config": "Not available on this server.",
    "delete cache": "Not available; use difyctl_refresh_catalog.",
}
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_FOLDER_FILES = 5000


class PolicyError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def words(operation: str) -> list[str]:
    if not isinstance(operation, str) or len(operation) > 200 or not OPERATION.fullmatch(operation):
        raise PolicyError("invalid_operation", "Operation must look like run.console_app.workflow or 'run console_app workflow'.")
    return re.split(r"[. ]", operation)


def classify(descriptor: dict) -> tuple[str, int]:
    """Return (operation id, effect level); a descriptor without op is not a catalog operation."""
    op = descriptor.get("op") if isinstance(descriptor, dict) else None
    if not isinstance(op, str) or not OPERATION.fullmatch(op):
        raise PolicyError("not_an_operation", "Only server catalog operations can be run; use difyctl_help to find one.")
    if op in FORBIDDEN:
        raise PolicyError("forbidden_operation", f"{op} is not available through MCP. {FORBIDDEN[op]}")
    if op in READ_OVERRIDES:
        return op, LEVELS["read"]
    if op in DESTRUCTIVE_EXACT or op.startswith(DESTRUCTIVE_PREFIXES):
        return op, LEVELS["destructive"]
    # A missing or unknown effect is treated as destructive.
    return op, LEVELS.get(descriptor.get("effect"), LEVELS["destructive"])


def effect_name(level: int) -> str:
    return next(name for name, value in LEVELS.items() if value == level)


def tool_for(entry_id: str, effect: str | None) -> str:
    dotted = entry_id.replace(" ", ".")
    spaced = entry_id.replace(".", " ")
    if spaced in LOCAL_COMMANDS:
        return LOCAL_COMMANDS[spaced]
    if dotted in FORBIDDEN:
        return "Not available: " + FORBIDDEN[dotted]
    try:
        _, level = classify({"op": dotted, "effect": effect})
    except PolicyError:
        return "Not available"
    return TOOL_FOR_LEVEL[level]


def _is_scalar(schema) -> bool:
    """Mirror difyctl's shape rules: only scalars take a literal '@...' string."""
    if not isinstance(schema, dict):
        return False
    alternatives = [a for key in ("anyOf", "oneOf") if isinstance(schema.get(key), list)
                    for a in schema[key] if isinstance(a, dict) and a.get("type") != "null"]
    if alternatives:
        return len(alternatives) == 1 and _is_scalar(alternatives[0])
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return True
    return schema.get("type") in ("string", "integer", "number", "boolean")


def check_input(op: str, descriptor: dict, data) -> bytes:
    if not isinstance(data, dict):
        raise PolicyError("invalid_input", "input must be a JSON object.")
    encoded = json.dumps(data, ensure_ascii=False).encode()
    if len(encoded) > MAX_INPUT_BYTES:
        raise PolicyError("input_too_large", "input is larger than 8 MiB; put large content in a file with difyctl_put_file.")
    properties = (descriptor.get("input") or {}).get("properties") or {}
    for key, value in data.items():
        # difyctl reads '@path' / '@-' for object, list and JSON fields, which would bypass file checks.
        if isinstance(value, str) and value.startswith("@") and not _is_scalar(properties.get(key)):
            raise PolicyError("file_reference_rejected",
                              f"input.{key}: '@file' references are not allowed; pass the JSON value itself.")
    if "include_secret" in data and data["include_secret"] not in (None, False):
        raise PolicyError("forbidden_input", "include_secret=true is not allowed: secrets must not leave Dify in plaintext.")
    return encoded


def file_fields(descriptor: dict) -> list[str]:
    return [k for k, v in (descriptor.get("bind") or {}).items() if v == "file"]


def _inside(path: str, root: Path) -> Path:
    if not isinstance(path, str) or not path or "\0" in path:
        raise PolicyError("invalid_file_path", "File paths must be non-empty strings.")
    resolved = Path(os.path.realpath(os.path.join(root, path)))
    if resolved == root or root not in resolved.parents:
        raise PolicyError("file_outside_workdir",
                          "File paths must point inside this account's work directory; upload with difyctl_put_file first.")
    return resolved


def _check_folder(folder: Path, root: Path) -> None:
    count = 0
    for current, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            entry = Path(current) / name
            if entry.is_symlink():
                raise PolicyError("file_outside_workdir", "Folders sent as input cannot contain symlinks.")
            _inside(str(entry), root)
            count += 1
            if count > MAX_FOLDER_FILES:
                raise PolicyError("invalid_file_path", "Folder has too many entries.")


def check_files(descriptor: dict, data: dict, workdir: Path) -> None:
    """Every bind=file value (path, folder, list or name->path map) must resolve inside workdir."""
    root = Path(os.path.realpath(workdir))
    for field in file_fields(descriptor):
        value = data.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            resolved = _inside(value, root)
            if resolved.is_dir():
                _check_folder(resolved, root)
            continue
        items = value.values() if isinstance(value, dict) else [value]
        for item in items:
            for path in (item if isinstance(item, list) else [item]):
                if _inside(path, root).is_dir():
                    raise PolicyError("invalid_file_path", f"input.{field}: a folder can only be the whole value.")
