"""
metadata.py — Application Metadata Extractor for myscoop

Extracts Windows file properties from installed executables using pywin32.
Saves metadata as JSON and displays it in a formatted terminal table.

Usage:
    meta = AppMetadata("C:/Users/you/myscoop/apps/postman/12.1.4/Postman.exe")
    info = meta.extract()
    meta.save("C:/Users/you/myscoop/apps/postman/12.1.4/")
    meta.display()
"""

import json
import os
import re
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    import win32api
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    import winreg
except ImportError:
    winreg = None

from colorama import Fore, Style, init as colorama_init


_NON_APP_EXE_KEYWORDS = (
    "uninstall",
    "unins",
    "uninst",
    "updater",
    "update",
    "setup",
    "installer",
    "install",
    "helper",
    "crash",
    "cleanup",
)


def resolve_shortcut_target(shortcut_path: str) -> Optional[str]:
    """
    Resolve a Windows .lnk shortcut to its target executable.

    The returned path is the shortcut target, not the .lnk file itself.
    """
    if Path(shortcut_path).suffix.lower() != ".lnk":
        return None

    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(os.path.abspath(shortcut_path))
        target = getattr(shortcut, "TargetPath", "") or ""
        if target and os.path.isfile(target):
            return os.path.normpath(target)
    except Exception:
        pass

    return None


ENABLE_RUNTIME_BINARY_VALIDATION = False


class ResolvedBinary(str):
    def __new__(cls, executable_path, confidence_score, resolution_reason):
        obj = super().__new__(cls, executable_path)
        obj.executable_path = executable_path
        obj.confidence_score = confidence_score
        obj.resolution_reason = resolution_reason
        return obj

    def __repr__(self):
        return f"ResolvedBinary(path={self.executable_path!r}, confidence={self.confidence_score!r}, reason={self.resolution_reason!r})"


class CandidateBinary:
    def __init__(self, path: str, size: int, depth: int, extension: str):
        self.path = os.path.normpath(path)
        self.size = size
        self.depth = depth
        self.extension = extension.lower()
        self.confidence = 0
        self.reasons = []


def _should_ignore_candidate(name: str) -> bool:
    name_lower = name.lower()
    ignore_keywords = [
        "uninstall", "unins", "setup", "installer", "update", "updater",
        "crash", "crashpad", "helper", "broker", "service", "elevate"
    ]
    return any(kw in name_lower for kw in ignore_keywords)


def _should_ignore_dir(name: str) -> bool:
    name_lower = name.lower()
    ignore_dirs = {"temp", "tmp", "cache", "logs", "$recycle.bin"}
    return name_lower in ignore_dirs


def get_pe_metadata(filepath: str) -> Dict[str, str]:
    metadata = {}
    fields = [
        "FileDescription",
        "ProductName",
        "InternalName",
        "OriginalFilename",
        "CompanyName",
        "FileVersion",
    ]
    if not HAS_WIN32:
        return {f: "" for f in fields}
    try:
        translations = win32api.GetFileVersionInfo(filepath, "\\VarFileInfo\\Translation")
        if translations:
            lang, codepage = translations[0]
            for field in fields:
                str_path = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field}"
                try:
                    value = win32api.GetFileVersionInfo(filepath, str_path)
                    metadata[field] = value.strip() if value else ""
                except Exception:
                    metadata[field] = ""
    except Exception:
        pass
    for f in fields:
        if f not in metadata:
            metadata[f] = ""
    return metadata


def format_size(size_bytes):
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def normalize_registry_text(text: str) -> str:
    """Normalize installed-app names from registry metadata for loose matching."""
    text = text.lower().replace("asp.net", "aspnet").replace(".net", "dotnet")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def iter_installed_app_registry_entries() -> Iterable[Dict[str, str]]:
    """Yield installed-application registry entries used for post-install discovery."""
    if winreg is None:
        return

    key_specs = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    ]
    wanted = (
        "DisplayName",
        "DisplayVersion",
        "InstallLocation",
        "Publisher",
    )

    for hive, path, wow_flag in key_specs:
        access = winreg.KEY_READ | wow_flag
        try:
            with winreg.OpenKey(hive, path, 0, access) as root_key:
                subkey_count = winreg.QueryInfoKey(root_key)[0]
                for index in range(subkey_count):
                    try:
                        subkey_name = winreg.EnumKey(root_key, index)
                        with winreg.OpenKey(root_key, subkey_name) as subkey:
                            data: Dict[str, str] = {}
                            for value_name in wanted:
                                try:
                                    value, _ = winreg.QueryValueEx(subkey, value_name)
                                except OSError:
                                    continue
                                if isinstance(value, str):
                                    data[value_name] = value
                            if data.get("DisplayName"):
                                yield data
                    except OSError:
                        continue
        except OSError:
            continue


