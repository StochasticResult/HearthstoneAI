import os
import tempfile
import time
import unittest
from pathlib import Path

from tracker_identity import IdentityUnavailable, MAX_TAIL, read_tracker_player_id


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tracker.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_current_id_without_printing_content(self):
        self.path.write_bytes(b"----- Game End -----\n----- Game Started -----\nlocalPlayer: (playerId: 2) private\n")
        self.assertEqual(read_tracker_player_id(self.path), 2)

    def test_reconnect_player_update_and_conflicts(self):
        self.path.write_bytes(b"----- Game Started -----\nGame.updatePlayers():1995 - private [PlayerId=1] vs other [PlayerId=2]\n")
        self.assertEqual(read_tracker_player_id(self.path), 1)
        self.path.write_bytes(b"----- Game Started -----\nlocalPlayer: playerId: 2\nGame.updatePlayers():1995 - private [PlayerId=1] vs other [PlayerId=2]\n")
        with self.assertRaisesRegex(IdentityUnavailable, "conflicting"):
            read_tracker_player_id(self.path)
        self.path.write_bytes(b"----- Game Started -----\nGame.updatePlayers():1995 - private [PlayerId=1] vs other [PlayerId=1]\n")
        with self.assertRaisesRegex(IdentityUnavailable, "duplicate"):
            read_tracker_player_id(self.path)

    def test_rejects_finished_or_conflicting_game(self):
        for data in (b"----- Game Started -----\nlocalPlayer: playerId: 1\n----- Game End -----\n",
                     b"----- Game Started -----\nlocalPlayer: playerId: 1\nlocalPlayer: playerId: 2\n"):
            self.path.write_bytes(data)
            with self.assertRaises(IdentityUnavailable):
                read_tracker_player_id(self.path)

    def test_rejects_stale_or_missing_start(self):
        self.path.write_bytes(b"----- Game Started -----\nlocalPlayer: playerId: 1\n")
        old = time.time() - 121
        os.utime(self.path, (old, old))
        with self.assertRaisesRegex(IdentityUnavailable, "stale"):
            read_tracker_player_id(self.path)
        self.path.write_bytes(b"----- Game Started -----\n" + b"x" * (MAX_TAIL + 1))
        with self.assertRaisesRegex(IdentityUnavailable, "bounded tail"):
            read_tracker_player_id(self.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
