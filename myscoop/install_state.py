"""Helpers for deciding whether a myscoop app folder is really installed."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_MIN_PAYLOAD_SIZE_BYTES = int(
    os.environ.get("MYSCOOP_MIN_INSTALL_PAYLOAD_BYTES", "1024")
)

# Marker written when an installation is verified through the Windows Registry
# rather than by files inside the managed apps folder (e.g. GUI installers that
# install into Program Files). Its presence marks the version folder as valid.
REGISTRY_MARKER_FILENAME = "registry_install.json"

DIAGNOSTIC_FILENAMES = {
    "install.json",
    "metadata.json",
    "metadata_all.json",
    "gui_install_info.json",
    REGISTRY_MARKER_FILENAME,
}
DIAGNOSTIC_EXTENSIONS = {".log", ".tmp"}


@dataclass(frozen=True)
class InstallState:
    """Result of inspecting an install directory."""

    path: str
    exists: bool
    valid: bool
    total_size_bytes: int
    payload_size_bytes: int
    reason: str


def inspect_install_path(
    install_path: str,
    min_payload_size_bytes: Optional[int] = None,
) -> InstallState:
    """Inspect a version folder and reject empty/negligible payloads."""
    threshold = (
        DEFAULT_MIN_PAYLOAD_SIZE_BYTES
        if min_payload_size_bytes is None
        else min_payload_size_bytes
    )
    install_path = os.path.abspath(install_path)

    if not os.path.isdir(install_path):
        return InstallState(
            path=install_path,
            exists=False,
            valid=False,
            total_size_bytes=0,
            payload_size_bytes=0,
            reason="install folder was not created",
        )

    # An install verified via the Windows Registry is valid even if it has no
    # payload inside the managed apps folder (it lives elsewhere on the system).
    if os.path.isfile(os.path.join(install_path, REGISTRY_MARKER_FILENAME)):
        return InstallState(
            path=install_path,
            exists=True,
            valid=True,
            total_size_bytes=0,
            payload_size_bytes=0,
            reason="installation verified via Windows Registry",
        )

    total_size = 0
    payload_size = 0
    for root, _dirs, files in os.walk(install_path):
        for filename in files:
            file_path = os.path.join(root, filename)
            try:
                size = os.path.getsize(file_path)
            except OSError:
                continue

            total_size += size
            if _is_payload_file(filename):
                payload_size += size

    if payload_size <= threshold:
        return InstallState(
            path=install_path,
            exists=True,
            valid=False,
            total_size_bytes=total_size,
            payload_size_bytes=payload_size,
            reason=(
                f"install folder payload is {payload_size} bytes, "
                f"which is empty or negligible"
            ),
        )

    return InstallState(
        path=install_path,
        exists=True,
        valid=True,
        total_size_bytes=total_size,
        payload_size_bytes=payload_size,
        reason="install folder contains a non-negligible payload",
    )


def get_valid_installed_versions(
    apps_dir: str,
    app_name: str,
    cleanup_invalid: bool = False,
) -> List[str]:
    """Return valid version folders for an app, optionally deleting bad ones."""
    app_path = os.path.join(apps_dir, app_name.lower())
    if not os.path.isdir(app_path):
        return []

    valid_versions: List[str] = []
    for entry in os.listdir(app_path):
        version_path = os.path.join(app_path, entry)
        if not os.path.isdir(version_path):
            continue

        state = inspect_install_path(version_path)
        if state.valid:
            valid_versions.append(entry)
        elif cleanup_invalid:
            remove_install_path(version_path)

    if cleanup_invalid and not valid_versions and _directory_is_empty(app_path):
        remove_install_path(app_path)

    return sorted(valid_versions)


def is_app_installed(
    apps_dir: str,
    app_name: str,
    cleanup_invalid: bool = False,
) -> bool:
    """Return True only when at least one valid version folder exists."""
    return bool(
        get_valid_installed_versions(
            apps_dir,
            app_name,
            cleanup_invalid=cleanup_invalid,
        )
    )


def record_registry_install(install_path: str, entry: Dict[str, str]) -> None:
    """Persist a marker recording that an install was verified via the registry."""
    os.makedirs(install_path, exist_ok=True)
    marker = {
        "verified_by": "windows_registry",
        "display_name": entry.get("DisplayName", ""),
        "publisher": entry.get("Publisher", ""),
        "display_version": entry.get("DisplayVersion", ""),
        "install_location": entry.get("InstallLocation", ""),
    }
    marker_path = os.path.join(install_path, REGISTRY_MARKER_FILENAME)
    try:
        with open(marker_path, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=4)
    except OSError:
        pass


def remove_install_path(path: str) -> None:
    """Best-effort cleanup for incomplete install folders."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def cleanup_empty_app_folder(apps_dir: str, app_name: str) -> None:
    """Remove the app folder if all invalid version folders were removed."""
    app_path = os.path.join(apps_dir, app_name.lower())
    if _directory_is_empty(app_path):
        remove_install_path(app_path)


def _is_payload_file(filename: str) -> bool:
    name = filename.lower()
    if name in DIAGNOSTIC_FILENAMES:
        return False
    if Path(name).suffix in DIAGNOSTIC_EXTENSIONS:
        return False
    return True


def _directory_is_empty(path: str) -> bool:
    try:
        return os.path.isdir(path) and not os.listdir(path)
    except OSError:
        return False
