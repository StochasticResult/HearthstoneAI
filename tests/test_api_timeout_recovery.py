import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from incremental_board import IncrementalBoard
from openai_advisor import AdvisorError, AdvisorTimeout, OpenAIAdvisor
from practice_driver import Driver, Stop
from test_incremental_board import fixture, power
from test_openai_advisor import response
from test_practice_driver import sample
from test_target_choice import target_fixture


class TimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.p = IncrementalBoard()
        self.p.feed(fixture() + power('TAG_CHANGE Entity=1 tag=TURN value=16').encode())
        self.d = Driver.__new__(Driver)
        self.d.parser = self.p
        self.d.owner = 1
        self.d.identity_game = self.p.game
        self.d.poll = Mock()
        self.d.identify = Mock(return_value=1)
        self.d.execute_main_action = Mock()
        self.payloads = []
        self.source = 14
        self.d.snapshot = self.snapshot

    def snapshot(self, _own):
        s = sample()
        s.update(game_type='GT_RANKED',format='FT_STANDARD',game_turn='16',game_step='MAIN_ACTION',
                 visual_turn='16',visual_step='MAIN_ACTION',options_id=self.source,phase='main')
        s['my_hand'][0]['id'] = self.source
        s['legal_options'][0]['id'] = self.source
        return s

    def run_with(self, fn):
        advisor = self.d.advisor = OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'},fn)
        with patch('practice_driver.RUN_MODE','ranked'),patch('practice_driver.mirror',return_value={}), \
             patch('practice_driver.time.sleep'),patch('builtins.print'):
            self.d.run(4,1)
        return advisor

    def test_timeout_reobserves_changed_option_before_any_input(self):
        def transport(payload):
            self.payloads.append(json.loads(payload['input'])['actions']['a0']['source_id'])
            if len(self.payloads) == 1:
                self.source = 41
                raise AdvisorTimeout('injected network timeout')
            return response('a0')
        advisor = self.run_with(transport)
        self.assertEqual(self.payloads,[14,41])
        self.assertGreaterEqual(self.d.identify.call_count,2)
        self.d.execute_main_action.assert_called_once()
        _,state,action,*_ = self.d.execute_main_action.call_args.args
        self.assertEqual((state['options_id'],action['source_id']),(41,41))
        self.assertEqual((advisor.calls,advisor.summary['api_responses'],advisor.summary['api_timeouts']),(2,1,1))
        self.assertEqual(advisor.summary['unconfirmed_decisions'],[2])
        self.assertEqual(advisor.consecutive_timeouts,0)

    def test_two_consecutive_timeouts_handoff_without_guessing_or_ending_turn(self):
        fn = Mock(side_effect=AdvisorTimeout('injected timeout'))
        with self.assertRaises(AdvisorTimeout):
            self.run_with(fn)
        self.assertEqual(fn.call_count,2)
        self.d.execute_main_action.assert_not_called()
        self.assertEqual(self.d.advisor.summary['unconfirmed_decisions'],[])
        self.assertEqual(self.d.advisor.summary['api_timeouts'],2)

    def test_fatal_api_error_is_not_retried(self):
        fn=Mock(side_effect=AdvisorError('HTTP 401'))
        with self.assertRaisesRegex(AdvisorError,'401'):
            self.run_with(fn)
        self.assertEqual(fn.call_count,1)
        self.d.execute_main_action.assert_not_called()

    def test_forced_local_action_does_not_count_as_a_successful_network_response(self):
        advisor=OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'},Mock(side_effect=AdvisorTimeout('timeout')))
        menu={'a0':{'kind':'play','source_id':14},'a1':{'kind':'end_turn'}}
        with patch('builtins.print'):
            for streak in (1,2):
                with self.assertRaises(AdvisorTimeout):
                    advisor.select({'phase':'main','game_turn':'16'},menu)
                self.assertEqual(advisor.consecutive_timeouts,streak)
                self.assertEqual(advisor.select({}, {'a0':{'kind':'end_turn'}}),{'kind':'end_turn'})
        self.assertEqual((advisor.calls,advisor.summary['api_responses']),(2,0))

    def test_target_window_is_reentered_before_an_action_is_taken(self):
        p=target_fixture()
        self.d.parser=p
        self.d.identity_game=p.game
        self.d.advisor=Mock()
        self.d.advisor.consecutive_timeouts=1
        target={'kind':'choose_target','target_id':40}
        self.d.advisor.target.side_effect=[AdvisorTimeout('injected timeout'),target]
        self.d.execute_target=Mock()
        with patch('practice_driver.RUN_MODE','ranked'),patch('practice_driver.mirror',return_value={}), \
             patch('practice_driver.time.sleep'),patch('builtins.print'):
            self.d.run(4,1)
        self.assertEqual(self.d.advisor.target.call_count,2)
        self.assertGreaterEqual(self.d.identify.call_count,2)
        self.d.execute_target.assert_called_once()
        self.d.advisor.confirm_target.assert_called_once_with(target)

    def test_a_new_game_during_timeout_cannot_receive_old_input(self):
        def changed(_):
            self.p.feed(fixture())
            raise AdvisorTimeout('injected timeout')
        with self.assertRaisesRegex(Stop,'game changed'):
            self.run_with(changed)
        self.d.execute_main_action.assert_not_called()

    def test_terminal_result_after_timeout_is_kept_without_another_request(self):
        def finish(_):
            self.p.feed(power('TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON').encode())
            raise AdvisorTimeout('injected timeout')
        advisor=self.run_with(finish)
        self.assertEqual((advisor.calls,advisor.summary['outcome']),(1,'WON'))
        self.d.execute_main_action.assert_not_called()

    def test_socket_and_worker_deadlines_are_typed_and_keep_the_hard_limit(self):
        advisor=OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'})
        for problem in (subprocess.TimeoutExpired('worker',18),
                        subprocess.CompletedProcess([],2,'{"transport_error":"timeout"}','')):
            with self.subTest(problem=type(problem).__name__),patch('openai_advisor.subprocess.run') as child:
                if isinstance(problem,Exception):child.side_effect=problem
                else:child.return_value=problem
                with self.assertRaises(AdvisorTimeout):
                    advisor._transport({})
                self.assertEqual(child.call_args.kwargs['timeout'],18)
                self.assertEqual(child.call_count,1)