def _legacy_find_installed_executable(
    app_dir: str,
    app_name: str = "",
    bin_entries: Optional[Iterable[str]] = None,
    shortcuts: Optional[Sequence[Sequence[str]]] = None,
) -> Optional[str]:
    app_dir = os.path.abspath(app_dir)

    for entry in bin_entries or []:
        resolved = _resolve_executable_candidate(app_dir, entry)
        if resolved:
            return resolved

    for shortcut_entry in shortcuts or []:
        if not shortcut_entry:
            continue
        resolved = _resolve_executable_candidate(app_dir, shortcut_entry[0])
        if resolved:
            return resolved

    return _find_best_executable_in_dir(app_dir, app_name=app_name)


def find_installed_executable(
    app_dir: str,
    app_name: str = "",
    bin_entries: Optional[Iterable[str]] = None,
    shortcuts: Optional[Sequence[Sequence[str]]] = None,
) -> Optional[str]:
    """
    Find the real installed executable to use for metadata extraction.

    Priority:
      1. Manifest bin entries under app_dir.
      2. Manifest shortcut targets under app_dir or resolved .lnk targets.
      3. Best executable discovered by scanning app_dir.

    This deliberately returns an .exe path, never a shortcut path.
    """
    executables = find_installed_executables(
        app_dir,
        app_name=app_name,
        bin_entries=bin_entries,
        shortcuts=shortcuts,
        max_results=1,
    )
    return executables[0] if executables else None


def find_installed_executables(
    app_dir: str,
    app_name: str = "",
    bin_entries: Optional[Iterable[str]] = None,
    shortcuts: Optional[Sequence[Sequence[str]]] = None,
    max_results: Optional[int] = None,
) -> List[ResolvedBinary]:
    """
    Find all likely launchable application binaries under app_dir.

    The resolver prefers manifest bins and shortcut targets, then uses PE
    metadata, portable-app structure, depth, size, and helper-name filters.
    This returns executable targets only, never .lnk files.
    """
    legacy_exe = _legacy_find_installed_executable(
        app_dir,
        app_name,
        bin_entries,
        shortcuts,
    )
    ranked_candidates = _rank_installed_executable_candidates(
        app_dir,
        app_name=app_name,
        bin_entries=bin_entries,
        shortcuts=shortcuts,
    )

    authoritative_reasons = {
        "shortcut_target_resolution",
        "manifest_bin_resolution",
        "pe_metadata_validation",
        "portable_app_structure",
    }

    selected: List[ResolvedBinary] = []
    seen_paths = set()

    for cand in ranked_candidates:
        if cand.extension == ".lnk":
            continue
        if any(r in cand.reasons for r in authoritative_reasons):
            primary_reason = next(
                r for r in cand.reasons if r in authoritative_reasons
            )
            resolved = ResolvedBinary(cand.path, cand.confidence, primary_reason)
            selected.append(resolved)
            seen_paths.add(os.path.normcase(cand.path))

    if not selected and legacy_exe:
        selected.append(ResolvedBinary(legacy_exe, 0, "legacy_fallback"))
        seen_paths.add(os.path.normcase(os.path.normpath(legacy_exe)))

    if ranked_candidates:
        best_confidence = ranked_candidates[0].confidence
        for cand in ranked_candidates:
            norm_path = os.path.normcase(cand.path)
            if cand.extension == ".lnk" or norm_path in seen_paths:
                continue
            if _is_ranked_app_candidate(cand, best_confidence):
                selected.append(
                    ResolvedBinary(cand.path, cand.confidence, "ranked_candidate")
                )
                seen_paths.add(norm_path)

    if max_results is not None:
        return selected[:max_results]
    return selected


