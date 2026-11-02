import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace

from practice_driver import Driver, Stop, NotReady, ClientFatal, client_error, choice_index, hover_matches, plan_inputs, visual_ready, mirror
from practice_state import prepare
from incremental_board import UnsafeState
from practice_driver import waiting_for_first_mana, run_checked
from incremental_board import IncrementalBoard
from test_incremental_board import fixture, power


def sample():
    return {"game_type": "GT_VS_AI", "format": "FT_WILD", "own_turn": True, "mana": 2, "options_id": 7,
            "my_hero": {"id": 64, "health": 30, "armor": 0}, "enemy_hero": {"id": 66, "health": 30, "armor": 0},
            "my_hand": [{"id": 14, "card_id": "TIME_025", "position": 1, "cost": 2, "card_type": "MINION", "attack": 4, "health": 4}],
            "my_board": [{"id": 33, "card_id": "CORE_UNG_205", "position": 1, "attack": 2, "health": 1, "taunt": False, "frozen": False}],
            "enemy_board": [{"id": 47, "card_id": "CS2_172", "position": 1, "attack": 3, "health": 2, "taunt": False, "frozen": False}],
            "legal_options": [{"id": 14, "type": "POWER", "targets": []}, {"id": 33, "type": "POWER", "targets": [47, 66]}]}


