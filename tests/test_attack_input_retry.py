import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from practice_driver import ActionUnconfirmed, Driver, click_log_cursor, client_rejected_drag
from coaching import thinking_review


WARNING = b'W 22:27:18.8266210 You clicked on something that is not being handled by InputManager.  Alert The Brode!\n'


class AttackInputRetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.power = Path(tmp.name) / 'Power.log'
        self.log = self.power.with_name('Hearthstone.log')
        self.log.touch()
        self.start = time.mktime(time.strptime('2026-11-02 22:27:18', '%Y-%m-%d %H:%M:%S')) + .6
        self.state = {'game_turn': '3', 'options_id': 8, 'my_board': [
            {'id': 38, 'card_id': 'CORE_UNG_205', 'windfury': False, 'mega_windfury': False, 'attacks_this_turn': 0}]}
        self.action = {'kind': 'attack', 'source_id': 38, 'target_id': 14}
        self.d = Driver.__new__(Driver)
        self.d.follower = Mock(path=self.power)
        self.d.identity_game = {'events': []}
        self.d.owner = 2
        self.d.parser = Mock(game=self.d.identity_game, sent_options=4, options_id=8, partial=False)
        self.d.parser.pending_choice.return_value = None
        self.d.advisor, self.d.revalidate, self.d.poll = Mock(), Mock(), Mock()
        self.d.snapshot = Mock(return_value=self.state)
        def wait(predicate, _seconds):
            if not predicate():
                raise ActionUnconfirmed('missing receipt')
        self.d.wait_until = wait

    def run_gesture(self, gesture, acknowledged):
        with patch('practice_driver.RUN_MODE', 'ranked'), patch('practice_driver.time.time', side_effect=[self.start, self.start+.5]), \
                patch('practice_driver.drag', side_effect=gesture) as send, patch('practice_driver.checked_geometry'):
            public_ui = {'board':{'friendly':{'mousedOverSlot':3,'boardCards':[{'entityId':19,'hovered':True}]}},'popup':False}
            with patch('practice_driver.mirror', return_value=public_ui):
                try:
                    self.d.execute_checked_drag(2, self.state, self.action, (682, 510), (735, 365), 4, 0, acknowledged)
                    return send.call_count, True
                except ActionUnconfirmed:
                    return send.call_count, False

    def test_222520_rejected_shard_drag_is_retried_once_with_the_same_target(self):
        calls = []
        def send(start, end):
            calls.append((start, end))
            if len(calls) == 1:
                self.log.write_bytes(WARNING)
            else:
                self.d.parser.sent_options += 1
        self.assertEqual(self.run_gesture(send, lambda: self.d.parser.sent_options == 5), (2, True))
        self.assertEqual(calls, [((682, 510), (735, 365))] * 2)
        self.d.revalidate.assert_called_once_with(2, self.state, self.action)
        self.assertEqual(self.d.advisor.record.call_args.args[0]['event'], 'attack_input_retry')

    def test_silence_late_submission_changed_options_and_windfury_do_not_resend(self):
        for case in ('silence', 'late_submission', 'changed_options', 'windfury', 'partial_log', 'new_combat'):
            with self.subTest(case=case):
                self.log.write_bytes(b'')
                self.d.parser.sent_options, self.d.parser.options_id, self.d.parser.partial = 4, 8, False
                self.d.identity_game['events'] = []
                self.state['my_board'][0]['windfury'] = case == 'windfury'
                def send(_start, _end):
                    if case != 'silence':
                        self.log.write_bytes(WARNING)
                    if case == 'late_submission':
                        self.d.parser.sent_options = 5
                    if case == 'changed_options':
                        self.d.parser.options_id = 9
                    if case == 'partial_log':
                        self.d.parser.partial = True
                    if case == 'new_combat':
                        self.d.identity_game['events'].append({'type': 'attack'})
                self.assertEqual(self.run_gesture(send, lambda: False), (1, False))
                self.d.advisor.record.assert_not_called()

    def test_second_miss_stays_unconfirmed_and_a_successful_first_drag_needs_no_retry(self):
        self.assertEqual(self.run_gesture(lambda *_: self.log.write_bytes(WARNING), lambda: False), (2, False))
        self.d.advisor.record.reset_mock()
        self.assertEqual(self.run_gesture(lambda *_: None, lambda: True), (1, True))
        self.d.advisor.record.assert_not_called()

    def test_submission_during_revalidation_suppresses_retry_and_review_requires_matching_receipt(self):
        self.d.revalidate.side_effect = lambda *_: setattr(self.d.parser, 'sent_options', 5)
        self.assertEqual(self.run_gesture(lambda *_: self.log.write_bytes(WARNING), lambda: False), (1, False))
        retry = {'event': 'attack_input_retry', 'decision_id': 6, 'game_turn': '3', 'action': self.action}
        receipt = {'event': 'confirmed', 'decision_id': 6, 'action': dict(self.action, target_id=64)}
        self.assertFalse(thinking_review([retry, receipt])['attack_input_retries'][0]['confirmed'])
        receipt['action'] = self.action
        self.assertTrue(thinking_review([retry, receipt])['attack_input_retries'][0]['confirmed'])

    def test_plain_minion_retakes_its_hand_point_only_after_a_gesture_rejection(self):
        self.action = {'kind':'play','source_id':21}
        self.d.verify_hand_point = Mock(return_value=(665,815))
        for label in ('confirmed','wrong_source','wrong_turn','wrong_submission','silent','late_submission','new_group'):
            with self.subTest(label=label):
                calls=[]
                self.log.write_bytes(b'')
                self.d.parser.sent_options,self.d.parser.options_id=4,8
                self.d.identity_game['events']=[]
                self.d.advisor.record.reset_mock()
                def rehover(*_):
                    if label=='late_submission': self.d.parser.sent_options=5
                    if label=='new_group': self.d.parser.options_id=9
                    return (665,815)
                self.d.verify_hand_point.side_effect=rehover
                def send(start,end):
                    calls.append((start,end))
                    if len(calls)==1 and label!='silent':
                        self.log.write_bytes(WARNING)
                    if len(calls)==2:
                        self.d.parser.sent_options=5
                        self.d.identity_game['untargeted_power_submission']=(9 if label=='wrong_submission' else 8,21)
                        self.d.identity_game['events'].append({'type':'play','actor':2,
                            'turn':4 if label=='wrong_turn' else 3, 'eid':19 if label=='wrong_source' else 21})
                count,ok=self.run_gesture(send,lambda:self.d.parser.sent_options==5)
                expected_retry=label in ('confirmed','wrong_source','wrong_turn','wrong_submission')
                self.assertEqual(count,2 if expected_retry else 1)
                self.assertEqual(ok,label in ('confirmed','late_submission'))
                if expected_retry:self.assertEqual(calls[1][0],(665,815))
                logged=[call.args[0] for call in self.d.advisor.record.call_args_list]
                self.assertEqual(logged[0]['event'],'play_drag_observed')
                self.assertEqual(logged[0]['client_rejected_drag'],label!='silent')
                if label=='silent':
                    self.assertEqual((logged[0]['friendly_slot_under_pointer'],logged[0]['board_card_under_pointer']),(3,19))
                self.assertEqual(any(r['event']=='play_input_retry' for r in logged),expected_retry)

    def test_review_keeps_silence_expiry_and_retry_receipt_separate(self):
        play={'kind':'play','source_id':21}
        observed={'event':'play_drag_observed','game_turn':'5','decision_id':11,'options_id':13,'action':play,
                  'same_window':True,'client_rejected_drag':False}
        expired=dict(observed,event='expired_without_submission')
        row=thinking_review([observed,expired])['play_drag_observations'][0]
        self.assertEqual((row['retry_requested'],row['outcome']),(False,'expired_without_submission'))
        retry=dict(observed,event='play_input_retry')
        wrong={'decision_id':11,'event':'confirmed','action':dict(play,source_id=20)}
        row=thinking_review([observed,retry,wrong])['play_drag_observations'][0]
        self.assertEqual((row['retry_requested'],row['outcome']),(True,None))
        row=thinking_review([observed,retry,dict(wrong,action=play)])['play_drag_observations'][0]
        self.assertEqual((row['retry_requested'],row['outcome']),(True,'confirmed'))

    def test_log_evidence_requires_one_fresh_complete_warning_in_the_gesture_interval(self):
        for suffix in (b'', WARNING[:-1], WARNING*2, b'chat '+WARNING, WARNING.replace(b'22:27:18', b'22:27:17')):
            with self.subTest(suffix=suffix):
                self.log.write_bytes(WARNING)
                cursor = click_log_cursor(self.power)
                with self.log.open('ab') as f:
                    f.write(suffix)
                self.assertFalse(client_rejected_drag(cursor, self.start, self.start+.5))
        cursor = click_log_cursor(self.power)
        self.log.rename(self.log.with_suffix('.old'))
        self.log.write_bytes(b'x'*cursor[1].st_size + WARNING)
        self.assertFalse(client_rejected_drag(cursor, self.start, self.start+.5))
