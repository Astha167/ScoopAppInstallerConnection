"""Windows Registry based install verification for myscoop.

Some installers (especially GUI .exe installers) do not place their payload
inside myscoop's managed ``apps`` directory; instead they install into
``C:\\Program Files``, ``%LOCALAPPDATA%``, the desktop, etc. The folder-payload
check in :mod:`myscoop.install_state` cannot see those installs, which made a
genuinely successful installation look like a failure.

This module decides whether an application is actually installed by inspecting
the standard Windows uninstall registry hives:

    HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall
    HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall
    HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall

If a registry entry whose ``DisplayName`` or ``Publisher`` matches the
application is present, the application is considered installed. Direct
registry reads via ``winreg`` are used first; if nothing is found a PowerShell
``Get-ItemProperty ... | Where-Object {$_.DisplayName -like "*name*"}`` lookup
is used as a fallback (mirroring the manual command).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Dict, List, Optional, Set, Tuple

try:  # Reuse the existing registry enumeration / normalization helpers.
    from myscoop.metadata import (
        iter_installed_app_registry_entries,
        normalize_registry_text,
    )
except Exception:  # pragma: no cover - metadata import should normally succeed
    iter_installed_app_registry_entries = None  # type: ignore[assignment]
    normalize_registry_text = None  # type: ignore[assignment]


# Tokens that describe the installer/packaging, not the product itself. These
# are stripped so that "LabelSuite-setup" matches a registry entry named
# "LabelSuite 2".
_IGNORED_NAME_TOKENS = {
    "setup",
    "installer",
    "install",
    "uninstall",
    "update",
    "updater",
    "x64",
    "x86",
    "amd64",
    "win",
    "win32",
    "win64",
    "windows",
    "exe",
    "msi",
    "full",
    "offline",
    "online",
    "latest",
    "version",
    "ver",
    "bit",
    "64bit",
    "32bit",
}

# Minimum length of a product token we are willing to match on, to avoid
# spurious matches on tiny fragments like "ix" or "ab".
_MIN_TOKEN_LEN = 3
# Minimum length of a compact (joined) candidate used for substring matching.
_MIN_COMPACT_LEN = 4

# Some installer filenames do not contain the product's registry DisplayName.
# Map a distinctive compact fragment of the filename to the real product
# tokens so e.g. "iX3.3.exe" still matches the "iX Developer 3.3" entry.
PRODUCT_TOKEN_ALIASES = {
    "ix3": ("ix", "developer"),
    "ivms": ("ivms",),
    "ftprush": ("ftp", "rush"),
    "axisiputility": ("axis", "ip", "utility"),
}


def _normalize(text: str) -> str:
    if normalize_registry_text is not None:
        return normalize_registry_text(text or "")
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _product_tokens(name: str) -> List[str]:
    """Return meaningful product tokens from a candidate name."""
    tokens: List[str] = []
    for token in _normalize(name).split():
        if token in _IGNORED_NAME_TOKENS:
            continue
        if token.isdigit():
            continue
        if len(token) < _MIN_TOKEN_LEN:
            continue
        tokens.append(token)
    return tokens


# A candidate group is (compact_form, frozenset_of_tokens). An entry matches a
# group if the group's compact form substring-matches the entry text, or all of
# the group's tokens are present in the entry text.
_CandidateGroup = Tuple[str, frozenset]


def _build_candidate_groups(names: Tuple[str, ...]) -> List[_CandidateGroup]:
    """Build matchable candidate groups from the supplied names (+ aliases)."""
    groups: List[_CandidateGroup] = []
    seen: Set[_CandidateGroup] = set()

    def _add(compact: str, tokens: Tuple[str, ...]) -> None:
        token_set = frozenset(t for t in tokens if t)
        compact = compact if len(compact) >= _MIN_COMPACT_LEN else ""
        if not compact and not token_set:
            return
        group: _CandidateGroup = (compact, token_set)
        if group not in seen:
            seen.add(group)
            groups.append(group)

    for name in names:
        if not name:
            continue

        product_tokens = _product_tokens(name)
        if product_tokens:
            _add("".join(product_tokens), tuple(product_tokens))
        else:
            # Every token was ignored (e.g. a name like "setup"); fall back to
            # the raw normalized, compacted form so we still have something.
            _add(_normalize(name).replace(" ", ""), ())

        # Add alias groups when the filename contains a known fragment.
        name_compact = _normalize(name).replace(" ", "")
        for key, alias_tokens in PRODUCT_TOKEN_ALIASES.items():
            if key in name_compact:
                _add("".join(alias_tokens), alias_tokens)

    return groups


def _group_matches_haystack(group: _CandidateGroup, haystack: str) -> bool:
    if not haystack:
        return False

    compact, tokens = group
    haystack_compact = haystack.replace(" ", "")

    if compact and (compact in haystack_compact or haystack_compact in compact):
        return True

    if tokens:
        haystack_tokens = set(haystack.split())
        # All product tokens present (e.g. {"label", "suite"} ⊆ name tokens).
        if tokens <= haystack_tokens:
            return True
        # A single distinctive product token appears in the compact text.
        if len(tokens) == 1:
            only = next(iter(tokens))
            if len(only) >= _MIN_COMPACT_LEN and only in haystack_compact:
                return True

    return False


def _entry_matches(entry: Dict[str, str], groups: List[_CandidateGroup]) -> bool:
    display = _normalize(entry.get("DisplayName", ""))
    publisher = _normalize(entry.get("Publisher", ""))
    for group in groups:
        if _group_matches_haystack(group, display) or _group_matches_haystack(group, publisher):
            return True
    return False


def find_app_in_registry(*names: str) -> Optional[Dict[str, str]]:
    """Find an installed-app uninstall entry matching any of ``names`` (winreg)."""
    if iter_installed_app_registry_entries is None:
        return None

    groups = _build_candidate_groups(names)
    if not groups:
        return None

    try:
        for entry in iter_installed_app_registry_entries():
            if _entry_matches(entry, groups):
                return entry
    except Exception:
        return None
    return None


def _run_powershell_lookup(pattern: str, timeout: int) -> Optional[Dict[str, str]]:
    """Run the Get-ItemProperty uninstall lookup for a ``-like`` pattern.

    ``pattern`` is a normalized token (alphanumeric only), so it is safe to
    interpolate into the PowerShell command without quoting concerns.
    """
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$paths=@("
        "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'"
        ");"
        f"$pat='*{pattern}*';"
        "$r=Get-ItemProperty $paths | "
        "Where-Object {$_.DisplayName -like $pat -or $_.Publisher -like $pat} | "
        "Select-Object DisplayName,Publisher,DisplayVersion -First 1;"
        "if($r){$r | ConvertTo-Json -Compress}"
    )

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None

    out = (result.stdout or "").strip()
    if not out:
        return None

    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None

    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not data.get("DisplayName"):
        return None

    return {
        "DisplayName": str(data.get("DisplayName") or ""),
        "Publisher": str(data.get("Publisher") or ""),
        "DisplayVersion": str(data.get("DisplayVersion") or ""),
    }


def find_app_via_powershell(*names: str, timeout: int = 30) -> Optional[Dict[str, str]]:
    """PowerShell fallback that mirrors the manual Get-ItemProperty lookup."""
    groups = _build_candidate_groups(names)

    patterns: List[str] = []
    for compact, tokens in groups:
        if compact:
            patterns.append(compact)
        patterns.extend(tokens)
    # Most specific patterns first (longer fragments).
    patterns.sort(key=len, reverse=True)

    seen: Set[str] = set()
    for pattern in patterns:
        if not pattern or len(pattern) < _MIN_TOKEN_LEN or pattern in seen:
            continue
        seen.add(pattern)
        entry = _run_powershell_lookup(pattern, timeout)
        if entry:
            return entry
    return None


def is_app_installed_in_registry(
    *names: str,
    use_powershell_fallback: bool = True,
    timeout: int = 30,
) -> Optional[Dict[str, str]]:
    """Return the matching uninstall entry if the app is installed, else None.

    Tries a direct ``winreg`` scan first and, only if that finds nothing, falls
    back to a PowerShell ``Get-ItemProperty ... -like`` lookup.
    """
    match = find_app_in_registry(*names)
    if match:
        return match
    if use_powershell_fallback:
        return find_app_via_powershell(*names, timeout=timeout)
    return None
