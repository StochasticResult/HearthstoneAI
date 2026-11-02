import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from session import validate_config, SessionError, lock_session, session_directory


class SessionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
