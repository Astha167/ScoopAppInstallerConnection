"""
bucket.py — Bucket Manager for myscoop

Manages bucket repositories (Git repos containing JSON manifests).
Supports adding, removing, listing, searching, and updating buckets.

Usage:
    bm = BucketManager("C:/Users/you/myscoop/buckets")
    bm.add_bucket("extras", "https://github.com/ScoopInstaller/Extras")
    results = bm.search("mysql")
"""

import json
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

try:
    from git import Repo, GitCommandError
except ImportError:
    Repo = None
    GitCommandError = Exception


class BucketError(Exception):
    """Raised when bucket operations fail."""
    pass


class BucketManager:
    """
    Manages myscoop buckets — directories of JSON app manifests.
    
    Buckets can be local directories or Git repositories. Git-based
    buckets can be updated via 'git pull'.
    """

    def __init__(self, buckets_dir: str) -> None:
        """
        Args:
            buckets_dir: Root directory containing all buckets.
        """
        self.buckets_dir: str = buckets_dir
        os.makedirs(buckets_dir, exist_ok=True)

    def add_bucket(self, name: str, url: str) -> str:
        """
        Add a new bucket by cloning a Git repository.

        Args:
            name: Name for the bucket (e.g. "extras").
            url:  Git repository URL to clone.

        Returns:
            Path to the created bucket directory.

        Raises:
            BucketError: If cloning fails or bucket already exists.
        """
        if Repo is None:
            raise BucketError(
                "gitpython is not installed. Run: pip install gitpython"
            )

        bucket_path = os.path.join(self.buckets_dir, name)

        if os.path.exists(bucket_path):
            raise BucketError(
                f"Bucket '{name}' already exists at: {bucket_path}\n"
                f"  To update it, run: myscoop bucket update"
            )

        try:
            print(f"  Cloning bucket '{name}' from {url} ...")
            Repo.clone_from(url, bucket_path, depth=1)  # Shallow clone for speed
            print(f"  Bucket '{name}' added successfully")
            return bucket_path
        except GitCommandError as e:
            # Clean up partial clone
            if os.path.exists(bucket_path):
                shutil.rmtree(bucket_path, ignore_errors=True)
            raise BucketError(
                f"Failed to clone bucket '{name}': {e}\n"
                f"  Check that the URL is correct and you have internet."
            )

    def remove_bucket(self, name: str) -> bool:
        """
        Remove a bucket by deleting its directory.

        Args:
            name: Bucket name to remove.

        Returns:
            True if removed.

        Raises:
            BucketError: If bucket doesn't exist.
        """
        bucket_path = os.path.join(self.buckets_dir, name)

        if not os.path.exists(bucket_path):
            raise BucketError(f"Bucket '{name}' not found.")

        shutil.rmtree(bucket_path, ignore_errors=True)
        print(f"  Bucket '{name}' removed")
        return True

    def list_buckets(self) -> List[Dict[str, str]]:
        """
        List all installed buckets.

        Returns:
            List of dicts with 'name', 'path', and 'manifests' count.
        """
        buckets = []
        for entry in sorted(os.listdir(self.buckets_dir)):
            entry_path = os.path.join(self.buckets_dir, entry)
            if os.path.isdir(entry_path):
                manifest_count = self._count_manifests(entry_path)
                buckets.append({
                    "name": entry,
                    "path": entry_path,
                    "manifests": str(manifest_count),
                })
        return buckets

    def search(self, query: str) -> List[Dict[str, str]]:
        """
        Search all buckets for apps matching the query.
        Matches against app name and description (case-insensitive).

        Args:
            query: Search term.

        Returns:
            List of dicts with 'name', 'version', 'description', 'bucket'.
        """
        query_lower = query.lower()
        results = []

        for bucket_name, bucket_path in self._iter_bucket_dirs():
            manifest_dirs = self._get_manifest_dirs(bucket_path)
            for manifest_dir in manifest_dirs:
                for filename in os.listdir(manifest_dir):
                    if not filename.endswith(".json"):
                        continue
                    app_name = filename[:-5]  # Strip .json

                    # Quick name match first (cheap)
                    name_match = query_lower in app_name.lower()

                    # Load manifest for description match
                    filepath = os.path.join(manifest_dir, filename)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        description = data.get("description", "")
                        desc_match = query_lower in description.lower()

                        if name_match or desc_match:
                            results.append({
                                "name": app_name,
                                "version": data.get("version", "?"),
                                "description": description,
                                "bucket": bucket_name,
                            })
                    except (json.JSONDecodeError, IOError):
                        # Skip invalid manifests
                        if name_match:
                            results.append({
                                "name": app_name,
                                "version": "?",
                                "description": "[invalid manifest]",
                                "bucket": bucket_name,
                            })

        return results

    def update_buckets(self) -> int:
        """
        Git pull on all bucket repos to get latest manifests.

        Returns:
            Number of buckets updated.
        """
        if Repo is None:
            raise BucketError(
                "gitpython is not installed. Run: pip install gitpython"
            )

        updated = 0
        for entry in sorted(os.listdir(self.buckets_dir)):
            entry_path = os.path.join(self.buckets_dir, entry)
            if not os.path.isdir(entry_path):
                continue

            # Check if it's a git repo
            git_dir = os.path.join(entry_path, ".git")
            if not os.path.exists(git_dir):
                continue

            try:
                print(f"  Updating bucket '{entry}' ...")
                repo = Repo(entry_path)
                origin = repo.remotes.origin
                origin.pull()
                updated += 1
                print(f"  Bucket '{entry}' updated")
            except GitCommandError as e:
                print(f"  Warning: Failed to update '{entry}': {e}")
            except Exception as e:
                print(f"  Warning: Error updating '{entry}': {e}")

        return updated

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _iter_bucket_dirs(self):
        """Yield (name, path) for each bucket directory."""
        for entry in sorted(os.listdir(self.buckets_dir)):
            entry_path = os.path.join(self.buckets_dir, entry)
            if os.path.isdir(entry_path):
                yield entry, entry_path

    def _get_manifest_dirs(self, bucket_path: str) -> List[str]:
        """
        Get the directories that may contain manifests.
        Checks both the bucket root and a 'bucket' subdirectory
        (common structure in Scoop bucket repos).
        """
        dirs = [bucket_path]
        sub_bucket = os.path.join(bucket_path, "bucket")
        if os.path.isdir(sub_bucket):
            dirs.append(sub_bucket)
        return dirs

    def _count_manifests(self, bucket_path: str) -> int:
        """Count the number of .json manifest files in a bucket."""
        count = 0
        for manifest_dir in self._get_manifest_dirs(bucket_path):
            try:
                count += sum(
                    1 for f in os.listdir(manifest_dir) if f.endswith(".json")
                )
            except OSError:
                pass
        return count
