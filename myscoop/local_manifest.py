"""
local_manifest.py - Generate manifests for user-provided local installer files.

Creates a minimal Scoop-style manifest in buckets/main when the user provides
an installer path that does not already have a known manifest.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, cast


from myscoop.manifest import Manifest, ManifestNotFoundError
from myscoop.metadata import AppMetadata
from myscoop.silent_installer import SilentInstaller


class LocalManifestError(Exception):
    """Raised when a manifest cannot be generated for a local installer."""


class LocalManifestManager:
    """Creates and reuses local manifests for installer files."""

    _KNOWN_SUFFIXES = (
        ".sfx.exe",
        ".exe",
        ".msi",
        ".zip",
        ".7z",
        ".msix",
        ".msixbundle",
        ".appx",
        ".appxbundle",
    )

    def __init__(self, buckets_dir: str, main_bucket_name: str = "main") -> None:
        self.buckets_dir = os.path.abspath(buckets_dir)
        self.main_bucket_dir = os.path.join(self.buckets_dir, main_bucket_name)
        os.makedirs(self.main_bucket_dir, exist_ok=True)

    def ensure_manifest(
        self,
        installer_path: str,
        app_name: Optional[str] = None,
    ) -> str:
        """
        Ensure a manifest exists for the given local installer.

        Returns the normalized app name that should be installed.
        """
        installer_path = os.path.abspath(installer_path)
        if not os.path.isfile(installer_path):
            raise LocalManifestError(f"Local installer not found: {installer_path}")

        normalized_name = self.normalize_app_name(app_name or installer_path)
        if app_name is None:
            matched_name = self.find_matching_manifest_for_installer(installer_path)
            if matched_name:
                return matched_name

        if self._manifest_exists_anywhere(normalized_name):
            return normalized_name

        manifest_path = os.path.join(self.main_bucket_dir, f"{normalized_name}.json")
        manifest_data = self.build_manifest(normalized_name, installer_path)

        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest_data, fh, indent=4, ensure_ascii=False)
            fh.write("\n")

        return normalized_name

    def build_manifest(self, app_name: str, installer_path: str) -> Dict[str, Any]:
        """Build a minimal manifest for a local installer file."""
        installer_path = os.path.abspath(installer_path)
        metadata = self._extract_file_metadata(installer_path)
        version = (
            metadata.get("fileversion")
            or self.extract_version_from_text(Path(installer_path).name)
            or "unknown"
        )
        description = (
            metadata.get("filedescription")
            or self._describe_from_filename(Path(installer_path).name)
        )
        installer_type = self.detect_installer_type(installer_path)

        manifest: Dict[str, object] = {
            "name": app_name,
            "version": version,
            "description": description,
            "homepage": "",
            "license": "Proprietary",
            "depends": [],
            "architecture": {
                "64bit": {
                    "url": Path(installer_path).resolve().as_uri(),
                }
            },
            "bin": [],
            "shortcuts": [],
            "post_install": [],
        }

        if installer_type:
            manifest["installer"] = {"type": installer_type}

        return manifest

    def find_matching_manifest_for_installer(self, installer_path: str) -> Optional[str]:
        """
        Try to reuse an existing manifest by matching the installer file name
        against manifest URLs already present in the buckets.

        When multiple manifests match (e.g. 'abb' and
        'abb-automation-builder-v2-9-0-322-x64'), the shortest name wins
        because user-created manifests with correct settings (like gui
        installer type) are typically shorter than auto-generated ones.
        """
        from urllib.parse import unquote

        target_name = os.path.basename(installer_path).lower()
        buckets_root = Path(self.buckets_dir)
        if not buckets_root.exists():
            return None

        matches: list[str] = []

        for bucket_dir in sorted(p for p in buckets_root.iterdir() if p.is_dir()):
            for search_dir in (bucket_dir, bucket_dir / "bucket"):
                if not search_dir.exists():
                    continue
                for manifest_file in search_dir.glob("*.json"):
                    try:
                        with open(manifest_file, "r", encoding="utf-8") as fh:
                            data = json.load(fh)
                    except Exception:
                        continue

                    for url in self._iter_manifest_urls(data):
                        # Decode URL-encoded characters (e.g. %20 -> space)
                        decoded_basename = unquote(os.path.basename(url)).lower()
                        if decoded_basename == target_name:
                            matches.append(manifest_file.stem.lower())

        if not matches:
            return None
        # Prefer shortest name (user-created manifests are typically shorter)
        return min(matches, key=len)

    @classmethod
    def normalize_app_name(cls, name_or_path: str) -> str:
        """Normalize an app name or file path into a manifest-safe app id."""
        candidate = Path(name_or_path).name if os.path.sep in name_or_path or "/" in name_or_path else name_or_path
        candidate = cls.strip_known_suffixes(candidate)
        candidate = candidate.replace("&", " and ")
        candidate = re.sub(r"[^A-Za-z0-9]+", "-", candidate.lower())
        candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
        return candidate or "local-installer"

    @classmethod
    def strip_known_suffixes(cls, filename: str) -> str:
        """Remove installer suffixes like .sfx.exe, .exe, .msi, etc."""
        lowered = filename.lower()
        for suffix in cls._KNOWN_SUFFIXES:
            if lowered.endswith(suffix):
                return filename[: -len(suffix)]
        return Path(filename).stem

    @staticmethod
    def extract_version_from_text(text: str) -> Optional[str]:
        """Extract a likely version string from a file name."""
        matches = re.findall(r"\d+(?:\.\d+){1,}", text)
        if matches:
            return cast(str, max(matches, key=len))

        year_match = re.search(r"(?<!\d)(20\d{2}|19\d{2})(?!\d)", text)
        if year_match:
            return year_match.group(1)
        return None

    def detect_installer_type(self, installer_path: str) -> str:
        """Infer installer type from extension and executable contents."""
        suffixes = [s.lower() for s in Path(installer_path).suffixes]
        if suffixes[-1:] == [".msi"]:
            return "msi"
        if suffixes[-1:] == [".zip"]:
            return "zip"
        if suffixes[-1:] == [".7z"]:
            return "7z"
        if suffixes[-2:] == [".sfx", ".exe"]:
            return ""

        if suffixes[-1:] == [".exe"]:
            detected = SilentInstaller(apps_dir="").detect_installer_type(installer_path)
            return detected or ""

        return ""

    def _manifest_exists_anywhere(self, app_name: str) -> bool:
        try:
            Manifest(app_name, self.buckets_dir)
            return True
        except ManifestNotFoundError:
            return False

    @staticmethod
    def _iter_manifest_urls(data: Dict[str, Any]) -> list[str]:
        urls: list[str] = []
        top_level_url = data.get("url")
        if isinstance(top_level_url, str) and top_level_url:
            urls.append(top_level_url.split("#", 1)[0])

        architecture = data.get("architecture")
        if isinstance(architecture, dict):
            for arch_data in architecture.values():
                if not isinstance(arch_data, dict):
                    continue
                url = arch_data.get("url")
                if isinstance(url, str) and url:
                    urls.append(url.split("#", 1)[0])
        return urls

    @staticmethod
    def _extract_file_metadata(installer_path: str) -> Dict[str, Optional[str]]:
        metadata: Dict[str, Optional[str]] = {
            "fileversion": None,
            "filedescription": None,
            "filemanufacturer": None,
        }
        try:
            app_metadata = AppMetadata(installer_path)
            extracted = app_metadata.extract()
            metadata["fileversion"] = extracted.get("fileversion")
            metadata["filedescription"] = extracted.get("filedescription")
            metadata["filemanufacturer"] = extracted.get("filemanufacturer")
        except Exception:
            pass
        return metadata

    @classmethod
    def _describe_from_filename(cls, filename: str) -> str:
        base = cls.strip_known_suffixes(filename)
        words = re.sub(r"[_\-\.]+", " ", base).strip()
        return words or filename
