import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "player"))
try:
    import session
except ImportError:
    session = None

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(session, "The independent playback session is missing")
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.addCleanup(self.directory.cleanup)
        self.files = []
        for name in ("first movie.mkv", "second ü.mkv", "third.mkv"):
            path = self.root / name
            path.write_bytes(b"fixture")
            self.files.append(str(path))

    def test_queue_append_replace_and_cancel_preserve_current_playback(self):
        commands = []
        active = {"value": False}
        def send(command):
            commands.append(command)
            if command[0] == "loadfile":
                active["value"] = True
        queue = session.PlaylistController(send, lambda: active["value"])
        self.assertEqual(queue.open(self.files[:2], "append", "append"), "started")
        self.assertEqual(commands, [["loadfile", self.files[0], "replace"], ["loadfile", self.files[1], "append"]])
        self.assertEqual(queue.open(self.files[2:], "auto", "ask"), "ask")
        self.assertEqual(queue.pending, self.files[2:])
        before = list(commands)
        self.assertEqual(queue.open([], "cancel", "ask"), "queued")
        self.assertEqual(commands, before)
        self.assertEqual(queue.pending, [])
        self.assertEqual(queue.open(self.files[2:], "replace", "append"), "started")
        self.assertEqual(commands[-1], ["loadfile", self.files[2], "replace"])

    def test_new_file_validation_rejects_a_batch_without_modifying_playlist(self):
        commands = []
        queue = session.PlaylistController(commands.append, lambda: True)
        with self.assertRaisesRegex(ValueError, "readable"):
            queue.open([self.files[0], str(self.root / "missing.mkv")], "append", "append")
        self.assertEqual(commands, [])

    def test_literal_media_names_reach_mpv_without_shell_expansion(self):
        path = self.root / "a `touch nope` $(whoami).mkv"
        path.write_bytes(b"fixture")
        commands = []
        queue = session.PlaylistController(commands.append, lambda: False)
        queue.open([str(path)], "auto", "append")
        self.assertEqual(commands, [["loadfile", str(path), "replace"]])
        self.assertFalse((self.root / "nope").exists())

    def test_ipc_matches_response_ids_and_retains_interleaved_events(self):
        address = str(self.root / "mpv.sock")
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(address)
        server.listen(1)
        observed = []
        def serve():
            peer, _ = server.accept()
            with peer, peer.makefile("rwb") as stream:
                request = json.loads(stream.readline())
                observed.append(request["command"])
                stream.write(b'{"event":"file-loaded"}\n')
                stream.write((json.dumps({"request_id": request["request_id"], "error": "success", "data": {"w": 320}}) + "\n").encode())
                stream.flush()
        thread = threading.Thread(target=serve)
        thread.start()
        client = session.MpvClient(address)
        self.addCleanup(client.close)
        self.assertEqual(client.command(["get_property", "video-out-params"]), {"w": 320})
        self.assertEqual(client.events.get(timeout=1), {"event": "file-loaded"})
        self.assertEqual(observed, [["get_property", "video-out-params"]])
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_private_state_rejects_symlink_and_creates_owner_only_files(self):
        state = self.root / "state"
        session.private_directory(state)
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        session.atomic_json(state / "event.json", {"status": "error", "message": "failed"})
        self.assertEqual((state / "event.json").stat().st_mode & 0o777, 0o600)
        link = self.root / "linked"
        link.symlink_to(state, target_is_directory=True)
        with self.assertRaises(ValueError):
            session.private_directory(link)

    def test_stale_rendezvous_is_not_a_live_session_and_does_not_signal_pid(self):
        state = self.root / "state"
        session.private_directory(state)
        session.atomic_json(state / "session.json", {"socket": str(self.root / "missing.sock"), "token": "not-live", "pid": 1})
        self.assertIsNone(session.connect_existing(state))
        self.assertEqual(session.status(state)["status"], "idle")
        self.assertFalse((state / "session.json").exists())

    def test_timed_out_live_probe_does_not_remove_rendezvous_or_spawn_a_worker(self):
        state = self.root / "state"
        session.private_directory(state)
        record = {"socket": str(self.root / "control.sock"), "token": "busy", "pid": 1}
        session.atomic_json(state / "session.json", record)
        with patch.object(session, "rpc", side_effect=TimeoutError("busy")), patch.object(session.subprocess, "Popen") as process:
            with self.assertRaises(TimeoutError):
                session.status(state)
            with self.assertRaises(TimeoutError):
                session.open_files(state, self.root, self.files[:1])
        self.assertEqual(session.read_json(state / "session.json"), record)
        process.assert_not_called()

    def test_packaged_playback_starts_the_controller_through_launch_services(self):
        contents = self.root / "Open SVPflow.app" / "Contents"
        resources = contents / "Resources"
        resources.mkdir(parents=True)
        host = contents / "Helpers" / "Open SVPflow Playback.app"
        host.mkdir(parents=True)
        record = {"pid": 12345}
        response = {"ok": True, "status": "started"}
        with (
            patch.object(session.sys, "platform", "darwin"),
            patch.object(session, "connect_existing", side_effect=[None, record]),
            patch.object(session, "rpc", return_value=response),
            patch.object(session.subprocess, "Popen") as process,
        ):
            self.assertEqual(session.open_files(self.root / "state", resources, self.files[:1]), response)
        command = process.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/open")
        self.assertEqual(command[command.index("-a") + 1], str(host))
        arguments = command[command.index("--args") + 1:]
        self.assertEqual(arguments[0:2], [sys.executable, "-B"])
        self.assertEqual(arguments[3:], ["serve", "--state-dir", str(self.root / "state"), "--bundle-root", str(resources)])

