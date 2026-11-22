import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.release_metadata import read_metadata


class ReleaseMetadataTests(unittest.TestCase):
    def parse(self, **changes):
        data = {"version": "v11", "draft": False, "prerelease": False, "notes": "Release notes"}
        data.update(changes)
        with TemporaryDirectory() as directory:
            file = Path(directory) / "release.json"
            file.write_text(json.dumps(data), encoding="utf-8")
            return read_metadata(file)

    def test_uses_one_public_version(self):
        self.assertEqual(self.parse()["version"], "v11")

    def test_rejects_non_numbered_release_tags(self):
        for value in ("0.1.11", "v0.1.11", "v0", "v011", "11"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Invalid release tag"):
                self.parse(version=value)

    def test_accepts_a_pinned_legacy_archive_replacement(self):
        replacement = {"previous_commit": "a" * 40, "previous_asset": "open-svpflow-0.1.11-arm64.zip"}
        self.assertEqual(self.parse(replacement=replacement)["replacement"], replacement)
        with self.assertRaisesRegex(ValueError, "Invalid release replacement"):
            self.parse(draft=True, replacement=replacement)

    def test_rejects_an_unpinned_or_unrelated_archive_replacement(self):
        replacements = (
            {"previous_asset": "open-svpflow-0.1.11-arm64.zip"},
            {"previous_commit": "main", "previous_asset": "open-svpflow-0.1.11-arm64.zip"},
            {"previous_commit": "a" * 40, "previous_asset": "../unrelated.zip"},
            None,
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, "Invalid release replacement"):
                self.parse(replacement=replacement)