def _rank_installed_executable_candidates(
    app_dir: str,
    app_name: str = "",
    bin_entries: Optional[Iterable[str]] = None,
    shortcuts: Optional[Sequence[Sequence[str]]] = None,
) -> List[CandidateBinary]:
    """Build and score executable candidates for metadata extraction."""
    app_dir = os.path.abspath(app_dir)
    candidates = {}

    if os.path.isdir(app_dir):
        for root, dirs, files in os.walk(app_dir):
            dirs[:] = [d for d in dirs if not _should_ignore_dir(d)]
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in {".exe", ".lnk", ".com", ".cmd", ".bat"}:
                    if _should_ignore_candidate(file):
                        continue
                    path = os.path.join(root, file)
                    norm_path = os.path.normpath(path)
                    try:
                        size = os.path.getsize(norm_path)
                    except OSError:
                        size = 0
                    rel = os.path.relpath(norm_path, app_dir)
                    depth = len([p for p in rel.split(os.sep) if p and p != "."])
                    candidates[norm_path] = CandidateBinary(norm_path, size, depth, ext)

    # 3. Shortcut target resolution
    for lnk_path, cand in list(candidates.items()):
        if cand.extension == ".lnk":
            target = resolve_shortcut_target(lnk_path)
            if target and os.path.isfile(target):
                norm_target = os.path.normpath(target)
                target_ext = os.path.splitext(norm_target)[1].lower()
                if target_ext in {".exe", ".com", ".cmd", ".bat"}:
                    if norm_target not in candidates:
                        try:
                            t_size = os.path.getsize(norm_target)
                        except OSError:
                            t_size = 0
                        t_rel = os.path.relpath(norm_target, app_dir)
                        t_depth = len([p for p in t_rel.split(os.sep) if p and p != "."])
                        candidates[norm_target] = CandidateBinary(norm_target, t_size, t_depth, target_ext)
                    target_cand = candidates[norm_target]
                    target_cand.confidence += 100
                    if "shortcut_target_resolution" not in target_cand.reasons:
                        target_cand.reasons.append("shortcut_target_resolution")

    # 4. Manifest bin resolution
    for entry in bin_entries or []:
        if not entry:
            continue
        entry_path = entry if os.path.isabs(entry) else os.path.join(app_dir, entry)
        norm_entry = os.path.normpath(entry_path)
        if os.path.isfile(norm_entry):
            if norm_entry not in candidates:
                ext = os.path.splitext(norm_entry)[1].lower()
                try:
                    t_size = os.path.getsize(norm_entry)
                except OSError:
                    t_size = 0
                t_rel = os.path.relpath(norm_entry, app_dir)
                t_depth = len([p for p in t_rel.split(os.sep) if p and p != "."])
                candidates[norm_entry] = CandidateBinary(norm_entry, t_size, t_depth, ext)
            cand = candidates[norm_entry]
            cand.confidence += 95
            if "manifest_bin_resolution" not in cand.reasons:
                cand.reasons.append("manifest_bin_resolution")

    # 5. PE metadata validation
    norm_app = _normalize_exe_name(app_name) if app_name else ""
    for path, cand in candidates.items():
        stem = Path(path).stem
        if norm_app and _normalize_exe_name(stem) == norm_app:
            cand.confidence += 75
            if "pe_metadata_validation" not in cand.reasons:
                cand.reasons.append("pe_metadata_validation")
        
        pe = get_pe_metadata(path)
        pe_matched = False
        if norm_app:
            for field in ["FileDescription", "ProductName", "InternalName", "OriginalFilename"]:
                val = _normalize_exe_name(pe.get(field, ""))
                if val and (norm_app in val or val in norm_app):
                    pe_matched = True
                    break
        if pe_matched:
            cand.confidence += 75
            if "pe_metadata_validation" not in cand.reasons:
                cand.reasons.append("pe_metadata_validation")

    # 6. Portable app structure detection
    portable_indicators = {"config", "data", "plugins", "resources", "assets"}
    for path, cand in candidates.items():
        parent_dir = os.path.dirname(path)
        try:
            if os.path.isdir(parent_dir):
                subdirs = {d.lower() for d in os.listdir(parent_dir) if os.path.isdir(os.path.join(parent_dir, d))}
                if subdirs.intersection(portable_indicators):
                    cand.confidence += 150
                    if "portable_app_structure" not in cand.reasons:
                        cand.reasons.append("portable_app_structure")
        except Exception:
            pass

    # 7. Wrapper prioritizing rules (soft prioritization)
    ext_weights = {
        ".exe": 50,
        ".lnk": 40,
        ".com": 30,
        ".cmd": 20,
        ".bat": 10
    }
    for path, cand in candidates.items():
        weight = ext_weights.get(cand.extension, 0)
        cand.confidence += weight

    ranked = [c for c in candidates.values() if c.extension != ".lnk"]
    ranked.sort(key=lambda c: (-c.confidence, c.depth, -c.size, c.path.lower()))
    return ranked


