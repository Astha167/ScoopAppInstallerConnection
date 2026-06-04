import json
import os
import shutil
import unittest

from myscoop.local_manifest import LocalManifestManager


class LocalManifestManagerTests(unittest.TestCase):
    def test_normalize_app_name_strips_sfx_suffix(self):
        name = LocalManifestManager.normalize_app_name(
            "autocad_plant_3d_2022_object_enabler_english_win_64bit_dlm.sfx.exe"
        )
        self.assertEqual(
            name,
            "autocad-plant-3d-2022-object-enabler-english-win-64bit-dlm",
        )

    def test_extract_version_from_year_style_filename(self):
        version = LocalManifestManager.extract_version_from_text(
            "autocad_plant_3d_2022_object_enabler_english_win_64bit_dlm.sfx.exe"
        )
        self.assertEqual(version, "2022")

    def test_ensure_manifest_creates_main_bucket_manifest_for_local_file(self):
        temp_dir = os.path.join(os.getcwd(), "testdata_local_manifest_create")
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            os.makedirs(temp_dir, exist_ok=True)
            buckets_dir = os.path.join(temp_dir, "buckets")
            os.makedirs(buckets_dir, exist_ok=True)

            installer_path = os.path.join(
                temp_dir,
                "ABB_Automation_Builder_V2.9.0_322_x64.exe",
            )
            with open(installer_path, "wb") as fh:
                fh.write(b"fake-exe")

            manager = LocalManifestManager(buckets_dir)
            app_name = manager.ensure_manifest(installer_path)
            manifest_path = os.path.join(buckets_dir, "main", f"{app_name}.json")

            self.assertTrue(os.path.isfile(manifest_path))

            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)

            self.assertEqual(app_name, "abb-automation-builder-v2-9-0-322-x64")
            self.assertNotIn("installer", manifest)
            self.assertTrue(
                manifest["architecture"]["64bit"]["url"].startswith("file:///")
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_ensure_manifest_reuses_existing_manifest_by_url_filename(self):
        temp_dir = os.path.join(os.getcwd(), "testdata_local_manifest_match")
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            os.makedirs(os.path.join(temp_dir, "buckets", "main"), exist_ok=True)

            existing_manifest_path = os.path.join(
                temp_dir, "buckets", "main", "abb-automation-builder.json"
            )
            with open(existing_manifest_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "version": "2.9.0.322",
                        "architecture": {
                            "64bit": {
                                "url": "https://example.com/ABB_Automation_Builder_V2.9.0_322_x64.exe"
                            }
                        },
                    },
                    fh,
                    indent=4,
                )

            installer_path = os.path.join(
                temp_dir, "ABB_Automation_Builder_V2.9.0_322_x64.exe"
            )
            with open(installer_path, "wb") as fh:
                fh.write(b"fake-exe")

            manager = LocalManifestManager(os.path.join(temp_dir, "buckets"))
            app_name = manager.ensure_manifest(installer_path)

            self.assertEqual(app_name, "abb-automation-builder")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
