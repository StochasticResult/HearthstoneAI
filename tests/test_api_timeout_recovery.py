import json
import subprocess
import threading
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

    def run_with(self, fn, backup=None):
        advisor = self.d.advisor = OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'},fn,decisions_transport=backup)
        self.addCleanup(advisor.close)
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

    def backup_for_original_window(self):
        def score(wire):
            evidence = json.loads(wire['input'])
            self.assertEqual(evidence['candidate_moves']['a0']['action']['source_id'],14)
            return {'model':'gpt-6-luna','usage':{'total_tokens':17},'answers':[
                {'name':q['name'],'type':'score','score':3 if q['name']=='a0' else 0}
                for q in wire['questions']]}
        return Mock(side_effect=score)

    def test_backup_uses_original_window_then_returns_to_sol(self):
        backup = self.backup_for_original_window()
        advisor = self.run_with(Mock(side_effect=AdvisorTimeout('timeout')),backup)
        self.assertEqual((backup.call_count,advisor.consecutive_timeouts,advisor.summary['api_timeouts']),(1,0,1))
        self.d.execute_main_action.assert_called_once()
        self.assertEqual(self.d.execute_main_action.call_args.args[2]['source_id'],14)
        advisor.transport = Mock(return_value=response('a0'))
        with patch('builtins.print'):
            advisor.ask(self.snapshot(1))
        self.assertEqual(backup.call_count,1)
        advisor.transport.assert_called_once()

    def test_changed_option_discards_prepared_decision_and_only_plays_fresh_sol(self):
        backup = self.backup_for_original_window()
        def changed(payload):
            if self.source == 14:
                self.source = 41
                self.p.options_id = 41
                raise AdvisorTimeout('timeout')
            return response('a0')
        advisor = self.run_with(changed,backup)
        self.assertEqual((advisor.summary['expired_decisions'],backup.call_count),(1,1))
        self.d.execute_main_action.assert_called_once()
        self.assertEqual(self.d.execute_main_action.call_args.args[2]['source_id'],41)

    def test_sol_wins_even_if_standby_result_is_already_ready(self):
        backup_done = threading.Event()
        score = self.backup_for_original_window()
        def standby(packet):
            result = score(packet)
            backup_done.set()
            return result
        def primary(_):
            self.assertTrue(backup_done.wait(2))
            return response('a1')
        advisor = OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'},primary,decisions_transport=standby)
        self.addCleanup(advisor.close)
        advisor.standby.delay = 0
        with patch('builtins.print'):
            action = advisor.ask(self.snapshot(1))
        advisor.close()
        self.assertEqual(action,{'kind':'attack','source_id':33,'target_id':47})
        self.assertIsNone(advisor.last_backup_id)
        self.assertEqual((advisor.summary['decisions_shadow'],advisor.summary['unconfirmed_decisions']),(1,[1]))
        self.assertEqual(score.call_count,1)

    def test_early_sol_reply_cancels_backup_before_network_dispatch(self):
        backup = self.backup_for_original_window()
        advisor = self.run_with(Mock(return_value=response('a0')),backup)
        advisor.close()
        backup.assert_not_called()
        self.assertEqual(advisor.summary.get('decisions_shadow',0),0)

    def test_failed_backup_rechecks_once_before_remaining_sol_retry(self):
        sol=Mock(side_effect=[AdvisorTimeout('timeout'),response('a0')])
        backup=Mock(return_value={'model':'wrong-model','answers':[]})
        a=self.run_with(sol,backup)
        self.assertEqual((sol.call_count,backup.call_count,a.summary['decisions_declined']),(2,1,1))
        self.assertGreaterEqual(self.d.identify.call_count,2)
        self.d.execute_main_action.assert_called_once()

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
