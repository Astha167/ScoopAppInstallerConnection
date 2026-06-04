import os
import shutil
import tempfile
import unittest

from myscoop.install_state import (
    get_valid_installed_versions,
    inspect_install_path,
    is_app_installed,
)


class InstallStateTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_install_state_")
        self.apps_dir = os.path.join(self.test_dir, "apps")
        os.makedirs(self.apps_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_file(self, relative_path: str, contents: bytes) -> str:
        path = os.path.join(self.apps_dir, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(contents)
        return path

    def test_metadata_only_folder_is_negligible_and_cleaned(self):
        self._write_file("demo/1.0/install.json", b"{}")
        self._write_file("demo/1.0/gui_install_info.json", b"{}")

        versions = get_valid_installed_versions(
            self.apps_dir,
            "demo",
            cleanup_invalid=True,
        )

        self.assertEqual(versions, [])
        self.assertFalse(os.path.exists(os.path.join(self.apps_dir, "demo")))

    def test_tiny_payload_does_not_count_as_installed(self):
        app_dir = os.path.join(self.apps_dir, "demo", "1.0")
        self._write_file("demo/1.0/demo.exe", b"tiny")

        state = inspect_install_path(app_dir)

        self.assertFalse(state.valid)
        self.assertFalse(is_app_installed(self.apps_dir, "demo"))

    def test_non_negligible_payload_counts_as_installed(self):
        self._write_file("demo/1.0/demo.exe", b"x" * 2048)

        self.assertTrue(is_app_installed(self.apps_dir, "demo"))
        self.assertEqual(
            get_valid_installed_versions(self.apps_dir, "demo"),
            ["1.0"],
        )


if __name__ == "__main__":
    unittest.main()
