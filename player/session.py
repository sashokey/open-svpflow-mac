from collections import deque
import contextlib
import fcntl
import hmac
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4


MAX_MESSAGE = 262144


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Application state must be an owned directory, not a symbolic link")
    path.chmod(0o700)
    return path


def atomic_json(path, value):
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Application state must not be a symbolic link")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_MESSAGE:
        raise ValueError("Application state exceeds its size limit")
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_MESSAGE:
            raise ValueError("Invalid application state file")
        data = stream.read(MAX_MESSAGE + 1)
    if len(data) > MAX_MESSAGE:
        raise ValueError("Application state exceeds its size limit")
    value = json.loads(data)
    if type(value) is not dict:
        raise ValueError("Application state must be an object")
    return value


def media_files(values):
    if type(values) is not list or len(values) > 4096:
        raise ValueError("A list of media files is required")
    result = []
    for value in values:
        if type(value) is not str or not value or any(ord(c) < 32 for c in value):
            raise ValueError("Media filenames must not contain control characters")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            result.append(value)
        else:
            path = Path(value).expanduser().resolve()
            if not path.is_file() or not os.access(path, os.R_OK):
                raise ValueError("A readable local movie file is required: " + value)
            result.append(str(path))
    return result


def playback_title(name):
    if type(name) is not str:
        return ""
    if "://" in name:
        name = name.partition("?")[0].partition("#")[0]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return "".join(character for character in name if ord(character) >= 32).strip()[:512]


def finite_number(value):
    return value if type(value) in (int, float) and -math.inf < value < math.inf else None


class PlaybackQuality:
    window = 30
    warmup = 4
    sample_gap = 6
    counters = ("output_drops", "decoder_drops", "display_delays")
    unchecked = dict.fromkeys((*counters, "av_offset_ms"), None) | {"quality": "checking", "window_seconds": 0}

    def __init__(self, title="", rate="auto"):
        self.rate = rate
        self.points = deque()
        self.current = {"title": playback_title(title), "paused": False, "interpolation": None,
                        "target_fps": None, "display_limited": False, **self.unchecked}
        self.last_seen = None

    def reset(self):
        self.points.clear()
        self.current = self.current | self.unchecked

    def snapshot(self):
        result = self.current
        if self.last_seen is not None and time.monotonic() - self.last_seen > self.sample_gap:
            return result | self.unchecked
        return dict(result)

    def filter_status(self, source, filtered, refresh):
        if not source or source <= 0 or not filtered or filtered <= 0:
            return None, None
        from policy import select_rate
        try:
            target = float(select_rate(source, refresh, self.rate))
        except ValueError:
            return None, None
        if abs(filtered - target) <= target * 0.02 and abs(filtered - target) < abs(filtered - source):
            return True, target
        return None, None

    def sample(self, values, now):
        source, filtered, refresh, speed = (finite_number(values.get(name)) for name in ("source_fps", "filtered_fps", "refresh", "speed"))
        interpolation, target = self.filter_status(source, filtered, refresh)
        if interpolation is None:
            interpolation, target = self.current["interpolation"], self.current["target_fps"]
        paused = values.get("paused") is True
        display_limited = bool(filtered and refresh and refresh > 0 and filtered * (speed or 1) > refresh * 1.02)
        title = playback_title(values.get("title")) or playback_title(values.get("filename")) or self.current["title"]
        info = self.current | self.unchecked | {"title": title, "paused": paused, "interpolation": interpolation,
                                               "target_fps": target, "display_limited": display_limited}
        self.last_seen = now
        if paused or values.get("seeking") is True:
            self.points.clear()
            self.current = info
            return
        counters = tuple(values.get(name) for name in self.counters)
        counters = tuple(value if type(value) is int and value >= 0 else None for value in counters)
        if self.points:
            previous_time, previous = self.points[-1]
            if now - previous_time > self.sample_gap or any(
                (a is None) != (b is None) or (a is not None and b is not None and b < a)
                for a, b in zip(previous, counters)
            ):
                self.points.clear()
        self.points.append((now, counters))
        while self.points[0][0] < now - self.window:
            self.points.popleft()
        oldest, baseline = self.points[0]
        if now - oldest >= self.warmup and any(value is not None for value in counters):
            totals = tuple(None if value is None else value - start for start, value in zip(baseline, counters))
            affected = sum(
                any(a is not None and b is not None and b > a for a, b in zip(first[1], second[1]))
                for first, second in zip(self.points, list(self.points)[1:])
            )
            count = max(value for value in totals if value is not None)
            avsync = finite_number(values.get("avsync"))
            info.update(zip(self.counters, totals))
            info["av_offset_ms"] = round(avsync * 1000, 1) if avsync is not None and abs(avsync) < 3600 else None
            info["window_seconds"] = min(self.window, int(now - oldest))
            info["quality"] = "attention" if affected >= 2 and count >= 5 else "good" if affected else "excellent"
        self.current = info


