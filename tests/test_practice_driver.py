import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace

from practice_driver import Driver, Stop, NotReady, ClientFatal, ExpiredProposal, client_error, choice_index, hover_matches, plan_inputs, visual_ready, mirror
from practice_state import prepare
from incremental_board import UnsafeState
from practice_driver import waiting_for_first_mana, run_checked
from incremental_board import IncrementalBoard, LogFollower
from test_incremental_board import fixture, power


def sample():
    return {"game_type": "GT_VS_AI", "format": "FT_WILD", "own_turn": True, "mana": 2, "options_id": 7,
            "my_hero": {"id": 64, "health": 30, "armor": 0}, "enemy_hero": {"id": 66, "health": 30, "armor": 0},
            "my_hand": [{"id": 14, "card_id": "TIME_025", "position": 1, "cost": 2, "card_type": "MINION", "attack": 4, "health": 4}],
            "my_board": [{"id": 33, "card_id": "CORE_UNG_205", "position": 1, "attack": 2, "health": 1, "taunt": False, "frozen": False}],
            "enemy_board": [{"id": 47, "card_id": "CS2_172", "position": 1, "attack": 3, "health": 2, "taunt": False, "frozen": False}],
            "legal_options": [{"id": 14, "type": "POWER", "targets": []}, {"id": 33, "type": "POWER", "targets": [47, 66]}]}


