"""Atomic writes for files that hold credentials, readable by the owner only.

On drvfs (WSL2 /mnt/<letter>) and native Windows chmod does nothing useful: the file
inherits the folder's NTFS ACL, so it is hardened with icacls instead."""

import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from services.platform import get_platform

logger = logging.getLogger(__name__)

_DRVFS = re.compile(r"^/mnt/([a-zA-Z])(/|$)")
_windows_user: str | None = None


def _run(args: list[str]) -> tuple[int, str]:
    """(exit code, stdout) of a helper binary; -1 when it cannot run."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("%s could not run: %s", args[0], e.__class__.__name__)
        return -1, ""
    return proc.returncode, proc.stdout.strip()


def is_drvfs(path: Path) -> bool:
    return get_platform() == "wsl2" and bool(_DRVFS.match(Path(path).resolve().as_posix()))


def _windows_path(path: Path) -> str:
    posix = Path(path).resolve().as_posix()
    match = _DRVFS.match(posix)
    rest = posix[match.end():] if match else ""
    return f"{match.group(1).upper()}:\\" + rest.replace("/", "\\")


def _wsl_windows_user() -> str | None:
    # Only a successful answer is kept: interop can be slow right after WSL boots.
    global _windows_user
    if _windows_user is None:
        code, out = _run(["whoami.exe"])
        if code == 0 and out:
            _windows_user = out
    return _windows_user


def _icacls(binary: str, target: str, user: str | None, path: Path) -> bool:
    if not user:
        logger.warning("Cannot restrict %s: the Windows user name is unknown", path)
        return False
    code, _ = _run([binary, target, "/inheritance:r", "/grant:r", f"{user}:F"])
    if code != 0:
        logger.warning("icacls could not restrict %s (exit %s)", path, code)
    return code == 0


def harden(path: Path) -> bool:
    """Make `path` readable by the owner only; True when that worked."""
    platform = get_platform()
    if platform == "windows":
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
        return _icacls("icacls", str(path), user or None, path)
    if is_drvfs(path):
        return _icacls("icacls.exe", _windows_path(path), _wsl_windows_user(), path)
    os.chmod(path, 0o600)
    return Path(path).stat().st_mode & 0o077 == 0


def _write(path: Path, text: str | Callable[[bool], str], protect: bool) -> bool:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        # Hardened while still empty, so the secret never sits in a world-readable file;
        # the rename keeps the explicit DACL on NTFS.
        protected = harden(tmp) if protect else False
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text(protected) if callable(text) else text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return protected


def write_secret(path: Path, text: str | Callable[[bool], str]) -> bool:
    """Atomically write a credential file; True when only the owner can read it. A callable `text`
    is rendered once that result is known, so the file can record it."""
    return _write(Path(path), text, protect=True)


def write_atomic(path: Path, text: str) -> None:
    """Atomically replace a non-secret state file (no ACL changes)."""
    _write(Path(path), text, protect=False)