class PlaylistController:
    def __init__(self, command, is_active):
        self.command = command
        self.is_active = is_active
        self.pending = []
        self.requested_entry = None

    def open(self, files, action="auto", policy="append"):
        if action not in ("auto", "append", "replace", "cancel") or policy not in ("append", "replace", "ask"):
            raise ValueError("Unsupported new-file action")
        if action == "cancel":
            self.pending = []
            return "queued" if self.is_active() else "idle"
        files = media_files(files)
        if not files:
            raise ValueError("Choose at least one movie")
        active = self.is_active()
        selected = policy if action == "auto" else action
        if active and selected == "ask":
            self.pending = files
            return "ask"
        self.pending = []
        replace = not active or selected == "replace"
        reply = self.command(["loadfile", files[0], "replace" if replace else "append"])
        self.requested_entry = reply.get("playlist_entry_id") if type(reply) is dict else None
        for file in files[1:]:
            self.command(["loadfile", file, "append"])
        return "started" if replace else "queued"


class MpvClient:
    def __init__(self, address, timeout=5):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect(str(address))
        self.socket.settimeout(None)
        self.events = queue.Queue()
        self.pending = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.closed = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            with self.socket.makefile("rb") as stream:
                while True:
                    line = stream.readline(MAX_MESSAGE + 1)
                    if not line or len(line) > MAX_MESSAGE:
                        break
                    value = json.loads(line)
                    identifier = value.get("request_id")
                    with self.lock:
                        reply = self.pending.pop(identifier, None)
                    if reply is not None:
                        reply.put(value)
                    elif "event" in value:
                        self.events.put(value)
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self.closed = True
                pending = list(self.pending.values())
                self.pending.clear()
            for reply in pending:
                reply.put({"error": "mpv disconnected"})
            self.events.put({"event": "connection-closed"})

    def command(self, command, timeout=5):
        reply = queue.Queue(maxsize=1)
        with self.lock:
            if self.closed:
                raise RuntimeError("mpv disconnected")
            self.sequence += 1
            identifier = self.sequence
            self.pending[identifier] = reply
            encoded = (json.dumps({"command": command, "request_id": identifier}, ensure_ascii=False) + "\n").encode()
            try:
                self.socket.sendall(encoded)
            except OSError:
                self.pending.pop(identifier, None)
                raise
        try:
            result = reply.get(timeout=timeout)
        except queue.Empty as error:
            with self.lock:
                self.pending.pop(identifier, None)
            raise TimeoutError("mpv did not answer in time") from error
        if result.get("error") != "success":
            raise RuntimeError("mpv: " + str(result.get("error", "invalid response")))
        return result.get("data")

    def get(self, name, default=None):
        try:
            return self.command(["get_property", name])
        except RuntimeError:
            return default

    def close(self):
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        if threading.current_thread() is not self.reader:
            self.reader.join(timeout=1)


