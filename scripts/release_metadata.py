import json
from pathlib import Path
import re


def read_metadata(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"version", "package_version", "draft", "prerelease", "notes"}:
        raise ValueError("Invalid release metadata fields")
    tag = data["version"]
    if not isinstance(tag, str) or not re.fullmatch(r"v[1-9][0-9]*", tag):
        raise ValueError("Invalid release tag")
    package_version = data["package_version"]
    if not isinstance(package_version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", package_version
    ):
        raise ValueError("Invalid package version")
    if type(data["draft"]) is not bool or type(data["prerelease"]) is not bool:
        raise ValueError("Release flags must be booleans")
    if not isinstance(data["notes"], str) or not data["notes"].strip():
        raise ValueError("Release notes must be nonempty")
    return data


if __name__ == "__main__":
    data = read_metadata(Path(__file__).resolve().parents[1] / ".github/release.json")
    for key in ("version", "package_version", "draft", "prerelease"):
        value = data[key]
        print(key + "=" + (str(value).lower() if type(value) is bool else value))

