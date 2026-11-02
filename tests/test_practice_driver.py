import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from practice_driver import Stop, NotReady, ClientFatal, client_error, choice_index, hover_matches, plan_inputs, visual_ready, mirror
from practice_state import prepare
from incremental_board import UnsafeState


def sample():
    return {"game_type": "GT_VS_AI", "format": "FT_WILD", "own_turn": True, "mana": 2, "options_id": 7,
            "my_hero": {"id": 64, "health": 30, "armor": 0}, "enemy_hero": {"id": 66, "health": 30, "armor": 0},
            "my_hand": [{"id": 14, "card_id": "TIME_025", "position": 1, "cost": 2, "card_type": "MINION", "attack": 4, "health": 4}],
            "my_board": [{"id": 33, "card_id": "CORE_UNG_205", "position": 1, "attack": 2, "health": 1, "taunt": False, "frozen": False}],
            "enemy_board": [{"id": 47, "card_id": "CS2_172", "position": 1, "attack": 3, "health": 2, "taunt": False, "frozen": False}],
            "legal_options": [{"id": 14, "type": "POWER", "targets": []}, {"id": 33, "type": "POWER", "targets": [47, 66]}]}


class DriverTests(unittest.TestCase):
    def test_client_timeout_is_classified_without_echoing_private_log_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            power=Path(tmp)/"Power.log"
            path=power.with_name("Hearthstone.log")
            path.write_text("private-account-data NetCache Error: TIMEOUT more-private-data")
            self.assertIn("NetCache TIMEOUT",client_error(power))
            self.assertNotIn("private",client_error(power))
        with patch("practice_driver.run_checked",return_value='{"scene":{"mode":9},"game_type":1}'):
            with self.assertRaises(ClientFatal): mirror()

    def test_reconnect_log_readiness_does_not_bypass_scene_loading(self):
        for loaded, transitioning in ((False, True), (True, True), (None, False)):
            observation = {"game_type": 1, "scene": {"mode": 4, "sceneLoaded": loaded, "transitioning": transitioning}}
            with patch("practice_driver.run_checked", return_value=json.dumps(observation)):
                with self.assertRaisesRegex(NotReady, "not ready"):
                    mirror()
        observation["scene"].update(sceneLoaded=True, transitioning=False)
        with patch("practice_driver.run_checked", return_value=json.dumps(observation)):
            self.assertEqual(mirror(), observation)
        observation["game_type"] = 2
        with patch("practice_driver.run_checked", return_value=json.dumps(observation)):
            with self.assertRaisesRegex(Stop, "non-practice"):
                mirror()

    def test_prepares_only_known_practice_and_public_board(self):
        ready = prepare(sample(), identity_verified=True)
        self.assertEqual(ready["enemy_board"][0]["model_card_id"], "VANILLA")
        with self.assertRaises(UnsafeState):
            prepare(sample(), identity_verified=False)
        unknown = sample()
        unknown["enemy_board"][0]["card_id"] = "UNKNOWN"
        with self.assertRaises(UnsafeState):
            prepare(unknown, identity_verified=True)
        archer = sample()
        archer["enemy_board"][0]["card_id"] = "CS2_189"
        self.assertEqual(prepare(archer, identity_verified=True)["enemy_board"][0]["model_card_id"], "VANILLA")
        taunt = sample()
        taunt["enemy_board"][0].update({"card_id": "CS2_121", "taunt": True})
        self.assertTrue(prepare(taunt, identity_verified=True)["enemy_board"][0]["taunt"])
        berserker = sample()
        berserker["enemy_board"][0]["card_id"] = "EX1_399"
        self.assertEqual(prepare(berserker, identity_verified=True)["enemy_board"][0]["model_card_id"], "EX1_399")

    def test_only_legal_action_and_targets_get_coordinates(self):
        state = sample()
        self.assertEqual(plan_inputs(state, {"kind": "play", "source_id": "14"}), ("drag", (705, 817), (735, 510)))
        self.assertEqual(plan_inputs(state, {"kind": "attack", "source_id": "33", "target_id": "47"}), ("drag", (735, 510), (735, 365)))
        with self.assertRaises(Stop):
            plan_inputs(state, {"kind": "attack", "source_id": "33", "target_id": "64"})
        with self.assertRaises(Stop):
            plan_inputs(state, {"kind": "play", "source_id": "999"})

    def test_choice_requires_visible_supported_choices(self):
        c = {"shown": True, "source_card_id": "TLC_451", "items": [{"card_id": "TIME_025"}, {"card_id": "BE_090"}, {"card_id": "BE_094"}]}
        self.assertEqual(choice_index(c, 1, 2), 0)
        c["shown"] = False
        with self.assertRaises(Stop):
            choice_index(c, 1, 2)

    def test_hover_requires_current_card_position_and_hand_size(self):
        hand = sample()["my_hand"]
        observed = {"isHand": True, "side": 1, "cardId": "TIME_025", "zonePosition": 1, "zoneSize": 1}
        self.assertTrue(hover_matches(observed, hand, 14))
        for key, value in (("isHand", False), ("side", 2), ("cardId", "TLC_603"),
                           ("zonePosition", 2), ("zoneSize", 2)):
            wrong = dict(observed, **{key: value})
            self.assertFalse(hover_matches(wrong, hand, 14))
        self.assertFalse(hover_matches(observed, hand, 999))

    def test_animation_must_reach_the_current_turn(self):
        state = {"game_turn": "6", "visual_turn": "5", "game_step": "MAIN_ACTION", "visual_step": "MAIN_ACTION"}
        self.assertFalse(visual_ready(state))
        state["visual_turn"] = "6"
        state["visual_step"] = "MAIN_START"
        self.assertFalse(visual_ready(state))
        state["visual_step"] = "MAIN_ACTION"
        self.assertTrue(visual_ready(state))


if __name__ == "__main__":
    unittest.main(verbosity=2)
