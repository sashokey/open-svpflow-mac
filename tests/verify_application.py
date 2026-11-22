import argparse
import os
from pathlib import Path
import plistlib
import subprocess


def command(*arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout, result.stderr


def verify(app):
    app = Path(app)
    assert app.name == "Open SVPflow.app"
    contents = app / "Contents"
    metadata = plistlib.loads((contents / "Info.plist").read_bytes())
    assert metadata["LSMinimumSystemVersion"] == "26.0"
    assert metadata["CFBundleExecutable"] == "OpenSVPflow"
    assert set(metadata["CFBundleLocalizations"]) == {"en", "zh-Hans", "hi", "es", "ar", "fr", "bn", "pt-PT", "id", "ur", "ru", "de", "ja"}
    label = metadata["OpenSVPflowReleaseVersion"]
    if label.startswith("v"):
        assert metadata["CFBundleShortVersionString"] == label[1:] + ".0.0"
        assert metadata["CFBundleVersion"] == label[1:]
    for locale in metadata["CFBundleLocalizations"]:
        localization = contents / "Resources" / (locale + ".lproj")
        command("/usr/bin/plutil", "-lint", "--", str(localization / "Localizable.strings"))
        if label.startswith("v"):
            localized_info = plistlib.loads((localization / "InfoPlist.strings").read_bytes())
            assert localized_info["CFBundleShortVersionString"] == label, locale
    assert os.access(contents / "MacOS/OpenSVPflow", os.X_OK), "The extracted application executable lost its execute permission"
    assert not any(key.startswith("NS") and key.endswith("UsageDescription") for key in metadata)
    for name in ("README.md", "assets/OpenSVPflow-screenshot.png", "LICENSE", "THIRD_PARTY_NOTICES.md", "OpenSVPflow.icns", "StatusIconTemplate.png", "player/application.py", "player/settings_api.py", "player/session.py", "player/launch.py", "player/dependencies.py", "player/compat.lua", "player/graph.vpy"):
        assert (contents / "Resources" / name).is_file(), name
    for relative in ("MacOS/OpenSVPflow", "Frameworks/libsvpflow1_vs.dylib", "Frameworks/libsvpflow2_vs.dylib"):
        path = contents / relative
        arch, _ = command("/usr/bin/lipo", "-archs", str(path))
        assert arch.strip() == "arm64", (relative, arch)
        _, signature = command("/usr/bin/codesign", "-d", "-vv", str(path))
        assert "Signature=adhoc" in signature, relative
        command("/usr/bin/codesign", "--verify", "--strict", str(path))
        entitlements, _ = command("/usr/bin/codesign", "-d", "--entitlements", ":-", str(path))
        assert not entitlements.strip() or not plistlib.loads(entitlements.encode()), relative
        assert b"/Users/" not in path.read_bytes(), "Private user path in " + relative
    command("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    for path in app.rglob("*"):
        assert not path.is_symlink(), path
        assert path.name not in ("mpv", "python", "python3", "libvsscript.dylib", "__pycache__", ".DS_Store"), path
        if path.suffix in (".py", ".vpy", ".lua", ".json", ".md", ".command", ".strings"):
            assert b"/Users/" not in path.read_bytes(), "Private user path in " + str(path.relative_to(app))
    print("PASS: ARM64 application, ad-hoc component signatures, no entitlements or bundled external runtimes, required resources and private-path scan")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    verify(parser.parse_args().package)


if __name__ == "__main__":
    main()
