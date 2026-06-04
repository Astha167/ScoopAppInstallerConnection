import os
import shutil
import tempfile
import unittest
import zipfile

from myscoop.extractor import ExtractionError, Extractor


class ExtractorZipTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_extractor_")
        self.apps_dir = os.path.join(self.test_dir, "apps")
        self.extractor = Extractor(self.apps_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_zip_extraction_blocks_path_traversal_entries(self):
        zip_path = os.path.join(self.test_dir, "bad.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../outside.txt", "nope")

        with self.assertRaises(ExtractionError):
            self.extractor.extract(zip_path, "demo", "1.0")

        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "outside.txt")))


if __name__ == "__main__":
    unittest.main()
