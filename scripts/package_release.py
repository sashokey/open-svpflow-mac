import argparse
from pathlib import Path
import plistlib
import re
import shutil
import zipfile

PAYLOAD = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
    "assets/OpenSVPflow-screenshot.png",
    "player/launch.py",
    "player/graph.vpy",
    "player/settings.json",
    "player/policy.py",
    "player/compat.lua",
    "player/settings_api.py",
    "player/application.py",
    "player/session.py",
    "player/dependencies.py",
    "player/install-homebrew.command",
    "player/install-dependencies.command",
)
PLUGINS = ("libsvpflow1_vs.dylib", "libsvpflow2_vs.dylib")
LOCALIZATIONS = ("en", "zh-Hans", "hi", "es", "ar", "fr", "bn", "pt-PT", "id", "ur", "ru", "de", "ja")
APP_NAME = "Open SVPflow.app"
WORKER_BUNDLE = "Helpers/Open SVPflow Playback.app/Contents"
WORKER_EXECUTABLE = WORKER_BUNDLE + "/MacOS/PlaybackWorker"


def validate_version(version):
    if not re.fullmatch(r"v[1-9][0-9]*|[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("Invalid release version")


def assemble_bundle(source, library, executable, icons, output, version, *, worker):
    validate_version(version)
    files = [(Path(source) / name, "Resources/" + name) for name in PAYLOAD]
    files += [(Path(library) / name, "Frameworks/" + name) for name in PLUGINS]
    files += [(Path(icons) / name, "Resources/" + name) for name in ("OpenSVPflow.icns", "StatusIconTemplate.png")]
    for locale in LOCALIZATIONS:
        directory = Path(source) / "macos/Localizations" / (locale + ".lproj")
        if directory.is_symlink():
            raise ValueError("Symlinked localization input: " + locale)
        files.append((directory / "Localizable.strings", "Resources/" + directory.name + "/Localizable.strings"))
    files += [(Path(executable), "MacOS/OpenSVPflow"), (Path(worker), WORKER_EXECUTABLE)]
    for path, name in files:
        if path.is_symlink():
            raise ValueError("Symlinked package input: " + name)
        if not path.is_file():
            raise FileNotFoundError("Missing package input: " + name)
    app = Path(output) / APP_NAME
    app.mkdir(parents=True, exist_ok=False)
    contents = app / "Contents"
    for origin, relative in files:
        destination = contents / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination)
        destination.chmod(0o755 if relative in ("MacOS/OpenSVPflow", WORKER_EXECUTABLE) else 0o644)
    numbered_release = version.startswith("v")
    numeric = version[1:] + ".0.0" if numbered_release else version.split("-", 1)[0]
    build_version = version[1:] if numbered_release else numeric
    if numbered_release:
        localized_info = plistlib.dumps({"CFBundleShortVersionString": version})
        for locale in LOCALIZATIONS:
            (contents / "Resources" / (locale + ".lproj") / "InfoPlist.strings").write_bytes(localized_info)
    metadata = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": list(LOCALIZATIONS),
        "CFBundleDisplayName": "Open SVPflow",
        "CFBundleName": "Open SVPflow",
        "CFBundleIdentifier": "org.opensvpflow.mac",
        "CFBundleExecutable": "OpenSVPflow",
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": numeric,
        "CFBundleVersion": build_version,
        "OpenSVPflowReleaseVersion": version,
        "CFBundleIconFile": "OpenSVPflow.icns",
        "LSMinimumSystemVersion": "26.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Video",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["public.movie", "public.audiovisual-content", "org.matroska.mkv", "org.webmproject.webm"],
            "CFBundleTypeExtensions": ["mkv", "mp4", "mov", "m4v", "avi", "webm", "m2ts", "ts", "mpg", "mpeg", "wmv", "flv", "mxf", "vob"],
        }],
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(metadata, sort_keys=True))
    worker_metadata = {
        "CFBundleIdentifier": "org.opensvpflow.mac.playback",
        "CFBundleName": "Open SVPflow Playback",
        "CFBundleExecutable": "PlaybackWorker",
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": numeric,
        "CFBundleVersion": build_version,
        "LSMinimumSystemVersion": "26.0",
        "LSBackgroundOnly": True,
    }
    (contents / WORKER_BUNDLE / "Info.plist").write_bytes(plistlib.dumps(worker_metadata))
    return app


def archive_bundle(app, output, version):
    validate_version(version)
    app = Path(app)
    if app.name != APP_NAME or app.is_symlink() or not app.is_dir():
        raise ValueError("A complete application bundle is required")
    files = []
    for path in sorted(app.rglob("*")):
        if path.is_symlink():
            raise ValueError("Application bundles must not contain symlinks")
        if path.is_file():
            files.append(path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / ("open-svpflow-" + version + "-arm64.zip")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            member = zipfile.ZipInfo(app.name + "/" + path.relative_to(app).as_posix())
            member.create_system = 3
            member.external_attr = (0o100000 | (path.stat().st_mode & 0o777)) << 16
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, path.read_bytes())
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--library", type=Path, default=Path("target/release"))
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-root", type=Path, default=Path(".build/distribution"))
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args()
    validate_version(args.version)
    from build_app import build_executable, build_worker, generate_icons, sign_bundle
    build = args.build_root / args.version
    build.mkdir(parents=True, exist_ok=False)
    executable = args.executable or build_executable(args.source, build / "swift")
    icons = generate_icons(args.source, build / "icons")
    worker = build_worker(args.source, build / "PlaybackWorker")
    app = assemble_bundle(args.source, args.library, executable, icons, build / "bundle", args.version, worker=worker)
    sign_bundle(app)
    print(archive_bundle(app, args.output, args.version))


if __name__ == "__main__":
    main()

