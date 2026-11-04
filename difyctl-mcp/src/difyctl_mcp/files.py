"""Per-account work directories for uploads, file-field inputs and large outputs."""
from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .policy import PolicyError
from .runner import redact

# Quota checks and the writes they admit are serialized, so concurrent uploads cannot overshoot.
STORAGE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Quota:
    users_dir: Path
    account_bytes: int
    account_files: int
    total_bytes: int


DIRECTORY_COST = 4096
MAX_PATH_DEPTH = 8


def usage(folder: Path) -> tuple[int, int]:
    """Bytes and entries under folder, without following symlinks; each directory costs one entry and a block."""
    size = count = 0
    for current, dirs, names in os.walk(folder, followlinks=False):
        size += DIRECTORY_COST * len(dirs)
        count += len(dirs)
        for name in names:
            try:
                info = os.lstat(os.path.join(current, name))
            except FileNotFoundError:
                continue
            size += info.st_size
            count += 1
    return size, count


def missing_directories(root: Path, path: Path) -> int:
    """How many directories writing path would create below root."""
    count, parent = 0, path.parent
    while parent != root and root in parent.parents and not parent.exists():
        count += 1
        parent = parent.parent
    return count


def check_quota(quota: Quota | None, root: Path, adding: int, replacing: int | None = None, new_dirs: int = 0) -> None:
    """Refuse a write that would exceed the account or server storage limit.

    replacing: size of an overwritten file; new_dirs: directories the write creates, charged like usage() does.
    """
    if quota is None:
        return
    used, files = usage(root)
    freed = replacing or 0
    adding += new_dirs * DIRECTORY_COST
    entries = new_dirs + (0 if replacing is not None else 1)
    if used - freed + adding > quota.account_bytes or files + entries > quota.account_files:
        raise PolicyError("quota_exceeded", f"This account's work directory is limited to {quota.account_bytes // (1024 * 1024)} MiB "
                          f"and {quota.account_files} files; files expire after 24 hours.")
    if usage(quota.users_dir)[0] - freed + adding > quota.total_bytes:
        raise PolicyError("quota_exceeded", "The server's work storage is full; try again later.")


def remove_if_over_quota(quota: Quota | None, root: Path, path: Path) -> bool:
    """After a write whose size was unknown in advance (a download): delete it if limits are now exceeded."""
    if quota is None or not path.is_file() or path.is_symlink():
        return False
    with STORAGE_LOCK:
        used, files = usage(root)
        if used <= quota.account_bytes and files <= quota.account_files and usage(quota.users_dir)[0] <= quota.total_bytes:
            return False
        path.unlink()
        return True


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
    if len(parts) > MAX_PATH_DEPTH:
        raise PolicyError("invalid_file_path", f"path may have at most {MAX_PATH_DEPTH} segments.")
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
             retention: int, quota: Quota | None = None) -> dict:
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
    with STORAGE_LOCK:
        if path.exists() and not overwrite:
            raise PolicyError("file_exists", "File exists; pass overwrite=true to replace it.")
        if path.exists() and not path.is_file():
            raise PolicyError("invalid_file_path", "A directory already uses that path.")
        check_quota(quota, root, len(data), path.stat().st_size if path.is_file() else None,
                    missing_directories(root, path))
        _publish(path, data, overwrite)
    relative = str(path.relative_to(root))
    return {"path": relative, "absolute_path": str(path), "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "expires_at": int(time.time()) + retention,
            "usage": f"Use \"{relative}\" (relative to the work directory) as the value of a file field."}


def _publish(path: Path, data: bytes, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        os.chmod(temporary, 0o600)
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                # link() fails if the path appeared meanwhile, so a concurrent upload is never replaced.
                os.link(temporary, path)
            except FileExistsError:
                raise PolicyError("file_exists", "File exists; pass overwrite=true to replace it.") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def get_file(root: Path, value: str, offset: int, length: int, encoding: str) -> dict:
    path = resolve(root, value, must_exist=True)
    size = path.stat().st_size
    if offset < 0 or length <= 0:
        raise PolicyError("invalid_range", "offset must be >= 0 and length > 0.")
    if encoding not in ("text", "base64"):
        raise PolicyError("invalid_content", "encoding must be text or base64.")
    with open(path, "rb") as source:
        source.seek(offset)
        data = source.read(length)
        if encoding == "text" and offset + len(data) < size:
            # Never split a UTF-8 character: stop before an incomplete trailing sequence.
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            decoder.decode(data, final=False)
            pending = len(decoder.getstate()[0])
            if pending and pending == len(data):
                data += source.read(4)
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                decoder.decode(data, final=False)
                pending = len(decoder.getstate()[0])
            data = data[:len(data) - pending]
    content = base64.b64encode(data).decode() if encoding == "base64" else redact(data.decode("utf-8", "replace"))
    end = offset + len(data)
    return {"path": str(path.relative_to(root)), "size": size, "offset": offset, "length": len(data),
            "next_offset": end if end < size else None, "eof": end >= size, "encoding": encoding, "content": content}


def save_output(root: Path, operation: str, text: str, quota: Quota | None = None) -> str:
    folder = root / "outputs"
    folder.mkdir(exist_ok=True, mode=0o700)
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + re.sub(r"[^a-z0-9_.-]", "_", operation)[:80] + \
        "-" + os.urandom(4).hex() + ".json"
    path = folder / name
    data = text.encode("utf-8")
    with STORAGE_LOCK:
        check_quota(quota, root, len(data))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
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