def rpc(record, command, payload=None, timeout=35):
    address = record.get("socket")
    token = record.get("token")
    if type(address) is not str or type(token) is not str or len(address.encode()) >= 104:
        raise ValueError("Invalid playback session")
    parent = Path(address).parent
    parent_info = parent.lstat()
    socket_info = Path(address).lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077 or not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.getuid():
        raise ValueError("Playback session is not private")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(address)
        connection.sendall((json.dumps({"token": token, "command": command, "payload": payload or {}}, ensure_ascii=False) + "\n").encode())
        with connection.makefile("rb") as stream:
            line = stream.readline(MAX_MESSAGE + 1)
        if not line or len(line) > MAX_MESSAGE:
            raise ValueError("Invalid playback response")
        result = json.loads(line)
        if type(result) is not dict or type(result.get("ok")) is not bool:
            raise ValueError("Invalid playback response")
        return result


def connect_existing(state_dir):
    try:
        record = read_json(Path(state_dir) / "session.json")
        result = rpc(record, "ping", timeout=1)
        if result.get("ok") and result.get("protocol") == 1 and result.get("pid") == record.get("pid"):
            return record
    except TimeoutError as error:
        raise TimeoutError("The playback session is busy; try again shortly") from error
    except (OSError, ValueError):
        pass
    return None


