"""
gui_installer.py — Adaptive GUI Automation for myscoop

Drives Windows installation wizards to completion by reading the live UI tree
and heuristically clicking through wizard pages. Designed for installers that
cannot be silenced via command-line flags (e.g. ABB Automation Builder).

Architecture:
    1. Launch the installer as a normal user-level process (no UAC).
    2. Detect the installer window via EnumWindows + process-tree matching.
    3. Loop: scan for buttons (pywinauto → ctypes → pyautogui fallback),
       score them, and click the best candidate.
    4. Handle license agreements, launch-after-install checkboxes, and
       stale-window situations automatically.
    5. On completion, record the files created in the managed app folder.

Enhanced Features for Complex Installers (ABB Automation Builder, etc.):
    - Multi-window tracking for installers that spawn child dialogs
    - Qt/WPF framework support via extended control type detection
    - Progress bar detection to wait during installation phases
    - Component selection handling with smart defaults
    - Installer-specific patterns (ABB, InstallShield, etc.)
    - Image-based click fallback for custom-drawn UIs
    - Extended timeout and retry logic for slow installers

Usage:
    gui = GUIInstaller()
    gui.install(r"C:\\path\\to\\setup.exe", r"C:\\Users\\you\\myscoop\\apps\\myapp\\1.0")
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("myscoop.gui_installer")

# ──────────────────────────────────────────────
# Optional imports — degrade gracefully
# ──────────────────────────────────────────────

try:
    import psutil
except ImportError:
    psutil = None

try:
    from pywinauto import Application, findwindows, Desktop
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.timings import TimeoutError as PywinautoTimeout
    _HAS_PYWINAUTO = True
except ImportError:
    _HAS_PYWINAUTO = False
    Desktop = None
    PywinautoTimeout = Exception

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.3
    _HAS_PYAUTOGUI = True
except ImportError:
    _HAS_PYAUTOGUI = False

try:
    from PIL import Image, ImageGrab
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

INSTALLER_TITLE_KEYWORDS = [
    "setup", "wizard", "install", "agreement", "update",
    "configuration", "preparing", "license", "welcome",
    "abb", "automation builder", "installshield",
    "extracting", "extract", "copying", "progress", "please wait",
    "compressed", "zipped",
]

IGNORED_WINDOW_TITLE_KEYWORDS = [
    "makingscoop installer dashboard",
    "visual studio code",
    "windows powershell",
    "administrator windows powershell",
    "command prompt",
    "task manager",
]

# Patterns that indicate the installer is busy (we should wait)
BUSY_TITLE_KEYWORDS = [
    "extracting", "copying", "installing", "preparing",
    "please wait", "progress", "processing", "configuring",
]

BUSY_TEXT_KEYWORDS = [
    "please wait while the installer initializes",
    "please wait",
    "reset plugin cache",
    "this may take some minutes",
    "installation manager",
    "package manager",
]

# Button text → priority score (higher = click first)
BUTTON_PRIORITY: Dict[str, int] = {
    # Completion (highest priority — means we're done)
    "finish":           100,
    "done":             100,
    "complete":          95,
    "exit setup":        93,
    "restart later":     92,
    "do not restart":    92,
    "no":                30,   # "restart now?" → we want "no" / "restart later"

    # Active installation
    "install":           85,
    "install now":       85,
    "start installation": 85,
    "begin installation": 85,
    "update":            82,
    "upgrade":           82,

    # Standard wizard progression
    "next":              70,
    "next >":            70,
    "weiter":            70,  # German
    "suivant":           70,  # French
    "continue":          68,
    "forward":           67,

    # License acceptance
    "i accept":          65,
    "i agree":           65,
    "accept":            63,
    "agree":             63,
    "i accept the terms": 66,
    "i accept the agreement": 66,

    # Generic confirmations
    "yes":               60,
    "ok":                60,
    "proceed":           50,
    "confirm":           48,
    "close":             45,
    "allow":             45,
    "apply":             40,
    "run":               35,
    "start":             33,

    # ABB-specific / industrial automation patterns
    "typical":           78,  # Typical installation option
    "standard":          77,
    "express":           76,
    "recommended":       75,
    "default":           74,
    "full":              73,
}

# Labels to NEVER click
SKIP_LABELS: Set[str] = {
    "cancel", "back", "zurück", "uninstall", "remove",
    "customize", "custom", "decline", "reject",
    "help", "about", "details", "browse",
    "print", "save", "export", "retry",
    "previous", "repair", "modify", "change",
    "ready to", "setup wizard", "click install", "to begin",
}

# Keywords indicating license acceptance controls
LICENSE_KEYWORDS = [
    "i accept", "accept the agreement", "accept the terms",
    "i agree", "accept the license", "i have read",
    "read and accept", "agree to the terms",
    "have read and agree", "license terms",
    "ich akzeptiere",  # German
    "j'accepte",       # French
]

LICENSE_REJECT_KEYWORDS = [
    "i do not accept", "do not accept", "don't accept",
    "i decline", "decline the agreement", "decline the terms",
    "not agree", "do not agree", "nicht akzeptiere",
]

# Keywords indicating launch-after-install checkboxes
LAUNCH_KEYWORDS = [
    "launch", "run ", "start ", "show readme",
    "view readme", "open ", "read me", "release notes",
    "view release", "start application",
]

# Control types to search for clickable elements (UIA)
CLICKABLE_CONTROL_TYPES = [
    "Button", "Hyperlink", "MenuItem", "TabItem", "SplitButton", "Custom",
    "RadioButton", "CheckBox",
]

# Class name patterns for buttons in various frameworks
BUTTON_CLASS_PATTERNS = [
    r"button",
    r"windowsforms.*button",
    r"qt.*button",
    r".*pushbutton.*",
    r"thunderrt6commandbutton",
    r"tbutton",  # Delphi
    r".*btn.*",
]


class GUIInstallError(Exception):
    """Raised when GUI automation fails."""
    pass


class GUIInstaller:
    """
    Adaptive GUI automation engine for Windows installation wizards.

    Installs entirely within the user's folder (no UAC required).
    Enhanced for complex installers like ABB Automation Builder:
    - Multi-window tracking
    - Progress bar detection
    - Extended control type support (Qt, WPF, custom)
    - Component selection handling
    - Installer-specific patterns
    """

    def __init__(
        self,
        max_wait_for_window: int = 180,  # Extended for slow extractors
        max_pages: int = 100,            # Extended for multi-step installers
        stale_threshold: int = 45,       # Extended for complex UI updates
        loop_interval: float = 1.0,      # Faster polling
        max_loop_interval: float = 5.0,  # Extended ceiling for installation phases
        installation_timeout: int = 1800, # 30 min max for large installs
        extractor_handoff_timeout: int = 60,
    ) -> None:
        """
        Args:
            max_wait_for_window: Seconds to wait for installer window to appear.
            max_pages:           Safety limit on wizard pages before aborting.
            stale_threshold:     Seconds of no change before trying fallback keys.
            loop_interval:       Initial sleep between scan cycles (seconds).
            max_loop_interval:   Maximum sleep (exponential backoff ceiling).
            installation_timeout: Maximum total installation time in seconds.
            extractor_handoff_timeout: Seconds to wait for a real setup wizard
                after a transient extractor/progress window closes.
        """
        self.max_wait_for_window = max_wait_for_window
        self.max_pages = max_pages
        self.stale_threshold = stale_threshold
        self.loop_interval = loop_interval
        self.max_loop_interval = max_loop_interval
        self.installation_timeout = installation_timeout
        self.extractor_handoff_timeout = extractor_handoff_timeout

        # State tracking
        self._installation_start: Optional[float] = None
        self._clicked_buttons: List[str] = []
        self._seen_window_titles: Set[str] = set()
        self._progress_detected: bool = False
        self._installer_name: str = ""
        self._target_dir: str = ""
        self._installer_root_pid: Optional[int] = None
        self._handled_special_windows: Set[int] = set()
        self._install_priority = dict(BUTTON_PRIORITY)

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def install(self, installer_path: str, app_dir: str) -> bool:
        """
        Launch an installer and drive it to completion via GUI automation.

        The installer is launched as a normal user process.
        Handles self-extracting installers that spawn child processes
        (possibly elevated) by tracking windows by title, not just PID.

        Args:
            installer_path: Absolute path to the setup executable.
            app_dir:        Target directory for the installation.

        Returns:
            True if installation completed successfully.

        Raises:
            GUIInstallError: If automation fails or times out.
        """
        installer_path = os.path.abspath(installer_path)
        app_dir = os.path.abspath(app_dir)
        os.makedirs(app_dir, exist_ok=True)

        if not os.path.isfile(installer_path):
            raise GUIInstallError(f"Installer not found: {installer_path}")

        print(f"    GUI automation starting: {os.path.basename(installer_path)}")
        logger.info(f"GUI automation starting: {installer_path}")
        logger.info(f"Target directory: {app_dir}")

        # Initialize state tracking
        self._installation_start = time.time()
        self._clicked_buttons = []
        self._seen_window_titles = set()
        self._progress_detected = False
        self._installer_name = os.path.basename(installer_path).lower()
        self._target_dir = app_dir
        self._installer_root_pid = None
        self._handled_special_windows = set()

        # Snapshot files before install (for tracking)
        pre_install_files = self._snapshot_directory(app_dir)

        # Take snapshot of existing windows BEFORE launching
        existing_windows = self._get_visible_windows()

        # Launch installer
        # Note: If the installer hard-requires Admin rights, it will trigger UAC.
        # We cannot use __COMPAT_LAYER=RunAsInvoker here because some enterprise
        # installers (like ABB) are hardcoded to crash if denied Admin rights.
        # If the installer path ends with .msi, we must launch it via msiexec
        cmd = [installer_path]
        if installer_path.lower().endswith(".msi"):
            cmd = ["msiexec", "/i", installer_path]
        process = subprocess.Popen(
            cmd,
            cwd=app_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"    Installer launched (PID: {process.pid}), waiting for setup window...")
        logger.info(f"Installer launched, PID: {process.pid}")
        self._installer_root_pid = process.pid

        try:
            # Wait for ANY installer window to appear
            # (PID-independent — handles self-extracting and elevated child processes)
            hwnd = self._wait_for_installer_window(
                process.pid, existing_windows
            )

            if hwnd is None:
                raise GUIInstallError(
                    f"No installer window detected within "
                    f"{self.max_wait_for_window}s. "
                    f"The installer may require manual intervention."
                )

            title = self._get_window_title(hwnd)
            self._seen_window_titles.add(title)
            print(f"    Installer window found: '{title}'")
            logger.info(f"Installer window found: HWND={hwnd}, title='{title}'")

            # Bring window to foreground for better interaction
            self._bring_to_foreground(hwnd)

            # Run the adaptive automation loop
            self._adaptive_loop(process, hwnd)

            elapsed = time.time() - self._installation_start
            print(f"    GUI automation finished (took {elapsed:.1f}s)")

            # Save install tracking info
            self._save_install_info(
                app_dir, installer_path, pre_install_files
            )

            return True

        except GUIInstallError:
            self._cleanup_failed_installer(process)
            raise
        except Exception as e:
            self._cleanup_failed_installer(process)
            raise GUIInstallError(f"GUI automation failed: {e}")

    # ──────────────────────────────────────────────
    # Window detection
    # ──────────────────────────────────────────────

    def _get_visible_windows(self) -> Set[int]:
        """Return a set of all currently visible window handles."""
        windows: Set[int] = set()
        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                windows.add(hwnd)
            return True

        user32.EnumWindows(enum_callback, 0)
        return windows

    def _get_window_title(self, hwnd: int) -> str:
        """Get the title/text of a window by handle."""
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _get_window_pid(self, hwnd: int) -> int:
        """Get the process ID that owns a window."""
        user32 = ctypes.windll.user32
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _get_process_tree_pids(self, root_pid: int) -> Set[int]:
        """Get all PIDs in a process tree (root + all descendants)."""
        pids = {root_pid}
        if psutil is not None:
            try:
                parent = psutil.Process(root_pid)
                for child in parent.children(recursive=True):
                    pids.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return pids

    def _cleanup_failed_installer(self, process: subprocess.Popen) -> None:
        """Stop a failed GUI installer before the AI fallback launches it again."""
        pids = self._get_process_tree_pids(process.pid)

        if psutil is not None:
            processes = []
            for pid in pids:
                try:
                    processes.append(psutil.Process(pid))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
                    continue

            for proc in processes:
                try:
                    if proc.is_running():
                        proc.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
                    pass

            _, alive = psutil.wait_procs(processes, timeout=5)
            for proc in alive:
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
                    pass

        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    process.kill()

    def _is_installer_window(self, title: str) -> bool:
        """Check if a window title looks like an installer wizard."""
        title_lower = title.lower()
        if self._should_ignore_window_title(title):
            return False
        return (
            any(kw in title_lower for kw in INSTALLER_TITLE_KEYWORDS)
            or self._title_matches_installer_filename(title)
        )

    def _should_ignore_window_title(self, title: str) -> bool:
        """Reject known non-installer windows that contain words like installer."""
        title_norm = self._normalize_label(title)
        if not title_norm:
            return True
        if "makingscoop installer dashboard" in title_norm:
            return True
        if "setup" in title_norm or "wizard" in title_norm:
            return False
        return any(
            ignored in title_norm
            for ignored in IGNORED_WINDOW_TITLE_KEYWORDS
        )

    def _title_matches_installer_filename(self, title: str) -> bool:
        """Match product-branded installer windows by tokens from the launched file."""
        title_norm = self._normalize_label(title)
        if not title_norm or not self._installer_name:
            return False

        filename = Path(self._installer_name).stem
        filename_norm = self._normalize_label(filename)
        tokens = re.findall(r"[a-z0-9]+", filename_norm)

        ignored = {
            "setup", "installer", "install", "update", "x64", "x86",
            "win", "windows", "exe", "msi",
        }
        for token in tokens:
            if token in ignored:
                continue
            if len(token) < 3:
                continue
            if token.isdigit() and len(token) < 4:
                continue
            if self._label_contains_phrase(title_norm, token):
                return True

        return False

    def _is_busy_window(self, title: str) -> bool:
        """Check if a window title indicates the installer is busy (progress/extracting)."""
        title_lower = title.lower()
        return any(kw in title_lower for kw in BUSY_TITLE_KEYWORDS)

    def _is_extractor_handoff_title(self, title: str) -> bool:
        """
        Detect transient extractor/bootstrap windows that often close before
        launching the real setup wizard.
        """
        norm = self._normalize_label(title or "")
        handoff_markers = (
            "extracting",
            "extract",
            "self extract",
            "preparing setup",
            "preparing to install",
        )
        return any(marker in norm for marker in handoff_markers)

    def _bring_to_foreground(self, hwnd: int) -> bool:
        """Attempt to bring the window to the foreground for better interaction."""
        try:
            user32 = ctypes.windll.user32
            # First, try SetForegroundWindow
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.1)

            # If that fails, try alternative methods
            if user32.GetForegroundWindow() != hwnd:
                # Send an alt key press to allow foreground change
                user32.keybd_event(0x12, 0, 0, 0)  # ALT down
                user32.keybd_event(0x12, 0, 2, 0)  # ALT up
                time.sleep(0.05)
                user32.SetForegroundWindow(hwnd)

            return True
        except Exception as e:
            logger.debug(f"Failed to bring window to foreground: {e}")
            return False

    def _get_window_rect(self, hwnd: int) -> Optional[Tuple[int, int, int, int]]:
        """Get the window rectangle (left, top, right, bottom)."""
        try:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            return None

    def _wait_for_installer_window(
        self, pid: int, existing_windows: Set[int]
    ) -> Optional[int]:
        """
        Poll until an installer window appears. Uses two strategies:
        1. PID-based: find windows belonging to the process tree
        2. Title-based: find ANY new window with installer-like title
           (handles self-extracting/elevated child processes)

        Returns:
            The HWND of the installer window, or None if timed out.
        """
        deadline = time.time() + self.max_wait_for_window
        logger.info("Waiting for installer window...")

        while time.time() < deadline:
            # Strategy 1: PID-based (process tree)
            tree_pids = self._get_process_tree_pids(pid)
            current_windows = self._get_visible_windows()
            new_windows = current_windows - existing_windows

            for hwnd in new_windows:
                try:
                    title = self._get_window_title(hwnd)
                    if not title:
                        continue
                    win_pid = self._get_window_pid(hwnd)
                    # Match by PID tree. Some installers show a product-titled
                    # bootstrap window before a "Setup" title appears.
                    if win_pid in tree_pids and self._is_process_window_candidate(hwnd, title):
                        logger.info(
                            f"Found installer process window: '{title}'"
                        )
                        return hwnd
                except OSError:
                    continue

            # Strategy 2: Title-based (PID-independent)
            # Catches elevated child processes, spawned dialogs, etc.
            for hwnd in new_windows:
                try:
                    title = self._get_window_title(hwnd)
                    if (
                        title
                        and self._is_installer_window(title)
                        and self._is_process_window_candidate(hwnd, title)
                    ):
                        logger.info(
                            f"Found installer window by title: '{title}'"
                        )
                        return hwnd
                except OSError:
                    continue

            # Strategy 3: Check ALL windows for installer titles
            # (some installers reuse existing window handles)
            for hwnd in current_windows:
                try:
                    title = self._get_window_title(hwnd)
                    if (
                        title
                        and self._is_installer_window(title)
                        and self._is_process_window_candidate(hwnd, title)
                    ):
                        win_pid = self._get_window_pid(hwnd)
                        if win_pid in tree_pids:
                            return hwnd
                    elif title and self._is_process_window_candidate(hwnd, title):
                        win_pid = self._get_window_pid(hwnd)
                        if win_pid in tree_pids:
                            return hwnd
                except OSError:
                    continue

            time.sleep(1.0)

        # Last resort: find ANY installer-titled window on the system
        for hwnd in self._get_visible_windows():
            try:
                title = self._get_window_title(hwnd)
                if (
                    title
                    and self._is_installer_window(title)
                    and self._is_process_window_candidate(hwnd, title)
                ):
                    if hwnd not in existing_windows:
                        return hwnd
            except OSError:
                continue

        return None

    def _is_process_window_candidate(self, hwnd: int, title: str = "") -> bool:
        """Accept visible top-level windows owned by the installer process tree."""
        title_norm = self._normalize_label(title or self._get_window_title(hwnd))
        if not title_norm:
            return False

        if self._should_ignore_window_title(title_norm):
            return False

        skip_titles = (
            "program manager",
            "windows powershell",
            "administrator windows powershell",
            "command prompt",
            "task manager",
        )
        if title_norm in skip_titles:
            return False

        rect = self._get_window_rect(hwnd)
        if rect:
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            if width < 120 or height < 80:
                return False

        return True

    def _is_window_alive(self, hwnd: int) -> bool:
        """Check if the window handle is still valid and visible."""
        user32 = ctypes.windll.user32
        return bool(
            user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd)
        )

    # ──────────────────────────────────────────────
    # Button scanning — multi-layer strategy
    # ──────────────────────────────────────────────

    def _normalize_label(self, text: str) -> str:
        """
        Normalize button text for comparison.
        Strips ampersands (Alt-key markers), ellipses, whitespace.
        """
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
        text = text.lower().strip()
        text = text.replace("&", "").replace("...", "").replace("…", "")
        text = text.replace(">", "").replace("<", "")
        text = text.replace("_", " ")  # Underscores sometimes used as separators
        text = " ".join(text.split())  # collapse whitespace
        return text

    def _score_button(self, label: str) -> int:
        """
        Score a button label. Returns 0 for skip labels, -1 for unknown.
        """
        norm = self._normalize_label(label)
        if not norm:
            return -1

        if self._looks_like_license_rejection_text(norm):
            return 0

        if self._is_non_action_label(norm):
            return -1

        # Check skip labels first
        for skip in SKIP_LABELS:
            # We only check if the skip phrase is inside the button text.
            # Example: skip "cancel" is in button "cancel installation" -> Skip!
            # We DO NOT check 'norm in skip' because that would mean
            # button "install" would match skip "uninstall"!
            if self._label_contains_phrase(norm, skip):
                return 0

        # Exact match first
        if norm in self._install_priority:
            return self._install_priority[norm]

        # Partial match (e.g. "I Accept the terms" contains "i accept")
        for key, score in sorted(
            self._install_priority.items(), key=lambda x: -x[1]
        ):
            if self._label_contains_phrase(norm, key):
                return score

        # Check for variations with special characters
        norm_clean = re.sub(r'[^a-z0-9\s]', '', norm)
        if norm_clean in self._install_priority:
            return self._install_priority[norm_clean]

        return -1  # Unknown button

    def _label_contains_phrase(self, normalized_label: str, phrase: str) -> bool:
        """Match button phrases on word boundaries to avoid heading false-positives."""
        label = self._normalize_label(normalized_label)
        target = self._normalize_label(phrase)
        if not label or not target:
            return False
        if label == target:
            return True

        parts = [re.escape(part) for part in target.split()]
        pattern = r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])"
        return bool(re.search(pattern, label))

    def _is_non_action_label(self, norm_label: str) -> bool:
        """Filter out status text, package names, and other non-button labels."""
        if any(keyword in norm_label for keyword in BUSY_TEXT_KEYWORDS):
            return True

        if "[" in norm_label or "]" in norm_label:
            return True

        if len(norm_label) > 35:
            return True

        if " installation manager" in norm_label:
            return True

        return False

    def _is_button_class(self, class_name: str) -> bool:
        """Check if a class name represents a button control."""
        class_lower = class_name.lower()
        for pattern in BUTTON_CLASS_PATTERNS:
            if re.search(pattern, class_lower):
                return True
        return False

    def _scan_layer1_pywinauto(
        self, hwnd: int
    ) -> List[Tuple[Any, str, int]]:
        """
        Layer 1: Use pywinauto UIA backend to find all clickable controls.
        Enhanced to scan multiple control types (Qt, WPF support).

        Returns:
            List of (control, label, score) tuples.
        """
        if not _HAS_PYWINAUTO:
            return []

        results = []
        try:
            app = Application(backend="uia").connect(handle=hwnd, timeout=5)
            dlg = app.window(handle=hwnd)

            # Scan multiple control types for better coverage
            for ctrl_type in CLICKABLE_CONTROL_TYPES:
                try:
                    for ctrl in dlg.descendants(control_type=ctrl_type):
                        try:
                            text = ctrl.window_text()
                            if text:
                                score = self._score_button(text)
                                if score > 0:
                                    if not self._is_actionable_control(ctrl, ctrl_type, text):
                                        continue
                                    # Check if control is enabled and visible
                                    try:
                                        if ctrl.is_enabled() and ctrl.is_visible():
                                            # Geometric filter: Ignore title bar 'X' buttons
                                            # If it's named "Close" and is in the top 45 pixels
                                            try:
                                                d_rect = dlg.rectangle()
                                                c_rect = ctrl.rectangle()
                                                if text.lower() == "close" and (c_rect.top - d_rect.top) < 45:
                                                    continue
                                            except Exception:
                                                pass
                                                
                                            results.append((ctrl, text, score))
                                    except Exception:
                                        # If we can't check, still include it
                                        results.append((ctrl, text, score))
                        except Exception:
                            continue
                except Exception:
                    continue

                if results and ctrl_type in {"Button", "SplitButton"}:
                    return results

        except PywinautoTimeout:
            logger.debug("Layer 1 (pywinauto) timed out connecting to window")
        except Exception as e:
            logger.debug(f"Layer 1 (pywinauto) failed: {e}")

        return results

    def _is_actionable_control(self, ctrl: Any, ctrl_type: str, text: str) -> bool:
        """Reject list/tree/status controls that merely contain clickable-looking text."""
        ctrl_type_lower = (ctrl_type or "").lower()
        text_norm = self._normalize_label(text)

        if self._is_non_action_label(text_norm):
            return False

        if (
            self._looks_like_license_acceptance_text(text_norm)
            or self._looks_like_license_rejection_text(text_norm)
        ):
            if ctrl_type_lower != "button":
                return False

        if ctrl_type_lower in {"listitem", "treeitem"}:
            return False

        if ctrl_type_lower == "custom":
            try:
                rect = ctrl.rectangle()
                if (rect.bottom - rect.top) > 90:
                    return False
            except Exception:
                pass

        return True

    def _scan_layer2_ctypes(
        self, hwnd: int
    ) -> List[Tuple[int, str, int]]:
        """
        Layer 2: Use ctypes EnumChildWindows to find buttons.
        Enhanced to recognize more button class patterns.

        Returns:
            List of (child_hwnd, label, score) tuples.
        """
        user32 = ctypes.windll.user32
        results = []

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_child_callback(child_hwnd, _lparam):
            try:
                # Skip invisible controls
                if not user32.IsWindowVisible(child_hwnd):
                    return True

                # Get class name
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(child_hwnd, class_buf, 256)
                class_name = class_buf.value

                # Enhanced button class matching
                if self._is_button_class(class_name):
                    # Get button text
                    text_len = user32.GetWindowTextLengthW(child_hwnd)
                    if text_len > 0:
                        text_buf = ctypes.create_unicode_buffer(text_len + 1)
                        user32.GetWindowTextW(
                            child_hwnd, text_buf, text_len + 1
                        )
                        text = text_buf.value
                        if self._is_license_option_label(text):
                            return True
                        score = self._score_button(text)
                        if score > 0:
                            # Check if button is enabled
                            if user32.IsWindowEnabled(child_hwnd):
                                results.append((child_hwnd, text, score))
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_child_callback, 0)
        except Exception as e:
            logger.debug(f"Layer 2 (ctypes) failed: {e}")

        return results

    def _scan_layer3_all_children(
        self, hwnd: int
    ) -> List[Tuple[int, str, int]]:
        """
        Layer 3: Scan ALL child windows for any text matching button keywords.
        More aggressive than layer 2 - catches custom controls.

        Returns:
            List of (child_hwnd, label, score) tuples.
        """
        user32 = ctypes.windll.user32
        results = []

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_all_callback(child_hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(child_hwnd):
                    return True
                if not user32.IsWindowEnabled(child_hwnd):
                    return True

                text_len = user32.GetWindowTextLengthW(child_hwnd)
                if text_len > 0 and text_len < 100:  # Button text is usually short
                    text_buf = ctypes.create_unicode_buffer(text_len + 1)
                    user32.GetWindowTextW(child_hwnd, text_buf, text_len + 1)
                    text = text_buf.value
                    if self._is_license_option_label(text):
                        return True
                    score = self._score_button(text)
                    if score > 0:
                        results.append((child_hwnd, text, score))
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_all_callback, 0)
        except Exception as e:
            logger.debug(f"Layer 3 (all children) failed: {e}")

        return results

    def _click_layer4_keyboard_fallback(self, key: str = "enter") -> None:
        """
        Layer 4: Fallback — press Enter/Space/Tab via pyautogui.
        Used when standard button detection fails.
        """
        if not _HAS_PYAUTOGUI:
            logger.debug("Layer 4 (pyautogui) unavailable")
            return

        try:
            logger.info(f"Layer 4 fallback: pressing {key}")
            pyautogui.press(key)
        except Exception as e:
            logger.debug(f"Layer 4 (pyautogui) failed: {e}")

    def _click_layer5_coordinates(self, hwnd: int, x: int, y: int) -> bool:
        """
        Layer 5: Click at specific coordinates relative to window.
        Used for custom-drawn UIs where no controls are detectable.
        """
        if not _HAS_PYAUTOGUI:
            return False

        try:
            rect = self._get_window_rect(hwnd)
            if rect:
                abs_x = rect[0] + x
                abs_y = rect[1] + y
                pyautogui.click(abs_x, abs_y)
                return True
        except Exception as e:
            logger.debug(f"Layer 5 (coordinate click) failed: {e}")
        return False

    # ──────────────────────────────────────────────
    # Progress detection
    # ──────────────────────────────────────────────

    def _detect_progress_bar(self, hwnd: int) -> bool:
        """
        Detect if the window contains an active progress bar.
        If progress is detected, we should wait rather than click.
        """
        if not _HAS_PYWINAUTO:
            return False

        try:
            app = Application(backend="uia").connect(handle=hwnd, timeout=3)
            dlg = app.window(handle=hwnd)

            # Look for ProgressBar controls
            for ctrl in dlg.descendants(control_type="ProgressBar"):
                try:
                    # Check if progress is between 0 and 100 (actively progressing)
                    value = ctrl.get_value()
                    if value is not None and 0 < value < 100:
                        logger.info(f"Progress bar detected: {value}%")
                        return True
                except Exception:
                    # Even if we can't read value, presence of progressbar suggests activity
                    return True

        except Exception:
            pass

        return False

    def _detect_busy_state(self, hwnd: int) -> bool:
        """
        Detect if the installer is in a busy state (copying, extracting, etc.).
        Combines window title check and progress bar detection.
        """
        title = self._get_window_title(hwnd)

        # A self-extract destination prompt is interactive, not "busy".
        if self._is_self_extract_prompt(hwnd, title):
            return False

        # Check title for busy keywords
        if self._is_busy_window(title):
            logger.info(f"Busy state detected from title: '{title}'")
            return True

        # Check for progress bars
        if self._detect_progress_bar(hwnd):
            return True

        # Check visible text content for status/progress phrases.
        if self._has_busy_text(hwnd):
            logger.info(f"Busy state detected from child text in window: '{title}'")
            return True

        return False

    def _has_busy_text(self, hwnd: int) -> bool:
        """Detect textual status content that indicates installation is still active."""
        title = self._get_window_title(hwnd)
        if not self._should_use_busy_text(title):
            return False

        if _HAS_PYWINAUTO:
            try:
                app = Application(backend="uia").connect(handle=hwnd, timeout=3)
                dlg = app.window(handle=hwnd)
                for ctrl in dlg.descendants():
                    try:
                        text = ctrl.window_text()
                    except Exception:
                        continue
                    norm = self._normalize_label(text)
                    if norm and any(keyword in norm for keyword in BUSY_TEXT_KEYWORDS):
                        return True
            except Exception:
                pass

        user32 = ctypes.windll.user32
        found = False

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(child_hwnd, _lparam):
            nonlocal found
            try:
                if not user32.IsWindowVisible(child_hwnd):
                    return True
                text_len = user32.GetWindowTextLengthW(child_hwnd)
                if text_len <= 0:
                    return True
                text_buf = ctypes.create_unicode_buffer(text_len + 1)
                user32.GetWindowTextW(child_hwnd, text_buf, text_len + 1)
                norm = self._normalize_label(text_buf.value)
                if norm and any(keyword in norm for keyword in BUSY_TEXT_KEYWORDS):
                    found = True
                    return False
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_callback, 0)
        except Exception:
            pass

        return found

    def _should_use_busy_text(self, title: str) -> bool:
        """Limit child-text busy detection to pages that are likely progress screens."""
        title_lower = (title or "").lower()

        blocked_page_keywords = (
            "selection page",
            "license page",
            "start page",
            "welcome",
            "finish page",
            "summary page",
        )
        if any(keyword in title_lower for keyword in blocked_page_keywords):
            return False

        allowed_page_keywords = (
            "installation page",
            "download page",
            "progress",
            "please wait",
            "extracting",
            "installing",
            "configuring",
        )
        return any(keyword in title_lower for keyword in allowed_page_keywords)

    # ──────────────────────────────────────────────
    # Special edge-case handlers
    # ──────────────────────────────────────────────

    def _handle_special_installer_windows(self, hwnd: int) -> bool:
        """Handle known custom dialogs before generic button scanning."""
        if hwnd in self._handled_special_windows:
            return False

        title = self._get_window_title(hwnd)
        if self._is_compressed_folder_run_prompt(hwnd, title):
            print("    Handling compressed-folder run prompt...")
            if self._click_dialog_button(hwnd, ("run",)):
                self._handled_special_windows.add(hwnd)
                return True

        if self._is_self_extract_prompt(hwnd, title):
            print("    Handling Autodesk self-extract prompt...")
            if self._submit_self_extract_dialog(hwnd):
                self._handled_special_windows.add(hwnd)
                return True

        return False

    def _is_compressed_folder_run_prompt(self, hwnd: int, title: str = "") -> bool:
        """Identify Windows' ZIP warning that offers Extract all / Run / Cancel."""
        title_norm = self._normalize_label(title or self._get_window_title(hwnd))
        if "compressed" not in title_norm and "zipped" not in title_norm:
            return False
        return self._window_has_button(hwnd, "run")

    def _window_has_button(self, hwnd: int, label: str) -> bool:
        return self._find_dialog_button(hwnd, (label,)) is not None

    def _click_dialog_button(self, hwnd: int, labels: Tuple[str, ...]) -> bool:
        button = self._find_dialog_button(hwnd, labels)
        if button is None:
            return False
        ctrl, layer = button
        self._bring_to_foreground(hwnd)
        if layer == "pywinauto":
            return self._click_pywinauto(ctrl)
        return self._click_ctypes(ctrl)

    def _find_dialog_button(
        self, hwnd: int, labels: Tuple[str, ...]
    ) -> Optional[Tuple[Any, str]]:
        wanted = {self._normalize_label(label) for label in labels}

        if _HAS_PYWINAUTO:
            try:
                app = Application(backend="uia").connect(handle=hwnd, timeout=3)
                dlg = app.window(handle=hwnd)
                for ctrl in dlg.descendants(control_type="Button"):
                    text = self._normalize_label(ctrl.window_text())
                    if text in wanted:
                        return ctrl, "pywinauto"
            except Exception:
                pass

        user32 = ctypes.windll.user32
        found: Optional[int] = None

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(child_hwnd, _lparam):
            nonlocal found
            try:
                if found is not None:
                    return False
                if not user32.IsWindowVisible(child_hwnd) or not user32.IsWindowEnabled(child_hwnd):
                    return True
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(child_hwnd, class_buf, 256)
                if not self._is_button_class(class_buf.value):
                    return True
                text_len = user32.GetWindowTextLengthW(child_hwnd)
                if text_len <= 0:
                    return True
                text_buf = ctypes.create_unicode_buffer(text_len + 1)
                user32.GetWindowTextW(child_hwnd, text_buf, text_len + 1)
                if self._normalize_label(text_buf.value) in wanted:
                    found = child_hwnd
                    return False
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_callback, 0)
        except Exception:
            pass

        if found is None:
            return None
        return found, "ctypes"

    def _is_self_extract_prompt(self, hwnd: int, title: str = "") -> bool:
        """Identify self-extract dialogs that ask for a destination folder."""
        title_lower = (title or self._get_window_title(hwnd)).lower()
        if "self-extract" not in title_lower and "extract to" not in title_lower:
            return False
        return self._window_has_edit_control(hwnd)

    def _window_has_edit_control(self, hwnd: int) -> bool:
        """Return True if the window contains a visible/editable text field."""
        if _HAS_PYWINAUTO:
            try:
                app = Application(backend="uia").connect(handle=hwnd, timeout=3)
                dlg = app.window(handle=hwnd)
                for ctrl in dlg.descendants(control_type="Edit"):
                    try:
                        if ctrl.is_visible():
                            return True
                    except Exception:
                        return True
            except Exception:
                pass

        user32 = ctypes.windll.user32
        found = False

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(child_hwnd, _lparam):
            nonlocal found
            try:
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(child_hwnd, class_buf, 256)
                if class_buf.value.lower() == "edit":
                    found = True
                    return False
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_callback, 0)
        except Exception:
            pass

        return found

    def _submit_self_extract_dialog(self, hwnd: int) -> bool:
        """Fill the destination field and confirm the self-extract dialog."""
        target_dir = self._target_dir
        if not target_dir:
            return False

        os.makedirs(target_dir, exist_ok=True)

        if _HAS_PYWINAUTO:
            try:
                app = Application(backend="uia").connect(handle=hwnd, timeout=3)
                dlg = app.window(handle=hwnd)
                edits = dlg.descendants(control_type="Edit")
                if edits:
                    edit = edits[0]
                    try:
                        edit.set_edit_text(target_dir)
                    except Exception:
                        try:
                            edit.click_input()
                            edit.type_keys("^a{BACKSPACE}", set_foreground=True)
                            edit.type_keys(target_dir, with_spaces=True, set_foreground=True)
                        except Exception:
                            pass

                    self._bring_to_foreground(hwnd)
                    try:
                        dlg.type_keys("{ENTER}")
                    except Exception:
                        if _HAS_PYAUTOGUI:
                            pyautogui.press("enter")
                    logger.info(f"Submitted self-extract dialog to: {target_dir}")
                    return True
            except Exception as e:
                logger.debug(f"Self-extract handler (pywinauto) failed: {e}")

        user32 = ctypes.windll.user32
        WM_SETTEXT = 0x000C
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        VK_RETURN = 0x0D
        edit_hwnd = None

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(child_hwnd, _lparam):
            nonlocal edit_hwnd
            try:
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(child_hwnd, class_buf, 256)
                if class_buf.value.lower() == "edit":
                    edit_hwnd = child_hwnd
                    return False
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_callback, 0)
            if edit_hwnd:
                user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, target_dir)
                self._bring_to_foreground(hwnd)
                user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0)
                user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0)
                logger.info(f"Submitted self-extract dialog via ctypes to: {target_dir}")
                return True
        except Exception as e:
            logger.debug(f"Self-extract handler (ctypes) failed: {e}")

        return False

    def _handle_license_agreements(self, hwnd: int) -> bool:
        """
        Scan for and accept license agreement radio buttons / checkboxes.
        Returns True if any license control was found and activated.
        """
        handled = False
        license_ready = False
        self._bring_to_foreground(hwnd)

        if _HAS_PYWINAUTO:
            try:
                app = Application(backend="uia").connect(handle=hwnd)
                dlg = app.window(handle=hwnd)

                # Check radio buttons
                for ctrl in dlg.descendants(control_type="RadioButton"):
                    try:
                        text = self._get_control_own_text(ctrl)
                        if (
                            not self._looks_like_license_acceptance_text(text)
                            and not self._looks_like_license_rejection_text(text)
                        ):
                            text = self._get_control_text_with_neighbors(ctrl, dlg).lower()
                        if self._looks_like_license_acceptance_text(text):
                            if self._option_is_selected(ctrl):
                                license_ready = True
                                break
                            try:
                                ctrl.select()
                            except Exception:
                                self._bring_to_foreground(hwnd)
                                ctrl.click_input()
                            logger.info(
                                f"License accepted (radio): {text or ctrl.window_text()}"
                            )
                            handled = True
                            time.sleep(0.5)
                            break
                    except Exception:
                        continue

                # Check checkboxes
                if not handled and not license_ready:
                    for ctrl in dlg.descendants(control_type="CheckBox"):
                        try:
                            text = self._get_control_own_text(ctrl)
                            if (
                                not self._looks_like_license_acceptance_text(text)
                                and not self._looks_like_license_rejection_text(text)
                            ):
                                text = self._get_control_text_with_neighbors(ctrl, dlg).lower()
                            if self._looks_like_license_acceptance_text(text):
                                if self._option_is_selected(ctrl):
                                    license_ready = True
                                    break
                                try:
                                    ctrl.toggle()
                                except Exception:
                                    self._bring_to_foreground(hwnd)
                                    ctrl.click_input()
                                logger.info(
                                    f"License accepted (checkbox): "
                                    f"{text or ctrl.window_text()}"
                                )
                                handled = True
                                time.sleep(0.5)
                                break
                        except Exception:
                            continue

                if not handled and not license_ready and self._is_license_page(hwnd):
                    handled = self._toggle_first_unchecked_option_on_license_page(dlg, hwnd)

            except Exception as e:
                logger.debug(f"License handler (pywinauto) failed: {e}")

        # Fallback: ctypes check for checkboxes with license text
        if license_ready:
            return False

        if not handled:
            handled = self._handle_license_ctypes(hwnd)

        return handled

    def _handle_license_ctypes(self, hwnd: int) -> bool:
        """Fallback license handling using ctypes."""
        user32 = ctypes.windll.user32
        BM_GETCHECK = 0x00F0
        BM_SETCHECK = 0x00F1
        BM_CLICK = 0x00F5
        BST_CHECKED = 1
        handled = False

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(child_hwnd, _lparam):
            nonlocal handled
            try:
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(child_hwnd, class_buf, 256)
                class_name = class_buf.value.lower()

                if "button" in class_name:
                    text_len = user32.GetWindowTextLengthW(child_hwnd)
                    if text_len > 0:
                        text_buf = ctypes.create_unicode_buffer(text_len + 1)
                        user32.GetWindowTextW(
                            child_hwnd, text_buf, text_len + 1
                        )
                        text = text_buf.value.lower()
                        if self._looks_like_license_acceptance_text(text):
                            # Check if it's unchecked and check it
                            state = user32.SendMessageW(
                                child_hwnd, BM_GETCHECK, 0, 0
                            )
                            if state != BST_CHECKED:
                                self._bring_to_foreground(hwnd)
                                user32.SendMessageW(
                                    child_hwnd, BM_SETCHECK, BST_CHECKED, 0
                                )
                                user32.SendMessageW(child_hwnd, BM_CLICK, 0, 0)
                                logger.info(
                                    f"License accepted (ctypes): "
                                    f"{text_buf.value}"
                                )
                                handled = True
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_callback, 0)
        except Exception:
            pass

        if not handled and self._is_license_page(hwnd):
            handled = self._handle_license_ctypes_first_option(hwnd)

        return handled

    def _looks_like_license_acceptance_text(self, text: str) -> bool:
        norm = self._normalize_label(text or "")
        if self._looks_like_license_rejection_text(norm):
            return False
        return any(kw in norm for kw in LICENSE_KEYWORDS)

    def _looks_like_license_rejection_text(self, text: str) -> bool:
        norm = self._normalize_label(text or "")
        return any(kw in norm for kw in LICENSE_REJECT_KEYWORDS)

    def _is_license_option_label(self, text: str) -> bool:
        return (
            self._looks_like_license_acceptance_text(text)
            or self._looks_like_license_rejection_text(text)
        )

    def _is_license_page(self, hwnd: int) -> bool:
        title = self._normalize_label(self._get_window_title(hwnd))
        if "license" in title or "agreement" in title:
            return True
        return self._window_contains_license_text(hwnd)

    def _window_contains_license_text(self, hwnd: int) -> bool:
        markers = (
            "software license agreement",
            "license agreement",
            "terms and conditions",
            "license terms",
            "i accept",
            "do not accept",
        )

        if _HAS_PYWINAUTO:
            try:
                app = Application(backend="uia").connect(handle=hwnd, timeout=3)
                dlg = app.window(handle=hwnd)
                for ctrl in dlg.descendants():
                    try:
                        norm = self._normalize_label(ctrl.window_text())
                    except Exception:
                        continue
                    if norm and any(marker in norm for marker in markers):
                        return True
            except Exception:
                pass

        user32 = ctypes.windll.user32
        found = False

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(child_hwnd, _lparam):
            nonlocal found
            try:
                if found or not user32.IsWindowVisible(child_hwnd):
                    return True
                text_len = user32.GetWindowTextLengthW(child_hwnd)
                if text_len <= 0:
                    return True
                text_buf = ctypes.create_unicode_buffer(text_len + 1)
                user32.GetWindowTextW(child_hwnd, text_buf, text_len + 1)
                norm = self._normalize_label(text_buf.value)
                if norm and any(marker in norm for marker in markers):
                    found = True
                    return False
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_callback, 0)
        except Exception:
            pass

        return found

    def _get_control_text_with_neighbors(self, ctrl: Any, dlg: Any) -> str:
        """Return a control's own text plus nearby text controls on the same row."""
        parts: List[str] = []
        try:
            own_text = ctrl.window_text()
            if own_text:
                parts.append(own_text)
        except Exception:
            pass

        try:
            ctrl_rect = ctrl.rectangle()
        except Exception:
            return " ".join(parts)

        try:
            for neighbor in dlg.descendants():
                if neighbor is ctrl:
                    continue
                try:
                    text = neighbor.window_text()
                    if not text:
                        continue
                    rect = neighbor.rectangle()
                    same_row = abs(rect.top - ctrl_rect.top) <= 24
                    to_right = rect.left >= ctrl_rect.left - 8
                    close = rect.left - ctrl_rect.right <= 420
                    if same_row and to_right and close:
                        parts.append(text)
                except Exception:
                    continue
        except Exception:
            pass

        return " ".join(parts)

    def _get_control_own_text(self, ctrl: Any) -> str:
        """Return a control's direct label text without scanning sibling controls."""
        try:
            return (ctrl.window_text() or "").lower()
        except Exception:
            return ""

    def _toggle_first_unchecked_option_on_license_page(self, dlg: Any, hwnd: int) -> bool:
        """Fallback for installers where the label is separate from the checkbox."""
        candidates: List[Any] = []
        for control_type in ("CheckBox", "RadioButton"):
            try:
                candidates.extend(dlg.descendants(control_type=control_type))
            except Exception:
                continue

        candidates.sort(key=self._control_sort_key)

        for ctrl in candidates:
            try:
                if not self._license_option_is_usable(ctrl):
                    continue
                text = self._get_control_text_with_neighbors(ctrl, dlg)
                if self._looks_like_license_rejection_text(text):
                    continue
                if not self._looks_like_license_acceptance_text(text):
                    continue
                if self._option_is_selected(ctrl):
                    return False
                self._activate_option(ctrl, hwnd)
                logger.info("License accepted using fallback checkbox/radio selection")
                time.sleep(0.5)
                return True
            except Exception:
                continue

        for ctrl in candidates:
            try:
                if not self._license_option_is_usable(ctrl):
                    continue
                text = self._get_control_text_with_neighbors(ctrl, dlg)
                if self._looks_like_license_rejection_text(text):
                    continue
                if self._option_is_selected(ctrl):
                    continue
                self._activate_option(ctrl, hwnd)
                logger.info("License accepted using first unchecked license option")
                time.sleep(0.5)
                return True
            except Exception:
                continue
        return False

    def _control_sort_key(self, ctrl: Any) -> Tuple[int, int]:
        try:
            rect = ctrl.rectangle()
            return rect.top, rect.left
        except Exception:
            return 99999, 99999

    def _license_option_is_usable(self, ctrl: Any) -> bool:
        try:
            rect = ctrl.rectangle()
            if rect.top < 100:
                return False
        except Exception:
            pass
        return True

    def _option_is_selected(self, ctrl: Any) -> bool:
        try:
            return bool(ctrl.get_toggle_state())
        except Exception:
            try:
                return bool(ctrl.get_check_state())
            except Exception:
                return False

    def _activate_option(self, ctrl: Any, hwnd: int) -> None:
        self._bring_to_foreground(hwnd)
        try:
            ctrl.select()
            return
        except Exception:
            pass
        try:
            ctrl.toggle()
            return
        except Exception:
            pass
        ctrl.click_input()

    def _handle_license_ctypes_first_option(self, hwnd: int) -> bool:
        """ctypes fallback for license pages whose label text is separate from the checkbox."""
        user32 = ctypes.windll.user32
        BM_GETCHECK = 0x00F0
        BM_CLICK = 0x00F5
        BST_CHECKED = 1
        candidates: List[Tuple[int, int, int, str]] = []

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(child_hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(child_hwnd) or not user32.IsWindowEnabled(child_hwnd):
                    return True
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(child_hwnd, class_buf, 256)
                class_name = class_buf.value.lower()
                if "button" not in class_name:
                    return True
                rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(child_hwnd, ctypes.byref(rect))
                if rect.top < 100:
                    return True
                text = ""
                text_len = user32.GetWindowTextLengthW(child_hwnd)
                if text_len > 0:
                    text_buf = ctypes.create_unicode_buffer(text_len + 1)
                    user32.GetWindowTextW(child_hwnd, text_buf, text_len + 1)
                    text = text_buf.value
                candidates.append((rect.top, rect.left, child_hwnd, text))
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_callback, 0)
        except Exception:
            return False

        sorted_candidates = sorted(candidates, key=lambda item: (item[0], item[1]))

        for _top, _left, child_hwnd, text in sorted_candidates:
            if not self._looks_like_license_acceptance_text(text):
                continue
            try:
                state = user32.SendMessageW(child_hwnd, BM_GETCHECK, 0, 0)
                if state == BST_CHECKED:
                    return False
                self._bring_to_foreground(hwnd)
                user32.SendMessageW(child_hwnd, BM_CLICK, 0, 0)
                logger.info("License accepted (ctypes fallback first option)")
                time.sleep(0.5)
                return True
            except Exception:
                continue

        for _top, _left, child_hwnd, text in sorted_candidates:
            if self._looks_like_license_rejection_text(text):
                continue
            try:
                state = user32.SendMessageW(child_hwnd, BM_GETCHECK, 0, 0)
                if state == BST_CHECKED:
                    continue
                self._bring_to_foreground(hwnd)
                user32.SendMessageW(child_hwnd, BM_CLICK, 0, 0)
                logger.info("License accepted (ctypes fallback first unchecked option)")
                time.sleep(0.5)
                return True
            except Exception:
                continue
        return False

    def _handle_launch_checkboxes(self, hwnd: int, is_finish: bool) -> None:
        """
        On the finish screen, uncheck 'Launch app' / 'Show readme'
        checkboxes so the app doesn't auto-start.
        """
        if not is_finish:
            return

        if _HAS_PYWINAUTO:
            try:
                app = Application(backend="uia").connect(handle=hwnd)
                dlg = app.window(handle=hwnd)

                for ctrl in dlg.descendants(control_type="CheckBox"):
                    try:
                        text = ctrl.window_text().lower()
                        if any(kw in text for kw in LAUNCH_KEYWORDS):
                            if ctrl.get_toggle_state():
                                ctrl.toggle()
                                logger.info(
                                    f"Unchecked launch checkbox: "
                                    f"{ctrl.window_text()}"
                                )
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"Launch checkbox handler failed: {e}")
                return

        # Fallback: ctypes
        self._handle_launch_checkboxes_ctypes(hwnd)

    def _handle_launch_checkboxes_ctypes(self, hwnd: int) -> None:
        """Fallback launch-checkbox handling using ctypes."""
        user32 = ctypes.windll.user32
        BM_GETCHECK = 0x00F0
        BM_SETCHECK = 0x00F1
        BST_UNCHECKED = 0
        BST_CHECKED = 1

        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def enum_callback(child_hwnd, _lparam):
            try:
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(child_hwnd, class_buf, 256)
                class_name = class_buf.value.lower()

                if "button" in class_name:
                    text_len = user32.GetWindowTextLengthW(child_hwnd)
                    if text_len > 0:
                        text_buf = ctypes.create_unicode_buffer(text_len + 1)
                        user32.GetWindowTextW(
                            child_hwnd, text_buf, text_len + 1
                        )
                        text = text_buf.value.lower()
                        if any(kw in text for kw in LAUNCH_KEYWORDS):
                            state = user32.SendMessageW(
                                child_hwnd, BM_GETCHECK, 0, 0
                            )
                            if state == BST_CHECKED:
                                user32.SendMessageW(
                                    child_hwnd, BM_SETCHECK,
                                    BST_UNCHECKED, 0
                                )
                                logger.info(
                                    f"Unchecked launch checkbox (ctypes): "
                                    f"{text_buf.value}"
                                )
            except Exception:
                pass
            return True

        try:
            user32.EnumChildWindows(hwnd, enum_callback, 0)
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # Click helpers
    # ──────────────────────────────────────────────

    def _click_pywinauto(self, ctrl: Any) -> bool:
        """Click a pywinauto control."""
        try:
            ctrl.click_input()
            return True
        except Exception:
            try:
                ctrl.click()
                return True
            except Exception as e:
                logger.debug(f"pywinauto click failed: {e}")
                return False

    def _click_ctypes(self, child_hwnd: int) -> bool:
        """Click a button by sending BM_CLICK message."""
        BM_CLICK = 0x00F5
        try:
            ctypes.windll.user32.SendMessageW(
                child_hwnd, BM_CLICK, 0, 0
            )
            return True
        except Exception as e:
            logger.debug(f"ctypes click failed: {e}")
            return False

    # ──────────────────────────────────────────────
    # The adaptive loop
    # ──────────────────────────────────────────────

    def _find_any_installer_window(self) -> Optional[int]:
        """
        Find ANY visible installer window on the system by title keywords.
        PID-independent — works even when the installer was spawned as an
        elevated child process with a different PID.
        """
        best_hwnd = None
        best_title = ""
        best_score = 0

        for hwnd in self._get_visible_windows():
            try:
                title = self._get_window_title(hwnd)
                if (
                    title
                    and self._is_installer_window(title)
                    and self._is_process_window_candidate(hwnd, title)
                ):
                    # Prioritize windows with more specific installer keywords
                    score = sum(1 for kw in INSTALLER_TITLE_KEYWORDS if kw in title.lower())
                    # Prefer longer titles (usually more specific)
                    score += len(title) / 100

                    if score > best_score:
                        best_hwnd = hwnd
                        best_title = title
                        best_score = score
            except OSError:
                continue

        return best_hwnd

    def _find_all_installer_windows(self) -> List[Tuple[int, str]]:
        """
        Find ALL visible installer windows on the system.
        Returns list of (hwnd, title) tuples.
        """
        results = []
        tree_pids: Set[int] = set()
        if self._installer_root_pid is not None:
            tree_pids = self._get_process_tree_pids(self._installer_root_pid)

        for hwnd in self._get_visible_windows():
            try:
                title = self._get_window_title(hwnd)
                if not title:
                    continue
                win_pid = self._get_window_pid(hwnd)
                if (
                    self._is_installer_window(title)
                    and self._is_process_window_candidate(hwnd, title)
                ):
                    results.append((hwnd, title))
                elif win_pid in tree_pids and self._is_process_window_candidate(hwnd, title):
                    results.append((hwnd, title))
            except OSError:
                continue
        return results

    def _handle_multiple_windows(self) -> Optional[int]:
        """
        Handle cases where multiple installer windows exist.
        Returns the best window to interact with, or None.
        """
        windows = self._find_all_installer_windows()

        if not windows:
            return None

        if len(windows) == 1:
            return windows[0][0]

        # Multiple windows - need to pick the best one
        # Prioritize: dialogs/popups (usually smaller, more urgent) over main windows
        # Also prioritize windows with action buttons visible

        best_hwnd = None
        best_priority = -1

        for hwnd, title in windows:
            priority = 0
            title_lower = title.lower()

            # Dialogs/prompts get higher priority
            if any(kw in title_lower for kw in ["agreement", "license", "warning", "error", "confirm"]):
                priority += 10

            # Check if window has actionable buttons
            buttons = self._scan_layer1_pywinauto(hwnd)
            if not buttons:
                buttons = self._scan_layer2_ctypes(hwnd)

            if buttons:
                priority += 5
                # Higher score buttons = more important window
                max_button_score = max(b[2] for b in buttons) if buttons else 0
                priority += max_button_score / 10

            if priority > best_priority:
                best_priority = priority
                best_hwnd = hwnd

        return best_hwnd

    def _adaptive_loop(self, process: subprocess.Popen, hwnd: int) -> None:
        """
        The main automation loop. Scans for buttons, scores them,
        and clicks the best candidate on each iteration.

        Enhanced for complex installers like ABB Automation Builder:
        - Multi-window handling
        - Progress detection (wait during installation phases)
        - Extended timeout handling
        - Better fallback strategies
        """
        pages_seen = 0
        last_button_set: str = ""
        stale_since: Optional[float] = None
        interval = self.loop_interval
        no_window_count = 0
        busy_wait_count = 0
        last_click_time = time.time()
        fallback_attempts = 0
        max_fallback_attempts = 5
        finish_clicked = False
        extractor_handoff_until: Optional[float] = None
        extractor_handoff_logged = False

        while True:
            # Check overall timeout
            if self._installation_start and time.time() - self._installation_start > self.installation_timeout:
                raise GUIInstallError(
                    f"Installation timeout: exceeded {self.installation_timeout}s. "
                    f"The installer may require manual intervention."
                )

            # Handle multiple windows (dialogs, popups, etc.)
            current_hwnd = self._handle_multiple_windows()

            if current_hwnd is None:
                no_window_count += 1
                if finish_clicked:
                    logger.info("Finish clicked and installer windows closed")
                    print("    Finish clicked - installation complete")
                    break
                if (
                    extractor_handoff_until is not None
                    and time.time() < extractor_handoff_until
                ):
                    if not extractor_handoff_logged:
                        print("    Extractor closed - waiting for setup wizard...")
                        extractor_handoff_logged = True
                    time.sleep(interval)
                    continue
                # Give it more cycles for complex installers that transition between windows
                if no_window_count >= 10:
                    logger.info(
                        "No installer windows for 10 cycles — assuming complete"
                    )
                    print("    No installer windows detected — installation complete")
                    break
                time.sleep(interval)
                continue
            else:
                no_window_count = 0
                hwnd = current_hwnd

            # Track new window titles
            title = self._get_window_title(hwnd)
            if title and self._is_extractor_handoff_title(title):
                extractor_handoff_until = (
                    time.time() + self.extractor_handoff_timeout
                )
                extractor_handoff_logged = False
            elif title:
                extractor_handoff_until = None
                extractor_handoff_logged = False

            if title and title not in self._seen_window_titles:
                self._seen_window_titles.add(title)
                print(f"    New window: '{title}'")
                logger.info(f"New installer window detected: '{title}'")
                # Bring new window to foreground
                self._bring_to_foreground(hwnd)

            # Handle extractor prompts and other custom dialogs before generic logic.
            if self._handle_special_installer_windows(hwnd):
                time.sleep(interval)
                continue

            # ── Step 0: Check if installer is busy (progress bar, extracting, etc.) ──
            if self._detect_busy_state(hwnd):
                busy_wait_count += 1
                self._progress_detected = True
                if self._is_extractor_handoff_title(title):
                    extractor_handoff_until = (
                        time.time() + self.extractor_handoff_timeout
                    )
                    extractor_handoff_logged = False
                if busy_wait_count % 10 == 0:  # Log every 10 cycles
                    print(f"    Installation in progress (waiting {busy_wait_count * interval:.0f}s)...")
                time.sleep(interval)
                continue
            else:
                if busy_wait_count > 0:
                    print(f"    Progress phase completed after {busy_wait_count * interval:.0f}s")
                busy_wait_count = 0

            # Safety limit - but increase if progress was detected
            pages_seen += 1
            effective_max_pages = self.max_pages * 2 if self._progress_detected else self.max_pages

            if pages_seen > effective_max_pages:
                logger.warning(
                    f"Exceeded max pages ({effective_max_pages}), aborting"
                )
                raise GUIInstallError(
                    f"GUI automation exceeded {effective_max_pages} page limit. "
                    f"The installer may require manual intervention."
                )

            # ── Step 1: Handle license agreements ──
            # License controls are handled after the button scan if no
            # enabled forward/action button is available.

            # ── Step 2: Scan for buttons using multiple layers ──
            buttons = self._scan_layer1_pywinauto(hwnd)
            layer_used = 1

            if not buttons:
                buttons = self._scan_layer2_ctypes(hwnd)
                if buttons:
                    layer_used = 2

            if not buttons:
                buttons = self._scan_layer3_all_children(hwnd)
                if buttons:
                    layer_used = 3

            # ── Step 3: Check for stale state ──
            current_set = str(
                sorted([(lbl, sc) for _, lbl, sc in buttons])
            )
            if current_set == last_button_set and buttons:
                if stale_since is None:
                    stale_since = time.time()
                elif time.time() - stale_since > self.stale_threshold:
                    fallback_attempts += 1
                    logger.info(f"Stale state detected, fallback attempt {fallback_attempts}")

                    if fallback_attempts <= max_fallback_attempts:
                        # Try different fallback strategies
                        if fallback_attempts == 1:
                            print("    Stale state — pressing Enter as fallback")
                            self._click_layer4_keyboard_fallback("enter")
                        elif fallback_attempts == 2:
                            print("    Stale state — pressing Space as fallback")
                            self._click_layer4_keyboard_fallback("space")
                        elif fallback_attempts == 3:
                            print("    Stale state — pressing Tab+Enter")
                            self._click_layer4_keyboard_fallback("tab")
                            time.sleep(0.3)
                            self._click_layer4_keyboard_fallback("enter")
                        elif fallback_attempts == 4:
                            print("    Stale state — trying Alt+N (Next shortcut)")
                            if _HAS_PYAUTOGUI:
                                pyautogui.hotkey("alt", "n")
                        else:
                            print("    Stale state — trying Alt+I (Install shortcut)")
                            if _HAS_PYAUTOGUI:
                                pyautogui.hotkey("alt", "i")

                    stale_since = time.time()
                    time.sleep(interval)
                    continue
            else:
                stale_since = None
                last_button_set = current_set
                interval = self.loop_interval
                fallback_attempts = 0

            if not buttons:
                if self._handle_license_agreements(hwnd):
                    time.sleep(interval)
                    continue

                interval = min(interval * 1.2, self.max_loop_interval)
                logger.debug(
                    f"No buttons found, sleeping {interval:.1f}s"
                )
                
                # DIAGNOSTIC: Track how many times we've had no buttons
                if not hasattr(self, '_no_button_count'):
                    self._no_button_count = 0
                self._no_button_count += 1
                
                if self._no_button_count == 4 and _HAS_PYWINAUTO:
                    print("    [DIAGNOSTIC] Stuck with no buttons. Dumping window UI tree:")
                    try:
                        app = Application(backend="uia").connect(handle=hwnd, timeout=3)
                        dlg = app.window(handle=hwnd)
                        import io, sys
                        out = io.StringIO()
                        orig = sys.stdout
                        sys.stdout = out
                        try:
                            dlg.print_control_identifiers(depth=4)
                        finally:
                            sys.stdout = orig
                        
                        for line in out.getvalue().split('\n'):
                            if line.strip():
                                print("        " + line.rstrip())
                    except Exception as e:
                        print(f"    [DIAGNOSTIC] Failed to dump tree: {e}")
                
                if self._no_button_count == 5:
                    print("    [FALLBACK] Completely blind to UI controls! Sending ENTER key...")
                    self._click_layer4_keyboard_fallback("enter")
                        
                time.sleep(interval)
                continue
            else:
                self._no_button_count = 0

            # ── Step 4: Pick the best button ──
            buttons.sort(key=lambda x: x[2], reverse=True)
            best_ctrl, best_label, best_score = buttons[0]

            # Check if this is a finish page
            is_finish = best_score >= 90

            # ── Step 5: Handle launch checkboxes on finish page ──
            self._handle_launch_checkboxes(hwnd, is_finish)

            # ── Step 6: Avoid clicking the same button repeatedly ──
            button_id = f"{best_label}@{hwnd}"
            if button_id in self._clicked_buttons[-5:]:  # Last 5 clicks
                # Already clicked this button recently - might be stuck
                logger.debug(f"Recently clicked '{best_label}', trying next best")
                if len(buttons) > 1:
                    best_ctrl, best_label, best_score = buttons[1]
                    button_id = f"{best_label}@{hwnd}"

            # ── Step 7: Click the best button ──
            print(f"    [{pages_seen}] Clicking: '{best_label}' (score={best_score}, layer={layer_used})")
            logger.info(
                f"Page {pages_seen}: Clicking '{best_label}' "
                f"(score={best_score}, layer={layer_used})"
            )

            clicked = False
            if layer_used == 1:
                clicked = self._click_pywinauto(best_ctrl)
            elif layer_used in (2, 3):
                clicked = self._click_ctypes(best_ctrl)

            if not clicked:
                logger.warning(
                    f"Failed to click '{best_label}', trying keyboard fallback"
                )
                self._click_layer4_keyboard_fallback("enter")
            else:
                self._clicked_buttons.append(button_id)
                last_click_time = time.time()
                if is_finish:
                    finish_clicked = True

            # Wait for the UI to update
            time.sleep(interval)

    # ──────────────────────────────────────────────
    # Install tracking
    # ──────────────────────────────────────────────

    def _snapshot_directory(self, directory: str) -> Set[str]:
        """Take a snapshot of all files in a directory (recursive)."""
        files: Set[str] = set()
        if os.path.isdir(directory):
            for root, _dirs, filenames in os.walk(directory):
                for fn in filenames:
                    rel = os.path.relpath(
                        os.path.join(root, fn), directory
                    )
                    files.add(rel)
        return files

    def _save_install_info(
        self,
        app_dir: str,
        installer_path: str,
        pre_install_files: Set[str],
    ) -> None:
        """
        Save install tracking info for diagnostics and metadata.
        Records which files were added by the installer.
        """
        post_install_files = self._snapshot_directory(app_dir)
        new_files = sorted(post_install_files - pre_install_files)

        info = {
            "installed_by": "gui_automation",
            "installer_path": installer_path,
            "install_timestamp": datetime.now().isoformat(),
            "files_installed": new_files,
            "total_files": len(new_files),
            "note": (
                "This application was installed via GUI automation into the "
                "managed myscoop app folder."
            ),
        }

        info_path = os.path.join(app_dir, "gui_install_info.json")
        try:
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(info, f, indent=4)
            logger.info(
                f"Install info saved: {len(new_files)} files tracked"
            )
        except Exception as e:
            logger.warning(f"Failed to save install info: {e}")


