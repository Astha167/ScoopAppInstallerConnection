"""
extractor.py — Archive & Installer Extractor for myscoop

Extracts app archives (.zip, .7z, .msi) or handles exe-as-7z archives.
Applies extract_dir flattening when specified.

IMPORTANT: For exe-as-7z (common with NSIS/Electron installers like Postman),
py7zr alone cannot extract them. We first try py7zr for pure .7z archives,
then fall back to the system 7z.exe command-line tool which can handle
NSIS self-extracting archives and many other formats.

Usage:
    ex = Extractor("C:/Users/you/myscoop/apps")
    app_dir = ex.extract(filepath, "postman", "12.1.4", "app", "#/dl.7z")
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional

try:
    import py7zr
except ImportError:
    py7zr = None


class ExtractionError(Exception):
    """Raised when extraction fails."""
    pass


class Extractor:
    """
    Extracts downloaded archives/installers into the apps directory.

    Target structure: apps_dir/<app_name>/<version>/
    Supports: .zip, .7z, .msi, and exe-as-7z (via url_hint).
    """

    # Common locations where 7-Zip command-line tool is installed
    _7Z_PATHS = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]

    def __init__(self, apps_dir: str) -> None:
        """
        Args:
            apps_dir: Root apps directory (e.g. C:/Users/you/myscoop/apps).
        """
        self.apps_dir: str = apps_dir
        os.makedirs(apps_dir, exist_ok=True)
        self._7z_exe: Optional[str] = self._find_7z_exe()

    def extract(
        self,
        filepath: str,
        app_name: str,
        version: str,
        extract_dir: str = "",
        url_hint: str = "",
        installer_type: str = "",
        installer_args: Optional[List[str]] = None,
    ) -> str:
        """
        Extract an archive or installer into apps/<app_name>/<version>/.

        Args:
            filepath:       Path to the downloaded file.
            app_name:       App name.
            version:        Version string.
            extract_dir:    Subfolder inside archive to flatten up.
            url_hint:       URL fragment hint (e.g. "#/dl.7z").
            installer_type: Manifest installer.type (e.g. "7z", "msi").
            installer_args: Custom arguments list to pass to the installer.

        Returns:
            Absolute path to the app installation directory.

        Raises:
            ExtractionError: If extraction fails.
        """
        app_dir = os.path.join(self.apps_dir, app_name, version)
        os.makedirs(app_dir, exist_ok=True)

        # Determine extraction method
        method = self._detect_method(filepath, url_hint, installer_type)
        print(f"  Extracting ({method}) ...")

        try:
            if method == "7z":
                self._extract_7z(filepath, app_dir)
            elif method == "zip":
                self._extract_zip(filepath, app_dir)
            else:
                raise ExtractionError(
                    f"Unsupported extraction method: {method}\n"
                    f"  File: {filepath}"
                )
        except ExtractionError:
            raise
        except Exception as e:
            raise ExtractionError(
                f"Extraction failed: {e}\n"
                f"  File: {filepath}\n"
                f"  Suggested fix: Try 'myscoop cache rm {app_name}' and retry."
            )

        # Handle Squirrel/nupkg packages (common in Electron apps like Postman)
        # After 7z extraction, we may find a *-full.nupkg which is a zip
        # containing the real app files under lib/net45/
        self._handle_squirrel_nupkg(app_dir)

        # Apply extract_dir flattening if specified
        if extract_dir:
            self._flatten_extract_dir(app_dir, extract_dir)

        print("  Extracting ... done")
        return app_dir

    # ──────────────────────────────────────────────
    # Extraction methods
    # ──────────────────────────────────────────────

    def _extract_7z(self, filepath: str, dest_dir: str) -> None:
        """
        Extract using 7z — tries py7zr first (pure .7z files),
        then falls back to 7z.exe CLI (handles NSIS, Inno, SFX, etc.).
        """
        errors: List[str] = []

        # Strategy 1: Try py7zr (works for pure .7z archives)
        if py7zr is not None:
            try:
                with py7zr.SevenZipFile(filepath, mode="r") as archive:
                    archive.extractall(path=dest_dir)
                return  # Success!
            except Exception as e:
                errors.append(f"py7zr: {e}")

        # Strategy 2: Try 7z.exe command-line tool
        # This handles NSIS self-extracting exes, Electron apps, etc.
        if self._7z_exe:
            try:
                self._extract_with_7z_exe(filepath, dest_dir)
                return  # Success!
            except Exception as e:
                errors.append(f"7z.exe: {e}")

        # Both failed
        error_detail = "\n    ".join(errors) if errors else "py7zr not installed and 7z.exe not found"
        raise ExtractionError(
            f"7z extraction failed:\n    {error_detail}\n"
            f"  The file may not be a valid 7z/NSIS archive.\n"
            f"  Suggested fix: Install 7-Zip (https://7-zip.org) or run 'myscoop cache rm <app>' and retry."
        )

    def _extract_with_7z_exe(self, filepath: str, dest_dir: str) -> None:
        """
        Extract using the 7z.exe command-line tool.
        
        Uses: 7z.exe x <archive> -o<dest_dir> -y
            x  = extract with full paths
            -o = output directory (no space between -o and path!)
            -y = assume Yes on all queries
        """
        result = subprocess.run(
            [self._7z_exe, "x", filepath, f"-o{dest_dir}", "-y"],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout for large archives
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            stdout = result.stdout.strip() if result.stdout else ""
            error_info = stderr or stdout or "Unknown error"
            raise RuntimeError(
                f"7z.exe exited with code {result.returncode}: {error_info}"
            )

    def _extract_zip(self, filepath: str, dest_dir: str) -> None:
        """Extract using Python's zipfile module."""
        try:
            with zipfile.ZipFile(filepath, "r") as zf:
                self._safe_extract_zip(zf, dest_dir)
        except zipfile.BadZipFile:
            raise ExtractionError(
                f"Not a valid ZIP file: {filepath}\n"
                f"  Suggested fix: Run 'myscoop cache rm <app>' and retry."
            )

    def _safe_extract_zip(self, zf: zipfile.ZipFile, dest_dir: str) -> None:
        """Extract zip entries while blocking paths outside dest_dir."""
        dest_root = Path(dest_dir).resolve()
        for member in zf.infolist():
            member_name = member.filename.replace("\\", "/")
            target = (dest_root / member_name).resolve()
            try:
                target.relative_to(dest_root)
            except ValueError:
                raise ExtractionError(
                    f"Unsafe ZIP entry blocked: {member.filename}"
                )
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

    def _handle_squirrel_nupkg(self, app_dir: str) -> None:
        """
        Handle Squirrel/NuGet packaged apps (common in Electron apps).

        After 7z extraction, Squirrel-based apps look like:
            app_dir/
                Postman-12.1.4-full.nupkg    ← zip containing the real app
                Update.exe
                setupIcon.ico
                RELEASES

        The .nupkg is a zip file with the structure:
            lib/net45/Postman.exe            ← the real app
            lib/net45/resources/...

        This method detects this pattern, extracts lib/net45/ contents
        to the app_dir, and removes the Squirrel scaffolding.
        """
        # Find *-full.nupkg files
        nupkg_files = [
            f for f in os.listdir(app_dir)
            if f.endswith("-full.nupkg")
        ]

        if not nupkg_files:
            return  # Not a Squirrel package

        nupkg_path = os.path.join(app_dir, nupkg_files[0])
        print(f"  Detected Squirrel package: {nupkg_files[0]}")
        print(f"  Extracting app from nupkg ...")

        try:
            # nupkg is a zip file — extract lib/net45/ contents
            with zipfile.ZipFile(nupkg_path, "r") as zf:
                # Find entries under lib/net45/ (or lib/net*/)
                lib_prefix = None
                for entry in zf.namelist():
                    if entry.startswith("lib/net") and "/" in entry[4:]:
                        # e.g. "lib/net45/" — capture the prefix
                        lib_prefix = entry.split("/")[0] + "/" + entry.split("/")[1] + "/"
                        break

                if not lib_prefix:
                    print("  Warning: No lib/net* folder found in nupkg, skipping")
                    return

                # Extract only files under lib/net45/ and flatten to app_dir
                for entry in zf.namelist():
                    if entry.startswith(lib_prefix) and not entry.endswith("/"):
                        # Relative path after removing the lib/net45/ prefix
                        relative = entry[len(lib_prefix):]
                        if relative:
                            dest_root = Path(app_dir).resolve()
                            target_path = (dest_root / relative).resolve()
                            try:
                                target_path.relative_to(dest_root)
                            except ValueError:
                                print(f"  Warning: skipped unsafe nupkg entry: {entry}")
                                continue
                            os.makedirs(target_path.parent, exist_ok=True)
                            with zf.open(entry) as src, open(target_path, "wb") as dst:
                                shutil.copyfileobj(src, dst)

            # Clean up Squirrel scaffolding files
            squirrel_files = [
                nupkg_files[0], "Update.exe", "RELEASES",
                "setupIcon.ico", "background.gif",
            ]
            for sf in squirrel_files:
                sf_path = os.path.join(app_dir, sf)
                if os.path.exists(sf_path):
                    os.remove(sf_path)

            print(f"  Extracted app files from {lib_prefix}")

        except Exception as e:
            print(f"  Warning: nupkg extraction failed: {e}")
            # Non-fatal — the app may still work from other extracted files

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _find_7z_exe(self) -> Optional[str]:
        """
        Find the 7z.exe command-line tool on the system.
        Checks common install locations and the system PATH.
        """
        # Check common install paths
        for path in self._7Z_PATHS:
            if os.path.exists(path):
                return path

        # Check if 7z is in PATH
        try:
            result = subprocess.run(
                ["where", "7z"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        except Exception:
            pass

        return None

    def _detect_method(self, filepath: str, url_hint: str, installer_type: str) -> str:
        """
        Determine the extraction method from url_hint, installer_type,
        or file extension (in that priority order).
        """
        # 1. URL hint takes highest priority
        if url_hint:
            hint_lower = url_hint.lower()
            if ".7z" in hint_lower:
                return "7z"
            if ".zip" in hint_lower:
                return "zip"
            if ".exe" in hint_lower:
                return "7z"  # self-extracting exe → treat as 7z archive

        # 2. Manifest installer.type
        if installer_type:
            it_lower = installer_type.lower()
            if it_lower in ("7z", "zip"):
                return it_lower

        # 3. File extension fallback
        ext = Path(filepath).suffix.lower()
        ext_map = {
            ".7z": "7z",
            ".zip": "zip",
        }
        if ext in ext_map:
            return ext_map[ext]

        # Default to 7z for .exe files (many are self-extracting 7z)
        if ext == ".exe":
            return "7z"

        return "unknown"

    def _flatten_extract_dir(self, app_dir: str, extract_dir: str) -> None:
        """
        If extract_dir is set (e.g. "app"), move everything from
        app_dir/app/* up to app_dir/* and remove the empty subfolder.
        """
        sub_path = os.path.join(app_dir, extract_dir)
        if not os.path.isdir(sub_path):
            # extract_dir doesn't exist — not an error, some archives
            # may not have it. Just skip silently.
            return

        # Move all contents from subfolder up to app_dir
        for item in os.listdir(sub_path):
            src = os.path.join(sub_path, item)
            dst = os.path.join(app_dir, item)
            if os.path.exists(dst):
                # If destination exists, remove it first
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
            shutil.move(src, dst)

        # Remove the now-empty extract_dir subfolder
        try:
            shutil.rmtree(sub_path)
        except OSError:
            pass  # Not critical if removal fails
