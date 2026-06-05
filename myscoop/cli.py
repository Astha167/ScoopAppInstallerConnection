"""
cli.py — Click CLI Entry Point for myscoop

The main command-line interface for the myscoop package manager.
All commands are implemented here, orchestrating the individual modules.

Usage:
    python myscoop.py install postman
    python myscoop.py list
    python myscoop.py search mysql
"""

import json
import os
import sys
import shutil
from pathlib import Path
from typing import Optional

import click
from colorama import Fore, Style, init as colorama_init

from myscoop.manifest import Manifest, ManifestNotFoundError
from myscoop.downloader import Downloader, DownloadError
from myscoop.extractor import Extractor, ExtractionError
from myscoop.silent_installer import SilentInstaller, SilentInstallError
from myscoop.shim import ShimManager
from myscoop.path_manager import PathManager, PathManagerError
from myscoop.dependency import DependencyResolver, CircularDependencyError
from myscoop.bucket import BucketManager, BucketError
from myscoop.metadata import AppMetadata, find_installed_executables
from myscoop.local_manifest import LocalManifestManager, LocalManifestError
from myscoop.install_state import (
    cleanup_empty_app_folder,
    get_valid_installed_versions,
    inspect_install_path,
    remove_install_path,
)

try:
    from myscoop.gui_installer import GUIInstaller, GUIInstallError
    _HAS_GUI_INSTALLER = True
except ImportError:
    _HAS_GUI_INSTALLER = False


# ──────────────────────────────────────────────
# Directory setup
# ──────────────────────────────────────────────

# All paths relative to the user's home directory
HOME = os.path.expanduser("~")
MYSCOOP_ROOT = os.path.join(HOME, "myscoop")
APPS_DIR = os.path.join(MYSCOOP_ROOT, "apps")
CACHE_DIR = os.path.join(MYSCOOP_ROOT, "cache")
SHIMS_DIR = os.path.join(MYSCOOP_ROOT, "shims")
BUCKETS_DIR = os.path.join(MYSCOOP_ROOT, "buckets")

# Also look for local buckets in the project directory
# (for development / testing with bundled manifests)
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_BUCKETS_DIR = os.path.join(PROJECT_DIR, "buckets")


def get_buckets_dir() -> str:
    """
    Return the buckets directory to use.
    Prefers the myscoop home directory, falls back to local project buckets.
    """
    if os.path.exists(BUCKETS_DIR) and os.listdir(BUCKETS_DIR):
        return BUCKETS_DIR
    if os.path.exists(LOCAL_BUCKETS_DIR) and os.listdir(LOCAL_BUCKETS_DIR):
        return LOCAL_BUCKETS_DIR
    return BUCKETS_DIR


def ensure_dirs() -> None:
    """Create all required directories if they don't exist."""
    for d in [APPS_DIR, CACHE_DIR, SHIMS_DIR, BUCKETS_DIR]:
        os.makedirs(d, exist_ok=True)


# ──────────────────────────────────────────────
# Error handling decorator
# ──────────────────────────────────────────────

ARCHIVE_EXTENSIONS = {".zip", ".7z"}
LOCAL_INSTALLER_EXTENSIONS = {".exe", ".msi"}


def _find_best_embedded_installer(root_dir: str) -> Optional[str]:
    """Find the most likely setup executable/MSI inside an extracted archive."""
    root = Path(root_dir)
    candidates = []

    if not root.is_dir():
        return None

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in LOCAL_INSTALLER_EXTENSIONS:
            continue

        score = _score_embedded_installer(path)
        if score <= 0:
            continue
        candidates.append((score, len(str(path)), str(path).lower(), path))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return str(candidates[0][3])


