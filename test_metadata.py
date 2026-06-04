import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

from myscoop.metadata import (
    AppMetadata,
    ResolvedBinary,
    find_installed_executable,
    find_installed_executables,
    format_size,
)


class MetadataExecutableResolutionTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="testdata_metadata_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_exe(self, relative_path: str, size: int = 8) -> str:
        path = os.path.join(self.test_dir, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path

    def test_uses_manifest_bin_as_real_exe_path(self):
        exe_path = self._make_exe("bin\\Demo.exe")

        self.assertEqual(
            find_installed_executable(self.test_dir, bin_entries=["bin\\Demo.exe"]),
            os.path.normpath(exe_path),
        )

    def test_scans_for_app_exe_when_manifest_has_no_bin(self):
        installer_path = self._make_exe("Setup.exe", size=200)
        updater_path = self._make_exe("tools\\Updater.exe", size=300)
        app_path = self._make_exe("Demo.exe", size=100)

        self.assertTrue(Path(installer_path).exists())
        self.assertTrue(Path(updater_path).exists())
        self.assertEqual(
            find_installed_executable(self.test_dir),
            os.path.normpath(app_path),
        )

    def test_prefers_exact_app_name_over_console_prefixed_exe(self):
        console_path = self._make_exe("cfityk.exe", size=300)
        app_path = self._make_exe("fityk.exe", size=100)

        self.assertTrue(Path(console_path).exists())
        self.assertEqual(
            find_installed_executable(self.test_dir, app_name="fityk"),
            os.path.normpath(app_path),
        )

    def test_does_not_return_shortcut_path(self):
        shortcut_path = os.path.join(self.test_dir, "Demo.lnk")
        with open(shortcut_path, "wb") as fh:
            fh.write(b"not a real shortcut")

        self.assertIsNone(
            find_installed_executable(
                self.test_dir,
                shortcuts=[["Demo.lnk", "Demo"]],
            )
        )

    def test_returns_resolved_binary_type(self):
        exe_path = self._make_exe("bin\\Demo.exe")
        resolved = find_installed_executable(self.test_dir, bin_entries=["bin\\Demo.exe"])
        self.assertIsInstance(resolved, ResolvedBinary)
        self.assertEqual(resolved, os.path.normpath(exe_path))
        self.assertEqual(resolved.executable_path, os.path.normpath(exe_path))
        self.assertEqual(resolved.resolution_reason, "manifest_bin_resolution")
        self.assertTrue(resolved.confidence_score > 0)

    @patch("myscoop.metadata.resolve_shortcut_target")
    def test_shortcut_resolution_boost(self, mock_resolve):
        exe_path = self._make_exe("Demo.exe")
        lnk_path = os.path.join(self.test_dir, "Demo.lnk")
        with open(lnk_path, "wb") as fh:
            fh.write(b"shortcut")

        mock_resolve.return_value = os.path.normpath(exe_path)

        resolved = find_installed_executable(
            self.test_dir,
            shortcuts=[["Demo.lnk", "Demo"]]
        )
        self.assertEqual(resolved, os.path.normpath(exe_path))
        self.assertEqual(resolved.resolution_reason, "shortcut_target_resolution")

    @patch("myscoop.metadata.get_pe_metadata")
    def test_pe_metadata_intel_boost(self, mock_get_pe):
        exe_path = self._make_exe("Demo.exe")
        mock_get_pe.return_value = {
            "FileDescription": "Postman App",
            "ProductName": "Postman",
            "InternalName": "Postman",
            "OriginalFilename": "Postman.exe",
            "CompanyName": "Postman Inc",
            "FileVersion": "12.0",
        }

        resolved = find_installed_executable(self.test_dir, app_name="postman")
        self.assertEqual(resolved, os.path.normpath(exe_path))
        self.assertEqual(resolved.resolution_reason, "pe_metadata_validation")

    def test_portable_structure_boost(self):
        exe_path = self._make_exe("Demo.exe")
        # Create portable directory: resources folder beside executable
        os.makedirs(os.path.join(self.test_dir, "resources"), exist_ok=True)

        resolved = find_installed_executable(self.test_dir)
        self.assertEqual(resolved, os.path.normpath(exe_path))
        self.assertEqual(resolved.resolution_reason, "portable_app_structure")

    def test_wrapper_scripts_priority(self):
        bat_path = self._make_exe("Demo.bat", size=100)
        exe_path = self._make_exe("Demo.exe", size=100)

        resolved = find_installed_executable(self.test_dir, app_name="Demo")
        self.assertEqual(resolved, os.path.normpath(exe_path))

    def test_finds_multiple_app_binaries_after_multi_app_install(self):
        app_one = self._make_exe("Viewer.exe", size=100)
        app_two = self._make_exe("Tools\\Analyzer.exe", size=200)
        setup = self._make_exe("setup.exe", size=300)
        updater = self._make_exe("Updater.exe", size=400)

        self.assertTrue(Path(setup).exists())
        self.assertTrue(Path(updater).exists())

        resolved = find_installed_executables(self.test_dir)
        paths = {os.path.normpath(path) for path in resolved}

        self.assertIn(os.path.normpath(app_one), paths)
        self.assertIn(os.path.normpath(app_two), paths)
        self.assertNotIn(os.path.normpath(setup), paths)
        self.assertNotIn(os.path.normpath(updater), paths)

    def test_format_size(self):
        self.assertEqual(format_size(0), "0.00 B")
        self.assertEqual(format_size(512), "512.00 B")
        self.assertEqual(format_size(1024), "1.00 KB")
        self.assertEqual(format_size(1024 * 1024), "1.00 MB")
        self.assertEqual(format_size(15531008), "14.81 MB")

    @patch("myscoop.metadata.AppMetadata._get_version_info")
    @patch("myscoop.metadata.AppMetadata._compute_crc32")
    def test_metadata_extraction_fields(self, mock_crc, mock_ver):
        mock_crc.return_value = "ABC12345"
        mock_ver.side_effect = lambda field: f"Mock_{field}"

        exe_path = self._make_exe("Demo.exe", size=100)
        meta = AppMetadata(exe_path)
        data = meta.extract()

        # Legacy fields
        self.assertEqual(data["filename"], "Demo.exe")
        self.assertEqual(data["filepath"], os.path.normpath(exe_path))
        self.assertEqual(data["filesize"]["bytes"], 100)
        self.assertEqual(data["filesize"]["human"], "100 B")
        self.assertEqual(data["filecrc32"], "ABC12345")
        self.assertEqual(data["fileversion"], "Mock_FileVersion")

        # New fields
        self.assertEqual(data["file_size"], "100.00 B")
        self.assertEqual(data["file_size_bytes"], 100)
        self.assertEqual(data["file_size_human"], "100.00 B")
        self.assertEqual(data["file_description"], "Mock_FileDescription")
        self.assertEqual(data["product_name"], "Mock_ProductName")
        self.assertEqual(data["company_name"], "Mock_CompanyName")
        self.assertEqual(data["version"], "Mock_FileVersion")
        self.assertEqual(data["checksum"], "ABC12345")
        self.assertEqual(data["executable_path"], os.path.normpath(exe_path))


if __name__ == "__main__":
    unittest.main()