def _is_ranked_app_candidate(cand: CandidateBinary, best_confidence: int) -> bool:
    """Decide whether a non-authoritative candidate is still app-like enough."""
    if cand.extension not in {".exe", ".com", ".cmd", ".bat"}:
        return False
    if cand.size <= 0:
        return False
    if cand.depth > 4:
        return False
    if cand.confidence < max(40, best_confidence - 100):
        return False
    return True


def _resolve_executable_candidate(app_dir: str, candidate: str) -> Optional[str]:
    """Resolve a manifest path to a real executable, following .lnk targets."""
    if not candidate:
        return None

    candidate_path = (
        candidate
        if os.path.isabs(candidate)
        else os.path.join(app_dir, candidate)
    )
    candidate_path = os.path.normpath(candidate_path)

    if os.path.isfile(candidate_path):
        suffix = Path(candidate_path).suffix.lower()
        if suffix == ".exe":
            return candidate_path
        if suffix == ".lnk":
            target = resolve_shortcut_target(candidate_path)
            if target and Path(target).suffix.lower() == ".exe":
                return target

    return None


def _find_best_executable_in_dir(app_dir: str, app_name: str = "") -> Optional[str]:
    """Scan an installed app directory and pick the most likely app executable."""
    if not os.path.isdir(app_dir):
        return None

    candidates = []
    normalized_app_name = _normalize_exe_name(app_name)
    for path in Path(app_dir).rglob("*.exe"):
        if not path.is_file():
            continue

        name = path.name.lower()
        stem = path.stem.lower()
        normalized_stem = _normalize_exe_name(stem)
        if any(keyword in name for keyword in _NON_APP_EXE_KEYWORDS):
            continue

        rel = path.relative_to(app_dir)
        depth = len(rel.parts)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        score = 100
        if depth == 1:
            score += 25
        if normalized_app_name and normalized_stem == normalized_app_name:
            score += 80
        elif normalized_app_name and normalized_stem.endswith(normalized_app_name):
            score += 20
        if normalized_app_name and normalized_stem == f"c{normalized_app_name}":
            score -= 30
        if "app" in name or "client" in name:
            score += 10

        candidates.append((score, -depth, size, str(path).lower(), str(path)))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return os.path.normpath(candidates[0][4])