# ──────────────────────────────────────────────
# Installer-specific pattern handlers
# ──────────────────────────────────────────────

class InstallerPatterns:
    """
    Collection of installer-specific patterns and handling strategies.
    Useful for complex installers like ABB Automation Builder, InstallShield, etc.
    """

    # ABB Automation Builder specific patterns
    ABB_TITLE_PATTERNS = [
        "abb", "automation builder", "automation-builder",
        "b&r automation", "codesys",
    ]

    ABB_BUTTON_OVERRIDES = {
        "typical installation": 85,
        "standard installation": 84,
        "minimal installation": 50,  # Lower priority
        "complete installation": 80,
    }

    # InstallShield specific patterns
    INSTALLSHIELD_TITLE_PATTERNS = [
        "installshield", "setup wizard", "installaware",
    ]

    # Common component selection patterns
    COMPONENT_ACCEPT_KEYWORDS = [
        "select all", "typical", "complete", "full",
        "recommended", "standard", "default",
    ]

    COMPONENT_SKIP_KEYWORDS = [
        "custom", "minimal", "compact", "none",
    ]

    @classmethod
    def is_abb_installer(cls, title: str, filename: str = "") -> bool:
        """Check if this is an ABB-style installer."""
        combined = (title + " " + filename).lower()
        return any(p in combined for p in cls.ABB_TITLE_PATTERNS)

    @classmethod
    def is_installshield(cls, title: str) -> bool:
        """Check if this is an InstallShield installer."""
        return any(p in title.lower() for p in cls.INSTALLSHIELD_TITLE_PATTERNS)

    @classmethod
    def get_button_override_score(cls, label: str, installer_type: str) -> Optional[int]:
        """Get an override score for specific installer types."""
        label_lower = label.lower()

        if installer_type == "abb":
            for pattern, score in cls.ABB_BUTTON_OVERRIDES.items():
                if pattern in label_lower:
                    return score

        return None

    @classmethod
    def should_select_component(cls, label: str) -> Optional[bool]:
        """
        Determine if a component checkbox should be selected.
        Returns True to select, False to deselect, None if uncertain.
        """
        label_lower = label.lower()

        # Skip these components (they might cause issues)
        skip_patterns = [
            "debug", "source", "samples", "documentation",
            "help files", "tutorials", "examples",
        ]
        if any(p in label_lower for p in skip_patterns):
            return None  # Leave as-is

        # Accept these components
        accept_patterns = [
            "runtime", "core", "main", "application",
            "required", "essential",
        ]
        if any(p in label_lower for p in accept_patterns):
            return True

        return None  # Leave as-is


def run_gui_installer(
    installer_path: str,
    app_dir: str,
    timeout: int = 1800,
    verbose: bool = False,
) -> bool:
    """
    Convenience function to run GUI installation.

    Args:
        installer_path: Path to the installer executable.
        app_dir: Target installation directory.
        timeout: Maximum installation time in seconds.
        verbose: Enable verbose logging.

    Returns:
        True if installation succeeded.

    Raises:
        GUIInstallError: If installation fails.
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    installer = GUIInstaller(installation_timeout=timeout)
    return installer.install(installer_path, app_dir)