def _resolve_embedded_installer_path(
    app_dir: str,
    explicit_gui_exe: str = "",
) -> Optional[str]:
    """Resolve an explicit setup path or discover the best embedded installer."""
    if explicit_gui_exe:
        normalized = explicit_gui_exe.replace("/", os.sep).replace("\\", os.sep)
        direct_candidate = os.path.join(app_dir, normalized)
        if os.path.isfile(direct_candidate):
            return direct_candidate

        # Archives often get flattened by extract_dir, so a manifest path like
        # "setup/Installer.exe" may become just "Installer.exe".
        wanted_name = os.path.basename(normalized).lower()
        if wanted_name:
            matches = [
                str(path)
                for path in Path(app_dir).rglob("*")
                if path.is_file()
                and path.name.lower() == wanted_name
                and path.suffix.lower() in LOCAL_INSTALLER_EXTENSIONS
            ]
            if matches:
                matches.sort(key=lambda p: (len(p), p.lower()))
                return matches[0]

    return _find_best_embedded_installer(app_dir)


def _score_embedded_installer(path: Path) -> int:
    """Score an embedded installer candidate by extension and filename."""
    name = path.name.lower()
    stem = path.stem.lower()

    skip_keywords = (
        "uninstall", "unins", "uninst", "updater", "update",
        "repair", "patch", "helper", "crash", "cleanup",
    )
    if any(keyword in name for keyword in skip_keywords):
        return -100

    score = 90 if path.suffix.lower() == ".msi" else 100

    boosts = {
        "setup": 60,
        "installer": 55,
        "install": 50,
        "clientonly": 40,
        "client-only": 40,
        "client": 25,
        "viewer": 18,
        "x64": 8,
        "64": 5,
    }
    penalties = {
        "server": 15,
        "service": 8,
    }

    for keyword, boost in boosts.items():
        if keyword in stem:
            score += boost
    for keyword, penalty in penalties.items():
        if keyword in stem:
            score -= penalty

    return score


def _run_embedded_installer_if_present(
    app_dir: str,
    explicit_gui_exe: str = "",
) -> bool:
    """Run the setup file found inside a local ZIP/7z archive, if any."""
    installer_path = _resolve_embedded_installer_path(app_dir, explicit_gui_exe)
    if installer_path is None:
        return False

    click.echo(
        f"{Fore.BLUE}  Found embedded installer: {installer_path}{Style.RESET_ALL}"
    )
    silent_installer = SilentInstaller(APPS_DIR)
    embedded_type = None
    if explicit_gui_exe:
        embedded_type = "gui"
    elif Path(installer_path).suffix.lower() == ".msi":
        embedded_type = "msi"
    silent_installer.install(
        filepath=installer_path,
        app_dir=app_dir,
        installer_type=embedded_type,
    )
    click.echo(f"{Fore.GREEN}  Embedded installer completed OK{Style.RESET_ALL}")
    return True


