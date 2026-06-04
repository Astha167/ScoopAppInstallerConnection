"""
silent_installer.py — GUI Installer Suppression for myscoop

Handles local installers by trying multiple strategies in order:
    1. MSI silent      — msiexec /a /qn TARGETDIR=
    2. NSIS silent     — exe /S /D=
    3. Inno Setup      — exe /VERYSILENT /SUPPRESSMSGBOXES /DIR=
    4. GUI automation  — existing UI automation fallback

Archive extraction is reserved for actual archive inputs like .7z/.zip
or when a manifest explicitly marks the payload as type "7z".

Each strategy is tried in order. If one fails, the next is attempted.

Usage:
    si = SilentInstaller("C:/Users/you/myscoop/apps")
    success = si.install(filepath, app_dir)
"""

import os
import subprocess
import time
import ctypes
import ctypes.wintypes
import shutil
import logging
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger("myscoop.silent_installer")

try:
    import psutil
except ImportError:
    psutil = None

try:
    import py7zr
except ImportError:
    py7zr = None

try:
    from myscoop.gui_installer import GUIInstaller, GUIInstallError
    _HAS_GUI_INSTALLER = True
except ImportError:
    _HAS_GUI_INSTALLER = False

from myscoop.extractor import Extractor


class SilentInstallError(Exception):
    """Raised when all silent install strategies fail."""
    pass


class _InteractiveInstallerWindow(RuntimeError):
    """Raised when a supposed silent install opens an interactive window."""
    pass


