"""
dependency.py — Dependency Resolver for myscoop

Recursively resolves app dependencies from manifest 'depends' fields.
Detects circular dependencies and returns a flat install order.

Usage:
    resolver = DependencyResolver(install_func, apps_dir, buckets_dir)
    order = resolver.resolve("mysqlworkbench")
    # Returns: ["vcredist2022", "mysqlworkbench"]
"""

import os
from typing import Callable, List, Optional, Set

from myscoop.manifest import Manifest, ManifestNotFoundError


class CircularDependencyError(Exception):
    """Raised when a circular dependency chain is detected."""
    pass


class DependencyResolver:
    """
    Resolves the dependency tree for an app and returns a flat
    install order (dependencies first, app last).
    
    Uses a 'visiting' set for cycle detection — if we encounter
    an app that is currently being visited, we have a cycle.
    """

    def __init__(
        self,
        apps_dir: str,
        buckets_dir: str,
    ) -> None:
        """
        Args:
            apps_dir:    Root apps directory to check installed status.
            buckets_dir: Buckets directory to load manifests.
        """
        self.apps_dir: str = apps_dir
        self.buckets_dir: str = buckets_dir

    def resolve(self, app_name: str) -> List[str]:
        """
        Resolve the full dependency tree for an app.

        Returns a flat ordered list: dependencies first, app last.
        Already-installed dependencies are excluded.

        Args:
            app_name: Name of the app to resolve dependencies for.

        Returns:
            Ordered list of app names to install.

        Raises:
            CircularDependencyError: If a cycle is detected.
            ManifestNotFoundError: If a dependency manifest is missing.
        """
        order: List[str] = []
        visited: Set[str] = set()       # Fully processed apps
        visiting: Set[str] = set()      # Currently being resolved (cycle detection)
        chain: List[str] = []           # Current resolution chain (for error messages)

        self._resolve_recursive(app_name, order, visited, visiting, chain)
        return order

    def is_installed(self, app_name: str) -> bool:
        """
        Check if an app is already installed.

        An app is considered installed if its directory exists under apps_dir
        and contains at least one version subfolder.

        Args:
            app_name: Name of the app to check.

        Returns:
            True if the app directory exists.
        """
        app_path = os.path.join(self.apps_dir, app_name.lower())
        if not os.path.exists(app_path):
            return False
        # Check that it has at least one version directory with files
        try:
            entries = os.listdir(app_path)
            return len(entries) > 0
        except OSError:
            return False

    def get_installed_version(self, app_name: str) -> Optional[str]:
        """
        Get the installed version of an app by looking at version subdirs.

        Returns:
            Version string or None if not installed.
        """
        app_path = os.path.join(self.apps_dir, app_name.lower())
        if not os.path.exists(app_path):
            return None
        try:
            versions = [
                d for d in os.listdir(app_path)
                if os.path.isdir(os.path.join(app_path, d))
            ]
            if versions:
                return sorted(versions)[-1]  # Return latest version
        except OSError:
            pass
        return None

    def get_dependency_tree(self, app_name: str, indent: int = 0) -> str:
        """
        Build a visual dependency tree string for display.

        Args:
            app_name: Root app name.
            indent:   Current indentation level.

        Returns:
            Multi-line string showing the dependency tree.
        """
        lines: List[str] = []
        self._build_tree_string(app_name, lines, indent, set())
        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    def _resolve_recursive(
        self,
        app_name: str,
        order: List[str],
        visited: Set[str],
        visiting: Set[str],
        chain: List[str],
    ) -> None:
        """
        Recursively resolve dependencies using DFS with cycle detection.

        Args:
            app_name: Current app being resolved.
            order:    Accumulating install order list.
            visited:  Set of fully resolved apps.
            visiting: Set of apps currently being resolved (for cycles).
            chain:    Current dependency chain (for error messages).
        """
        name = app_name.lower()

        # Already fully processed
        if name in visited:
            return

        # Cycle detection: if we're already visiting this node,
        # we have a circular dependency
        if name in visiting:
            cycle_str = " → ".join(chain + [name])
            raise CircularDependencyError(
                f"Circular dependency detected: {cycle_str}"
            )

        # Mark as currently being visited
        visiting.add(name)
        chain.append(name)

        # Load manifest to get dependencies
        try:
            manifest = Manifest(name, self.buckets_dir)
        except ManifestNotFoundError:
            raise ManifestNotFoundError(
                f"Dependency '{name}' not found in any bucket.\n"
                f"  Required by: {' → '.join(chain[:-1]) if len(chain) > 1 else 'user'}\n"
                f"  Suggested fix: Add a bucket that contains '{name}'"
            )

        # Recursively resolve each dependency first
        for dep in manifest.depends:
            self._resolve_recursive(dep, order, visited, visiting, chain)

        # Done visiting this node
        visiting.remove(name)
        chain.pop()
        visited.add(name)

        # Add to install order (dependencies are already added before this)
        if name not in order:
            order.append(name)

    def _build_tree_string(
        self,
        app_name: str,
        lines: List[str],
        indent: int,
        seen: Set[str],
    ) -> None:
        """Build a visual tree representation recursively."""
        name = app_name.lower()
        prefix = "  " * indent + ("├── " if indent > 0 else "")
        installed = " [installed]" if self.is_installed(name) else ""

        if name in seen:
            lines.append(f"{prefix}{name} (circular ref){installed}")
            return

        seen.add(name)
        lines.append(f"{prefix}{name}{installed}")

        try:
            manifest = Manifest(name, self.buckets_dir)
            for dep in manifest.depends:
                self._build_tree_string(dep, lines, indent + 1, seen)
        except ManifestNotFoundError:
            lines.append(f"{'  ' * (indent + 1)}├── [manifest not found]")