def handle_errors(func):
    """Decorator to catch all known errors and print user-friendly messages."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ManifestNotFoundError as e:
            click.echo(f"\n{Fore.RED}Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
        except DownloadError as e:
            click.echo(f"\n{Fore.RED}Download Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
        except ExtractionError as e:
            click.echo(f"\n{Fore.RED}Extraction Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
        except SilentInstallError as e:
            click.echo(f"\n{Fore.RED}Install Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
        except CircularDependencyError as e:
            click.echo(f"\n{Fore.RED}Dependency Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
        except BucketError as e:
            click.echo(f"\n{Fore.RED}Bucket Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
        except PathManagerError as e:
            click.echo(f"\n{Fore.RED}PATH Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
        except LocalManifestError as e:
            click.echo(f"\n{Fore.RED}Manifest Error: {e}{Style.RESET_ALL}")
            sys.exit(1)
        except KeyboardInterrupt:
            click.echo(f"\n{Fore.YELLOW}Cancelled by user.{Style.RESET_ALL}")
            sys.exit(1)
        except Exception as e:
            # Catch GUIInstallError by name (module may not be installed)
            if type(e).__name__ == "GUIInstallError":
                click.echo(f"\n{Fore.RED}GUI Install Error: {e}{Style.RESET_ALL}")
                sys.exit(1)
            click.echo(f"\n{Fore.RED}Unexpected error: {e}{Style.RESET_ALL}")
            sys.exit(1)
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


# ──────────────────────────────────────────────
# Core install logic (used by install and update)
# ──────────────────────────────────────────────

def install_single_app(
    app_name: str,
    buckets_dir: str,
    is_dependency: bool = False,
    local_file: Optional[str] = None,
) -> bool:
    """
    Install a single app (no dependency resolution — just download+extract+shim).

    Args:
        app_name:       Name of the app.
        buckets_dir:    Buckets directory.
        is_dependency:  If True, print as dependency install.
        local_file:     Optional path to a local installer exe (skips download).

    Returns:
        True if installed successfully.
    """
    colorama_init(autoreset=True)

    # Load manifest
    manifest = Manifest(app_name, buckets_dir)

    # Check if already installed
    resolver = DependencyResolver(APPS_DIR, buckets_dir)
    if resolver.is_installed(app_name) and not is_dependency:
        click.echo(
            f"{Fore.YELLOW}'{app_name}' is already installed. "
            f"Run: myscoop update {app_name}{Style.RESET_ALL}"
        )
        return True

    if is_dependency:
        click.echo(f"\n{Fore.BLUE}Installing dependency: {app_name}{Style.RESET_ALL}")
    else:
        click.echo(
            f"\n{Fore.BLUE}Installing '{manifest.name}' "
            f"({manifest.version}) [64bit]{Style.RESET_ALL}"
        )

    # Download or use local file
    if local_file:
        filepath = os.path.abspath(local_file)
        if not os.path.isfile(filepath):
            click.echo(f"{Fore.RED}  Local file not found: {filepath}{Style.RESET_ALL}")
            return False
        click.echo(f"{Fore.GREEN}  Using local file: {filepath}{Style.RESET_ALL}")
    elif manifest.url:
        downloader = Downloader(CACHE_DIR)
        filepath = downloader.download(manifest.url, app_name, manifest.version)
    else:
        click.echo(f"{Fore.YELLOW}  No download URL in manifest. Skipping download.{Style.RESET_ALL}")
        filepath = None

    # Extract / Install
    if filepath:
        app_dir = os.path.join(APPS_DIR, app_name, manifest.version)
        os.makedirs(app_dir, exist_ok=True)

        if local_file:
            ext = Path(filepath).suffix.lower()
            if ext in ARCHIVE_EXTENSIONS:
                extractor = Extractor(APPS_DIR)
                extractor.extract(
                    filepath=filepath,
                    app_name=app_name,
                    version=manifest.version,
                    extract_dir=manifest.extract_dir,
                    url_hint=manifest.url_hint or f"#/{os.path.basename(filepath)}",
                    installer_type=manifest.installer_type,
                    installer_args=manifest.installer_args,
                )
                should_run_embedded = (
                    manifest.installer_needs_gui
                    or bool(manifest.gui_exe)
                    or not manifest.bin
                )
                if should_run_embedded:
                    ran_embedded = _run_embedded_installer_if_present(
                        app_dir=app_dir,
                        explicit_gui_exe=manifest.gui_exe,
                    )
                    if not ran_embedded:
                        click.echo(
                            f"{Fore.YELLOW}  No embedded .exe/.msi installer found in archive."
                            f"{Style.RESET_ALL}"
                        )
            else:
                click.echo(
                    f"{Fore.BLUE}  Attempting silent installation first ...{Style.RESET_ALL}"
                )
                silent_installer = SilentInstaller(APPS_DIR)
                silent_installer.install(
                    filepath=filepath,
                    app_dir=app_dir,
                    installer_type=manifest.installer_type or None,
                    installer_args=manifest.installer_args,
                )
                click.echo(
                    f"{Fore.GREEN}  Local installer completed OK{Style.RESET_ALL}"
                )

        # For GUI installers downloaded from URL, extract first, then run gui_exe
        elif manifest.installer_needs_gui:
            extractor = Extractor(APPS_DIR)
            extractor.extract(
                filepath=filepath,
                app_name=app_name,
                version=manifest.version,
                extract_dir=manifest.extract_dir,
                url_hint=manifest.url_hint,
            )

            gui_exe = manifest.gui_exe
            if gui_exe:
                gui_exe_path = _resolve_embedded_installer_path(app_dir, gui_exe)
                if gui_exe_path and os.path.isfile(gui_exe_path):
                    click.echo(
                        f"{Fore.BLUE}  Launching GUI automation for "
                        f"setup wizard ...{Style.RESET_ALL}"
                    )
                    if _HAS_GUI_INSTALLER:
                        gui = GUIInstaller()
                        success = gui.install(gui_exe_path, app_dir)
                        if success:
                            click.echo(
                                f"{Fore.GREEN}  GUI automation "
                                f"completed OK{Style.RESET_ALL}"
                            )
                        else:
                            click.echo(
                                f"{Fore.RED}  GUI automation "
                                f"reported failure{Style.RESET_ALL}"
                            )
                            raise SilentInstallError(
                                "GUI automation reported failure."
                            )
                    else:
                        click.echo(
                            f"{Fore.YELLOW}  GUI automation unavailable. "
                            f"Install deps: pip install pywinauto "
                            f"pyautogui psutil{Style.RESET_ALL}"
                        )
                        raise SilentInstallError(
                            "GUI automation is unavailable."
                        )
                else:
                    click.echo(
                        f"{Fore.YELLOW}  GUI exe not found: "
                        f"{gui_exe_path}{Style.RESET_ALL}"
                    )
                    raise SilentInstallError(
                        f"GUI exe not found for '{manifest.name}'."
                    )
        elif manifest.installer_type == "msi" or (filepath and Path(filepath).suffix.lower() == ".msi"):
            click.echo(
                f"{Fore.BLUE}  Attempting native silent MSI installation ...{Style.RESET_ALL}"
            )
            silent_installer = SilentInstaller(APPS_DIR)
            silent_installer.install(
                filepath=filepath,
                app_dir=app_dir,
                installer_type="msi",
                installer_args=manifest.installer_args,
            )
            click.echo(
                f"{Fore.GREEN}  MSI installer completed OK{Style.RESET_ALL}"
            )
        else:
            extractor = Extractor(APPS_DIR)
            extractor.extract(
                filepath=filepath,
                app_name=app_name,
                version=manifest.version,
                extract_dir=manifest.extract_dir,
                url_hint=manifest.url_hint,
                installer_type=manifest.installer_type,
                installer_args=manifest.installer_args,
            )
    else:
        app_dir = os.path.join(APPS_DIR, app_name, manifest.version)
        os.makedirs(app_dir, exist_ok=True)

    install_state = inspect_install_path(app_dir)
    if not install_state.valid:
        remove_install_path(app_dir)
        cleanup_empty_app_folder(APPS_DIR, app_name)
        click.echo(
            f"{Fore.RED}  Installation unsuccessful for '{manifest.name}': "
            f"{install_state.reason}. Removed incomplete folder: "
            f"{install_state.path}{Style.RESET_ALL}"
        )
        raise SilentInstallError(
            f"Installation unsuccessful for '{manifest.name}': "
            f"{install_state.reason}."
        )

    # Create shims for all bin entries
    if manifest.bin:
        shim_manager = ShimManager(SHIMS_DIR)
        for exe_name in manifest.bin:
            exe_path = os.path.join(app_dir, exe_name)
            # Check if it's a GUI app (heuristic: has "shortcuts" defined)
            is_gui = bool(manifest.shortcuts)
            shim_manager.create_shim(exe_name, exe_path, gui=is_gui)
            click.echo(f"{Fore.GREEN}  Creating shim: {Path(exe_name).stem}{Style.RESET_ALL}")

    # Create Start Menu shortcuts
    if manifest.shortcuts:
        for shortcut_entry in manifest.shortcuts:
            if len(shortcut_entry) >= 2:
                exe_path = os.path.join(app_dir, shortcut_entry[0])
                shortcut_name = shortcut_entry[1]
                _create_shortcut(exe_path, shortcut_name)
                click.echo(f"{Fore.GREEN}  Creating shortcut: {shortcut_name}{Style.RESET_ALL}")

    # Ensure shims dir is in PATH
    try:
        pm = PathManager()
        if pm.add_to_path(SHIMS_DIR):
            click.echo(f"{Fore.BLUE}  Added shims folder to PATH{Style.RESET_ALL}")
    except PathManagerError:
        pass  # Non-critical

    # Run post_install commands
    if manifest.post_install:
        click.echo(f"{Fore.BLUE}  Running post-install commands ...{Style.RESET_ALL}")
        for cmd in manifest.post_install:
            try:
                os.system(cmd)
            except Exception as e:
                click.echo(f"{Fore.YELLOW}  Warning: post_install failed: {e}{Style.RESET_ALL}")

    # Extract and save metadata from every likely app binary, not shortcuts.
    metadata_exes = []
    try:
        metadata_exes = find_installed_executables(
            app_dir,
            app_name=manifest.name,
            bin_entries=manifest.bin,
            shortcuts=manifest.shortcuts,
        )
        if metadata_exes:
            all_metadata = []
            total = len(metadata_exes)
            for index, metadata_exe in enumerate(metadata_exes, start=1):
                meta = AppMetadata(metadata_exe)
                data = meta.extract()
                data["resolution_reason"] = getattr(
                    metadata_exe,
                    "resolution_reason",
                    "",
                )
                data["confidence_score"] = getattr(
                    metadata_exe,
                    "confidence_score",
                    0,
                )
                all_metadata.append(data)

                if index == 1:
                    meta.save(app_dir)
                meta.display(title=f"App Metadata {index}/{total}")

            metadata_all_path = os.path.join(app_dir, "metadata_all.json")
            with open(metadata_all_path, "w", encoding="utf-8") as f:
                json.dump(all_metadata, f, indent=4, ensure_ascii=False)
    except Exception:
        pass  # Metadata is non-critical

    detected_bin_entries = []
    if not manifest.bin and metadata_exes:
        shim_manager = ShimManager(SHIMS_DIR)
        for metadata_exe in metadata_exes:
            exe_path = str(metadata_exe)
            if not os.path.isfile(exe_path):
                continue
            try:
                rel_path = os.path.relpath(exe_path, app_dir)
            except ValueError:
                rel_path = exe_path
            detected_bin_entries.append(rel_path)
            shim_manager.create_shim(
                os.path.basename(exe_path),
                exe_path,
                gui=Path(exe_path).suffix.lower() == ".exe",
            )
            click.echo(
                f"{Fore.GREEN}  Creating detected shim: "
                f"{Path(exe_path).stem}{Style.RESET_ALL}"
            )

    # Save install info
    install_info = {
        "name": manifest.name,
        "version": manifest.version,
        "bin": manifest.bin,
        "shortcuts": manifest.shortcuts,
        "url": manifest.url,
        "metadata_executable": metadata_exes[0] if metadata_exes else None,
        "metadata_executables": [str(exe) for exe in metadata_exes],
        "detected_bin": detected_bin_entries,
    }
    info_path = os.path.join(app_dir, "install.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(install_info, f, indent=4)

    click.echo(
        f"\n{Fore.GREEN}'{manifest.name}' ({manifest.version}) "
        f"installed successfully OK{Style.RESET_ALL}"
    )
    return True


def _create_shortcut(exe_path: str, shortcut_name: str) -> None:
    """
    Create a Start Menu shortcut for an application.
    Uses PowerShell to create .lnk files.
    """
    start_menu = os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "MyScoop"
    )
    os.makedirs(start_menu, exist_ok=True)

    lnk_path = os.path.join(start_menu, f"{shortcut_name}.lnk")

    # Use PowerShell to create the shortcut
    ps_cmd = (
        f'$WshShell = New-Object -ComObject WScript.Shell; '
        f'$Shortcut = $WshShell.CreateShortcut("{lnk_path}"); '
        f'$Shortcut.TargetPath = "{exe_path}"; '
        f'$Shortcut.Save()'
    )
    os.system(f'powershell -Command "{ps_cmd}" >nul 2>&1')


# ──────────────────────────────────────────────
# CLI Group
# ──────────────────────────────────────────────

@click.group()
def cli():
    """myscoop — A Python-based package manager for Windows"""
    colorama_init(autoreset=True)
    ensure_dirs()


# ──────────────────────────────────────────────
# install
# ──────────────────────────────────────────────

@cli.command()
@click.argument("app")
@click.option(
    "--file", "local_file", default=None, type=click.Path(exists=True, dir_okay=False),
    help="Path to a local installer/archive file (skips download)."
)
@handle_errors
def install(app: str, local_file: Optional[str] = None):
    """Install an application."""
    buckets_dir = get_buckets_dir()
    app, local_file = _resolve_install_target(app, local_file, buckets_dir)

    # Resolve dependencies
    resolver = DependencyResolver(APPS_DIR, buckets_dir)
    install_order = resolver.resolve(app)

    # Install dependencies first, then the app
    app_installed = False
    for i, dep_name in enumerate(install_order):
        is_dep = (dep_name.lower() != app.lower())

        # Skip already-installed dependencies
        if is_dep and resolver.is_installed(dep_name):
            click.echo(f"{Fore.BLUE}  Dependency '{dep_name}' already installed OK{Style.RESET_ALL}")
            continue

        # Only pass --file for the main app, not dependencies
        file_arg = local_file if not is_dep else None
        try:
            installed = install_single_app(
                dep_name,
                buckets_dir,
                is_dependency=is_dep,
                local_file=file_arg,
            )
        except BaseException:
            # A failed/aborted install (e.g. GUI automation never found the
            # setup window) can leave behind an empty apps/<app>/<version>
            # folder. Purge invalid/empty version folders so the app is not
            # mistaken for "already installed" on a retry or by the
            # MakingScoop AI fallback engine.
            get_valid_installed_versions(APPS_DIR, dep_name, cleanup_invalid=True)
            raise
        if not is_dep:
            app_installed = installed

    if not app_installed:
        click.echo(
            f"{Fore.RED}Installation unsuccessful for '{app}'.{Style.RESET_ALL}"
        )
        sys.exit(1)


def _resolve_install_target(
    app: str,
    local_file: Optional[str],
    buckets_dir: str,
) -> tuple[str, Optional[str]]:
    """Allow install to accept either an app name or an installer path."""
    manager = LocalManifestManager(buckets_dir)

    if local_file:
        app_name = manager.ensure_manifest(local_file, app_name=app)
        return app_name, os.path.abspath(local_file)

    if os.path.isfile(app):
        local_path = os.path.abspath(app)
        app_name = manager.ensure_manifest(local_path)
        return app_name, local_path

    return app.lower(), None


# ──────────────────────────────────────────────
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# update
# ──────────────────────────────────────────────

@cli.command()
@click.argument("app")
@handle_errors
def update(app: str):
    """Update an application to the latest version."""
    app_lower = app.lower()
    buckets_dir = get_buckets_dir()

    resolver = DependencyResolver(APPS_DIR, buckets_dir)
    if not resolver.is_installed(app_lower):
        click.echo(f"{Fore.RED}'{app}' is not installed. Run: myscoop install {app}{Style.RESET_ALL}")
        sys.exit(1)

    # Get current installed version
    current_version = resolver.get_installed_version(app_lower)

    # Get latest version from manifest
    manifest = Manifest(app_lower, buckets_dir)
    latest_version = manifest.version

    if current_version == latest_version:
        click.echo(
            f"{Fore.GREEN}'{app}' is already up to date ({current_version}) OK{Style.RESET_ALL}"
        )
        return

    click.echo(
        f"{Fore.BLUE}Updating '{app}': {current_version} -> {latest_version}{Style.RESET_ALL}"
    )

    # Install the new version (keeps old version until confirmed)
    install_single_app(app_lower, buckets_dir)

    # Remove old version folder if different
    if current_version and current_version != latest_version:
        old_path = os.path.join(APPS_DIR, app_lower, current_version)
        if os.path.exists(old_path):
            shutil.rmtree(old_path, ignore_errors=True)
            click.echo(f"  Removed old version: {current_version}")


# ──────────────────────────────────────────────
# list
# ──────────────────────────────────────────────

@cli.command(name="list")
@handle_errors
def list_apps():
    """List all installed applications."""
    if not os.path.exists(APPS_DIR):
        click.echo("No apps installed.")
        return

    apps = []
    for app_name in sorted(os.listdir(APPS_DIR)):
        app_path = os.path.join(APPS_DIR, app_name)
        if not os.path.isdir(app_path):
            continue
        versions = get_valid_installed_versions(
            APPS_DIR,
            app_name,
            cleanup_invalid=True,
        )
        if versions:
            version = versions[-1]
            apps.append((app_name, version))

    if not apps:
        click.echo("No apps installed.")
        return

    click.echo(f"\n{Fore.BLUE}Installed apps:{Style.RESET_ALL}")
    click.echo(f"{'-' * 40}")
    click.echo(f"  {'Name':<20} {'Version':<15}")
    click.echo(f"{'-' * 40}")
    for name, version in apps:
        click.echo(f"  {name:<20} {version:<15}")
    click.echo(f"{'-' * 40}")
    click.echo(f"  Total: {len(apps)} app(s)\n")


# ──────────────────────────────────────────────
# search
# ──────────────────────────────────────────────

@cli.command()
@click.argument("query")
@handle_errors
def search(query: str):
    """Search for apps across all buckets."""
    buckets_dir = get_buckets_dir()
    bm = BucketManager(buckets_dir)
    results = bm.search(query)

    if not results:
        click.echo(f"{Fore.YELLOW}No apps found matching '{query}'.{Style.RESET_ALL}")
        return

    click.echo(f"\n{Fore.BLUE}Search results for '{query}':{Style.RESET_ALL}")
    click.echo(f"{'-' * 60}")
    click.echo(f"  {'Name':<20} {'Version':<12} {'Description'}")
    click.echo(f"{'-' * 60}")
    for r in results:
        click.echo(f"  {r['name']:<20} {r['version']:<12} {r['description']}")
    click.echo(f"{'-' * 60}")
    click.echo(f"  {len(results)} result(s)\n")


# ──────────────────────────────────────────────
# info
# ──────────────────────────────────────────────

@cli.command()
@click.argument("app")
@handle_errors
def info(app: str):
    """Show detailed info about an app from its manifest."""
    buckets_dir = get_buckets_dir()
    manifest = Manifest(app, buckets_dir)

    click.echo(f"\n{Fore.BLUE}{'-' * 50}{Style.RESET_ALL}")
    click.echo(f"  {Fore.WHITE}{Style.BRIGHT}{manifest.name}{Style.RESET_ALL}")
    click.echo(f"{Fore.BLUE}{'-' * 50}{Style.RESET_ALL}")
    click.echo(f"  Description:  {manifest.description}")
    click.echo(f"  Version:      {manifest.version}")
    click.echo(f"  Homepage:     {manifest.homepage}")
    click.echo(f"  License:      {manifest.license}")
    click.echo(f"  Executables:  {', '.join(manifest.bin) if manifest.bin else 'none'}")
    click.echo(f"  Dependencies: {', '.join(manifest.depends) if manifest.depends else 'none'}")
    click.echo(f"  Installer:    {manifest.installer_type or 'auto'}")
    click.echo(f"  URL:          {manifest.url}")
    if manifest.url_hint:
        click.echo(f"  URL hint:     {manifest.url_hint}")
    if manifest.shortcuts:
        names = [s[1] for s in manifest.shortcuts if len(s) >= 2]
        click.echo(f"  Shortcuts:    {', '.join(names)}")

    # Show installed status
    resolver = DependencyResolver(APPS_DIR, buckets_dir)
    if resolver.is_installed(app):
        version = resolver.get_installed_version(app)
        click.echo(f"  Status:       {Fore.GREEN}Installed ({version}){Style.RESET_ALL}")
    else:
        click.echo(f"  Status:       {Fore.YELLOW}Not installed{Style.RESET_ALL}")

    click.echo(f"{Fore.BLUE}{'-' * 50}{Style.RESET_ALL}\n")


# ──────────────────────────────────────────────
# bucket
# ──────────────────────────────────────────────

@cli.group()
def bucket():
    """Manage app buckets (repositories)."""
    pass


@bucket.command(name="add")
@click.argument("name")
@click.argument("url")
@handle_errors
def bucket_add(name: str, url: str):
    """Add a new bucket from a Git repository URL."""
    bm = BucketManager(BUCKETS_DIR)
    bm.add_bucket(name, url)


@bucket.command(name="list")
@handle_errors
def bucket_list():
    """List all added buckets."""
    bm = BucketManager(get_buckets_dir())
    buckets = bm.list_buckets()

    if not buckets:
        click.echo("No buckets added.")
        return

    click.echo(f"\n{Fore.BLUE}Buckets:{Style.RESET_ALL}")
    click.echo(f"{'-' * 50}")
    click.echo(f"  {'Name':<15} {'Manifests':<10} {'Path'}")
    click.echo(f"{'-' * 50}")
    for b in buckets:
        click.echo(f"  {b['name']:<15} {b['manifests']:<10} {b['path']}")
    click.echo(f"{'-' * 50}\n")


@bucket.command(name="update")
@handle_errors
def bucket_update():
    """Update all buckets (git pull)."""
    bm = BucketManager(BUCKETS_DIR)
    count = bm.update_buckets()
    click.echo(f"\n{Fore.GREEN}Updated {count} bucket(s) OK{Style.RESET_ALL}")


@bucket.command(name="rm")
@click.argument("name")
@handle_errors
def bucket_rm(name: str):
    """Remove a bucket."""
    bm = BucketManager(BUCKETS_DIR)
    bm.remove_bucket(name)


# ──────────────────────────────────────────────
# cache
# ──────────────────────────────────────────────

@cli.group()
def cache():
    """Manage the download cache."""
    pass


@cache.command(name="rm")
@click.argument("app")
@handle_errors
def cache_rm(app: str):
    """Remove cached files for an app."""
    dl = Downloader(CACHE_DIR)
    if dl.remove_cached(app):
        click.echo(f"{Fore.GREEN}Cache cleared for '{app}' OK{Style.RESET_ALL}")
    else:
        click.echo(f"{Fore.YELLOW}No cached files found for '{app}'.{Style.RESET_ALL}")


# ──────────────────────────────────────────────
# depends
# ──────────────────────────────────────────────

@cli.command()
@click.argument("app")
@handle_errors
def depends(app: str):
    """Show the dependency tree for an app."""
    buckets_dir = get_buckets_dir()
    resolver = DependencyResolver(APPS_DIR, buckets_dir)

    click.echo(f"\n{Fore.BLUE}Dependency tree for '{app}':{Style.RESET_ALL}")
    tree = resolver.get_dependency_tree(app)
    click.echo(tree)
    click.echo()


# ──────────────────────────────────────────────
# metadata
# ──────────────────────────────────────────────

@cli.command()
@click.argument("app")
@handle_errors
def metadata(app: str):
    """Show metadata for an installed app."""
    app_lower = app.lower()
    app_path = os.path.join(APPS_DIR, app_lower)

    if not os.path.exists(app_path):
        click.echo(f"{Fore.RED}'{app}' is not installed.{Style.RESET_ALL}")
        sys.exit(1)

    # Find metadata.json — search version subdirs
    meta_path = None
    for version_dir in os.listdir(app_path):
        version_path = os.path.join(app_path, version_dir)
        all_candidate = os.path.join(version_path, "metadata_all.json")
        single_candidate = os.path.join(version_path, "metadata.json")
        if os.path.exists(all_candidate):
            meta_path = all_candidate
            break
        if os.path.exists(single_candidate):
            meta_path = single_candidate
            break

    if meta_path is None:
        click.echo(
            f"{Fore.YELLOW}No metadata found for '{app}'. "
            f"Try reinstalling.{Style.RESET_ALL}"
        )
        sys.exit(1)

    with open(meta_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        total = len(data)
        for index, item in enumerate(data, start=1):
            meta = AppMetadata.load_from_dict(item)
            meta.display(title=f"App Metadata {index}/{total}")
    else:
        meta = AppMetadata.load_from_dict(data)
        meta.display()


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    """Main entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
