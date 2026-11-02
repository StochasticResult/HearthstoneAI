import unittest

from incremental_board import IncrementalBoard, UnsafeState
from mirror_identity import practice_identity
from test_incremental_board import fixture, power


def one_minion(eid, card_id, controller, pos):
    return (power("FULL_ENTITY - Creating ID=%d CardID=%s" % (eid, card_id)) +
            power("  tag=CARDTYPE value=MINION") + power("  tag=ZONE value=PLAY") +
            power("  tag=CONTROLLER value=%d" % controller) + power("  tag=ZONE_POSITION value=%d" % pos)).encode()


def mirror(friendly, opposing):
    return {"game_type": 1, "scene": {"mode": 4}, "board": {
        "friendly": {"boardCards": friendly}, "opposing": {"boardCards": opposing}}}


class MirrorIdentityTests(unittest.TestCase):
    def setUp(self):
        self.parser = IncrementalBoard()
        self.parser.feed(fixture().replace(b"GameType=GT_RANKED", b"GameType=GT_VS_AI") +
                         one_minion(41, "A", 1, 1) + one_minion(42, "B", 2, 1))
        self.a = {"entityId": 41, "cardId": "A", "zonePosition": 1}
        self.b = {"entityId": 42, "cardId": "B", "zonePosition": 1}

    def test_both_sides_match_and_reverse(self):
        self.assertEqual(practice_identity(self.parser.game, mirror([self.a], [self.b])), 1)
        self.assertEqual(practice_identity(self.parser.game, mirror([self.b], [self.a])), 2)

    def test_stale_board_or_wrong_mode_is_rejected(self):
        with self.assertRaisesRegex(UnsafeState, "disagree"):
            practice_identity(self.parser.game, mirror([self.a], []))
        with self.assertRaisesRegex(UnsafeState, "empty"):
            practice_identity(self.parser.game, mirror([], []))
        wrong = mirror([self.a], [self.b])
        wrong["game_type"] = 2
        with self.assertRaisesRegex(UnsafeState, "not in practice"):
            practice_identity(self.parser.game, wrong)

    def test_real_mirror_null_for_empty_side(self):
        parser = IncrementalBoard()
        parser.feed(fixture().replace(b"GameType=GT_RANKED", b"GameType=GT_VS_AI") + one_minion(42, "B", 2, 1))
        status = mirror([], [self.b])
        status["board"]["friendly"] = None
        self.assertEqual(practice_identity(parser.game, status), 1)
        del status["board"]["friendly"]
        with self.assertRaisesRegex(UnsafeState, "incomplete"):
            practice_identity(parser.game, status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
