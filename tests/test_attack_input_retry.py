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
            try:
                self.d.execute_attack_drag(2, self.state, self.action, (682, 510), (735, 365), 4, 0, acknowledged)
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
