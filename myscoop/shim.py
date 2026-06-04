"""
shim.py — Shim Manager for myscoop

Creates lightweight .bat shim files in the shims directory so apps
can be launched from anywhere without polluting PATH.

Each shim is a simple batch file that forwards to the real executable.

Usage:
    sm = ShimManager("C:/Users/you/myscoop/shims")
    sm.create_shim("Postman.exe", "C:/Users/you/myscoop/apps/postman/12.1.4/Postman.exe")
"""

import os
from pathlib import Path
from typing import Dict, List


class ShimManager:
    """
    Manages .bat shim files for installed applications.

    Shims are stored in a single directory that gets added to PATH once.
    Each shim forwards execution to the real exe with all arguments.
    """

    def __init__(self, shims_dir: str) -> None:
        """
        Args:
            shims_dir: Path to the shims directory (added to user PATH).
        """
        self.shims_dir: str = shims_dir
        os.makedirs(shims_dir, exist_ok=True)

    def create_shim(
        self,
        exe_name: str,
        exe_path: str,
        gui: bool = False,
    ) -> str:
        """
        Create a .bat shim for an executable.

        Args:
            exe_name: Name of the executable (e.g. "Postman.exe").
            exe_path: Full absolute path to the real executable.
            gui:      If True, use 'start /b' to avoid terminal flash
                      for GUI applications.

        Returns:
            Path to the created .bat shim file.
        """
        # Strip .exe extension if present to get the shim name
        shim_name = Path(exe_name).stem
        shim_path = os.path.join(self.shims_dir, f"{shim_name}.bat")

        # Normalize the exe path to use backslashes (Windows standard)
        exe_path_normalized = os.path.normpath(exe_path)

        if gui:
            # GUI app: use 'start /b' to launch without holding the terminal
            content = f'@echo off\nstart "" /b "{exe_path_normalized}" %*\n'
        else:
            # CLI app: normal forwarding, keeps terminal attached
            content = f'@echo off\n"{exe_path_normalized}" %*\n'

        with open(shim_path, "w", encoding="utf-8") as f:
            f.write(content)

        return shim_path

    def remove_shim(self, exe_name: str) -> bool:
        """
        Remove a shim .bat file.

        Args:
            exe_name: Name of the exe (e.g. "Postman.exe") or shim name.

        Returns:
            True if the shim was found and removed.
        """
        shim_name = Path(exe_name).stem
        shim_path = os.path.join(self.shims_dir, f"{shim_name}.bat")

        if os.path.exists(shim_path):
            os.remove(shim_path)
            return True
        return False

    def remove_shims_for_app(self, bin_list: List[str]) -> int:
        """
        Remove all shims for a given app's bin entries.

        Args:
            bin_list: List of exe names from the manifest bin field.

        Returns:
            Number of shims removed.
        """
        removed = 0
        for exe_name in bin_list:
            if self.remove_shim(exe_name):
                removed += 1
        return removed

    def list_shims(self) -> List[Dict[str, str]]:
        """
        List all installed shims.

        Returns:
            List of dicts with 'name' and 'target' keys.
        """
        shims = []
        for filename in sorted(os.listdir(self.shims_dir)):
            if filename.endswith(".bat"):
                filepath = os.path.join(self.shims_dir, filename)
                target = self._read_shim_target(filepath)
                shims.append({
                    "name": Path(filename).stem,
                    "target": target,
                })
        return shims

    def shim_exists(self, exe_name: str) -> bool:
        """Check if a shim exists for the given exe name."""
        shim_name = Path(exe_name).stem
        shim_path = os.path.join(self.shims_dir, f"{shim_name}.bat")
        return os.path.exists(shim_path)

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _read_shim_target(shim_path: str) -> str:
        """
        Read a shim .bat file and extract the target exe path.
        Parses the line containing the quoted exe path.
        """
        try:
            with open(shim_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    # Skip @echo off and empty lines
                    if not line or line.lower() == "@echo off":
                        continue
                    # Extract path from quotes: "C:\path\to\exe" %*
                    # or: start "" /b "C:\path\to\exe" %*
                    parts = line.split('"')
                    for part in parts:
                        if os.path.sep in part or "/" in part:
                            return part
        except (IOError, OSError):
            pass
        return "unknown"
