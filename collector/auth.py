"""Local ingest-token creation and permission checks."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


class TokenFileError(RuntimeError):
    pass


def _check_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except PermissionError:
        pass


def _load_or_create_private_value(path: str | Path, *, label: str) -> str:
    token_path = Path(path).expanduser()
    _check_parent(token_path)
    if token_path.is_symlink():
        raise TokenFileError(f"{label} file must not be a symbolic link")

    if not token_path.exists():
        value = secrets.token_urlsafe(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(token_path, flags, 0o600)
        try:
            os.write(descriptor, (value + "\n").encode("ascii"))
        finally:
            os.close(descriptor)

    details = token_path.stat()
    if not stat.S_ISREG(details.st_mode):
        raise TokenFileError(f"{label} path must be a regular file")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise TokenFileError(f"{label} file permissions must be 0600 or stricter")
    value = token_path.read_text(encoding="ascii").strip()
    if len(value) < 32 or len(value) > 256:
        raise TokenFileError(f"{label} file contains an invalid value")
    return value


def load_or_create_token(path: str | Path) -> str:
    return _load_or_create_private_value(path, label="token")


def load_or_create_identity_salt(path: str | Path) -> str:
    return _load_or_create_private_value(path, label="identity salt")