class DriverTests(unittest.TestCase):
    def test_append_after_eof_is_drained_before_any_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'Power.log'
            send = b'D 01:16:40.5659690 GameState.SendOption() - selectedOption=2 selectedSubOption=-1 selectedTarget=0 selectedPosition=0\n'
            path.write_bytes(fixture() + send)
            d = self.identified_driver()
            d.parser = IncrementalBoard()
            d.follower = LogFollower(path, d.parser)
            self.addCleanup(d.follower.close)
            while d.follower.poll():
                pass
            d.capture = Mock()
            d.watchdog = 123
            d.proposal_window = None
            read = d.follower.poll
            prefix = 'D 01:16:41.0806210 GameState.DebugPrintEntityChoices() - '
            lines = ['id=3 Player=local TaskList=109 ChoiceType=GENERAL CountMin=1 CountMax=1',
                     'Source=[id=64 cardId=TLC_451 player=2]',
                     'Entities[0]=[id=96 cardId=BE_086 player=2]',
                     'Entities[1]=[id=94 cardId=BE_094 player=2]',
                     'Entities[2]=[id=95 cardId=TIME_029 player=2]']
            late_choices = ''.join(prefix + line + '\n' for line in lines)
            appends = [(late_choices + 'D 01:16:41.1 ChoiceCardMgr.WaitThenShowChoices() - id=3 BEGIN\n').encode()]
            self.assertIsNone(d.parser.pending_choice(2))
            def read_while_writing():
                count = read()
                if not count and appends:
                    with path.open('ab') as writer:
                        writer.write(appends.pop())
                return count
            with patch.object(d.follower,'poll',side_effect=read_while_writing), \
                 patch('practice_driver.watchdog_alive',return_value=True), \
                 patch('practice_driver.time.sleep') as pause, patch('practice_driver.click') as click:
                d.poll()
            self.assertEqual(d.follower.offset, path.stat().st_size)
            choice = d.parser.pending_choice(2)
            self.assertEqual((choice['id'],choice['source_card_id'],choice['shown']),(3,'TLC_451',True))
            self.assertEqual([c['card_id'] for c in choice['items']],['BE_086','BE_094','TIME_029'])
            self.assertEqual(d.parser.sent_options,1)
            pause.assert_called_once()
            click.assert_not_called()

    def test_continuous_unread_data_cannot_authorize_input(self):
        d = self.identified_driver()
        d.watchdog = 123
        d.capture = Mock()
        d.follower = Mock(offset=100)
        d.follower.checked_size.return_value = 128
        d.follower.poll.return_value = 0
        d.check_proposal_window = Mock()
        with patch('practice_driver.watchdog_alive',return_value=True), \
             patch('practice_driver.time.monotonic',side_effect=[0,9]), \
             patch('practice_driver.click') as click:
            with self.assertRaisesRegex(Stop,'28 unread bytes'):
                d.poll()
        d.check_proposal_window.assert_not_called()
        click.assert_not_called()

    def test_late_receipt_allows_new_choice_without_replaying_the_input(self):
        d = self.identified_driver()
        p = d.parser
        p.feed(power('TAG_CHANGE Entity=1 tag=TURN value=4').encode())
        p.options_id = 12
        window = (p.game, 4, p.sent_options)
        received = b'D 01:16:40 GameState.SendOption() - selectedOption=2 selectedSubOption=-1 selectedTarget=0 selectedPosition=0\n'
        choices = [None, {'id':3,'type':'GENERAL'}]
        def poll():
            if p.sent_options == 0:
                p.feed(received)
        d.poll = poll
        with patch.object(p,'pending_choice',side_effect=choices),patch('practice_driver.time.sleep'), \
             patch('practice_driver.click') as click:
            self.assertFalse(d.wait_for_action_boundary(1, window, 12))
        self.assertEqual(p.sent_options,1)
        click.assert_not_called()

    def test_unsubmitted_input_expires_on_later_turn_and_match_changes_refuse_recovery(self):
        d = self.identified_driver()
        p = d.parser
        window = (p.game, 4, p.sent_options)
        d.poll = lambda:p.feed(power('TAG_CHANGE Entity=1 tag=TURN value=5').encode())
        self.assertTrue(d.wait_for_action_boundary(1,window,12))
        d.poll = lambda:p.feed(fixture())
        with self.assertRaisesRegex(Stop,'game changed'):
            d.wait_for_action_boundary(1,window,12)

    def test_submitted_or_same_turn_actions_are_never_treated_as_expired(self):
        d=self.identified_driver()
        p=d.parser
        p.feed(power('TAG_CHANGE Entity=1 tag=TURN value=9').encode())
        d.proposal_window=(p.game,9,p.sent_options)
        d.check_proposal_window()
        p.feed((power('TAG_CHANGE Entity=1 tag=TURN value=10')+
                power('TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0')+
                power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1')).encode())
        with self.assertRaises(ExpiredProposal):
            d.check_proposal_window()
        p.feed(b'D 10:00:01.0000000 GameState.SendOption() - selectedOption=4 selectedTarget=34\n')
        d.check_proposal_window()

    def test_expired_unsent_attack_resumes_next_own_turn_and_keeps_result(self):
        d=self.identified_driver();p=d.parser
        p.feed(power('TAG_CHANGE Entity=1 tag=TURN value=9').encode())
        p.options_id=7
        d.poll=Mock()
        d.identify=lambda _:1
        baseline=sample()
        def snapshot(_):
            turn=p.game['entities'][1]['tags']['TURN']
            return {**baseline,'game_turn':turn,'visual_turn':turn,'game_step':'MAIN_ACTION',
                    'visual_step':'MAIN_ACTION','options_id':p.options_id}
        d.snapshot=snapshot
        d.advisor.ask.return_value={'kind':'attack','source_id':33,'target_id':66}
        finished=[]
        d.game_result=lambda:'WON' if finished else None
        def execute(_,state,*args):
            if state['game_turn']=='9':
                p.feed((power('TAG_CHANGE Entity=1 tag=TURN value=10')+
                        power('TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0')+
                        power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1')).encode())
                d.check_proposal_window()
                self.fail('Expired action was allowed through')
            finished.append(state['game_turn'])
        def pause(_):
            if d.opponent_turn():
                p.feed((power('TAG_CHANGE Entity=1 tag=TURN value=11')+
                        power('TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1')+
                        power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=0')).encode())
                p.options_id=8
        d.execute_main_action=execute
        with patch('practice_driver.mirror',return_value={}),patch('practice_driver.time.sleep',side_effect=pause),patch('builtins.print'):
            d.run(0,0)
        self.assertEqual([c.args[0]['game_turn'] for c in d.advisor.ask.call_args_list],['9','11'])
        self.assertEqual(finished,['11'])
        self.assertEqual([c.args[0]['event'] for c in d.advisor.record.call_args_list],
                         ['expired_without_submission','game_complete'])

    def identified_driver(self):
        driver=Driver.__new__(Driver)
        driver.parser=IncrementalBoard();driver.parser.feed(fixture())
        driver.owner=1;driver.identity_game=driver.parser.game
        driver.board_mismatch_since=driver.board_mismatch_signature=None
        driver.advisor=Mock();driver.deck_snapshot=None
        return driver

    def test_opponent_animations_require_no_mirror_but_our_choice_or_turn_does(self):
        d=self.identified_driver()
        self.assertFalse(d.waiting_on_opponent())
        d.parser.feed((power('TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0')+
                       power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1')).encode())
        self.assertTrue(d.waiting_on_opponent())
        with patch.object(d.parser,'pending_choice',return_value={'type':'TARGET'}):
            self.assertFalse(d.waiting_on_opponent())
        d.identity_game={};self.assertFalse(d.waiting_on_opponent())

    def test_stable_board_mismatch_still_stops_and_changing_boards_reset_timer(self):
        d=self.identified_driver()
        d.parser.game['meta']['GameType']='GT_VS_AI'
        d.parser.game['entities'][1]['tags'].update(TURN='3',STEP='MAIN_ACTION')
        d.parser.visual_turn='3';d.parser.visual_step='MAIN_ACTION'
        status={'board':{'friendly':{'boardCards':[{'entityId':90,'cardId':'PUBLIC_CARD','zonePosition':1,
                                                   'private_field':'must-not-be-logged'}]}}}
        with patch('practice_driver.practice_identity',side_effect=UnsafeState('boards disagree')), \
             patch('practice_driver.time.monotonic',side_effect=[10,10,19,19,28]),patch('builtins.print') as output:
            with self.assertRaises(NotReady):d.identify(status)
            status['board']['friendly']['boardCards'][0]['entityId']=91
            with self.assertRaises(NotReady):d.identify(status)
            with self.assertRaisesRegex(Stop,'tracker board'):d.identify(status)
        self.assertNotIn('must-not-be-logged',str(output.call_args))
        d.advisor.ask.assert_not_called()

    def test_passive_enemy_turn_checks_client_without_board_or_input(self):
        d=self.identified_driver()
        d.parser.feed((power('TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0')+
                       power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1')).encode())
        d.poll=Mock();d.game_result=Mock(side_effect=[None,'WON'])
        with patch('practice_driver.mirror',return_value={}) as observe,patch('practice_driver.click') as click, \
             patch('practice_driver.time.sleep'),patch('builtins.print'):
            d.run(0,0)
        observe.assert_called_once_with(include_board=False)
        click.assert_not_called();d.advisor.ask.assert_not_called()
        self.assertEqual(d.poll.call_count,2)

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
        driver.identity_game=driver.parser.game;driver.owner=1
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

    def test_end_turn_uses_exact_submission_and_turn_flags_while_effect_resolves(self):
        driver = self.identified_driver()
        parser = driver.parser
        parser.feed(power('TAG_CHANGE Entity=1 tag=TURN value=9').encode())
        options = b'D 00:52:12.4044260 GameState.DebugPrintOptions() - '
        parser.feed(options + b'id=31\n' + options + b'  option 0 type=END_TURN mainEntity= error=INVALID errorParam=\n')
        state = {'game_turn': '9', 'options_id': 31}
        driver.revalidate = Mock()
        driver.poll = Mock()
        driver.snapshot = Mock(side_effect=UnsafeState('game is resolving an action or choice'))
        def after_click(done, _seconds):
            self.assertFalse(done())
            parser.feed(b'D 00:52:14.6055000 GameState.SendOption() - selectedOption=0 selectedSubOption=-1 selectedTarget=0 selectedPosition=0\n')
            self.assertFalse(done())
            parser.feed((power('TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0') +
                         power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1') +
                         power('TAG_CHANGE Entity=1 tag=TURN value=10')).encode())
            self.assertTrue(done())
            parser.feed(b'D 00:52:16 GameState.SendOption() - selectedOption=1 selectedSubOption=-1 selectedTarget=0 selectedPosition=0\n')
            self.assertFalse(done())
        driver.wait_until = after_click
        with patch('practice_driver.click') as send:
            driver.execute_end_turn(1, state)
        send.assert_called_once_with((1180,435))
        driver.snapshot.assert_not_called()

    def test_end_turn_retries_ignored_click_but_never_a_sent_option(self):
        for submitted, expected_clicks in ((False,2),(True,1)):
            driver,state,_ = self.hand_driver()
            driver.parser = SimpleNamespace(sent_options=0, last_end_turn_options_id=None)
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
