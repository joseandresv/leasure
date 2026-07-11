"""Runtime platform detection for the device layer and UI copy.

Leasure runs on three targets with different removable-drive models:
- windows: drives appear as letters, auto-mounted by the OS
- wsl2:    Windows drive letters bridged to /mnt/<letter> via drvfs (manual mount)
- linux:   desktop auto-mount under /media/<user>/ or /run/media/<user>/
"""

import os
import platform
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def get_platform() -> str:
    """Return 'windows', 'wsl2', or 'linux'."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux") and "microsoft" in platform.release().lower():
        return "wsl2"
    return "linux"


def device_path_placeholder() -> str:
    """Example sync-target path shown in the device UI."""
    p = get_platform()
    if p == "windows":
        return "E:\\"
    if p == "wsl2":
        return "/mnt/e"
    return f"/media/{os.environ.get('USER', 'user')}/H2"
