import unittest
from unittest.mock import Mock
from mirror_identity import mulligan_identity
from incremental_board import UnsafeState


class MulliganIdentityTests(unittest.TestCase):
    def test_visible_multiset_proves_one_player_without_tracker(self):
        parser = Mock()
        parser.game = {"meta": {"GameType": "GT_VS_AI"}}
        choices = {1: {"type": "MULLIGAN", "items": [{"card_id": x} for x in ("A", "B", "B")]},
                   2: {"type": "MULLIGAN", "items": [{"card_id": x} for x in ("", "", "")]}}
        parser.pending_choice.side_effect = choices.get
        mirror = {"game_type": 1, "scene": {"mode": 4}, "mulligan_waiting": True,
                  "mulligan": {"waitingForUserInput": True, "mulliganCards": [{"cardId": x} for x in ("B", "A", "B")]}}
        self.assertEqual(mulligan_identity(parser, mirror), 1)
        choices[2] = choices[1]
        with self.assertRaisesRegex(UnsafeState, "uniquely"):
            mulligan_identity(parser, mirror)

    def test_hidden_or_wrong_mode_opening_is_rejected(self):
        parser = Mock(); parser.game = {"meta": {"GameType": "GT_RANKED"}}
        with self.assertRaises(UnsafeState):
            mulligan_identity(parser, {})


if __name__ == "__main__":
    unittest.main()
