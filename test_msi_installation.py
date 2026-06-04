import os
import shutil
import unittest
import subprocess
from unittest.mock import patch, MagicMock

from myscoop.silent_installer import SilentInstaller, SilentInstallError
from myscoop.local_manifest import LocalManifestManager


class MsiInstallationTests(unittest.TestCase):
    def setUp(self):
        # Create a temp directory for test apps_dir
        self.test_dir = os.path.join(os.getcwd(), "testdata_msi_installation")
        shutil.rmtree(self.test_dir, ignore_errors=True)
        os.makedirs(self.test_dir, exist_ok=True)
        self.apps_dir = os.path.join(self.test_dir, "apps")
        os.makedirs(self.apps_dir, exist_ok=True)
        self.installer = SilentInstaller(self.apps_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @patch("subprocess.run")
    @patch("myscoop.silent_installer.SilentInstaller._validate_msi_installation")
    def test_successful_msi_install(self, mock_validate, mock_run):
        # Setup mocks
        mock_validate.return_value = True
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Installation successful"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        # Run strategy
        filepath = os.path.join(self.test_dir, "setup.msi")
        dest_dir = os.path.join(self.test_dir, "dummy_app")
        success = self.installer._strategy_msi(filepath, dest_dir, ["/l*v", "install.log"])

        self.assertTrue(success)
        mock_run.assert_called_once()
        called_args = mock_run.call_args[0][0]
        self.assertEqual(called_args[0], "msiexec")
        self.assertEqual(called_args[1], "/i")
        self.assertTrue(called_args[2].endswith("setup.msi"))
        self.assertEqual(called_args[3], "/qn")
        self.assertEqual(called_args[4], "/norestart")
        self.assertIn(f"TARGETDIR={os.path.abspath(dest_dir)}", called_args)
        self.assertIn(f"INSTALLDIR={os.path.abspath(dest_dir)}", called_args)
        self.assertIn(f"APPDIR={os.path.abspath(dest_dir)}", called_args)
        self.assertEqual(called_args[-2], "/l*v")
        self.assertEqual(called_args[-1], "install.log")

    @patch("subprocess.run")
    @patch("myscoop.silent_installer.SilentInstaller._validate_msi_installation")
    def test_msi_custom_arguments_passthrough(self, mock_validate, mock_run):
        # Setup mocks
        mock_validate.return_value = True
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        # Run strategy
        filepath = os.path.join(self.test_dir, "setup.msi")
        dest_dir = os.path.join(self.test_dir, "dummy_app")
        custom_args = ["ADDLOCAL=ALL", "INSTALLLEVEL=3"]
        success = self.installer._strategy_msi(filepath, dest_dir, custom_args)

        self.assertTrue(success)
        mock_run.assert_called_once()
        called_args = mock_run.call_args[0][0]
        
        self.assertEqual(called_args[0], "msiexec")
        self.assertEqual(called_args[1], "/i")
        self.assertTrue(called_args[2].endswith("setup.msi"))
        self.assertEqual(called_args[3], "/qn")
        self.assertEqual(called_args[4], "/norestart")
        self.assertIn(f"TARGETDIR={os.path.abspath(dest_dir)}", called_args)
        self.assertIn(f"INSTALLDIR={os.path.abspath(dest_dir)}", called_args)
        self.assertIn(f"APPDIR={os.path.abspath(dest_dir)}", called_args)
        self.assertEqual(called_args[-2], "ADDLOCAL=ALL")
        self.assertEqual(called_args[-1], "INSTALLLEVEL=3")

    @patch("subprocess.run")
    @patch("myscoop.silent_installer.SilentInstaller._strategy_gui")
    @patch("myscoop.silent_installer.SilentInstaller._validate_msi_installation")
    def test_msi_failure_falls_back_to_gui(self, mock_validate, mock_gui, mock_run):
        # Setup mock for install failure (exit code 1603)
        mock_result = MagicMock()
        mock_result.returncode = 1603
        mock_result.stdout = ""
        mock_result.stderr = "Fatal error during installation."
        mock_run.return_value = mock_result

        filepath = os.path.join(self.test_dir, "setup.msi")
        app_dir = os.path.join(self.test_dir, "dummy_app")
        
        # Calling install should trigger fallback
        self.installer.install(filepath, app_dir, installer_type="msi")
        
        # Check that GUI fallback was called because silent install failed
        mock_gui.assert_called_once_with(filepath, app_dir)

    @patch("subprocess.run")
    @patch("myscoop.silent_installer.SilentInstaller._strategy_gui")
    @patch("myscoop.silent_installer.SilentInstaller._validate_msi_installation")
    def test_validation_failure_falls_back_to_gui(self, mock_validate, mock_gui, mock_run):
        # Setup mock for successful process but failed validation
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        mock_validate.side_effect = [False, True]

        filepath = os.path.join(self.test_dir, "setup.msi")
        app_dir = os.path.join(self.test_dir, "dummy_app")
        
        self.installer.install(filepath, app_dir, installer_type="msi")
        
        # Check that GUI fallback was called because validation failed
        mock_gui.assert_called_once_with(filepath, app_dir)

    @patch("subprocess.run")
    def test_msi_timeout_handling(self, mock_run):
        # Mock timeout expiration
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["msiexec"], timeout=600, output="partial stdout", stderr="partial stderr"
        )

        filepath = os.path.join(self.test_dir, "setup.msi")
        dest_dir = os.path.join(self.test_dir, "dummy_app")
        success = self.installer._strategy_msi(filepath, dest_dir)
        self.assertFalse(success)

    @patch("myscoop.silent_installer.SilentInstaller._get_msi_properties_com")
    @patch("myscoop.metadata.iter_installed_app_registry_entries")
    def test_discover_msi_install_directory_registry(self, mock_iter, mock_get_props):
        mock_get_props.return_value = {"ProductName": "TestApp Pro"}
        mock_iter.return_value = [
            {"DisplayName": "TestApp Pro", "InstallLocation": self.test_dir}
        ]
        # Create a file in test_dir so it counts as non-empty
        dummy_file = os.path.join(self.test_dir, "some_file.txt")
        with open(dummy_file, "w") as f:
            f.write("data")

        app_dir = os.path.join(self.apps_dir, "testapp", "1.0")
        discovered = self.installer._discover_msi_install_directory(app_dir, "dummy.msi")
        self.assertEqual(discovered, self.test_dir)

    @patch("myscoop.silent_installer.SilentInstaller._get_msi_properties_com")
    @patch("myscoop.metadata.resolve_shortcut_target")
    @patch("os.path.isdir")
    @patch("os.path.isfile")
    @patch("os.listdir")
    @patch("os.walk")
    def test_discover_msi_install_directory_shortcut(self, mock_walk, mock_listdir, mock_isfile, mock_isdir, mock_resolve, mock_get_props):
        mock_get_props.return_value = {"ProductName": "testapp"}
        # Setup shortcut path mocks
        mock_isdir.return_value = True
        mock_isfile.return_value = True
        mock_listdir.return_value = ["testapp.exe"]
        mock_walk.return_value = [
            ("some_dir", [], ["testapp.lnk"])
        ]
        dummy_exe = os.path.join(self.test_dir, "testapp.exe")
        mock_resolve.return_value = dummy_exe
        
        app_dir = os.path.join(self.apps_dir, "testapp", "1.0")
        discovered = self.installer._discover_msi_install_directory(app_dir, "dummy.msi")
        self.assertEqual(discovered, self.test_dir)

    @patch("myscoop.silent_installer.SilentInstaller._discover_msi_install_directory")
    @patch("os.walk")
    def test_validate_msi_installation_copies_files(self, mock_walk, mock_discover):
        # Setup mocks
        mock_discover.return_value = self.test_dir
        # Mock walk to return an executable file
        mock_walk.return_value = [
            (self.test_dir, [], ["testapp.exe"])
        ]
        # Create the source file
        dummy_exe = os.path.join(self.test_dir, "testapp.exe")
        with open(dummy_exe, "w") as f:
            f.write("binary content")

        app_dir = os.path.join(self.apps_dir, "testapp", "1.0")
        success = self.installer._validate_msi_installation(app_dir, "dummy.msi")
        
        self.assertTrue(success)
        # Check that file was copied to app_dir
        copied_file = os.path.join(app_dir, "testapp.exe")
        self.assertTrue(os.path.isfile(copied_file))

    def test_local_manifest_detects_msi(self):
        manager = LocalManifestManager(self.test_dir)
        # Create a dummy .msi file
        msi_path = os.path.join(self.test_dir, "test_app.msi")
        with open(msi_path, "wb") as f:
            f.write(b"MSI")

        # Mock AppMetadata.extract to return dummy metadata
        with patch("myscoop.metadata.AppMetadata.extract") as mock_extract:
            mock_extract.return_value = {
                "fileversion": "1.0.0",
                "filedescription": "Test App Description",
            }
            manifest_data = manager.build_manifest("test-app", msi_path)
            self.assertEqual(manifest_data["installer"]["type"], "msi")


if __name__ == "__main__":
    unittest.main()
