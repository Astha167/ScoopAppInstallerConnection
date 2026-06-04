import unittest
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from myscoop.cli import (
    _resolve_embedded_installer_path,
    _run_embedded_installer_if_present,
    _score_embedded_installer,
)


class EmbeddedInstallerSelectionTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_cli_embedded_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_file(self, relative_path: str) -> str:
        path = os.path.join(self.test_dir, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"test")
        return path

    def test_client_setup_scores_higher_than_server_setup_inside_zip(self):
        client = _score_embedded_installer(Path("Integriti_ClientOnly_Setup_24.0.exe"))
        server = _score_embedded_installer(Path("Integriti_Server_Setup_24.0.exe"))

        self.assertGreater(client, server)

    def test_update_and_removal_helpers_are_ignored(self):
        self.assertLess(_score_embedded_installer(Path("Update.exe")), 0)
        self.assertLess(_score_embedded_installer(Path("uninstall.exe")), 0)
        self.assertGreater(_score_embedded_installer(Path("ProductSetup.msi")), 0)

    def test_explicit_gui_exe_resolves_after_archive_flattening(self):
        setup_path = self._make_file("Installer.exe")

        resolved = _resolve_embedded_installer_path(
            self.test_dir,
            "payload\\Installer.exe",
        )

        self.assertEqual(resolved, setup_path)

    @patch("myscoop.cli.SilentInstaller")
    def test_explicit_gui_exe_runs_as_gui_strategy(self, mock_silent_installer):
        setup_path = self._make_file("payload\\Installer.exe")
        instance = mock_silent_installer.return_value
        instance.install.return_value = True

        self.assertTrue(
            _run_embedded_installer_if_present(
                self.test_dir,
                explicit_gui_exe="payload\\Installer.exe",
            )
        )

        instance.install.assert_called_once_with(
            filepath=setup_path,
            app_dir=self.test_dir,
            installer_type="gui",
        )


if __name__ == "__main__":
    unittest.main()
