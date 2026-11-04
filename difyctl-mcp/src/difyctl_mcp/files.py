"""Per-account work directories for uploads, file-field inputs and large outputs."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .policy import PolicyError
from .runner import redact

SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")


def account_directory(users_dir: Path, account_id: str) -> Path:
    name = account_id if SAFE_ID.fullmatch(account_id) else "h-" + hashlib.sha256(account_id.encode()).hexdigest()[:40]
    return users_dir / name


def workdir(users_dir: Path, account_id: str) -> Path:
    path = account_directory(users_dir, account_id) / "work"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def relative_path(value: str) -> Path:
    """A normalized relative path made only of plain segments: no '..', no absolute paths, no hidden files."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise PolicyError("invalid_file_path", "path must be a relative path such as uploads/report.pdf.")
    parts = value.replace("\\", "/").strip("/").split("/")
    if value.startswith("/") or any(not SEGMENT.fullmatch(p) or p in (".", "..") for p in parts):
        raise PolicyError("invalid_file_path", "path segments may use letters, digits, '.', '_' and '-', and cannot start with '.' or be '..'.")
    return Path(*parts)


def resolve(root: Path, value: str, *, must_exist: bool) -> Path:
    path = root / relative_path(value)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise PolicyError("invalid_file_path", "Symlinks are not allowed in the work directory.")
    real = Path(os.path.realpath(path))
    if root.resolve() not in real.parents:
        raise PolicyError("file_outside_workdir", "path escapes the work directory.")
    if must_exist and not real.is_file():
        raise PolicyError("file_not_found", "No such file in this account's work directory.")
    return path


def put_file(root: Path, value: str, content: str, encoding: str, overwrite: bool, max_bytes: int,
             retention: int) -> dict:
    if encoding == "base64":
        if len(content) > (max_bytes * 4) // 3 + 4:
            raise PolicyError("file_too_large", f"Files are limited to {max_bytes} bytes.")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise PolicyError("invalid_content", "content is not valid base64.") from None
    elif encoding == "text":
        data = content.encode()
    else:
        raise PolicyError("invalid_content", "encoding must be text or base64.")
    if len(data) > max_bytes:
        raise PolicyError("file_too_large", f"Files are limited to {max_bytes} bytes.")
    path = resolve(root, value, must_exist=False)
    if path.exists() and not overwrite:
        raise PolicyError("file_exists", "File exists; pass overwrite=true to replace it.")
    if path.exists() and not path.is_file():
        raise PolicyError("invalid_file_path", "A directory already uses that path.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    relative = str(path.relative_to(root))
    return {"path": relative, "absolute_path": str(path), "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "expires_at": int(time.time()) + retention,
            "usage": f"Use \"{relative}\" (relative to the work directory) as the value of a file field."}


def get_file(root: Path, value: str, offset: int, length: int, encoding: str) -> dict:
    path = resolve(root, value, must_exist=True)
    size = path.stat().st_size
    if offset < 0 or length <= 0:
        raise PolicyError("invalid_range", "offset must be >= 0 and length > 0.")
    with open(path, "rb") as source:
        source.seek(offset)
        data = source.read(length)
    if encoding == "base64":
        content = base64.b64encode(data).decode()
    elif encoding == "text":
        content = redact(data.decode("utf-8", "replace"))
    else:
        raise PolicyError("invalid_content", "encoding must be text or base64.")
    end = offset + len(data)
    return {"path": str(path.relative_to(root)), "size": size, "offset": offset, "length": len(data),
            "next_offset": end if end < size else None, "eof": end >= size, "encoding": encoding, "content": content}


def save_output(root: Path, operation: str, text: str) -> str:
    folder = root / "outputs"
    folder.mkdir(exist_ok=True, mode=0o700)
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + re.sub(r"[^a-z0-9_.-]", "_", operation)[:80] + \
        "-" + os.urandom(4).hex() + ".json"
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(text)
    return str(path.relative_to(root))


def expire_files(users_dir: Path, retention: int, now: float | None = None) -> int:
    """Delete regular files older than the retention period; never follows symlinks."""
    now = time.time() if now is None else now
    removed = 0
    if not users_dir.is_dir():
        return 0
    for account in users_dir.iterdir():
        work = account / "work"
        if account.is_symlink() or work.is_symlink() or not work.is_dir():
            continue
        for current, dirs, files in os.walk(work, topdown=False, followlinks=False):
            for name in files:
                entry = Path(current) / name
                try:
                    if entry.is_symlink() or entry.lstat().st_mtime < now - retention:
                        entry.unlink()
                        removed += 1
                except FileNotFoundError:
                    pass
            for name in dirs:
                entry = Path(current) / name
                try:
                    if entry.is_symlink():
                        entry.unlink()
                    elif not any(entry.iterdir()):
                        entry.rmdir()
                except OSError:
                    pass
    return removed


def remove_tree(path: str | Path | None, parent: Path) -> None:
    """Remove a config directory, but only one that lives directly under the sessions directory."""
    if not path:
        return
    path = Path(path)
    if path.parent != parent or path.is_symlink():
        return
    shutil.rmtree(path, ignore_errors=True)
