import os
import shutil
import unittest

from myscoop.silent_installer import SilentInstaller, _InteractiveInstallerWindow


class RecordingSilentInstaller(SilentInstaller):
    def __init__(
        self,
        apps_dir: str,
        detected_type=None,
        gui_available=True,
        interactive_silent=False,
        extractable_setup=False,
    ) -> None:
        super().__init__(apps_dir)
        self.detected_type = detected_type
        self.gui_available = gui_available
        self.interactive_silent = interactive_silent
        self.extractable_setup = extractable_setup
        self.calls = []

    def detect_installer_type(self, filepath: str):
        return self.detected_type

    def _looks_like_extractable_setup(self, filepath: str) -> bool:
        return self.extractable_setup

    def _strategy_7z(self, filepath: str, app_dir: str) -> None:
        self.calls.append("7z")

    def _strategy_msi(self, filepath: str, app_dir: str) -> None:
        self.calls.append("msi")

    def _strategy_nsis(self, filepath: str, app_dir: str) -> None:
        self.calls.append("nsis")
        if self.interactive_silent:
            raise _InteractiveInstallerWindow(
                "NSIS installer ignored silent flags and opened interactive UI"
            )
        raise RuntimeError("nsis failed")

    def _strategy_inno(self, filepath: str, app_dir: str) -> None:
        self.calls.append("inno")
        raise RuntimeError("inno failed")

    def _strategy_gui(self, filepath: str, app_dir: str) -> None:
        self.calls.append("gui")


class SilentInstallerStrategyTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.join(os.getcwd(), "testdata_silent_installer")
        shutil.rmtree(self.test_dir, ignore_errors=True)
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_file(self, name: str) -> str:
        path = os.path.join(self.test_dir, name)
        with open(path, "wb") as fh:
            fh.write(b"test")
        return path

    def test_exe_does_not_extract_before_silent_or_gui(self):
        installer_path = self._make_file("setup.exe")
        app_dir = os.path.join(self.test_dir, "app")
        installer = RecordingSilentInstaller(self.test_dir, detected_type=None)

        installer.install(installer_path, app_dir)

        self.assertEqual(installer.calls, ["nsis", "inno", "gui"])

    def test_archive_payload_uses_extraction(self):
        installer_path = self._make_file("package.7z")
        app_dir = os.path.join(self.test_dir, "app")
        installer = RecordingSilentInstaller(self.test_dir)

        installer.install(installer_path, app_dir)

        self.assertEqual(installer.calls, ["7z"])

    def test_interactive_nsis_falls_through_to_gui(self):
        installer_path = self._make_file("bootstrapper.exe")
        app_dir = os.path.join(self.test_dir, "app")
        installer = RecordingSilentInstaller(
            self.test_dir,
            detected_type=None,
            interactive_silent=True,
        )

        installer.install(installer_path, app_dir)

        self.assertEqual(installer.calls, ["nsis", "gui"])

    def test_extractable_exe_uses_archive_before_gui(self):
        installer_path = self._make_file("MAPSetup.exe")
        app_dir = os.path.join(self.test_dir, "app")
        installer = RecordingSilentInstaller(
            self.test_dir,
            detected_type=None,
            extractable_setup=True,
        )

        installer.install(installer_path, app_dir)

        self.assertEqual(installer.calls, ["nsis", "inno", "7z"])

    def test_explicit_gui_installer_skips_silent_flags(self):
        installer_path = self._make_file("AtmoControlSetupV2.11.exe")
        app_dir = os.path.join(self.test_dir, "app")
        installer = RecordingSilentInstaller(self.test_dir)

        installer.install(installer_path, app_dir, installer_type="gui")

        self.assertEqual(installer.calls, ["gui"])

    def test_child_setup_title_matches_installer_file(self):
        installer = RecordingSilentInstaller(self.test_dir)
        installer_path = os.path.join(self.test_dir, "Integriti_(24.0.0.21780).exe")

        self.assertTrue(
            installer._title_matches_installer_file(
                "Setup - Integriti Pro version 24.0.0.21780",
                installer_path,
            )
        )


if __name__ == "__main__":
    unittest.main()