def status(state_dir):
    state_dir = private_directory(state_dir)
    descriptor = os.open(state_dir / "startup.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        existing = connect_existing(state_dir)
        if existing:
            return rpc(existing, "status", timeout=5)
        with contextlib.suppress(FileNotFoundError):
            (state_dir / "session.json").unlink()
    event = state_dir / "event.json"
    try:
        result = read_json(event)
        event.unlink()
        return result
    except FileNotFoundError:
        return {"ok": True, "status": "idle"}


def gui_bundle(bundle_root):
    root = Path(bundle_root).resolve()
    return root.parent.parent if root.name == "Resources" and root.parent.name == "Contents" and root.parent.parent.suffix == ".app" else None


def reopen_gui(bundle_root):
    bundle = gui_bundle(bundle_root)
    if bundle is not None:
        subprocess.Popen(["/usr/bin/open", "-a", str(bundle), "--args", "--session-event"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


class PlaybackSession:
    def __init__(self, state_dir, bundle_root, runtime_dir, notify=reopen_gui):
        self.state_dir = private_directory(state_dir)
        self.bundle_root = Path(bundle_root).resolve()
        self.runtime_dir = private_directory(runtime_dir)
        self.notify = notify
        self.client = None
        self.process = None
        self.log = None
        self.active = False
        self.ever_started = False
        self.error = None
        self.failure = None
        self.graph_attempt = 0
        self.probing_viewport = False
        self.loading_entry = 0
        self.confirmed_entry = 0
        self.file_title = ""
        self.after_start = "quit_app"
        self.quality = PlaybackQuality()
        self.ready = threading.Event()
        self.bridge_ready = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.RLock()
        self.failure_lock = threading.Lock()
        self.last_drop = None
        self.drop_jobs = queue.Queue(maxsize=32)
        self.playlist = PlaylistController(self._command, lambda: self.active)

    def _document(self):
        from settings_api import load_document
        return load_document(self.state_dir / "settings.json")

    def _command(self, command):
        if self.client is None:
            raise RuntimeError("The player is not running")
        return self.client.command(command)

    def start(self):
        if self.client is not None:
            return
        from dependencies import inspect_environment
        from launch import runtime_environment
        inspection = inspect_environment(self.bundle_root)
        if not inspection.get("ready"):
            raise ValueError(inspection.get("message", "Playback dependencies are unavailable"))
        runtime = inspection.get("runtime", {})
        executable = runtime.get("mpv")
        if not executable or not Path(executable).is_file():
            raise ValueError("The external mpv executable is unavailable")
        self.runtime = runtime
        address = self.runtime_dir / "mpv.sock"
        environment = runtime_environment(runtime.get("vsscript"), config_root=self.runtime_dir / "configuration")
        environment["OPEN_SVPFLOW_GRAPH_DIR"] = str(self.runtime_dir)
        descriptor = os.open(self.state_dir / "playback.log", os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.log = os.fdopen(descriptor, "wb")
        command = [str(executable), "--idle=yes", "--force-window=no", "--terminal=no", "--input-terminal=no", "--input-ipc-server=" + str(address), "--input-builtin-drag-and-drop=no", "--script=" + str(self.bundle_root / "player" / "compat.lua")]
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log, env=environment, close_fds=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("mpv exited before its control channel was ready; see playback.log")
            if address.exists():
                try:
                    self.client = MpvClient(address)
                    break
                except OSError:
                    pass
            time.sleep(0.025)
        if self.client is None:
            raise TimeoutError("mpv did not create its local control channel")
        self.client.command(["observe_property", 1, "idle-active"])
        self.client.command(["observe_property", 2, "dropped-files"])
        self.client.command(["observe_property", 3, "vf"])
        self.client.command(["request_log_messages", "warn"])
        threading.Thread(target=self._events, daemon=True).start()
        threading.Thread(target=self._drops, daemon=True).start()
        name = self.client.command(["client_name"])
        deadline = time.monotonic() + 3
        while not self.bridge_ready.is_set() and time.monotonic() < deadline:
            self.client.command(["script-message", "open-svpflow-connect", name])
            self.bridge_ready.wait(0.1)
        if not self.bridge_ready.is_set():
            raise RuntimeError("The playback configuration bridge did not start")

    def configure_file(self):
        from policy import build_command
        from dependencies import plugin_directory
        from settings_api import playback_settings, rate_argument
        file = self.client.get("path")
        self.file_title = playback_title(file)
        document = self._document()
        settings = playback_settings(document)
        self.ready.clear()
        self.active = False
        self.graph_attempt += 1
        self.probing_viewport = False
        self.after_start = document["preferences"]["after_start"]
        rate = rate_argument(document)
        self.quality = PlaybackQuality(file, rate)
        plugins = plugin_directory(self.bundle_root)
        command = build_command(str(self.runtime["mpv"]), file, self.bundle_root / "player" / "graph.vpy", plugins, settings, rate, attempt=self.graph_attempt)
        filters = self.client.get("vf", []) or []
        for value in filters:
            if value.get("label") in ("svp-input", "svp-interpolate"):
                self.client.command(["vf", "remove", "@" + value["label"]])
        for argument in command:
            if argument.startswith("--vf-add="):
                self.client.command(["vf", "add", argument[len("--vf-add="):]])
        self.client.command(["set_property", "hr-seek-framedrop", "no"])

    def _filter_ok(self):
        return any(value.get("label") == "svp-interpolate" and value.get("name") == "vapoursynth" and value.get("enabled", True)
                   for value in self.client.get("vf", []) or [])

    def graph_report(self):
        try:
            return read_json(self.runtime_dir / f"graph-{self.graph_attempt}.json")
        except (OSError, ValueError):
            return {}

    def fail_playback(self, message, report=None):
        report = self.graph_report() if report is None else report
        self.fail(report.get("message") or message, rate_error=report.get("rate_error") is True)

    def process_event(self, event):
        name = event.get("event")
        if name == "client-message":
            arguments = event.get("args", [])
            if arguments and arguments[0] == "open-svpflow-bridge-ready":
                self.bridge_ready.set()
            elif arguments == ["open-svpflow-probing-viewport"]:
                self.probing_viewport = True
            elif arguments == ["open-svpflow-viewport-failed"]:
                self.fail_playback("The interpolation filter could not start.")
            elif arguments and arguments[0] == "open-svpflow-before-load":
                try:
                    self.configure_file()
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    self.fail(str(error))
                if not self.error:
                    self.client.command(["script-message", "open-svpflow-continue"])
            elif self.active and len(arguments) == 2 and arguments[0] == "open-svpflow-playback-sample":
                with contextlib.suppress(ValueError, TypeError):
                    sample = json.loads(arguments[1])
                    if type(sample) is dict:
                        self.quality.sample(sample, time.monotonic())
        elif name == "start-file":
            self.loading_entry = event["playlist_entry_id"]
        elif name == "playback-restart":
            if self.probing_viewport:
                self.probing_viewport = False
                return
            report = self.graph_report()
            target = finite_number(report.get("target_fps"))
            if not self._filter_ok():
                self.fail_playback("The interpolation filter failed or was disabled", report)
            elif not target or target <= 0 or not self.client.get("video-out-params"):
                self.fail_playback("The selected source did not produce an interpolated video frame.", report)
            else:
                self.quality.current.update(interpolation=True, target_fps=target)
                self.confirmed_entry = self.loading_entry
                self.active = True
                self.ever_started = True
                self.ready.set()
        elif name == "seek":
            self.quality.reset()
        elif name == "end-file":
            was_active = self.active
            self.active = False
            if event.get("reason") == "error":
                self.fail_playback("mpv could not decode or process the selected movie")
            elif event.get("reason") == "eof" and not was_active:
                self.fail_playback("The selected source did not produce an interpolated video frame.")
            elif event.get("reason") == "quit":
                self.stopped.set()
                self.ready.set()
        elif name == "shutdown":
            self.active = False
            self.stopped.set()
            self.ready.set()
        elif name == "connection-closed":
            if not self.stopped.is_set():
                self.fail("The mpv control channel closed unexpectedly; see playback.log")
            self.stopped.set()
            self.ready.set()
        elif name == "property-change":
            property_name = event.get("name")
            if property_name == "idle-active" and event.get("data") is True and self.ever_started:
                self.stopped.set()
            elif property_name == "vf" and self.active and not self._filter_ok():
                self.fail("The interpolation filter failed or was disabled")
            elif property_name == "dropped-files" and type(event.get("data")) is dict:
                data = event["data"]
                stamp = data.get("time")
                files = data.get("files", [])
                if stamp == self.last_drop or not files:
                    return
                self.last_drop = stamp
                extensions = self.client.get("options/sub-auto-exts", []) or []
                if type(extensions) is str:
                    extensions = extensions.split(",")
                subtitles = [file for file in files if type(file) is str and Path(file).suffix.lstrip(".").lower() in extensions]
                videos = [file for file in files if file not in subtitles]
                for file in subtitles:
                    self.client.command(["sub-add", file, "select"])
                if videos:
                    try:
                        self.drop_jobs.put_nowait(videos)
                    except queue.Full:
                        self.fail("Too many file-open requests are pending")
        elif name == "log-message" and event.get("level") in ("error", "fatal"):
            prefix = event.get("prefix", "")
            if "vapoursynth" in prefix:
                self.fail_playback("The interpolation filter could not process this video.")

    def _events(self):
        while not self.stopped.is_set():
            try:
                event = self.client.events.get(timeout=0.25)
            except queue.Empty:
                if self.process is not None and self.process.poll() is not None:
                    self.stopped.set()
                    self.ready.set()
                continue
            try:
                self.process_event(event)
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                self.fail(str(error))

    def _drops(self):
        while not self.stopped.is_set():
            try:
                files = self.drop_jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                result = self.open(files)
                if result.get("status") == "ask":
                    self.notify(self.bundle_root)
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                self.fail(str(error))

    def open(self, files, action="auto"):
        with self.lock:
            document = self._document()
            self.after_start = document["preferences"]["after_start"]
            if action == "cancel":
                self.playlist.open([], action, document["preferences"]["new_files"])
                return self.snapshot()
            files = media_files(files)
            if not files:
                raise ValueError("Choose at least one movie")
            if self.client is None:
                self.file_title = playback_title(files[0])
                try:
                    self.start()
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    self.fail(str(error))
                    return self.snapshot()
            selected = document["preferences"]["new_files"] if action == "auto" else action
            if not self.active or selected == "replace":
                self.ready.clear()
            result = self.playlist.open(files, action, document["preferences"]["new_files"])
            if result == "ask":
                return self.snapshot()
            if result == "started":
                deadline = time.monotonic() + 30
                expected = self.playlist.requested_entry
                while True:
                    self.ready.clear()
                    if self.error or self.stopped.is_set():
                        return self.snapshot()
                    if expected is not None and self.confirmed_entry >= expected:
                        break
                    if expected is None or not self.ready.wait(max(0, deadline - time.monotonic())):
                        self.fail("The movie did not start in time; see playback.log")
                        return self.snapshot()
            return self.snapshot(result)

    def snapshot(self, status=None):
        if self.failure is not None:
            return self.failure
        active = self.active
        result = {"ok": True, "status": status or ("started" if active else "idle"), "after_start": self.after_start}
        if active:
            result["playback"] = self.quality.snapshot()
        if self.playlist.pending:
            result.update(status="ask", files=list(self.playlist.pending))
        return result

    def fail(self, message, *, rate_error=False):
        with self.failure_lock:
            if self.failure is not None:
                return
            self.active = False
            self.failure = {"ok": False, "status": "error", "message": str(message), "after_start": self.after_start,
                            "failure_id": uuid4().hex, "file_title": self.file_title, "rate_error": rate_error}
            self.error = self.failure["message"]
        with contextlib.suppress(OSError, ValueError):
            atomic_json(self.state_dir / "event.json", self.failure)
        if self.client is not None:
            with contextlib.suppress(OSError, ValueError, RuntimeError, TimeoutError):
                self.client.command(["quit"], timeout=1)
        self.stopped.set()
        self.ready.set()
        self.notify(self.bundle_root)

    def close(self):
        self.stopped.set()
        if self.client is not None:
            with contextlib.suppress(OSError, RuntimeError, TimeoutError):
                self.client.command(["quit"], timeout=2)
            self.client.close()
        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        if self.log is not None:
            self.log.close()


class ControlServer:
    def __init__(self, state_dir, bundle_root, worker_factory=PlaybackSession):
        self.state_dir = private_directory(state_dir)
        self.bundle_root = Path(bundle_root).resolve()
        self.runtime_dir = Path(tempfile.mkdtemp(prefix="osvp-", dir="/private/tmp"))
        self.runtime_dir.chmod(0o700)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.runtime_dir / "control.sock"))
        (self.runtime_dir / "control.sock").chmod(0o600)
        self.socket.listen(8)
        self.socket.settimeout(0.25)
        self.token = secrets.token_hex(32)
        self.closed = threading.Event()
        self.worker = worker_factory(self.state_dir, self.bundle_root, self.runtime_dir)
        self.record = {"socket": str(self.runtime_dir / "control.sock"), "token": self.token, "pid": os.getpid()}
        atomic_json(self.state_dir / "session.json", self.record)
        self.created = time.monotonic()
        self.used = False
        self.mutations = threading.Lock()
        self.clients = set()
        self.clients_lock = threading.Lock()

    def dispatch(self, request):
        if type(request) is not dict or not hmac.compare_digest(str(request.get("token", "")), self.token):
            return {"ok": False, "message": "Playback session authentication failed"}
        command = request.get("command")
        if command == "ping":
            return {"ok": True, "protocol": 1, "pid": os.getpid()}
        if command == "status":
            return self.worker.snapshot()
        if command == "open":
            value = request.get("payload", {})
            if type(value) is not dict or not set(value) <= {"files", "action"}:
                raise ValueError("Invalid playback request")
            with self.mutations:
                if self.closed.is_set() or self.worker.stopped.is_set():
                    return {"ok": False, "status": "error", "message": "The playback session has ended"}
                result = self.worker.open(value.get("files", []), value.get("action", "auto"))
                self.used = self.used or result.get("status") in ("started", "queued", "ask")
                if result.get("status") == "ask":
                    return result
                event = self.state_dir / "event.json"
                if result.get("ok") and event.exists():
                    event.unlink()
                return result
        return {"ok": False, "message": "Unsupported playback request"}

    def handle_client(self, peer):
        try:
            with peer:
                peer.settimeout(35)
                try:
                    with peer.makefile("rb") as stream:
                        line = stream.readline(MAX_MESSAGE + 1)
                    if not line or len(line) > MAX_MESSAGE:
                        raise ValueError("Playback request exceeds its size limit")
                    result = self.dispatch(json.loads(line))
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    result = {"ok": False, "status": "error", "message": str(error)}
                try:
                    peer.sendall((json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n").encode())
                except OSError:
                    pass
        finally:
            with self.clients_lock:
                self.clients.discard(threading.current_thread())

    def serve(self):
        try:
            while not self.closed.is_set() and not self.worker.stopped.is_set():
                with self.clients_lock:
                    if not self.used and not self.clients and time.monotonic() - self.created > 20:
                        break
                try:
                    peer, _ = self.socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                client = threading.Thread(target=self.handle_client, args=(peer,), daemon=True)
                with self.clients_lock:
                    self.clients.add(client)
                client.start()
        finally:
            self.close()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        self.socket.close()
        self.worker.close()
        try:
            if read_json(self.state_dir / "session.json").get("token") == self.token:
                (self.state_dir / "session.json").unlink()
        except (OSError, ValueError):
            pass
        shutil.rmtree(self.runtime_dir)


def serve(state_dir, bundle_root):
    state_dir = private_directory(state_dir)
    descriptor = os.open(state_dir / "worker.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        ControlServer(state_dir, bundle_root).serve()


def worker_command(state_dir, bundle_root):
    command = [sys.executable, "-B", str(Path(__file__).with_name("application.py")), "serve",
               "--state-dir", str(Path(state_dir).resolve()), "--bundle-root", str(Path(bundle_root).resolve())]
    app = gui_bundle(bundle_root)
    if sys.platform != "darwin" or app is None:
        return command

    worker = app / "Contents/Helpers/Open SVPflow Playback.app"
    if not worker.is_dir():
        raise RuntimeError("The application is missing its playback controller.")
    return ["/usr/bin/open", "-n", "-g", "-j", "-W", "--stdin", "/dev/null", "-a", str(worker), "--args", *command]


def open_files(state_dir, bundle_root, files, action="auto"):
    if action not in ("auto", "append", "replace", "cancel"):
        raise ValueError("Unsupported new-file action")
    state_dir = private_directory(state_dir)
    if action == "cancel":
        existing = connect_existing(state_dir)
        return rpc(existing, "open", {"files": [], "action": "cancel"}) if existing else {"ok": True, "status": "idle"}
    files = media_files(files)
    if not files:
        raise ValueError("Choose at least one movie")
    descriptor = os.open(state_dir / "startup.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        existing = connect_existing(state_dir)
        if existing is None:
            command = worker_command(state_dir, bundle_root)
            log_descriptor = os.open(state_dir / "session.log", os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(log_descriptor, "wb") as output:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output, close_fds=True, start_new_session=True)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                existing = connect_existing(state_dir)
                if existing:
                    break
                if child.poll() is not None:
                    raise RuntimeError("Unable to start the playback session; see session.log")
                time.sleep(0.025)
            if existing is None:
                raise TimeoutError("The playback session did not become ready")
    return rpc(existing, "open", {"files": files, "action": action})
