from pathlib import Path
import shutil
import subprocess


def generate_icons(source, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    iconset = output / "OpenSVPflow.iconset"
    iconset.mkdir()
    magick = shutil.which("magick")
    if magick is None:
        raise RuntimeError("ImageMagick is required only when building application icons")
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = "icon_" + str(size) + "x" + str(size) + ("@2x" if scale == 2 else "") + ".png"
            pixels = str(size * scale)
            subprocess.run([magick, "-background", "none", str(Path(source) / "assets/AppIcon.svg"), "-resize", pixels + "x" + pixels, str(iconset / name)], check=True, timeout=30)
    subprocess.run(["/usr/bin/iconutil", "--convert", "icns", "--output", str(output / "OpenSVPflow.icns"), str(iconset)], check=True, timeout=30)
    subprocess.run([magick, "-background", "none", str(Path(source) / "assets/StatusIcon.svg"), "-units", "PixelsPerInch", "-density", "144", str(output / "StatusIconTemplate.png")], check=True, timeout=30)
    return output


def build_executable(source, scratch):
    command = ["/usr/bin/xcrun", "swift", "build", "--package-path", str(Path(source) / "macos"), "--scratch-path", str(scratch), "--disable-sandbox", "-c", "release", "-debug-info-format", "none", "--arch", "arm64"]
    subprocess.run(command, check=True, timeout=600)
    result = subprocess.run(command + ["--show-bin-path"], check=True, capture_output=True, text=True, timeout=30)
    return Path(result.stdout.strip()) / "OpenSVPflow"


def build_worker(source, output):
    output = Path(output)
    subprocess.run([
        "/usr/bin/xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=26.0", "-Os",
        "-Wall", "-Wextra", "-Werror", str(Path(source) / "macos/PlaybackWorker.c"), "-o", str(output),
    ], check=True, timeout=30)
    return output


def sign_bundle(app):
    contents = Path(app) / "Contents"
    for relative in ("Frameworks/libsvpflow1_vs.dylib", "Frameworks/libsvpflow2_vs.dylib", "MacOS/OpenSVPflow",
                     "Helpers/Open SVPflow Playback.app", "."):
        path = Path(app) if relative == "." else contents / relative
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(path)], check=True, timeout=30)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True, timeout=30)
