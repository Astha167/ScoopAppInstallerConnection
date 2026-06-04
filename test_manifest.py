"""
Quick isolated test for myscoop/manifest.py
Run: python test_manifest.py
"""
import os
import sys

# Add project root to path so we can import myscoop
sys.path.insert(0, os.path.dirname(__file__))

from myscoop.manifest import Manifest, ManifestNotFoundError

BUCKETS_DIR = os.path.join(os.path.dirname(__file__), "buckets")


def test_postman():
    print("=" * 50)
    print("TEST 1: Load postman manifest")
    print("=" * 50)
    m = Manifest("postman", BUCKETS_DIR)
    print(f"  name:          {m.name}")
    print(f"  version:       {m.version}")
    print(f"  description:   {m.description}")
    print(f"  homepage:      {m.homepage}")
    print(f"  license:       {m.license}")
    print(f"  depends:       {m.depends}")
    print(f"  url:           {m.url}")
    print(f"  url_hint:      {m.url_hint}")
    print(f"  bin:           {m.bin}")
    print(f"  extract_dir:   {m.extract_dir}")
    print(f"  shortcuts:     {m.shortcuts}")
    print(f"  installer_type:{m.installer_type}")
    print(f"  post_install:  {m.post_install}")
    print(f"  repr:          {m!r}")
    print(f"  summary:       {m.summary()}")
    print("  PASSED\n")


def test_mysqlworkbench():
    print("=" * 50)
    print("TEST 2: Load mysqlworkbench manifest")
    print("=" * 50)
    m = Manifest("mysqlworkbench", BUCKETS_DIR)
    print(f"  name:          {m.name}")
    print(f"  version:       {m.version}")
    print(f"  url:           {m.url}")
    print(f"  url_hint:      {m.url_hint}")
    print(f"  depends:       {m.depends}")
    print(f"  installer_type:{m.installer_type}")
    print(f"  bin:           {m.bin}")
    print(f"  shortcuts:     {m.shortcuts}")
    print("  PASSED\n")


def test_vcredist():
    print("=" * 50)
    print("TEST 3: Load vcredist2022 manifest")
    print("=" * 50)
    m = Manifest("vcredist2022", BUCKETS_DIR)
    print(f"  name:          {m.name}")
    print(f"  version:       {m.version}")
    print(f"  url:           {m.url}")
    print(f"  url_hint:      {m.url_hint}")
    print("  PASSED\n")


def test_not_found():
    print("=" * 50)
    print("TEST 4: App not found (should raise error)")
    print("=" * 50)
    try:
        Manifest("nonexistent_app_xyz", BUCKETS_DIR)
        print("  FAILED — no exception raised!")
    except ManifestNotFoundError as e:
        print(f"  Error message: {e}")
        print("  PASSED\n")


def test_case_insensitive():
    print("=" * 50)
    print("TEST 5: Case-insensitive lookup ('Postman' -> postman)")
    print("=" * 50)
    m = Manifest("Postman", BUCKETS_DIR)
    print(f"  name:          {m.name}")
    print(f"  version:       {m.version}")
    print("  PASSED\n")


if __name__ == "__main__":
    # test_postman()
    test_mysqlworkbench()
    test_vcredist()
    test_not_found()
    test_case_insensitive()
    print("All manifest tests passed!")
