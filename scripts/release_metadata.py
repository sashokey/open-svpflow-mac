import json
from pathlib import Path
import re


def read_metadata(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"version", "draft", "prerelease", "notes"}
    if not isinstance(data, dict) or not required <= data.keys() <= required | {"replacement"}:
        raise ValueError("Invalid release metadata fields")
    tag = data["version"]
    if not isinstance(tag, str) or not re.fullmatch(r"v[1-9][0-9]*", tag):
        raise ValueError("Invalid release tag")
    if type(data["draft"]) is not bool or type(data["prerelease"]) is not bool:
        raise ValueError("Release flags must be booleans")
    if not isinstance(data["notes"], str) or not data["notes"].strip():
        raise ValueError("Release notes must be nonempty")
    if "replacement" in data:
        replacement = data["replacement"]
        if not isinstance(replacement, dict) or set(replacement) != {"previous_commit", "previous_asset"} or data["draft"]:
            raise ValueError("Invalid release replacement")
        commit, asset = replacement["previous_commit"], replacement["previous_asset"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Invalid release replacement commit")
        if not isinstance(asset, str) or not re.fullmatch(r"open-svpflow-[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?-arm64\.zip", asset):
            raise ValueError("Invalid release replacement asset")
    return data


if __name__ == "__main__":
    data = read_metadata(Path(__file__).resolve().parents[1] / ".github/release.json")
    for key in ("version", "draft", "prerelease"):
        value = data[key]
        print(key + "=" + (str(value).lower() if type(value) is bool else value))
    for key in ("previous_commit", "previous_asset"):
        print(key + "=" + data.get("replacement", {}).get(key, ""))
