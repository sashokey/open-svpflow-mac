import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.release_metadata import read_metadata


class ReleaseMetadataTests(unittest.TestCase):
    def parse(self, **changes):
        data = {
            "version": "v11",
            "package_version": "0.1.11",
            "draft": False,
            "prerelease": False,
            "notes": "Release notes",
        }
        data.update(changes)
        with TemporaryDirectory() as directory:
            file = Path(directory) / "release.json"
            file.write_text(json.dumps(data), encoding="utf-8")
            return read_metadata(file)

    def test_release_tag_and_package_version_are_independent(self):
        release = self.parse()
        self.assertEqual(release["version"], "v11")
        self.assertEqual(release["package_version"], "0.1.11")

    def test_rejects_non_numbered_release_tags(self):
        for value in ("0.1.11", "v0.1.11", "v0", "v011", "11"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Invalid release tag"):
                self.parse(version=value)

    def test_rejects_a_release_tag_as_the_package_version(self):
        with self.assertRaisesRegex(ValueError, "Invalid package version"):
            self.parse(package_version="v11")