class SilentInstaller:
    """
    Runs installers using a silent-first cascade.

    Real installer execution is preferred for .exe/.msi inputs so that
    "success" means the application was actually installed, not just unpacked.
    """

    # Common locations where 7-Zip CLI is installed
    _7Z_PATHS = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    _INTERACTIVE_WINDOW_KEYWORDS = (
        "setup",
        "install",
        "installer",
        "wizard",
        "extract",
        "self-extract",
        "extract to",
    )

    def __init__(self, apps_dir: str) -> None:
        """
        Args:
            apps_dir: Root apps directory.
        """
        self.apps_dir: str = apps_dir
        self._7z_exe: Optional[str] = self._find_7z_exe()
        self._interactive_window_pids: Set[int] = set()

    def install(
        self,
        filepath: str,
        app_dir: str,
        installer_type: Optional[str] = None,
        installer_args: Optional[List[str]] = None,
    ) -> bool:
        """
        Try to silently install an installer file.

        Tries strategies in this exact order:
            1. Explicit manifest hint
            2. MSI silent (for .msi)
            3. NSIS silent (/S)
            4. Inno Setup silent (/VERYSILENT)
            5. Archive extraction for archive payloads only
            6. GUI automation

        Args:
            filepath:       Path to the installer file.
            app_dir:        Target directory to install/extract into.
            installer_type: Optional hint from manifest ("7z", "msi", "nsis", "inno").
            installer_args: Optional custom arguments to pass to the installer.

        Returns:
            True if installation succeeded with any strategy.

        Raises:
            SilentInstallError: If ALL strategies fail.
        """
        os.makedirs(app_dir, exist_ok=True)
        errors = []
        ext = Path(filepath).suffix.lower()

        # MSI Native silent installation with GUI fallback
        if installer_type == "msi" or ext == ".msi":
            silent_success = False
            try:
                print("  Trying: MSI native silent install ...")
                silent_success = self._strategy_msi(filepath, app_dir, installer_args)
            except Exception as e:
                print(f"  MSI silent install failed: {e}")
                logger.warning(f"MSI native silent install threw exception: {e}")
                silent_success = False

            if not silent_success:
                print("  MSI silent install failed or validation failed. Falling back to GUI automation ...")
                if _HAS_GUI_INSTALLER:
                    try:
                        print("  Trying: GUI automation ...")
                        self._strategy_gui(filepath, app_dir)
                        # After GUI installer finishes, validate the installation and copy the files!
                        validation_passed = self._validate_msi_installation(app_dir, filepath)
                        if not validation_passed:
                            raise RuntimeError("MSI installation validation failed after GUI automation.")
                        print("  Strategy: GUI automation succeeded")
                        return True
                    except Exception as e:
                        raise SilentInstallError(
                            f"MSI installation failed (Silent failed and GUI fallback failed: {e})"
                        ) from e
                else:
                    raise SilentInstallError(
                        "MSI silent installation failed and GUI automation is unavailable."
                    )
            else:
                print("  Strategy: MSI native silent install succeeded")
                return True

        # If installer_type is explicitly specified, try that first
        if (
            installer_args
            and ext == ".exe"
            and (installer_type or "").lower() != "gui"
        ):
            try:
                print("  Trying: manifest custom installer arguments ...")
                self._run_executable_strategy(
                    filepath,
                    installer_args,
                    timeout=600,
                    strategy_name="Custom",
                )
                print("  Strategy: manifest custom installer arguments succeeded")
                return True
            except _InteractiveInstallerWindow as e:
                errors.append(f"custom args: {e}")
            except Exception as e:
                errors.append(f"custom args: {e}")

        if installer_type:
            strategy = self._get_strategy_for_type(installer_type)
            if strategy:
                try:
                    strategy(filepath, app_dir)
                    return True
                except Exception as e:
                    errors.append(f"{installer_type}: {e}")
                    if installer_type.lower() == "gui":
                        error_details = "\n    ".join(errors)
                        raise SilentInstallError(
                            f"Explicit GUI automation failed for: {filepath}\n"
                            f"  Errors:\n    {error_details}"
                        )

        archive_like_type = installer_type.lower() if installer_type else None
        is_archive_payload = ext in {".7z", ".zip"} or archive_like_type in {"7z", "zip"}

        if is_archive_payload:
            try:
                print("  Trying: archive extraction ...")
                self._strategy_7z(filepath, app_dir)
                print("  Strategy: archive extraction succeeded")
                return True
            except Exception as e:
                errors.append(f"archive: {e}")

        # Strategy 2/3: executable installers
        if ext == ".exe":
            detected = self.detect_installer_type(filepath)
            interactive_detected = False

            if detected == "nsis" or installer_type == "nsis":
                try:
                    print("  Trying: NSIS silent install ...")
                    self._strategy_nsis(filepath, app_dir)
                    print("  Strategy: NSIS silent install succeeded")
                    return True
                except _InteractiveInstallerWindow as e:
                    errors.append(f"NSIS: {e}")
                    interactive_detected = True
                except Exception as e:
                    errors.append(f"NSIS: {e}")

            # Strategy 4: Inno Setup silent (/VERYSILENT)
            if not interactive_detected and (detected == "inno" or installer_type == "inno"):
                try:
                    print("  Trying: Inno Setup silent install ...")
                    self._strategy_inno(filepath, app_dir)
                    print("  Strategy: Inno Setup silent install succeeded")
                    return True
                except _InteractiveInstallerWindow as e:
                    errors.append(f"Inno: {e}")
                    interactive_detected = True
                except Exception as e:
                    errors.append(f"Inno: {e}")

            # If detection was inconclusive, try both common silent modes
            if (
                not interactive_detected
                and detected is None
                and installer_type not in ("nsis", "inno")
            ):
                try:
                    print("  Trying: NSIS silent install ...")
                    self._strategy_nsis(filepath, app_dir)
                    print("  Strategy: NSIS silent install succeeded")
                    return True
                except _InteractiveInstallerWindow as e:
                    errors.append(f"NSIS: {e}")
                    interactive_detected = True
                except Exception as e:
                    errors.append(f"NSIS: {e}")

                if not interactive_detected:
                    try:
                        print("  Trying: Inno Setup silent install ...")
                        self._strategy_inno(filepath, app_dir)
                        print("  Strategy: Inno Setup silent install succeeded")
                        return True
                    except _InteractiveInstallerWindow as e:
                        errors.append(f"Inno: {e}")
                        interactive_detected = True
                    except Exception as e:
                        errors.append(f"Inno: {e}")

            # Some setup.exe files are really extractable archives containing
            # MSI/MSIZip payloads. Prefer extraction over blind GUI driving.
            if not interactive_detected and self._looks_like_extractable_setup(filepath):
                try:
                    print("  Trying: archive extraction ...")
                    self._strategy_7z(filepath, app_dir)
                    print("  Strategy: archive extraction succeeded")
                    return True
                except Exception as e:
                    errors.append(f"archive: {e}")

        # For non-exe archive-like payloads that were not explicitly marked,
        # try extraction before GUI fallback.
        if ext in {".7z", ".zip"} and not is_archive_payload:
            try:
                print("  Trying: archive extraction ...")
                self._strategy_7z(filepath, app_dir)
                print("  Strategy: archive extraction succeeded")
                return True
            except Exception as e:
                errors.append(f"archive: {e}")

        # Strategy 4/5: GUI automation (last resort for installers)
        if _HAS_GUI_INSTALLER:
            try:
                print("  Trying: GUI automation ...")
                self._strategy_gui(filepath, app_dir)
                # If app_dir has no launchable binaries, try to discover and sync files from the system
                self._validate_msi_installation(app_dir, filepath)
                print("  Strategy: GUI automation succeeded")
                return True
            except Exception as e:
                errors.append(f"GUI: {e}")

        # All strategies failed
        error_details = "\n    ".join(errors)
        raise SilentInstallError(
            f"All silent install strategies failed for: {filepath}\n"
            f"  Errors:\n    {error_details}\n"
            f"  Suggested fix: Try 'myscoop install vcredist2022' if missing dependencies."
        )

    # ──────────────────────────────────────────────
    # Individual strategies
    # ──────────────────────────────────────────────

    def _strategy_7z(self, filepath: str, app_dir: str) -> None:
        """Strategy 1: Treat the file as a 7z archive. Try py7zr first, then 7z.exe."""
        errors: List[str] = []

        # Try py7zr first (works for pure .7z archives)
        if py7zr is not None:
            try:
                with py7zr.SevenZipFile(filepath, mode="r") as archive:
                    archive.extractall(path=app_dir)
                return
            except Exception as e:
                errors.append(f"py7zr: {e}")

        # Try 7z.exe CLI (handles NSIS, Inno, SFX, etc.)
        if self._7z_exe:
            try:
                result = subprocess.run(
                    [self._7z_exe, "x", filepath, f"-o{app_dir}", "-y"],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode == 0:
                    return
                errors.append(f"7z.exe exit code {result.returncode}")
            except Exception as e:
                errors.append(f"7z.exe: {e}")

        raise RuntimeError(f"7z extraction failed: {'; '.join(errors)}")

    def _strategy_msi(
        self,
        filepath: str,
        app_dir: str,
        args: Optional[List[str]] = None,
    ) -> bool:
        """Strategy 2: MSI native silent install using msiexec /i."""
        filepath_abs = os.path.abspath(filepath)
        cmd = ["msiexec", "/i", filepath_abs, "/qn", "/norestart"]
        existing_props = {
            arg.split("=", 1)[0].upper()
            for arg in (args or [])
            if isinstance(arg, str) and "=" in arg
        }
        target_props = []
        for prop in ("TARGETDIR", "INSTALLDIR", "APPDIR"):
            if prop not in existing_props:
                target_props.append(f"{prop}={os.path.abspath(app_dir)}")
        cmd.extend(target_props)
        if args:
            cmd.extend(args)

        logger.info(f"Executing MSI install command: {' '.join(cmd)}")
        print(f"  Command: {' '.join(cmd)}")

        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes timeout
            )
            duration = time.time() - start_time
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            exit_code = result.returncode

            logger.info(f"MSI install finished in {duration:.2f}s with exit code {exit_code}")
            print(f"  MSI exit code: {exit_code} (took {duration:.1f}s)")
            if stdout.strip():
                logger.debug(f"MSI stdout: {stdout}")
            if stderr.strip():
                logger.debug(f"MSI stderr: {stderr}")
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start_time
            logger.error(f"MSI install timed out after {duration:.2f}s")
            print(f"  MSI install timed out after 10 minutes.")
            return False
        except Exception as e:
            logger.error(f"MSI install execution failed: {e}")
            return False

        if exit_code not in (0, 3010, 1641):
            logger.warning(f"MSI install failed with exit code {exit_code}")
            return False

        # Run installation validation
        validation_passed = self._validate_msi_installation(app_dir, filepath)
        if not validation_passed:
            logger.warning("MSI installation validation failed.")
            return False

        return True

    def _get_msi_properties_com(self, filepath: str) -> dict:
        """Read MSI properties using win32com.client."""
        properties = {}
        try:
            import win32com.client
            installer = win32com.client.Dispatch("WindowsInstaller.Installer")
            # OpenDatabase mode 0 = readonly
            database = installer.OpenDatabase(filepath, 0)
            view = database.OpenView("SELECT Property, Value FROM Property")
            view.Execute()
            while True:
                record = view.Fetch()
                if not record:
                    break
                prop = record.StringData(1)
                val = record.StringData(2)
                properties[prop] = val
        except Exception as e:
            logger.debug(f"Failed to read MSI properties via COM: {e}")
        return properties

    def _get_msi_registry_install_location(self, product_code: str) -> Optional[str]:
        """Query the registry directly for InstallLocation of a ProductCode."""
        try:
            import winreg
        except ImportError:
            return None

        key_specs = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
        ]

        for hive, base_path, wow_flag in key_specs:
            access = winreg.KEY_READ | wow_flag
            path = f"{base_path}\\{product_code}"
            try:
                with winreg.OpenKey(hive, path, 0, access) as key:
                    try:
                        loc, _ = winreg.QueryValueEx(key, "InstallLocation")
                        if loc and os.path.isdir(loc) and os.listdir(loc):
                            return loc
                    except OSError:
                        pass
            except OSError:
                continue

        return None

    def _discover_msi_install_directory(self, app_dir: str, filepath: str) -> Optional[str]:
        """
        Locate the actual installed directory of the application on the system.
        Uses ProductCode, ProductName, Registry entries, Start Menu shortcuts, and common folders.
        """
        app_name = Path(app_dir).parent.name
        msi_properties = self._get_msi_properties_com(filepath)
        
        product_code = msi_properties.get("ProductCode")
        product_name = msi_properties.get("ProductName")
        
        # Heuristic 0: Direct search via ProductCode in Registry
        if product_code:
            loc = self._get_msi_registry_install_location(product_code)
            if loc:
                logger.info(f"Discovered install directory from exact ProductCode '{product_code}': {loc}")
                return loc

        # Build candidate names to check against system folders, shortcuts, and registry entries
        import re
        from myscoop.metadata import normalize_registry_text
        names_to_check = []
        if product_name:
            names_to_check.append(normalize_registry_text(product_name))
        names_to_check.append(normalize_registry_text(app_name))
        
        # Add clean name without version suffixes like -2-3-1, -12-1-4, etc.
        clean_app_name = re.sub(r"[-_.]v?\d+(?:[-_.]\d+)*$", "", app_name)
        if clean_app_name != app_name:
            names_to_check.append(normalize_registry_text(clean_app_name))

        # Heuristic 1: Search Registry Uninstall Entries for InstallLocation using ProductName or app_name
        try:
            from myscoop.metadata import iter_installed_app_registry_entries
            
            for entry in iter_installed_app_registry_entries():
                display_name = normalize_registry_text(entry.get("DisplayName", ""))
                if not display_name:
                    continue
                # Match if display_name contains any of our candidate names or vice versa
                for candidate in names_to_check:
                    if candidate in display_name or display_name in candidate:
                        install_loc = entry.get("InstallLocation")
                        if install_loc and os.path.isdir(install_loc):
                            if os.listdir(install_loc):
                                logger.info(f"Discovered install directory from Registry DisplayName: {install_loc}")
                                return install_loc
        except Exception as e:
            logger.debug(f"Registry-based discovery failed: {e}")

        # Heuristic 2: Search shortcuts in Start Menu and Desktop using ProductName or app_name
        try:
            from myscoop.metadata import resolve_shortcut_target
            
            shortcut_dirs = [
                os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
                os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
                os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
                os.path.join(os.environ.get("Public", ""), "Desktop"),
            ]
            for s_dir in shortcut_dirs:
                if not os.path.isdir(s_dir):
                    continue
                for root, _, files in os.walk(s_dir):
                    for file in files:
                        if file.lower().endswith(".lnk"):
                            normalized_file = normalize_registry_text(file)
                            for candidate in names_to_check:
                                if candidate in normalized_file:
                                    lnk_path = os.path.join(root, file)
                                    target = resolve_shortcut_target(lnk_path)
                                    if target and os.path.isfile(target):
                                        parent_dir = os.path.dirname(target)
                                        if os.path.isdir(parent_dir) and os.listdir(parent_dir):
                                            logger.info(f"Discovered install directory from shortcut '{file}': {parent_dir}")
                                            return parent_dir
        except Exception as e:
            logger.debug(f"Shortcut-based discovery failed: {e}")

        # Heuristic 3: Scan common installation folders
        try:
            common_dirs = [
                os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files")),
                os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
            ]
            for c_dir in common_dirs:
                if not os.path.isdir(c_dir):
                    continue
                for sub in os.listdir(c_dir):
                    normalized_sub = normalize_registry_text(sub)
                    for candidate in names_to_check:
                        if candidate == normalized_sub or candidate in normalized_sub:
                            candidate_path = os.path.join(c_dir, sub)
                            if os.path.isdir(candidate_path) and os.listdir(candidate_path):
                                logger.info(f"Discovered install directory from common folder scan: {candidate_path}")
                                return candidate_path
        except Exception as e:
            logger.debug(f"Common folder scan discovery failed: {e}")

        return None

    def _validate_msi_installation(self, app_dir: str, filepath: str) -> bool:
        """
        Validate that the MSI installation succeeded by verifying:
        - Executable discovery (in app_dir or system)
        - Shortcut creation
        - Installed directories
        - Registry uninstall entries
        - Product registration
        - Launchable binaries
        
        If found in the system, copies the files to app_dir so that
        the rest of the myscoop pipeline works.
        """
        # 1. Check if files are already in app_dir (e.g. if installed directly to app_dir)
        has_launchable_in_app_dir = False
        launch_extensions = {".exe", ".com", ".cmd", ".bat"}
        if os.path.isdir(app_dir):
            from myscoop.metadata import _should_ignore_candidate
            for root, _, files in os.walk(app_dir):
                for f in files:
                    if os.path.splitext(f)[1].lower() in launch_extensions:
                        if not _should_ignore_candidate(f):
                            has_launchable_in_app_dir = True
                            break
                if has_launchable_in_app_dir:
                    break

        if has_launchable_in_app_dir:
            logger.info(f"MSI installation validated: files already present in {app_dir}")
            return True

        # 2. Discover installed directory from system
        discovered_dir = self._discover_msi_install_directory(app_dir, filepath)
        if not discovered_dir:
            logger.warning("Could not discover installed directory for MSI on the system.")
            return False

        # 3. Verify launchable binaries exist in discovered_dir
        has_launchable = False
        from myscoop.metadata import _should_ignore_candidate
        for root, _, files in os.walk(discovered_dir):
            for f in files:
                if os.path.splitext(f)[1].lower() in launch_extensions:
                    if not _should_ignore_candidate(f):
                        has_launchable = True
                        break
            if has_launchable:
                break

        if not has_launchable:
            logger.warning(f"Discovered directory {discovered_dir} does not contain launchable binaries.")
            return False

        # 4. Copy files from discovered_dir to app_dir
        logger.info(f"Copying installed files from {discovered_dir} to {app_dir}...")
        try:
            import shutil
            # We want to copy everything from discovered_dir to app_dir.
            for root, dirs, files in os.walk(discovered_dir):
                rel_path = os.path.relpath(root, discovered_dir)
                dest_root = app_dir if rel_path == "." else os.path.join(app_dir, rel_path)
                os.makedirs(dest_root, exist_ok=True)
                for f in files:
                    src_file = os.path.join(root, f)
                    dst_file = os.path.join(dest_root, f)
                    try:
                        shutil.copy2(src_file, dst_file)
                    except Exception as e:
                        # Sometimes some system files might be locked, log it but continue
                        logger.debug(f"Could not copy file {src_file}: {e}")
            logger.info("Copy completed successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to copy installed files to app_dir: {e}")
            return False

    def _strategy_nsis(self, filepath: str, app_dir: str) -> None:
        """Strategy 3: NSIS installer with /S (silent) and /D= (target dir)."""
        self._run_executable_strategy(
            filepath,
            ["/S", f"/D={app_dir}"],
            timeout=300,
            strategy_name="NSIS",
        )

    def _strategy_inno(self, filepath: str, app_dir: str) -> None:
        """Strategy 4: Inno Setup with /VERYSILENT /SUPPRESSMSGBOXES /DIR=."""
        self._run_executable_strategy(
            filepath,
            [
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                f"/DIR={app_dir}",
            ],
            timeout=300,
            strategy_name="Inno Setup",
        )

    def _strategy_gui(self, filepath: str, app_dir: str) -> None:
        """Strategy 5: GUI automation — drive the installer wizard via UI tree."""
        if not _HAS_GUI_INSTALLER:
            raise RuntimeError(
                "GUI automation requires pywinauto and psutil. "
                "Run: pip install pywinauto pyautogui psutil"
            )
        gui = GUIInstaller()
        success = gui.install(filepath, app_dir)
        if not success:
            raise RuntimeError("GUI automation reported failure")

    # ──────────────────────────────────────────────
    # Installer type detection
    # ──────────────────────────────────────────────

    def detect_installer_type(self, filepath: str) -> Optional[str]:
        """
        Peek at the binary contents of an exe to detect if it's
        an NSIS or Inno Setup installer.

        Args:
            filepath: Path to the exe file.

        Returns:
            "nsis", "inno", or None if detection is inconclusive.
        """
        try:
            with open(filepath, "rb") as f:
                # Read first 64KB for signature detection
                header = f.read(65536)

                # NSIS markers: "Nullsoft" or "NSIS" appear in the header
                if b"NullsoftInst" in header or b"NSIS" in header:
                    return "nsis"

                # Inno Setup markers
                if b"Inno Setup" in header or b"InnoSetup" in header:
                    return "inno"

                # Also check deeper in the file for some installers
                f.seek(0)
                # Read a larger chunk for installers that embed signatures later
                content = f.read(1024 * 512)  # 512KB
                if b"NullsoftInst" in content:
                    return "nsis"
                if b"Inno Setup" in content:
                    return "inno"

        except (IOError, OSError):
            pass

        return None

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _find_7z_exe(self) -> Optional[str]:
        """Find the 7z.exe command-line tool on the system."""
        for path in self._7Z_PATHS:
            if os.path.exists(path):
                return path
        try:
            result = subprocess.run(
                ["where", "7z"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        except Exception:
            pass
        return None

    def _get_strategy_for_type(self, installer_type: str):
        """Map installer type string to strategy method."""
        type_map = {
            "7z": self._strategy_7z,
            "msi": self._strategy_msi,
            "nsis": self._strategy_nsis,
            "inno": self._strategy_inno,
            "gui": self._strategy_gui,
        }
        return type_map.get(installer_type.lower())

    def _looks_like_extractable_setup(self, filepath: str) -> bool:
        """
        Detect EXEs that are better treated as archives.

        This catches installers like MAPSetup.exe where 7-Zip can list MSI/MSIZip
        contents directly, so extraction is a safer path than GUI automation.
        """
        if not self._7z_exe:
            return False

        try:
            result = subprocess.run(
                [self._7z_exe, "l", filepath],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception:
            return False

        if result.returncode != 0:
            return False

        listing = f"{result.stdout}\n{result.stderr}".lower()
        archive_markers = (
            "method = msizip",
            " msizip",
            ".msi",
            ".cab",
        )
        return any(marker in listing for marker in archive_markers)

    def _run_executable_strategy(
        self,
        filepath: str,
        args: List[str],
        timeout: int,
        strategy_name: str,
    ) -> None:
        """
        Run an EXE-based silent strategy, but fail fast if it shows UI.
        """
        # Inject RunAsInvoker to bypass embedded requireAdministrator manifests 
        # so Windows doesn't immediately throw WinError 740 and we can stay silent.
        env = os.environ.copy()
        env["__COMPAT_LAYER"] = "RunAsInvoker"
        self._interactive_window_pids.clear()

        try:
            process = subprocess.Popen(
                [filepath, *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except OSError as e:
            if getattr(e, "winerror", None) == 740:
                print(f"  Elevation required for {strategy_name} despite RunAsInvoker. Requesting UAC...")
                # Escape arguments for PowerShell
                ps_args = " ".join(args)
                ps_cmd = f"Start-Process -FilePath '{filepath}' -ArgumentList '{ps_args}' -Wait -WindowStyle Hidden"
                result = subprocess.run(["powershell", "-Command", ps_cmd], check=False, timeout=timeout)
                if result.returncode != 0:
                    raise RuntimeError(f"{strategy_name} elevated installer exited with code {result.returncode}")
                return
            raise

        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._has_interactive_window(process.pid, filepath):
                self._terminate_process(process)
                raise _InteractiveInstallerWindow(
                    f"{strategy_name} installer ignored silent flags and opened interactive UI"
                )

            returncode = process.poll()
            if returncode is not None:
                if returncode != 0:
                    raise RuntimeError(
                        f"{strategy_name} installer exited with code {returncode}"
                    )
                return

            time.sleep(0.5)

        self._terminate_process(process)
        raise RuntimeError(
            f"{strategy_name} installer timed out after {timeout}s"
        )

    def _terminate_process(self, process: subprocess.Popen) -> None:
        """Best-effort cleanup for timed out or interactive processes."""
        if psutil is not None:
            procs = []
            try:
                parent = psutil.Process(process.pid)
                procs.extend(parent.children(recursive=True))
                procs.append(parent)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
                pass

            for pid in self._interactive_window_pids:
                try:
                    procs.append(psutil.Process(pid))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            if procs:
                unique_procs = list({proc.pid: proc for proc in procs}.values())
                try:
                    for proc in unique_procs:
                        proc.terminate()
                    _, alive = psutil.wait_procs(unique_procs, timeout=5)
                    for proc in alive:
                        proc.kill()
                    return
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
                    pass

        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass

    def _get_process_tree_pids(self, root_pid: int) -> Set[int]:
        """Return the root process and all currently visible descendants."""
        pids = {root_pid}
        if psutil is None:
            return pids
        try:
            parent = psutil.Process(root_pid)
            for child in parent.children(recursive=True):
                pids.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
            pass
        return pids

    def _has_interactive_window(self, pid: int, filepath: str = "") -> bool:
        """Detect whether a visible top-level installer window appeared."""
        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            return False

        found = False
        process_tree_pids = self._get_process_tree_pids(pid)

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(hwnd, _lparam):
            nonlocal found

            if found or not user32.IsWindowVisible(hwnd):
                return True

            window_pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))

            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value.lower()

            if not any(keyword in title for keyword in self._INTERACTIVE_WINDOW_KEYWORDS):
                return True

            if (
                window_pid.value in process_tree_pids
                or self._title_matches_installer_file(title, filepath)
            ):
                self._interactive_window_pids.add(window_pid.value)
                found = True
                return False

            return True

        try:
            user32.EnumWindows(enum_callback, 0)
        except Exception:
            return False

        return found

    def _title_matches_installer_file(self, title: str, filepath: str) -> bool:
        """Match child installer windows by product tokens from the exe name."""
        if not filepath:
            return False

        title_norm = self._normalize_window_text(title)
        filename_norm = self._normalize_window_text(Path(filepath).stem)
        tokens = [
            token for token in filename_norm.split()
            if len(token) >= 3
            and token not in {
                "setup",
                "install",
                "installer",
                "windows",
                "win",
                "x64",
                "x86",
                "exe",
                "msi",
            }
        ]
        return any(token in title_norm for token in tokens)

    def _normalize_window_text(self, text: str) -> str:
        """Normalize installer titles for conservative token matching."""
        text = text.lower()
        text = "".join(ch if ch.isalnum() else " " for ch in text)
        return " ".join(text.split())
