"""
path_manager.py — User PATH Manager for myscoop

Manages the user-level PATH environment variable via the Windows registry.
NEVER touches the system PATH (HKEY_LOCAL_MACHINE). NEVER requires admin.

Usage:
    pm = PathManager()
    pm.add_to_path("C:/Users/you/myscoop/shims")
    print(pm.is_in_path("C:/Users/you/myscoop/shims"))
"""

import os
import ctypes
from typing import List

# winreg is Windows-only
try:
    import winreg
except ImportError:
    winreg = None


# SendMessageTimeout constants for broadcasting env change
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002


class PathManagerError(Exception):
    """Raised when PATH operations fail."""
    pass


class PathManager:
    """
    Reads and writes the user-level PATH environment variable.
    
    Uses HKEY_CURRENT_USER\\Environment (no admin needed).
    After modifying PATH, broadcasts WM_SETTINGCHANGE so new
    terminal windows pick up the change immediately.
    """

    # Registry key for user environment variables
    _ENV_KEY = r"Environment"

    def __init__(self) -> None:
        if winreg is None:
            raise PathManagerError(
                "winreg module not available. This tool only works on Windows."
            )

    def add_to_path(self, folder: str) -> bool:
        """
        Add a folder to the user's PATH if not already present.

        Args:
            folder: Absolute path to the folder to add.

        Returns:
            True if the folder was added, False if already present.
        """
        folder_normalized = os.path.normpath(folder)

        if self.is_in_path(folder_normalized):
            return False

        # Read current PATH
        current_path = self._read_user_path()

        # Append our folder
        if current_path and not current_path.endswith(";"):
            new_path = current_path + ";" + folder_normalized
        else:
            new_path = (current_path or "") + folder_normalized

        # Write back to registry
        self._write_user_path(new_path)

        # Broadcast the change so new terminals pick it up
        self._broadcast_change()

        print(f"  Added to PATH: {folder_normalized}")
        return True

    def remove_from_path(self, folder: str) -> bool:
        """
        Remove a folder from the user's PATH.

        Args:
            folder: Path to remove.

        Returns:
            True if removed, False if it wasn't in PATH.
        """
        folder_normalized = os.path.normpath(folder).lower()

        current_path = self._read_user_path()
        if not current_path:
            return False

        # Split, filter, rejoin
        parts = [p for p in current_path.split(";") if p.strip()]
        new_parts = [
            p for p in parts
            if os.path.normpath(p).lower() != folder_normalized
        ]

        if len(new_parts) == len(parts):
            return False  # Nothing was removed

        new_path = ";".join(new_parts)
        self._write_user_path(new_path)
        self._broadcast_change()

        print(f"  Removed from PATH: {folder}")
        return True

    def is_in_path(self, folder: str) -> bool:
        """
        Check if a folder is already in the user's PATH.

        Args:
            folder: Path to check (case-insensitive comparison).

        Returns:
            True if the folder is in PATH.
        """
        folder_normalized = os.path.normpath(folder).lower()
        current_path = self._read_user_path()

        if not current_path:
            return False

        for entry in current_path.split(";"):
            if os.path.normpath(entry.strip()).lower() == folder_normalized:
                return True

        return False

    def get_path_entries(self) -> List[str]:
        """Return the current user PATH as a list of entries."""
        current_path = self._read_user_path()
        if not current_path:
            return []
        return [p.strip() for p in current_path.split(";") if p.strip()]

    # ──────────────────────────────────────────────
    # Registry operations (HKEY_CURRENT_USER only)
    # ──────────────────────────────────────────────

    def _read_user_path(self) -> str:
        """Read the user PATH from HKCU\\Environment."""
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._ENV_KEY, 0, winreg.KEY_READ
            ) as key:
                value, reg_type = winreg.QueryValueEx(key, "Path")
                return value
        except FileNotFoundError:
            # PATH variable doesn't exist yet
            return ""
        except OSError as e:
            raise PathManagerError(f"Failed to read user PATH: {e}")

    def _write_user_path(self, new_path: str) -> None:
        """Write the user PATH to HKCU\\Environment using REG_EXPAND_SZ."""
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self._ENV_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                # REG_EXPAND_SZ allows %VARIABLE% expansion in the path
                winreg.SetValueEx(
                    key, "Path", 0, winreg.REG_EXPAND_SZ, new_path
                )
        except OSError as e:
            raise PathManagerError(f"Failed to write user PATH: {e}")

    @staticmethod
    def _broadcast_change() -> None:
        """
        Broadcast WM_SETTINGCHANGE so new terminal windows
        pick up the PATH change without requiring a reboot.
        """
        try:
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                "Environment",
                SMTO_ABORTIFHUNG,
                5000,  # 5 second timeout
                ctypes.byref(ctypes.c_ulong(0)),
            )
        except Exception:
            pass  # Non-critical if broadcast fails
