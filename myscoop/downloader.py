"""
downloader.py — File Downloader for myscoop

Downloads app installers/archives from URLs with a tqdm progress bar.
Caches downloads so repeated installs don't re-download.

Usage:
    dl = Downloader("C:/Users/you/myscoop/cache")
    filepath = dl.download("https://example.com/app.zip", "myapp", "1.0.0")
"""

import os
import sys
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:  # pragma: no cover - exercised on minimal test envs
    requests = None

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - exercised on minimal test envs
    tqdm = None


class DownloadError(Exception):
    """Raised when a download fails."""
    pass


class Downloader:
    """
    Downloads files from URLs into a local cache directory.
    
    Files are cached as: cache_dir/<app_name>-<version>-<filename>
    If the file already exists in cache, the download is skipped.
    """

    def __init__(self, cache_dir: str) -> None:
        """
        Args:
            cache_dir: Absolute path to the cache directory.
        """
        self.cache_dir: str = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def download(self, url: str, app_name: str, version: str) -> str:
        """
        Download a file from url into the cache directory.

        Args:
            url:       Direct download URL (hint fragment already stripped).
            app_name:  Name of the app (used in cache filename).
            version:   Version string (used in cache filename).

        Returns:
            Absolute path to the downloaded (or cached) file.

        Raises:
            DownloadError: If the download fails for any reason.
        """
        # Build a cache-friendly filename: appname-version-originalfilename
        original_filename = self._extract_filename(url)
        cache_filename = f"{app_name}-{version}-{original_filename}"
        filepath = os.path.join(self.cache_dir, cache_filename)

        # Skip download if already cached
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            print(f"  Using cached: {cache_filename}")
            return filepath

        # Download with progress bar
        if requests is None:
            raise DownloadError(
                "The 'requests' package is required for downloads. "
                "Install project dependencies with: pip install -r requirements.txt"
            )
        if tqdm is None:
            raise DownloadError(
                "The 'tqdm' package is required for download progress. "
                "Install project dependencies with: pip install -r requirements.txt"
            )

        try:
            print(f"  Downloading {original_filename} ...")
            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise DownloadError(
                "Cannot connect. Check your internet connection."
            )
        except requests.exceptions.Timeout:
            raise DownloadError(
                "Download timed out. Check your internet connection and retry."
            )
        except requests.exceptions.HTTPError as e:
            raise DownloadError(
                f"Download failed (HTTP {response.status_code}): {url}\n"
                f"  {e}"
            )
        except requests.exceptions.RequestException as e:
            raise DownloadError(f"Download failed: {e}")

        # Get total file size for progress bar (may be unknown)
        total_size = int(response.headers.get("content-length", 0))

        # Write to a temp file first, rename on success (atomic-ish)
        temp_filepath = filepath + ".downloading"
        try:
            with open(temp_filepath, "wb") as f:
                with tqdm(
                    total=total_size if total_size > 0 else None,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=f"  {original_filename}",
                    ncols=80,
                    file=sys.stdout,
                ) as pbar:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))

            # Rename temp file to final name
            if os.path.exists(filepath):
                os.remove(filepath)
            os.rename(temp_filepath, filepath)

        except (IOError, OSError) as e:
            # Clean up temp file on failure
            if os.path.exists(temp_filepath):
                os.remove(temp_filepath)
            raise DownloadError(f"Failed to save file: {e}")

        downloaded_size = os.path.getsize(filepath)
        print(f"  Downloaded: {self._human_size(downloaded_size)}")
        return filepath

    def remove_cached(self, app_name: str) -> bool:
        """
        Remove all cached files for a given app.

        Args:
            app_name: Name of the app whose cache to clear.

        Returns:
            True if any files were removed, False if nothing was cached.
        """
        removed = False
        for filename in os.listdir(self.cache_dir):
            if filename.startswith(f"{app_name}-"):
                os.remove(os.path.join(self.cache_dir, filename))
                print(f"  Removed cached: {filename}")
                removed = True
        return removed

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _extract_filename(url: str) -> str:
        """Extract the filename from a URL path, URL-decoded."""
        from urllib.parse import unquote, urlparse
        path = urlparse(url).path
        filename = unquote(os.path.basename(path))
        # Fallback if URL has no clear filename
        return filename if filename else "download"

    @staticmethod
    def _human_size(size_bytes: int) -> str:
        """Convert bytes to human-readable string (e.g. '245.1 MB')."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"