if __name__ == "__main__":
    unittest.main()

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.addCleanup(self.directory.cleanup)
        self.assertTrue(hasattr(session, "PlaybackSession"), "Playback ownership is missing")

    def test_lua_load_bridge_configures_before_continuing_the_file(self):
        effects = []
        class Client:
            def command(self, command, **kwargs):
                effects.append(command)
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.client = Client()
        owner.configure_file = lambda: effects.append("configured")
        owner.process_event({"event": "client-message", "args": ["open-svpflow-before-load"]})
        self.assertEqual(effects, ["configured", ["script-message", "open-svpflow-continue"]])

    def test_each_new_file_uses_saved_settings_without_touching_unrelated_filters(self):
        import settings_api
        import policy
        plugins = self.root / "lib"
        plugins.mkdir()
        for name in ("libsvpflow1_vs.dylib", "libsvpflow2_vs.dylib"):
            (plugins / name).write_bytes(b"fixture")
        player = self.root / "player"
        player.mkdir()
        (player / "graph.vpy").write_text("fixture")
        movie = self.root / "video ü.mkv"
        movie.write_bytes(b"fixture")
        commands = []
        class Client:
            def get(self, name, default=None):
                return str(movie) if name == "path" else [{"label": "user-filter"}, {"label": "svp-input"}, {"label": "svp-interpolate"}]
            def command(self, command, **kwargs):
                commands.append(command)
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.client = Client()
        owner.runtime = {"mpv": "/external/mpv"}
        document = settings_api.default_document()
        settings_api.save_document(owner.state_dir / "settings.json", document)
        owner.configure_file()
        first = next(command[2] for command in commands if command[:2] == ["vf", "add"])
        self.assertIn("@svp-interpolate:vapoursynth=", first)
        self.assertNotIn(["vf", "remove", "@user-filter"], commands)
        document["playback"].update(rate="fixed", fps="72")
        before = list(commands)
        settings_api.save_document(owner.state_dir / "settings.json", document)
        self.assertEqual(commands, before)
        owner.configure_file()
        updated = commands[len(before):]
        self.assertEqual(updated[:2], [["vf", "remove", "@svp-input"], ["vf", "remove", "@svp-interpolate"]])
        self.assertNotIn(["vf", "remove", "@user-filter"], updated)
        second = next(command[2] for command in updated if command[:2] == ["vf", "add"])
        self.assertNotEqual(second, first)
        self.assertEqual(owner.graph_attempt, 2)

    def test_playback_log_cannot_overwrite_a_symlink_target(self):
        target = self.root / "preserved.txt"
        target.write_text("preserved")
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        (owner.state_dir / "playback.log").symlink_to(target)
        runtime = {"ready": True, "runtime": {"mpv": sys.executable, "vsscript": None}}
        with patch("dependencies.inspect_environment", return_value=runtime), patch("launch.runtime_environment", return_value={}), patch.object(session.subprocess, "Popen") as process:
            with self.assertRaises((OSError, RuntimeError)):
                owner.start()
        process.assert_not_called()
        self.assertEqual(target.read_text(), "preserved")

    def test_missing_or_disabled_interpolation_is_fatal_before_started(self):
        class Client:
            def get(self, name, default=None):
                return [{"name": "vapoursynth", "label": "svp-interpolate", "enabled": False}] if name == "vf" else default
            def command(self, command, **kwargs):
                return None
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.client = Client()
        owner.process_event({"event": "playback-restart"})
        self.assertFalse(owner.active)
        self.assertIn("interpolation", owner.error.lower())
        self.assertTrue(owner.ready.is_set())

    def test_viewport_probe_waits_for_the_first_interpolated_frame(self):
        class Client:
            def get(self, name, default=None):
                return {
                    "vf": [{"label": "svp-interpolate", "name": "vapoursynth", "enabled": True}],
                    "video-out-params": {"w": 2560, "h": 1440},
                }.get(name, default)

            def command(self, command, **kwargs):
                return None

        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.client = Client()
        owner.process_event({"event": "client-message", "args": ["open-svpflow-probing-viewport"]})
        session.atomic_json(owner.runtime_dir / "graph-0.json", {"target_fps": 72})
        owner.process_event({"event": "playback-restart"})
        self.assertIsNone(owner.error)
        self.assertFalse(owner.active)
        self.assertFalse(owner.ready.is_set())

        owner.process_event({"event": "playback-restart"})
        self.assertTrue(owner.active)
        self.assertTrue(owner.ready.is_set())

    def test_active_filter_label_and_video_are_insufficient_without_graph_confirmation(self):
        commands = []
        class Client:
            def get(self, name, default=None):
                return {
                    "vf": [{"name": "vapoursynth", "label": "svp-interpolate", "enabled": True}],
                    "video-out-params": {"w": 320, "h": 192},
                }.get(name, default)
            def command(self, command, **kwargs):
                commands.append(command)
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.client = Client()
        owner.process_event({"event": "playback-restart"})
        self.assertFalse(owner.active)
        self.assertEqual(owner.snapshot()["status"], "error")
        self.assertIn(["quit"], commands)

    def test_persisting_the_failure_cannot_prevent_mpv_shutdown(self):
        commands = []
        class Client:
            def command(self, command, **kwargs):
                commands.append(command)
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.client = Client()
        (owner.state_dir / "event.json").symlink_to(self.root / "unavailable")
        owner.fail("The interpolation filter failed")
        self.assertEqual(owner.snapshot()["message"], "The interpolation filter failed")
        self.assertTrue(owner.stopped.is_set())
        self.assertTrue(owner.ready.is_set())
        self.assertEqual(commands, [["quit"]])

    def test_normal_mpv_shutdown_does_not_reopen_the_gui(self):
        notified = []
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: notified.append(root))
        owner.active = True
        owner.process_event({"event": "shutdown"})
        self.assertTrue(owner.stopped.is_set())
        self.assertFalse(owner.active)
        self.assertEqual(notified, [])

    def test_mpv_end_file_quit_then_ipc_close_is_quiet(self):
        notified = []
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=notified.append)
        owner.active = True
        owner.process_event({"event": "end-file", "reason": "quit"})
        owner.process_event({"event": "connection-closed"})
        self.assertTrue(owner.stopped.is_set())
        self.assertEqual(owner.snapshot()["status"], "idle")
        self.assertEqual(notified, [])

    def report_playback(self, owner, timestamp, **sample):
        report = {"title": "Movie title", "filename": "/tmp/a film.mkv", "paused": False, "seeking": False,
                  "source_fps": 24.0, "filtered_fps": 48.0, "refresh": 120.0, "speed": 1.0,
                  "output_drops": 120, "decoder_drops": 10, "avsync": -0.012, **sample}
        with patch.object(session.time, "monotonic", return_value=timestamp):
            owner.process_event({"event": "client-message", "args": ["open-svpflow-playback-sample", json.dumps(report)]})
            return owner.snapshot().get("playback", {})

    def test_quality_uses_recent_deltas_and_clears_old_drops(self):
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.active = True
        start = self.report_playback(owner, 0)
        self.assertEqual(start.get("quality"), "checking")
        self.assertEqual(start["title"], "Movie title")
        self.assertIsNone(start["output_drops"])
        self.report_playback(owner, 2)
        smooth = self.report_playback(owner, 4)
        self.assertEqual(smooth["quality"], "excellent")
        self.assertEqual(smooth["output_drops"], 0)
        self.assertEqual(smooth["window_seconds"], 4)
        self.assertEqual(smooth["av_offset_ms"], -12)
        occasional = self.report_playback(owner, 6, output_drops=122)
        self.assertEqual(occasional["quality"], "good")
        self.assertEqual(occasional["output_drops"], 2)
        recurring = self.report_playback(owner, 8, output_drops=129)
        self.assertEqual(recurring["quality"], "attention")
        for timestamp in range(10, 40, 2):
            self.report_playback(owner, timestamp, output_drops=129)
        recovered = self.report_playback(owner, 40, output_drops=129)
        self.assertEqual(recovered["quality"], "excellent")
        self.assertEqual(recovered["window_seconds"], 30)

    def test_quality_excludes_pause_and_seek_and_does_not_fabricate_missing_counters(self):
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.active = True
        self.report_playback(owner, 0)
        self.report_playback(owner, 2)
        paused = self.report_playback(owner, 4, paused=True, output_drops=225)
        self.assertTrue(paused.get("paused"))
        self.assertEqual(paused["quality"], "checking")
        self.assertEqual(self.report_playback(owner, 6, output_drops=230)["quality"], "checking")
        owner.process_event({"event": "seek"})
        self.report_playback(owner, 8, output_drops=300, seeking=True)
        self.report_playback(owner, 10, output_drops=None, decoder_drops=None)
        self.report_playback(owner, 12, output_drops=None, decoder_drops=None)
        resumed = self.report_playback(owner, 14, output_drops=None, decoder_drops=None)
        self.assertEqual(resumed["quality"], "checking")
        self.assertIsNone(resumed["output_drops"])
        self.assertIsNone(resumed["decoder_drops"])

    def test_quality_distinguishes_output_beyond_display_refresh_and_counter_reset(self):
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.active = True
        self.report_playback(owner, 0, refresh=30)
        self.report_playback(owner, 2, refresh=30, output_drops=121)
        mismatch = self.report_playback(owner, 4, refresh=30, output_drops=130)
        self.assertEqual(mismatch.get("quality"), "attention")
        self.assertTrue(mismatch["display_limited"])
        reset = self.report_playback(owner, 6, refresh=30, output_drops=0)
        self.assertEqual(reset["quality"], "checking")
        self.assertIsNone(reset["output_drops"])

    def test_playback_metadata_distinguishes_confirmed_target_from_unverified_samples(self):
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.active = True
        owner.quality = session.PlaybackQuality("/tmp/Part #1?.mkv", "auto")
        initial = owner.snapshot()["playback"]
        self.assertEqual(initial["title"], "Part #1?.mkv")
        self.assertIsNone(initial["interpolation"])
        unverified = self.report_playback(owner, 0, refresh=None, filtered_fps=24.0)
        self.assertIsNone(unverified["interpolation"])
        self.assertIsNone(unverified["target_fps"])

        owner.quality = session.PlaybackQuality("Movie title", "48")
        confirmed = self.report_playback(owner, 1, filtered_fps=47.98)
        self.assertIs(confirmed["interpolation"], True)
        self.assertEqual(confirmed["target_fps"], 48)

    def test_lost_control_channel_is_fatal_while_player_is_still_running(self):
        from types import SimpleNamespace
        notified = []
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=notified.append)
        owner.process = SimpleNamespace(poll=lambda: None)
        owner.active = True
        owner.process_event({"event": "connection-closed"})
        self.assertFalse(owner.active)
        self.assertIn("control channel", owner.error)
        self.assertEqual(notified, [self.root])

    def test_native_subtitle_drop_does_not_mutate_playlist(self):
        commands = []
        class Client:
            def get(self, name, default=None):
                return ["srt", "ass"] if name == "options/sub-auto-exts" else default
            def command(self, command, **kwargs):
                commands.append(command)
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.client = Client()
        owner.process_event({"event": "property-change", "name": "dropped-files", "data": {"files": ["/tmp/captions.srt"], "time": 1}})
        self.assertEqual(commands, [["sub-add", "/tmp/captions.srt", "select"]])

    def test_replacement_waits_for_its_own_start_instead_of_reusing_previous_ready(self):
        import time
        class Client:
            def get(self, name, default=None):
                return {"video-out-params": {"w": 320}, "vf": [{"label": "svp-interpolate", "name": "vapoursynth"}]}.get(name, default)
            def command(self, command, **kwargs):
                return {"playlist_entry_id": 2} if command[0] == "loadfile" else None
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: None)
        owner.client = Client()
        session.atomic_json(owner.runtime_dir / "graph-0.json", {"target_fps": 48})
        owner.active = True
        owner.process_event({"event": "start-file", "playlist_entry_id": 1})
        owner.ready.set()
        owner._document = lambda: {"preferences": {"after_start": "quit_app", "new_files": "append"}}
        movie = self.root / "replacement.mkv"
        movie.write_bytes(b"fixture")
        results = []
        thread = threading.Thread(target=lambda: results.append(owner.open([str(movie)], "replace")))
        thread.start()
        time.sleep(0.05)
        waiting = thread.is_alive()
        owner.process_event({"event": "playback-restart"})
        thread.join(0.1)
        ignored_old_restart = thread.is_alive()
        owner.process_event({"event": "start-file", "playlist_entry_id": 2})
        owner.process_event({"event": "playback-restart"})
        thread.join(2)
        self.assertTrue(waiting, "Replacement was reported ready before its first frame")
        self.assertTrue(ignored_old_restart, "Playback restart of the previous file confirmed the replacement")
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "started")

    def test_fatal_error_is_persisted_after_worker_exit_for_reopened_gui(self):
        notified = []
        owner = session.PlaybackSession(self.root / "state", self.root, self.root / "runtime", notify=lambda root: notified.append(root))
        owner.fail("Unable to load the interpolation graph")
        self.assertEqual(session.status(self.root / "state")["message"], "Unable to load the interpolation graph")
        self.assertEqual(session.status(self.root / "state")["status"], "idle")
        self.assertEqual(notified, [self.root])

class ControlServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.addCleanup(self.directory.cleanup)
        self.assertTrue(hasattr(session, "ControlServer"), "The independent control server is missing")

    def test_authenticated_unix_rpc_reconnects_and_rejects_unknown_commands(self):
        class Worker:
            def __init__(self, *args):
                self.stopped = threading.Event()
                self.calls = []
            def snapshot(self):
                return {"ok": True, "status": "started", "after_start": "quit_app"}
            def open(self, files, action):
                self.calls.append((files, action))
                return {"ok": True, "status": "queued", "after_start": "quit_app"}
            def close(self):
                self.stopped.set()
        control = session.ControlServer(self.root / "state", self.root, worker_factory=Worker)
        self.addCleanup(control.close)
        thread = threading.Thread(target=control.serve)
        thread.start()
        self.addCleanup(lambda: thread.join(2))
        record = session.connect_existing(self.root / "state")
        self.assertIsNotNone(record)
        self.assertEqual(session.rpc(record, "status")["status"], "started")
        self.assertEqual(session.rpc(record, "open", {"files": ["movie"], "action": "append"})["status"], "queued")
        self.assertEqual(control.worker.calls, [(["movie"], "append")])
        self.assertFalse(session.rpc(record, "exec", {"command": ["touch", "bad"]})["ok"])
        wrong = dict(record, token="incorrect")
        self.assertFalse(session.rpc(wrong, "open", {"files": ["bad"]})["ok"])
        self.assertEqual(control.worker.calls, [(["movie"], "append")])
        control.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse((self.root / "state" / "session.json").exists())

    def test_rejected_start_does_not_disable_idle_worker_cleanup(self):
        class Worker:
            def __init__(self, *args):
                self.stopped = threading.Event()
            def open(self, files, action):
                raise ValueError("Dependencies are missing")
            def close(self):
                self.stopped.set()
        control = session.ControlServer(self.root / "state", self.root, worker_factory=Worker)
        self.addCleanup(control.close)
        with self.assertRaisesRegex(ValueError, "Dependencies"):
            control.dispatch({"token": control.token, "command": "open", "payload": {"files": ["fixture"]}})
        self.assertFalse(control.used)

    def test_slow_open_keeps_live_worker_discoverable_for_status_and_second_open(self):
        class Worker:
            def __init__(self, *args):
                self.stopped = threading.Event()
                self.entered = threading.Event()
                self.release = threading.Event()
                self.calls = []
                self.active = False
                self.overlap = False
                self.opening = False
            def snapshot(self):
                return {"ok": True, "status": "started" if self.active else "idle"}
            def open(self, files, action):
                self.overlap = self.overlap or self.opening
                self.opening = True
                self.calls.append((files, action))
                if len(self.calls) == 1:
                    self.entered.set()
                    self.release.wait(3)
                self.active = True
                self.opening = False
                return {"ok": True, "status": "started" if len(self.calls) == 1 else "queued"}
            def close(self):
                self.release.set()
                self.stopped.set()
        control = session.ControlServer(self.root / "state", self.root, worker_factory=Worker)
        server = threading.Thread(target=control.serve)
        server.start()
        responses = []
        first = threading.Thread(target=lambda: responses.append(session.rpc(control.record, "open", {"files": ["first"], "action": "append"})))
        second = None
        try:
            first.start()
            self.assertTrue(control.worker.entered.wait(1))
            started = time.monotonic()
            self.assertTrue(session.status(self.root / "state")["ok"])
            self.assertTrue((self.root / "state" / "session.json").exists(), "A slow open erased the live rendezvous")
            self.assertLess(time.monotonic() - started, 0.75)
            movie = self.root / "second movie.mkv"
            movie.write_bytes(b"fixture")
            with patch.object(session.subprocess, "Popen") as process:
                second = threading.Thread(target=lambda: responses.append(session.open_files(self.root / "state", self.root, [str(movie)], "append")))
                second.start()
                self.assertEqual(session.connect_existing(self.root / "state"), control.record)
                time.sleep(0.05)
                self.assertEqual(len(control.worker.calls), 1)
                control.worker.release.set()
                first.join(2)
                second.join(2)
                process.assert_not_called()
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertFalse(control.worker.overlap)
            self.assertEqual([value[0] for value in control.worker.calls], [["first"], [str(movie)]])
            self.assertEqual(sorted(value["status"] for value in responses), ["queued", "started"])
            self.assertEqual(session.connect_existing(self.root / "state"), control.record)
        finally:
            control.worker.release.set()
            first.join(2)
            if second is not None:
                second.join(2)
            control.close()
            server.join(2)

    def test_worker_survives_transient_clients_and_reconnects_to_the_same_process(self):
        source = """
import signal, sys, threading
sys.path.insert(0, sys.argv[1])
import session
class Worker:
    def __init__(self, *args):
        self.stopped = threading.Event()
    def snapshot(self):
        return {"ok": True, "status": "started", "after_start": "quit_app"}
    def open(self, files, action):
        return self.snapshot()
    def close(self):
        self.stopped.set()
control = session.ControlServer(sys.argv[2], sys.argv[3], worker_factory=Worker)
signal.signal(signal.SIGTERM, lambda *args: control.close())
control.serve()
"""
        state = self.root / "state"
        child = subprocess.Popen([sys.executable, "-B", "-c", source, str(ROOT / "player"), str(state), str(ROOT)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 3
            record = None
            while time.monotonic() < deadline and child.poll() is None:
                record = session.connect_existing(state)
                if record:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(record)
            arguments = [sys.executable, "-B", str(ROOT / "player" / "application.py"), "status", "--state-dir", str(state), "--bundle-root", str(ROOT)]
            for _ in range(2):
                response = subprocess.run(arguments, input="{}", capture_output=True, text=True, timeout=3)
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertEqual(json.loads(response.stdout)["status"], "started")
                self.assertEqual(session.connect_existing(state)["pid"], child.pid)
                self.assertIsNone(child.poll())
        finally:
            child.terminate()
            _, error = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 0, error.decode())
        self.assertFalse((state / "session.json").exists())
