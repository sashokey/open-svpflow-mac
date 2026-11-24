import argparse
import contextlib
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from smoke_mpv import write_video
from smoke_plugins import check_intermediate


def require_isolated_gui(environment):
    if environment.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("GUI acceptance runs only on the isolated GitHub Actions runner")


def bundle_resources(app):
    app = Path(app).resolve()
    resources = app / "Contents" / "Resources"
    if app.suffix != ".app" or not (resources / "player" / "application.py").is_file():
        raise ValueError("Choose the extracted application bundle")
    return resources


def make_fixtures(directory):
    configuration = directory / "mpv configuration"
    configuration.mkdir()
    (configuration / "mpv.conf").write_text("vo=null\nao=null\nforce-window=no\nloop-file=inf\nload-scripts=no\n")
    files = [directory / name for name in ("first motion.y4m", "second ü motion.y4m", "third motion.y4m")]
    for file in files:
        write_video(file)
    return files, configuration


def make_gui_fixtures(files):
    movies = [file.with_suffix(".mp4") for file in files]
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-stream_loop", "89",
        "-i", str(files[0]), "-an", "-c:v", "libx264", "-preset", "ultrafast",
        "-movflags", "+faststart", "-n", str(movies[0]),
    ], check=True, timeout=30)
    for movie in movies[1:]:
        shutil.copyfile(movies[0], movie)
    return movies


