import importlib.util
import json
import plistlib
import os
from pathlib import Path
import re
import sys
import tempfile
import subprocess
import unittest
import zipfile


root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_release", root / "scripts/package_release.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackageTests(unittest.TestCase):
    @unittest.skipUnless(Path("/usr/bin/plutil").is_file(), "macOS strings parser")
    def test_bundled_translations_match_settings_and_keep_format_arguments(self):
        sys.path.insert(0, str(root / "player"))
        try:
            from settings_api import describe_settings
        finally:
            sys.path.pop(0)
        description = describe_settings()
        labels = set(description["limitations"])
        for field in description["fields"]:
            labels.add(field["label"])
            labels.update(option["label"] for option in field.get("options", []))
            if field.get("help"):
                labels.add(field["help"])
        for path in (root / "macos/Sources/OpenSVPflow").glob("*.swift"):
            labels.update(re.findall(r'AppText\("([^"\\]+)"\)', path.read_text(encoding="utf-8")))
        expected = None
        for locale in module.LOCALIZATIONS:
            with self.subTest(locale=locale):
                catalog = root / "macos/Localizations" / (locale + ".lproj") / "Localizable.strings"
                data = subprocess.check_output(["/usr/bin/plutil", "-convert", "json", "-o", "-", "--", str(catalog)], timeout=10)
                strings = json.loads(data)
                self.assertFalse(labels - strings.keys(), labels - strings.keys())
                if expected is None:
                    expected = set(strings)
                self.assertEqual(set(strings), expected)
                self.assertTrue(all(value.strip() for value in strings.values()))
                for key, value in strings.items():
                    self.assertEqual(key.count("%@"), value.count("%@"), key)
                if locale != "en":
                    self.assertNotEqual(strings["Playback"], "Playback")

    def test_distribution_contains_one_app_with_external_runtime_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            for name in (*module.PAYLOAD, "docs/usage.md"):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            localizations = {"en", "zh-Hans", "hi", "es", "ar", "fr", "bn", "pt-PT", "id", "ur", "ru", "de", "ja"}
            for locale in localizations:
                catalog = source / "macos/Localizations" / (locale + ".lproj") / "Localizable.strings"
                catalog.parent.mkdir(parents=True)
                catalog.write_text('"Playback" = "' + locale + '";\n')
            libraries = Path(directory) / "libraries"
            libraries.mkdir()
            for name in module.PLUGINS:
                (libraries / name).write_bytes(b"native-plugin")
            executable = Path(directory) / "OpenSVPflow"
            executable.write_bytes(b"native-launcher")
            executable.chmod(0o755)
            icons = Path(directory) / "icons"
            icons.mkdir()
            (icons / "OpenSVPflow.icns").write_bytes(b"app-icon")
            (icons / "StatusIconTemplate.png").write_bytes(b"status-icon")
            app = module.assemble_bundle(source, libraries, executable, icons, Path(directory) / "build", "v11")
            result = module.archive_bundle(app, Path(directory) / "dist", "v11")
            self.assertEqual(result.name, "open-svpflow-v11-arm64.zip")
            with zipfile.ZipFile(result) as archive:
                names = archive.namelist()
                self.assertEqual({name.split('/')[0] for name in names}, {"Open SVPflow.app"})
                prefix = "Open SVPflow.app/Contents/"
                self.assertEqual(archive.read(prefix + "Frameworks/libsvpflow1_vs.dylib"), b"native-plugin")
                self.assertEqual(archive.read(prefix + "Resources/player/settings_api.py"), b"player/settings_api.py")
                self.assertEqual(archive.read(prefix + "Resources/LICENSE"), b"LICENSE")
                self.assertIn(prefix + "Resources/assets/OpenSVPflow-screenshot.png", names)
                self.assertFalse(any("docs/" in name or "__pycache__" in name for name in names))
                self.assertFalse(any(name.endswith("/mpv") or name.endswith("/python3") or "libvsscript" in name for name in names))
                self.assertTrue(archive.getinfo(prefix + "MacOS/OpenSVPflow").external_attr >> 16 & 0o111)
                metadata = plistlib.loads(archive.read(prefix + "Info.plist"))
                self.assertEqual(metadata["CFBundleExecutable"], "OpenSVPflow")
                self.assertEqual(metadata["CFBundleShortVersionString"], "11.0.0")
                self.assertEqual(metadata["CFBundleVersion"], "11")
                self.assertEqual(metadata["OpenSVPflowReleaseVersion"], "v11")
                self.assertEqual(metadata["LSMinimumSystemVersion"], "26.0")
                self.assertEqual(set(metadata.get("CFBundleLocalizations", [])), localizations)
                for locale in localizations:
                    name = "Resources/" + locale + ".lproj/Localizable.strings"
                    self.assertEqual(archive.read(prefix + name), ('"Playback" = "' + locale + '";\n').encode())
                    info = plistlib.loads(archive.read(prefix + "Resources/" + locale + ".lproj/InfoPlist.strings"))
                    self.assertEqual(info["CFBundleShortVersionString"], "v11")

    def test_invalid_version_fails_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with self.assertRaises(ValueError):
                module.archive_bundle(Path(directory), output, "../escape")
            self.assertFalse(output.exists())

    @unittest.skipUnless(Path("/usr/bin/ditto").is_file(), "macOS archive extraction")
    def test_native_archive_extraction_preserves_application_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / module.APP_NAME
            executable = app / "Contents/MacOS/OpenSVPflow"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"executable fixture")
            executable.chmod(0o755)
            archive = module.archive_bundle(app, root / "dist", "0.1.3-mac.5")
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(root / "extracted")], check=True, timeout=10)
            extracted = root / "extracted" / module.APP_NAME / "Contents/MacOS/OpenSVPflow"
            self.assertTrue(os.access(extracted, os.X_OK))
            self.assertEqual(extracted.read_bytes(), b"executable fixture")

    def test_archive_rejects_symlinks_instead_of_collecting_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Open SVPflow.app"
            app.mkdir()
            outside = Path(directory) / "private"
            outside.write_text("not part of the app")
            (app / "alias").symlink_to(outside)
            with self.assertRaises(ValueError):
                module.archive_bundle(app, Path(directory) / "dist", "0.1.3-mac.5")


if __name__ == "__main__":
    unittest.main()