class DriverTests(unittest.TestCase):
    def test_only_input_rejected_before_sending_can_retry_a_brief_focus_handoff(self):
        unfocused = SimpleNamespace(returncode=1, stderr='Hearthstone is not the frontmost app\n', stdout='')
        success = SimpleNamespace(returncode=0, stderr='', stdout='ok\n')
        with patch('practice_driver.subprocess.run', side_effect=[unfocused, success]) as run, \
             patch('practice_driver.time.sleep'):
            self.assertEqual(run_checked(['native', '--input', 'click', '1', '2']), 'ok')
            self.assertEqual(run.call_count, 2)
        failure = SimpleNamespace(returncode=1, stderr='input failed', stdout='')
        for args, result in ((['mirror-status'], unfocused), (['native', '--input', 'click'], failure)):
            with patch('practice_driver.subprocess.run', return_value=result) as run:
                with self.assertRaises(Stop): run_checked(args)
                run.assert_called_once()

    def test_zero_quotas_still_enter_the_loop_and_observe_game_end(self):
        driver=Driver.__new__(Driver)
        driver.poll=Mock();driver.game_result=Mock(return_value='WON')
        driver.advisor = Mock()
        with patch('builtins.print'):driver.run(0,0)
        driver.advisor.record.assert_called_once_with({'event':'game_complete','outcome':'WON'})
        driver.poll.assert_called_once();driver.game_result.assert_called_once()
    def test_second_player_waits_for_first_observed_mana_without_defaulting_to_zero(self):
        p=IncrementalBoard();p.feed(fixture());player=p.game['entities'][3]
        player['tags'].pop('RESOURCES');p.game['entities'][1]['tags']['TURN']='1'
        self.assertTrue(waiting_for_first_mana(p,2))
        with self.assertRaisesRegex(UnsafeState,'missing RESOURCES'):p.snapshot(2)
        p.feed((power('TAG_CHANGE Entity=3 tag=RESOURCES value=1')+
                power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1')).encode())
        self.assertFalse(waiting_for_first_mana(p,2));self.assertEqual(p.snapshot(2)['mana'],1)
        player['tags'].pop('RESOURCES')
        self.assertFalse(waiting_for_first_mana(p,2))
        player['tags']['CURRENT_PLAYER']='0';p.game['entities'][1]['tags']['TURN']='3'
        self.assertFalse(waiting_for_first_mana(p,2))

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
            with self.assertRaisesRegex(Stop, "unexpected game type"):
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

    def hand_driver(self):
        state = sample()
        state.update(game_turn='6', visual_turn='6', game_step='MAIN_ACTION', visual_step='MAIN_ACTION')
        driver = Driver.__new__(Driver)
        driver.watchdog = 123
        driver.poll = lambda: None
        driver.snapshot = lambda _own: state
        seen = {'isHand':True, 'side':1, 'cardId':'TIME_025', 'zonePosition':1, 'zoneSize':1}
        return driver, state, seen

    def test_ready_hand_hover_is_used_immediately_without_a_fixed_delay(self):
        driver, state, seen = self.hand_driver()
        with patch('practice_driver.move'), patch('practice_driver.watchdog_alive',return_value=True), \
             patch('practice_driver.hover',side_effect=[{},seen]), patch('practice_driver.time.sleep') as sleep:
            self.assertEqual(driver.verify_hand_point(1,state,14,(705,817)),(705,817))
            sleep.assert_not_called()

    def test_lagging_hover_is_polled_until_the_right_card_is_observed(self):
        driver, state, seen = self.hand_driver()
        wrong = dict(seen, cardId='TLC_603')
        with patch('practice_driver.move'), patch('practice_driver.watchdog_alive',return_value=True), \
             patch('practice_driver.hover',side_effect=[wrong,{},wrong,seen]), patch('practice_driver.time.sleep') as sleep:
            self.assertEqual(driver.verify_hand_point(1,state,14,(705,817)),(705,817))
            self.assertEqual(sleep.call_count,2)

    def test_fast_hover_still_rejects_a_state_change_before_returning_a_point(self):
        driver, state, seen = self.hand_driver()
        with patch.object(driver,'snapshot',side_effect=[state,dict(state,options_id=8)]), \
             patch('practice_driver.move'), patch('practice_driver.watchdog_alive',return_value=True), \
             patch('practice_driver.hover',side_effect=[{},seen]), patch('practice_driver.time.sleep'):
            with self.assertRaisesRegex(Stop,'changed'):
                driver.verify_hand_point(1,state,14,(705,817))

    def test_already_confirmed_action_needs_no_fixed_sleep(self):
        driver, _, _ = self.hand_driver()
        with patch('practice_driver.time.sleep') as sleep:
            driver.wait_until(lambda: True)
            sleep.assert_not_called()

    def test_next_decision_waits_for_new_options_even_when_card_state_already_changed(self):
        driver, initial, _ = self.hand_driver()
        initial.update(my_hero_power={'id':50},legal_options=[{'id':50,'type':'POWER','targets':[]}])
        changed = dict(initial,mana=0)
        fresh = dict(changed,options_id=8,legal_options=[],end_turn_allowed=True)
        driver.parser = SimpleNamespace(pending_choice=lambda _own: None,
                                        game={'entities':{50:{'tags':{}}}}, options=[], options_id=7, sent_options=0)
        driver.game_result = lambda: None
        driver.identify = lambda _status: 1
        driver.revalidate = Mock()
        driver.wait_until = Mock()
        driver.execute_end_turn = Mock()
        driver.advisor = Mock()
        driver.advisor.ask.side_effect = [{'kind':'hero_power','source_id':50},{'kind':'end_turn'}]
        states = iter([initial,changed,fresh])
        def snapshot(_own):
            state = next(states)
            driver.parser.options_id = state['options_id']
            return state
        with patch.object(driver,'snapshot',side_effect=snapshot), \
             patch('practice_driver.mirror',return_value={}), patch('practice_driver.checked_geometry'), \
             patch('practice_driver.click'), patch('practice_driver.time.sleep'), patch('builtins.print'):
            driver.run(10,2)
        self.assertEqual([c.args[0]['options_id'] for c in driver.advisor.ask.call_args_list],[7,8])

    def test_end_turn_retries_ignored_click_but_never_a_sent_option(self):
        for submitted, expected_clicks in ((False,2),(True,1)):
            driver,state,_ = self.hand_driver()
            driver.parser = SimpleNamespace(sent_options=0)
            driver.game_result = lambda: None
            driver.revalidate = Mock()
            def click(_point):
                if submitted:driver.parser.sent_options += 1
            with patch.object(driver,'wait_until',side_effect=[Stop('action was not confirmed in game log'),None]), \
                 patch('practice_driver.click',side_effect=click) as send:
                driver.execute_end_turn(1,state)
                self.assertEqual(send.call_count,expected_clicks)

    def test_end_turn_rejects_changed_state_before_retry(self):
        driver,state,_ = self.hand_driver()
        driver.parser = SimpleNamespace(sent_options=0)
        driver.game_result = lambda: None
        driver.revalidate = Mock(side_effect=[None,Stop('changed state')])
        with patch.object(driver,'wait_until',side_effect=Stop('action was not confirmed in game log')), \
             patch('practice_driver.click') as send:
            with self.assertRaisesRegex(Stop,'changed state'):driver.execute_end_turn(1,state)
            self.assertEqual(send.call_count,1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