def wait_for(check, message, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def backend(resources, state, command, request=None, environment=None):
    result = subprocess.run([sys.executable, "-B", str(resources / "player" / "application.py"), command, "--state-dir", str(state), "--bundle-root", str(resources)], input=json.dumps(request or {}), capture_output=True, text=True, env=environment, timeout=45)
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise AssertionError("The application backend returned invalid JSON: " + result.stderr[-4096:]) from error
    if result.returncode or not value.get("ok"):
        raise AssertionError("The application backend failed: " + json.dumps(value) + result.stderr[-4096:])
    return value


def intermediate_pixels(resources):
    import vapoursynth as vs
    from fractions import Fraction
    graph = runpy.run_path(str(resources / "player" / "graph.vpy"))
    core = vs.core
    source = core.std.BlankClip(width=320, height=192, format=vs.YUV420P8, length=4, fpsnum=24, fpsden=1, color=[32, 128, 128])
    def moving_bar(n, f):
        output = f.copy()
        pixels = output[0]
        for y in range(output.height):
            for x in range(output.width):
                pixels[y, x] = (180 if 64 + n * 4 <= x < 112 + n * 4 else 32) + n
        return output
    source = core.std.ModifyFrame(source, clips=source, selector=moving_bar)
    output = graph["interpolate"](source, graph["policy"].DEFAULT_SETTINGS, Fraction(24), Fraction(48), resources.parent / "Frameworks", gpu=False)
    assert output.fps == Fraction(48)
    with source.get_frame(0) as previous, source.get_frame(1) as following, output.get_frame(1) as generated:
        metrics = check_intermediate(bytes(previous[0]), bytes(following[0]), bytes(generated[0]))
    print("PASS: extracted application plugins generated a motion-compensated intermediate: " + str(metrics), flush=True)


def client_for(state):
    import session
    record = session.read_json(state / "session.json")
    return record, session.MpvClient(Path(record["socket"]).parent / "mpv.sock")


def wait_for_started_session(resources, state, environment, timeout=60):
    def ready():
        if not (state / "session.json").exists():
            return False
        result = backend(resources, state, "status", environment=environment)
        if result.get("status") == "error":
            raise AssertionError(result.get("message", "Playback startup failed"))
        if result.get("status") != "started":
            return False
        try:
            return client_for(state)
        except (FileNotFoundError, ConnectionRefusedError):
            return False
    return wait_for(ready, "The GUI did not start playback with available owned mpv IPC", timeout=timeout)


def playlist(client):
    return [value["filename"] for value in client.get("playlist", [])]


def same_media_path(actual, expected):
    if not actual:
        return False
    try:
        return expected.samefile(actual)
    except OSError:
        return False


def assert_playlist(client, expected):
    actual = playlist(client)
    assert len(actual) == len(expected) and all(
        same_media_path(path, reference) for path, reference in zip(actual, expected)
    ), (actual, expected)


def stop_session(state, record, client):
    if record is None and (state / "session.json").exists():
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            record, client = client_for(state)
    if client is not None:
        with contextlib.suppress(OSError, RuntimeError, TimeoutError):
            client.command(["quit"], timeout=2)
        client.close()
    if record is not None:
        wait_for(lambda: not (state / "session.json").exists(), "Session rendezvous was not cleaned", timeout=25 if client is None else 8)
        wait_for(lambda: not Path(record["socket"]).parent.exists(), "Session temporary resources were not cleaned", timeout=8)


def headless_session(resources, directory, files, environment):
    state = directory / "headless state"
    state.mkdir(mode=0o700)
    document = backend(resources, state, "settings-get", environment=environment)["document"]
    document["interpolation"]["backend"] = "cpu"
    document["playback"].update(rate="fixed", fps="48")
    backend(resources, state, "settings-save", {"document": document}, environment)
    record = client = None
    try:
        result = backend(resources, state, "open", {"files": [str(path) for path in files[:2]]}, environment)
        assert result["status"] == "started" and result["after_start"] == "quit_app", result
        record, client = client_for(state)
        worker_pid, player_pid = record["pid"], client.get("pid")
        assert alive(worker_pid) and alive(player_pid)
        assert backend(resources, state, "status", environment=environment)["status"] == "started"
        assert json.loads((state / "session.json").read_text())["pid"] == worker_pid
        assert client.get("pid") == player_pid
        assert_playlist(client, files[:2])
        def playback_status():
            return backend(resources, state, "status", environment=environment).get("playback", {})
        status = wait_for(
            lambda: (value if (value := playback_status()).get("interpolation") is True and value.get("target_fps") == 48 else None),
            "mpv did not report active interpolation at the configured target",
        )
        assert status["title"] == files[0].name, status
        client.command(["set_property", "pause", True])
        wait_for(lambda: playback_status().get("paused") is True, "mpv did not report paused playback")
        before_filters = client.get("vf")
        document["playback"]["fps"] = "72"
        document["preferences"]["new_files"] = "ask"
        backend(resources, state, "settings-save", {"document": document}, environment)
        assert client.get("vf") == before_filters, "Settings changed the current movie graph"
        assert playback_status().get("target_fps") == 48, "Settings changed the current target in the playback status"
        requested = backend(resources, state, "open", {"files": [str(files[2])]}, environment)
        assert requested["status"] == "ask"
        assert_playlist(client, files[:2])
        backend(resources, state, "open", {"action": "cancel"}, environment)
        assert_playlist(client, files[:2])
        backend(resources, state, "open", {"files": [str(files[2])], "action": "append"}, environment)
        assert_playlist(client, files)
        client.command(["playlist-next", "force"])
        wait_for(lambda: same_media_path(client.get("path"), files[1]) and client.get("vf") != before_filters, "The next playlist entry did not receive changed settings")
        client.command(["set_property", "pause", False])
        wait_for(lambda: "interpolation target 72 FPS" in (state / "playback.log").read_text(errors="replace"), "The next file did not generate the requested 72 FPS graph")
        wait_for(lambda: playback_status().get("target_fps") == 72, "The next file's status did not report the new target")
        replaced = backend(resources, state, "open", {"files": [str(files[2])], "action": "replace"}, environment)
        assert replaced["status"] == "started"
        assert_playlist(client, files[2:])
        assert client.get("pid") == player_pid
        print("PASS: first frames, detached worker, reconnect without second mpv, ordered queue, ask/cancel/append/replace, current-file stability and next-file settings", flush=True)
    finally:
        try:
            stop_session(state, record, client)
        finally:
            for name in ("session.log", "playback.log"):
                path = state / name
                if path.exists():
                    print(name + ":\n" + path.read_text(errors="replace")[-12000:], flush=True)
    assert backend(resources, state, "status", environment=environment)["status"] == "idle"
    print("PASS: owned session and temporary resources cleaned after mpv exit", flush=True)


def interpolation_failures(resources, directory, files, configuration):
    from session import PlaybackSession, read_json
    from settings_api import default_document, save_document

    state = directory / "failure state"
    settings = state / "settings.json"
    document = default_document()
    document["interpolation"]["backend"] = "cpu"
    document["playback"].update(rate="fixed", fps="48")
    save_document(settings, document)

    with patch.dict(os.environ, {"MPV_HOME": str(configuration)}):
        with tempfile.TemporaryDirectory(prefix="osvp-fail-", dir="/private/tmp") as runtime:
            owner = PlaybackSession(state, resources, runtime, notify=lambda root: None)
            try:
                assert owner.open([str(file) for file in files[:2]])["status"] == "started"
                document["playback"]["fps"] = "24"
                save_document(settings, document)
                owner.client.command(["playlist-next", "force"])
                wait_for(owner.stopped.is_set, "The session continued after failing to interpolate the next file")
                failure = read_json(state / "event.json")
                assert failure["file_title"] == files[1].name, failure
                assert failure["rate_error"] and "must be higher" in failure["message"], failure
                assert owner.process.wait(timeout=5) == 0
            finally:
                owner.close()

        document["playback"]["fps"] = "48"
        save_document(settings, document)
        with tempfile.TemporaryDirectory(prefix="osvp-retry-", dir="/private/tmp") as runtime:
            owner = PlaybackSession(state, resources, runtime, notify=lambda root: None)
            try:
                assert owner.open([str(files[2])])["status"] == "started"
                owner.client.command(["vf", "remove", "@svp-interpolate"])
                wait_for(owner.stopped.is_set, "The session continued after losing the interpolation filter")
                failure = read_json(state / "event.json")
                assert failure["file_title"] == files[2].name, failure
                assert "filter failed or was disabled" in failure["message"], failure
                assert owner.process.wait(timeout=5) == 0
            finally:
                owner.close()
    print("PASS: incompatible next file and filter loss closed mpv, reported the reason, and allowed a fresh session", flush=True)


def gui_pids(app):
    executable = str(app / "Contents" / "MacOS" / "OpenSVPflow")
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,args="], capture_output=True, text=True, check=True, timeout=5)
    result = []
    for line in listing.stdout.splitlines():
        values = line.strip().split(None, 1)
        if len(values) == 2 and (values[1] == executable or values[1].startswith(executable + " ")):
            result.append(int(values[0]))
    return result


def matches_gui_mode(value, mode):
    if value.get("running") is not True:
        return False
    if mode == "menu_bar":
        return value.get("policy") == "accessory" and value.get("visibleWindows") == 0
    if mode == "keep_open":
        return value.get("policy") == "regular" and value.get("visibleWindows", 0) > 0
    return False


def gui_metadata(app, probe):
    identifiers = gui_pids(app)
    if len(identifiers) != 1:
        return {"pids": identifiers}
    result = subprocess.run([str(probe), str(identifiers[0])], check=True, capture_output=True, text=True, timeout=5)
    return json.loads(result.stdout)


def gui_mode(app, probe, mode):
    value = gui_metadata(app, probe)
    return value if matches_gui_mode(value, mode) else False


def cold_quit_app(app, resources, state, movie, environment):
    record = client = None
    command = ["/usr/bin/open", "--env", "MPV_HOME=" + environment["MPV_HOME"], "-a", str(app)]
    try:
        subprocess.run([*command, "-n", "-W", str(movie)], check=True, timeout=60)
        assert not gui_pids(app), "The cold launcher did not exit under Quit App"
        record, client = wait_for_started_session(resources, state, environment)
        worker_pid, player_pid = record["pid"], client.get("pid")
        start = client.get("playback-time")
        assert isinstance(start, (int, float))

        def advancing():
            position = client.get("playback-time")
            return isinstance(position, (int, float)) and position >= start + 15

        wait_for(advancing, "Playback did not continue while the launcher was closed", timeout=28)
        subprocess.run(command, check=True, timeout=5)
        wait_for(lambda: gui_pids(app), "The launcher did not reopen")
        resumed = backend(resources, state, "status", environment=environment)
        assert resumed["status"] == "started" and resumed["playback"]["title"] == movie.name, resumed
        assert json.loads((state / "session.json").read_text())["pid"] == worker_pid
        assert client.get("pid") == player_pid
        print("PASS: cold Quit App kept playback advancing and the reopened launcher found the same movie", flush=True)
    finally:
        for identifier in gui_pids(app):
            os.kill(identifier, signal.SIGTERM)
        wait_for(lambda: not gui_pids(app), "The owned cold-launch GUI did not close", timeout=5)
        stop_session(state, record, client)


def gui_lifetime(app, resources, directory, files, configuration, environment):
    require_isolated_gui(os.environ)
    files = make_gui_fixtures(files)
    state = Path.home() / "Library" / "Application Support" / "Open SVPflow"
    if state.exists() or gui_pids(app):
        raise AssertionError("The isolated GUI smoke requires a fresh application state")
    state.mkdir(parents=True, mode=0o700)
    document = backend(resources, state, "settings-get", environment=environment)["document"]
    document["interpolation"]["backend"] = "cpu"
    document["playback"].update(rate="fixed", fps="48")
    document["preferences"]["after_start"] = "quit_app"
    backend(resources, state, "settings-save", {"document": document}, environment)
    cold_quit_app(app, resources, state, files[0], environment)
    document["preferences"]["after_start"] = "keep_open"
    backend(resources, state, "settings-save", {"document": document}, environment)
    probe = directory / "gui-runtime-probe"
    subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", "-module-cache-path", str(directory / "swift-modules"), str(Path(__file__).with_name("gui_runtime_probe.swift")), "-o", str(probe)], check=True, timeout=90)
    record = client = None
    launched = None
    observed_pids = set()
    gui_stderr = directory / "application-stderr.log"
    try:
        command = [
            "/usr/bin/open", "-n", "-W", "--stderr", str(gui_stderr),
            "--env", "MPV_HOME=" + str(configuration), "-a", str(app), str(files[0]),
        ]
        launched = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        record, client = wait_for_started_session(resources, state, environment)
        worker_pid, player_pid = record["pid"], client.get("pid")
        first = wait_for(lambda: gui_mode(app, probe, "keep_open"), "Keep Window Open did not leave a regular GUI with a visible window", timeout=30)
        observed_pids.update(gui_pids(app))
        print("PASS: Keep Window Open actual runtime metadata: " + json.dumps(first), flush=True)
        document["preferences"]["after_start"] = "menu_bar"
        backend(resources, state, "settings-save", {"document": document}, environment)
        subprocess.run(["/usr/bin/open", "-a", str(app), str(files[1])], check=True, timeout=5)
        wait_for(lambda: any(same_media_path(actual, files[1]) for actual in playlist(client)), "LaunchServices did not deliver the second movie to the existing session", timeout=30)
        def menu_bar_mode():
            if launched.poll() is not None:
                raise AssertionError("The GUI process exited during Move to Menu Bar")
            return gui_mode(app, probe, "menu_bar")
        second = wait_for(menu_bar_mode, "Move to Menu Bar did not leave an accessory GUI without a main window", timeout=30)
        assert_playlist(client, files[:2])
        assert json.loads((state / "session.json").read_text())["pid"] == worker_pid
        assert client.get("pid") == player_pid
        print("PASS: Move to Menu Bar actual runtime metadata: " + json.dumps(second), flush=True)
        document["preferences"]["after_start"] = "quit_app"
        backend(resources, state, "settings-save", {"document": document}, environment)
        subprocess.run(["/usr/bin/open", "-a", str(app), str(files[2])], check=True, timeout=5)
        deadline = time.monotonic() + 60
        while launched.poll() is None and time.monotonic() < deadline:
            observed_pids.update(gui_pids(app))
            time.sleep(0.05)
        output, errors = launched.communicate(timeout=2)
        assert launched.returncode == 0, output + errors
        assert observed_pids and not gui_pids(app), "The actual GUI process did not exit under Quit App"
        assert alive(worker_pid) and alive(player_pid)
        assert_playlist(client, files)
        assert backend(resources, state, "status", environment=environment)["status"] == "started"
        subprocess.run(["/usr/bin/open", "--env", "MPV_HOME=" + str(configuration), "-a", str(app)], check=True, timeout=5)
        wait_for(lambda: gui_pids(app), "The GUI did not reopen")
        time.sleep(2)
        observed_pids.update(gui_pids(app))
        assert json.loads((state / "session.json").read_text())["pid"] == worker_pid
        assert client.get("pid") == player_pid
        print("PASS: LaunchServices file-open, actual GUI process exit, playback survival and reopen attached to the same worker/mpv", flush=True)
    except Exception:
        if gui_stderr.exists():
            print("Application stderr:\n" + gui_stderr.read_text(errors="replace")[-4096:], flush=True)
        for label, collect in (
            ("GUI metadata", lambda: gui_metadata(app, probe)),
            ("LaunchServices exit status", lambda: launched.poll() if launched else None),
            ("Original GUI process", lambda: {pid: alive(pid) for pid in observed_pids}),
            ("Session status", lambda: backend(resources, state, "status", environment=environment)),
            ("Playlist", lambda: [Path(path).name for path in playlist(client)] if client else []),
        ):
            try:
                print(label + ": " + json.dumps(collect()), flush=True)
            except Exception as error:
                print(label + " unavailable: " + str(error), flush=True)
        raise
    finally:
        if launched is not None and launched.poll() is None:
            launched.terminate()
            launched.wait(timeout=3)
        for pid in gui_pids(app):
            observed_pids.add(pid)
            os.kill(pid, signal.SIGTERM)
        wait_for(lambda: not gui_pids(app), "The owned test GUI did not close", timeout=5)
        stop_session(state, record, client)


def main():
    parser = argparse.ArgumentParser(description="Bounded extracted-application integration acceptance")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--gui", action="store_true")
    options = parser.parse_args()
    if options.gui:
        require_isolated_gui(os.environ)
    app = options.package.resolve()
    resources = bundle_resources(app)
    sys.path.insert(0, str(resources / "player"))
    with tempfile.TemporaryDirectory(prefix="osvp-accept-") as temporary:
        directory = Path(temporary)
        inspection = backend(resources, directory / "dependency check", "check")
        assert inspection.get("ready"), inspection
        intermediate_pixels(resources)
        files, configuration = make_fixtures(directory)
        environment = dict(os.environ, MPV_HOME=str(configuration))
        headless_session(resources, directory, files, environment)
        interpolation_failures(resources, directory, files, configuration)
        if options.gui:
            gui_lifetime(app, resources, directory, files, configuration, environment)


if __name__ == "__main__":
    main()
