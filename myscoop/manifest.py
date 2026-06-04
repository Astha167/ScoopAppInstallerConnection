"""
manifest.py — Manifest Loader & Parser for myscoop

Searches bucket directories for app JSON manifests, parses them,
and exposes all manifest fields as clean properties.

Usage:
    manifest = Manifest("postman", "C:/Users/you/myscoop/buckets")
    print(manifest.url)         # download URL (hint stripped)
    print(manifest.url_hint)    # e.g. "#/dl.7z"
    print(manifest.version)     # "12.1.4"
    print(manifest.bin)         # ["Postman.exe"]
"""

import json
import os
import platform
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ManifestNotFoundError(Exception):
    """Raised when no manifest JSON file is found for the given app name."""
    pass


class Manifest:
    """
    Loads and parses a myscoop app manifest from the buckets directory.

    The manifest JSON can live in any subfolder of buckets_dir (e.g.
    buckets/main/postman.json). The class searches all subdirectories
    for a file named <app_name>.json.

    Attributes are exposed as properties so callers never touch raw JSON.
    """

    def __init__(self, app_name: str, buckets_dir: str) -> None:
        """
        Args:
            app_name:   Name of the app to look up (e.g. "postman").
            buckets_dir: Absolute path to the buckets root folder.

        Raises:
            ManifestNotFoundError: If no matching JSON file is found.
        """
        self.app_name: str = app_name.lower().strip()
        self.buckets_dir: str = buckets_dir
        self._data: Dict[str, Any] = {}
        self._arch_data: Dict[str, Any] = {}
        self._raw_url: str = ""
        self._url_hint: str = ""
        self._clean_url: str = ""

        # Load and parse the manifest on construction
        self._load()

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    def _load(self) -> None:
        """Search all bucket subdirectories for <app_name>.json and parse it."""
        manifest_path = self._find_manifest_file()
        with open(manifest_path, "r", encoding="utf-8") as f:
            self._data = json.load(f)

        # Resolve architecture-specific block (prefer 64bit on 64-bit OS)
        self._resolve_architecture()

        # Parse URL and separate the hint fragment
        self._parse_url()

    def _find_manifest_file(self) -> str:
        """
        Walk every immediate subdirectory of buckets_dir looking for
        a file named <app_name>.json (case-insensitive).

        Returns:
            Absolute path to the manifest file.

        Raises:
            ManifestNotFoundError: If not found in any bucket.
        """
        buckets_path = Path(self.buckets_dir)

        if not buckets_path.exists():
            raise ManifestNotFoundError(
                f"Buckets directory does not exist: {self.buckets_dir}\n"
                f"  Suggested fix: Run 'myscoop bucket add main <repo_url>'"
            )

        # Search each bucket subfolder (e.g. buckets/main/, buckets/extras/)
        for bucket_dir in sorted(buckets_path.iterdir()):
            if not bucket_dir.is_dir():
                continue
            # Also search nested "bucket" subfolder (common in Scoop repos)
            search_dirs = [bucket_dir, bucket_dir / "bucket"]
            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue
                candidate = search_dir / f"{self.app_name}.json"
                if candidate.exists():
                    return str(candidate)

        raise ManifestNotFoundError(
            f"App '{self.app_name}' not found in any bucket.\n"
            f"  Suggested fix: Run 'myscoop search {self.app_name}' "
            f"to find available apps."
        )

    def _resolve_architecture(self) -> None:
        """
        Pick the correct architecture block from the manifest.
        On 64-bit Windows → use "64bit", fall back to "32bit".
        If no architecture block exists, use top-level fields directly.
        """
        arch_block = self._data.get("architecture", {})

        if arch_block:
            # Detect current architecture
            is_64bit = platform.machine().endswith("64")
            if is_64bit and "64bit" in arch_block:
                self._arch_data = arch_block["64bit"]
            elif "32bit" in arch_block:
                self._arch_data = arch_block["32bit"]
            elif "64bit" in arch_block:
                # Fallback: only 64bit is defined
                self._arch_data = arch_block["64bit"]
            else:
                self._arch_data = {}
        else:
            self._arch_data = {}

    def _parse_url(self) -> None:
        """
        Extract the download URL. The URL may come from the architecture
        block or from a top-level "url" field. Strip the fragment hint
        (e.g. #/dl.7z) and store it separately.
        """
        # Architecture-specific URL takes priority
        raw = self._arch_data.get("url", "")
        if not raw:
            raw = self._data.get("url", "")

        self._raw_url = raw

        # Split at '#' to separate hint — e.g. "...exe#/dl.7z"
        if "#" in raw:
            self._clean_url, self._url_hint = raw.split("#", 1)
            self._url_hint = "#" + self._url_hint  # keep the # prefix
        else:
            self._clean_url = raw
            self._url_hint = ""

    # ──────────────────────────────────────────────
    # Public properties
    # ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        """App name from manifest (or the lookup name if not in JSON)."""
        return self._data.get("name", self.app_name)

    @property
    def version(self) -> str:
        """Version string, e.g. '12.1.4'."""
        return self._data.get("version", "unknown")

    @property
    def description(self) -> str:
        """Human-readable app description."""
        return self._data.get("description", "")

    @property
    def homepage(self) -> str:
        """App homepage URL."""
        return self._data.get("homepage", "")

    @property
    def license(self) -> str:
        """License identifier."""
        lic = self._data.get("license", "")
        # License can be a string or a dict with "identifier" key
        if isinstance(lic, dict):
            return lic.get("identifier", str(lic))
        return str(lic)

    @property
    def depends(self) -> List[str]:
        """List of app names this app depends on."""
        return self._data.get("depends", [])

    @property
    def url(self) -> str:
        """
        Clean download URL with the hint fragment removed.
        e.g. "https://dl.pstmn.io/download/12.1.4/Postman.exe"
        """
        return self._clean_url

    @property
    def url_hint(self) -> str:
        """
        The fragment hint from the URL, if any.
        e.g. "#/dl.7z" means treat the downloaded file as a 7z archive.
        Returns empty string if no hint.
        """
        return self._url_hint

    @property
    def bin(self) -> List[str]:
        """List of executable filenames to create shims for."""
        return self._data.get("bin", [])

    @property
    def extract_dir(self) -> str:
        """
        Subfolder inside the archive where the real app files live.
        After extraction, contents of this subfolder are moved up to root.
        Returns empty string if not set.
        """
        # Check architecture block first, then top-level
        val = self._arch_data.get("extract_dir", "")
        if not val:
            val = self._data.get("extract_dir", "")
        return val

    @property
    def shortcuts(self) -> List[List[str]]:
        """
        List of [exe_path, shortcut_name] pairs for Start Menu shortcuts.
        e.g. [["Postman.exe", "Postman"]]
        """
        return self._data.get("shortcuts", [])

    @property
    def installer(self) -> Dict[str, Any]:
        """
        Installer configuration dict.
        e.g. {"type": "7z"} or {"type": "msi", "args": ["/qn"]}
        """
        return self._data.get("installer", {})

    @property
    def installer_type(self) -> str:
        """Shortcut: installer type string (e.g. '7z', 'msi', 'nsis', 'inno')."""
        return self.installer.get("type", "")

    @property
    def installer_args(self) -> List[str]:
        """Shortcut: installer arguments list."""
        return self.installer.get("args", [])

    @property
    def installer_needs_gui(self) -> bool:
        """True if the installer requires GUI automation (type='gui')."""
        return self.installer_type.lower() == "gui"

    @property
    def gui_exe(self) -> str:
        """
        Relative path to the GUI setup executable inside the extracted dir.
        e.g. 'AutomationBuilderSetup\\ABB_Setup.exe'
        """
        return self.installer.get("gui_exe", "")

    @property
    def post_install(self) -> List[str]:
        """List of commands to run after installation."""
        return self._data.get("post_install", [])

    @property
    def raw_data(self) -> Dict[str, Any]:
        """The complete raw manifest dict (for debugging / info display)."""
        return self._data

    # ──────────────────────────────────────────────
    # Display
    # ──────────────────────────────────────────────

    def summary(self) -> str:
        """Return a short one-line summary string."""
        return f"{self.name} ({self.version}) — {self.description}"

    def __repr__(self) -> str:
        return f"Manifest(name={self.name!r}, version={self.version!r})"
