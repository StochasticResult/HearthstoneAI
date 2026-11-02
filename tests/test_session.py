import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from navigation import login_intro_backdrop
from session import (validate_config, SessionError, lock_session, session_directory,
                     EventLog, emit, guard_event, drain_guards, launch_hearthstone, BATTLENET_EXEC, scene_ready, navigation_click,
                     focus_hearthstone, GAME_EXEC)


class SessionTests(unittest.TestCase):
    def test_authenticated_login_intro_only_clicks_empty_backdrop_before_game(self):
        status = {'scene': {'mode': 2, 'sceneLoaded': True, 'transitioning': False},
                  'game_type': 0, 'format': 0, 'popup': True, 'shop_open': False,
                  'game_menu': False, 'mulligan_waiting': False}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'Hearthstone_today'
            directory.mkdir()
            log = directory / 'Login.log'
            logs = ('I 00:31:18 We are now logged in, stopping processing challenges\n'
                    'I 00:31:27 Initial download done, Waiting Intro popup...\n')
            log.write_text(logs)
            self.assertEqual(login_intro_backdrop(status, 0, root), (180,155))
            self.assertIsNone(login_intro_backdrop(status, log.stat().st_mtime + 30, root))
            for key, value in (('shop_open', True), ('game_menu', True), ('popup', False),
                               ('game_type', 7), ('format', 2), ('mulligan_waiting', True),
                               ('scene', {'mode': 4, 'sceneLoaded': True, 'transitioning': False})):
                self.assertIsNone(login_intro_backdrop({**status, key: value}, 0, root))
            (directory / 'Power.log').touch()
            self.assertIsNone(login_intro_backdrop(status, 0, root))
            (directory / 'Power.log').unlink()
            log.write_text(logs.splitlines()[-1])
            self.assertIsNone(login_intro_backdrop(status, 0, root))
            log.write_text(logs)
            (root / 'Hearthstone_ambiguous').mkdir()
            self.assertIsNone(login_intro_backdrop(status, 0, root))

    def test_foreground_request_is_only_for_the_fixed_game_bundle(self):
        with patch('session.subprocess.run') as run:
            focus_hearthstone()
            self.assertEqual(run.call_args.args[0], ['/usr/bin/open','-a',str(Path(GAME_EXEC).parents[2])])
            self.assertTrue(run.call_args.kwargs['check'])
            self.assertEqual(run.call_args.kwargs['timeout'],3)

    def test_navigation_retries_only_a_pre_input_foreground_rejection(self):
        geometry = SimpleNamespace(stdout='{"x":0,"y":33,"width":1470,"height":846}')
        rejected = SimpleNamespace(returncode=1, stderr='Hearthstone is not the frontmost app\n')
        success = SimpleNamespace(returncode=0, stderr='')
        with patch('session.subprocess.run', side_effect=[geometry, rejected, success]) as run, \
             patch('session.time.sleep'):
            navigation_click((735,310))
            self.assertEqual(run.call_count,3)
        with patch('session.subprocess.run', side_effect=[geometry, success]) as run:
            navigation_click((735,310))
            self.assertEqual(run.call_count,2)
        with patch('session.subprocess.run', side_effect=[geometry, SimpleNamespace(returncode=1, stderr='Cannot create mouse event')]) as run:
            with self.assertRaisesRegex(SessionError,'Cannot create mouse event'):
                navigation_click((735,310))
            self.assertEqual(run.call_count,2)

    def test_navigation_foreground_wait_is_bounded(self):
        geometry = SimpleNamespace(stdout='{"x":0,"y":33,"width":1470,"height":846}')
        rejected = SimpleNamespace(returncode=1, stderr='Hearthstone is not the frontmost app')
        with patch('session.subprocess.run', side_effect=[geometry, rejected]) as run, \
             patch('session.time.monotonic',side_effect=[0,1]):
            with self.assertRaisesRegex(SessionError,'frontmost'):
                navigation_click((735,310))
            self.assertEqual(run.call_count,2)

    def test_no_shell_paths_or_extras_in_settings(self):
        base = {"api_key": "fake-key-not-a-secret-123", "model": "gpt-5.6-terra"}
        self.assertEqual(validate_config(base), base)
        for change in ({"model": "$(echo x)"}, {"endpoint": "https://elsewhere.test"}, {"api_key": "short"}):
            with self.assertRaises(SessionError):
                validate_config({**base, **change})

    def test_second_session_cannot_acquire_lock(self):
        with tempfile.TemporaryDirectory() as tmp, patch("session.DATA", Path(tmp)):
            first = lock_session()
            try:
                with self.assertRaises(SessionError):
                    lock_session()
            finally:
                first.close()
            lock_session().close()

    def test_bounded_retention_only_for_app_owned_runs(self):
        with tempfile.TemporaryDirectory() as tmp, patch("session.DATA", Path(tmp)):
            runs = Path(tmp)/"runs"; runs.mkdir()
            for n in range(7):
                (runs/("run-%02d" % n)).mkdir()
            unrelated = runs/"unrelated"; unrelated.mkdir()
            session_directory()
            self.assertEqual(len(list(runs.glob("run-*"))), 5)
            self.assertTrue(unrelated.exists())

    def test_stop_reason_is_visible_and_persisted_with_bounded_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"events.jsonl"
            sink = EventLog(path, limit=1024)
            with patch("session._event_log", sink), patch("builtins.print") as output:
                guard_event(b'{"watchdog_result":"game did not start"}', "game")
                self.assertIn("game did not start", output.call_args.args[0])
                for _ in range(50):
                    emit("bounded" * 40)
            sink.close()
            self.assertLessEqual(path.stat().st_size, 1024)
            first = json.loads(path.read_text().splitlines()[0])
            self.assertEqual(first["metrics"]["watchdog_result"], "game did not start")

    def test_final_guard_bytes_are_drained_after_process_exit(self):
        read, write = os.pipe()
        with os.fdopen(read, "rb") as stream:
            os.write(write, b'{"restored_target_fps":60}\n{"watchdog_result":"interrupted"}')
            os.close(write)
            streams, sources = {stream: b""}, {stream: "game"}
            with patch("session.emit") as output:
                drain_guards(streams, sources)
                drain_guards(streams, sources)
                self.assertEqual(len(output.call_args_list), 2)
                self.assertIn("60 FPS", output.call_args_list[0].args[0])
                self.assertIn("interrupted", output.call_args_list[1].args[0])
            self.assertFalse(streams)

    def test_launch_uses_only_fixed_hearthstone_product(self):
        with patch("session.subprocess.run") as launch:
            launch_hearthstone()
            self.assertEqual(launch.call_args.args[0], [BATTLENET_EXEC, "--exec=launch WTCG"])
            self.assertTrue(launch.call_args.kwargs["check"])
            self.assertLessEqual(launch.call_args.kwargs["timeout"], 8)

    def test_no_focus_before_a_known_scene_has_loaded(self):
        for value in ({}, {"scene": None}, {"scene": {"mode": 3, "sceneLoaded": False}},
                      {"scene": {"mode": 3, "sceneLoaded": True, "transitioning": True}},
                      {"scene": {"mode": 0, "sceneLoaded": True}}):
            self.assertFalse(scene_ready(value))
        self.assertTrue(scene_ready({"scene": {"mode": 3, "sceneLoaded": True, "transitioning": False}}))


if __name__ == "__main__":
    unittest.main()