def _normalize_exe_name(value: str) -> str:
    """Normalize app/exe names for loose matching."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


class AppMetadata:
    """
    Extracts and manages metadata for an installed application executable.

    Uses the pywin32 library to read Windows file version info
    (the same data you see in Properties → Details).
    """

    def __init__(self, filepath: str) -> None:
        """
        Args:
            filepath: Absolute path to the exe file.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        self.filepath: str = os.path.normpath(filepath)
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(
                f"File not found: {self.filepath}\n"
                f"  Cannot extract metadata for a file that doesn't exist."
            )
        self._metadata: Optional[Dict[str, Any]] = None

    def extract(self) -> Dict[str, Any]:
        """
        Extract all metadata fields from the executable.

        Returns a dict with these keys:
            filename, filepath, filesize, filecrc32,
            fileversion, filedescription, filemanufacturer

        Returns:
            Dict containing all 7 metadata fields.
        """
        file_size = os.path.getsize(self.filepath)

        self._metadata = {
            "filename": os.path.basename(self.filepath),
            "filepath": self.filepath,
            "filesize": {
                "bytes": file_size,
                "human": self._human_size(file_size),
            },
            "filecrc32": self._compute_crc32(),
            "fileversion": self._get_version_info("FileVersion"),
            "filedescription": self._get_version_info("FileDescription"),
            "filemanufacturer": self._get_version_info("CompanyName"),
        }

        self._metadata["file_size"] = format_size(file_size)
        self._metadata["file_size_bytes"] = file_size
        self._metadata["file_size_human"] = format_size(file_size)
        self._metadata["file_description"] = self._metadata.get("filedescription") or ""
        self._metadata["product_name"] = self._get_version_info("ProductName") or ""
        self._metadata["company_name"] = self._metadata.get("filemanufacturer") or ""
        self._metadata["version"] = self._metadata.get("fileversion") or ""
        self._metadata["checksum"] = self._metadata.get("filecrc32") or ""
        self._metadata["executable_path"] = self.filepath

        return self._metadata

    def save(self, output_dir: str) -> str:
        """
        Save metadata as a JSON file to the specified directory.

        Args:
            output_dir: Directory to save metadata.json into.

        Returns:
            Path to the saved metadata.json file.
        """
        if self._metadata is None:
            self.extract()

        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "metadata.json")

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self._metadata, f, indent=4, ensure_ascii=False)

        return output_path

    def display(self, title: str = "App Metadata") -> None:
        """Print metadata in a clean formatted table using colorama."""
        colorama_init(autoreset=True)

        if self._metadata is None:
            self.extract()

        data = self._metadata

        # Table dimensions
        label_width = 18
        value_width = 40
        total_width = label_width + value_width + 3  # borders + separator

        # Format file size display
        size_info = data["filesize"]
        size_str = data.get("file_size_human") or data.get("file_size") or size_info.get("human")
        size_str = f"{size_str} ({size_info['bytes']:,} bytes)"

        rows = [
            ("Filename", data["filename"]),
            ("Filepath", self._truncate_path(data["filepath"], value_width)),
            ("File Size", size_str),
            ("CRC32", data["filecrc32"] or "N/A"),
            ("File Version", data["fileversion"] or "N/A"),
            ("Description", data["filedescription"] or "N/A"),
            ("Manufacturer", data["filemanufacturer"] or "N/A"),
        ]

        # Print table
        print(f"\n{Fore.CYAN}+{'-' * total_width}+")
        title_text = title[:total_width - 2]
        print(f"|  {Fore.WHITE}{Style.BRIGHT}{title_text}{Style.RESET_ALL}"
              f"{' ' * max(0, total_width - len(title_text) - 2)}{Fore.CYAN}|")
        print(f"+{'-' * label_width}+{'-' * (value_width + 2)}+")

        for label, value in rows:
            print(
                f"|  {Fore.WHITE}{label:<{label_width - 2}}"
                f"{Fore.CYAN}|  {Fore.GREEN}{value:<{value_width}}{Fore.CYAN}|"
            )   

        print(f"+{'-' * label_width}+{'-' * (value_width + 2)}+{Style.RESET_ALL}\n")

    @classmethod
    def load_from_json(cls, json_path: str) -> "AppMetadata":
        """
        Load metadata from a previously saved metadata.json file.

        Args:
            json_path: Path to metadata.json.

        Returns:
            AppMetadata instance with loaded data.
        """
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return cls.load_from_dict(data)

    @classmethod
    def load_from_dict(cls, data: Dict[str, Any]) -> "AppMetadata":
        """Create an AppMetadata instance from already-loaded metadata."""
        instance = object.__new__(cls)
        instance.filepath = data.get("filepath", "")
        instance._metadata = data
        return instance

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    def _compute_crc32(self) -> str:
        """Compute CRC32 checksum of the file."""
        try:
            crc = 0
            with open(self.filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    crc = zlib.crc32(chunk, crc)
            # Format as uppercase hex string
            return f"{crc & 0xFFFFFFFF:08X}"
        except (IOError, OSError):
            return None

    def _get_version_info(self, field: str) -> Optional[str]:
        """
        Read a string from the exe's Windows version info resource.

        Uses win32api.GetFileVersionInfo to read fields like
        FileVersion, FileDescription, CompanyName.

        Args:
            field: Version info field name.

        Returns:
            Field value string, or None if not available.
        """
        if not HAS_WIN32:
            return None

        try:
            info = win32api.GetFileVersionInfo(self.filepath, "\\")
            # Get the translation table to find the right language/codepage
            translations = win32api.GetFileVersionInfo(
                self.filepath,
                "\\VarFileInfo\\Translation"
            )
            if translations:
                lang, codepage = translations[0]
                str_path = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field}"
                value = win32api.GetFileVersionInfo(self.filepath, str_path)
                if value:
                    return value.strip()
        except Exception:
            pass

        return None

    @staticmethod
    def _human_size(size_bytes: int) -> str:
        """Convert bytes to human-readable string (matches Windows Explorer)."""
        import math
        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024.0:
                # Match Windows Explorer:
                #   < 100 → show 1 decimal (e.g. 63.4 MB)
                #   >= 100 → whole number  (e.g. 185 MB)
                if size_bytes >= 100:
                    return f"{int(size_bytes)} {unit}"
                truncated = math.floor(size_bytes * 10) / 10
                return f"{truncated:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{int(size_bytes)} TB"

    @staticmethod
    def _truncate_path(path: str, max_len: int) -> str:
        """Truncate a file path for display if too long."""
        if len(path) <= max_len:
            return path
        # Show ...\last_parts
        parts = path.split(os.sep)
        result = path
        while len(result) > max_len and len(parts) > 2:
            parts.pop(1)
            result = parts[0] + os.sep + "..." + os.sep + os.sep.join(parts[1:])
        return result
