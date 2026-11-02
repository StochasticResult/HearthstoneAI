import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ranked_autoplay import run_autoplay, stop_reason, AutoplayCancelled, AfterGameStop, continued_swap_baseline, reviewed_streak_boundary


def completed(name='run-1', outcome='WON', losses=0):
    return {'run':name, 'outcome':outcome, 'game_type':'GT_RANKED', 'game_closed':True,
            'unconfirmed_decisions':[], 'mulligan_confirmed':True,
            'started_without_mulligan':False, 'consecutive_losses':losses}


class RankedAutoplayTests(unittest.TestCase):
    def test_only_acknowledging_the_finished_three_loss_review_sets_a_new_boundary(self):
        prior = dict(autoplay_id='s',status='needs_review',stop_reason='three_consecutive_losses',
                     runs=['a','b','c'],completed_games=3)
        ack = dict(autoplay_id='s',completed_games=3)
        self.assertEqual(reviewed_streak_boundary(prior, ack), 'c')
        for invalid in ({},dict(ack,autoplay_id='x'),dict(ack,completed_games=2)):
            self.assertIsNone(reviewed_streak_boundary(prior,invalid))
        self.assertIsNone(reviewed_streak_boundary(dict(prior,status='running'),ack))
        self.assertEqual(reviewed_streak_boundary({'reviewed_loss_run':'c'},{}),'c')

    def test_acknowledged_run_is_discounted_once_but_new_three_loss_streak_still_pauses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history.json'
            history = [completed(str(n),'LOST',n) for n in range(1,4)]
            for n in range(4,7):
                current = completed(str(n),'LOST',n)
                history.append(current)
                path.write_text(json.dumps(history))
                options = dict(reviewed_loss_run='3',history_path=path)
                self.assertEqual(stop_reason(current,**options), 'three_consecutive_losses' if n == 6 else None)
                self.assertEqual(stop_reason(dict(current,unconfirmed_decisions=[7]),**options),'unconfirmed_action')
                self.assertEqual(stop_reason(current,reviewed_loss_run='unrelated',history_path=path),'three_consecutive_losses')
                self.assertEqual(current['consecutive_losses'],n)
            path.write_text('[]')
            self.assertEqual(stop_reason(current,**options),'three_consecutive_losses')

    def test_gui_wake_lifetime_follows_user_intent_across_reviews_and_faults(self):
        for reason, expected, release in (('review_update','paused_for_review',False),
                                          ('user_requested_stop','cancelled',True),
                                          ('fault','needs_review',False),
                                          ('fault_with_stop','cancelled',True)):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp, \
                 patch('ranked_autoplay.AppWake.acquire') as acquire:
                acquire.return_value.pid=42
                data=Path(tmp)
                def game(*_,**__):
                    if reason!='fault':
                        request='user_requested_stop' if reason=='fault_with_stop' else reason
                        (data/'learning/pause-after-game.json').write_text(json.dumps({'reason':request}))
                    if reason.startswith('fault'):
                        raise RuntimeError('driver failed')
                    return completed()
                if reason.startswith('fault'):
                    with self.assertRaisesRegex(RuntimeError,'driver failed'):
                        run_autoplay({'api_key':'fake'},os.getppid(),data,game,Mock(),Mock())
                    state=json.loads((data/'learning/autoplay-state.json').read_text())
                else:
                    state=run_autoplay({'api_key':'fake'},os.getppid(),data,game,Mock(),Mock())
                self.assertEqual(state['status'],expected)
                self.assertEqual(acquire.return_value.release.called,release)
                self.awake.assert_not_called()

    def test_planned_resume_preserves_old_baseline_instead_of_hiding_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'state.json'
            for status in ('paused_for_review','needs_review','waiting_for_resources'):
                p.write_text(json.dumps({'status':status,'swap_baseline_mib':1000}))
                self.assertEqual(continued_swap_baseline(p,1700*1024**2),1000*1024**2)
                self.assertEqual(continued_swap_baseline(p,800*1024**2),800*1024**2)
            p.write_text(json.dumps({'status':'cancelled','swap_baseline_mib':1000}))
            self.assertEqual(continued_swap_baseline(p,1700*1024**2),1700*1024**2)

    def test_stop_during_result_wait_retains_user_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp)
            def game(*_,**__):
                (data/'learning/pause-after-game.json').write_text('{"reason":"user_requested_stop"}')
                raise AfterGameStop()
            state=run_autoplay({'api_key':'private-test'},None,data,game,Mock(),Mock())
            self.assertEqual(state['status'],'cancelled')
            self.assertEqual(state['stop_reason'],'user_after_game_stop')
            self.assertEqual(state['completed_games'],0)

    def setUp(self):
        self.baseline = 1031 * 1024**2
        mock_swap = patch('ranked_autoplay.swap_used', return_value=self.baseline)
        mock_swap.start();self.addCleanup(mock_swap.stop)
        ready = patch('ranked_autoplay.readiness', return_value={'ready':True, 'reasons':[]})
        ready.start();self.addCleanup(ready.stop)
        awake = patch('ranked_autoplay.subprocess.Popen')
        self.awake = awake.start();self.addCleanup(awake.stop)
        self.awake.return_value.poll.return_value = None

    def test_continues_beyond_three_games_and_preserves_private_fresh_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);config={'api_key':'fake-test-secret-123456789','model':'gpt-6.1-sol'}
            received=[]
            def play(settings, parent, mode, *, swap_baseline):
                received.append(settings['api_key']);settings['api_key']=''
                self.assertEqual(mode,'ranked')
                self.assertEqual(swap_baseline,self.baseline)
                if len(received)==5:
                    (data/'learning/pause-after-game.json').write_text(json.dumps({'reason':'user_requested_stop'}))
                return completed('run-%d'%len(received))
            emit=Mock();pause=Mock()
            state=run_autoplay(config,None,data,play,emit,pause)
            self.assertEqual(len(received),5);self.assertTrue(all(received))
            self.assertEqual(state['status'],'cancelled');self.assertEqual(pause.call_count,4)
            self.assertEqual(config['api_key'],'');self.assertTrue(state['review_due'])
            saved=(data/'learning/autoplay-state.json').read_text()
            self.assertNotIn('fake-test-secret',saved);self.assertNotIn('api_key',saved)
            self.assertNotIn('fake-test-secret',str(emit.call_args_list))
            self.awake.assert_called_once()
            self.awake.return_value.terminate.assert_called_once()

    def test_single_loss_can_continue_but_faults_and_three_losses_stop(self):
        self.assertIsNone(stop_reason(completed(outcome='LOST',losses=1)))
        changes=[{'outcome':'INCOMPLETE'}, {'game_closed':False}, {'unconfirmed_decisions':[1]},
                 {'consecutive_losses':3}, {'started_without_mulligan':True}, {'game_type':'GT_VS_AI'}]
        for change in changes:
            with tempfile.TemporaryDirectory() as tmp:
                run=Mock(return_value={**completed(),**change})
                state=run_autoplay({'api_key':'private-test'},None,Path(tmp),run,Mock(),Mock())
                self.assertEqual(run.call_count,1);self.assertEqual(state['status'],'needs_review')

    def test_session_failure_never_starts_another_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Mock(side_effect=RuntimeError('state mismatch'))
            with self.assertRaisesRegex(RuntimeError,'state mismatch'):
                run_autoplay({'api_key':'private-test'},None,Path(tmp),run,Mock(),Mock())
            state=json.loads((Path(tmp)/'learning/autoplay-state.json').read_text())
            self.assertEqual(state['status'],'needs_review');self.assertEqual(run.call_count,1)

    def test_review_stop_only_happens_after_current_game_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);events=[]
            def game(*args, **kwargs):
                events.append('playing')
                (data/'learning/pause-after-game.json').write_text(json.dumps({'reason':'review_update'}))
                events.append('game_completed')
                return completed()
            state=run_autoplay({'api_key':'private-test'},None,data,game,Mock(),Mock())
            self.assertEqual(events,['playing','game_completed'])
            self.assertEqual(state['completed_games'],1);self.assertEqual(state['status'],'paused_for_review')

    def test_stop_during_cooldown_persists_cancelled_and_clears_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={'api_key':'private-test'};run=Mock(return_value=completed())
            with self.assertRaises(AutoplayCancelled):
                run_autoplay(config,None,Path(tmp),run,Mock(),Mock(side_effect=AutoplayCancelled('stop')))
            self.assertEqual(run.call_count,1);self.assertEqual(config['api_key'],'')
            state=json.loads((Path(tmp)/'learning/autoplay-state.json').read_text())
            self.assertEqual(state['status'],'cancelled')

    def test_resources_wait_then_resume_without_starting_or_interrupting_a_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);order=[]
            samples=[{'ready':False,'reasons':['observing current paging']},
                     {'ready':True,'reasons':[]}, {'ready':False,'reasons':['disk reserve']},
                     {'ready':True,'reasons':[]}]
            def play(*args, **kwargs):
                order.append('game')
                if order.count('game') == 2:
                    (data/'learning/pause-after-game.json').write_text('{"reason":"user_requested_stop"}')
                return completed('run-%d'%order.count('game'))
            with patch('ranked_autoplay.readiness',side_effect=samples) as ready, \
                 patch('ranked_autoplay.cleanup',side_effect=lambda _: order.append('cleanup')):
                state=run_autoplay({'api_key':'fake-secret'},None,data,play,Mock(),lambda _:order.append('wait'))
            self.assertEqual(order,['cleanup','wait','game','wait','cleanup','wait','game'])
            self.assertEqual(state['completed_games'],2)
            self.assertTrue(all(c.args == (self.baseline,) for c in ready.call_args_list))
            self.assertEqual([c.kwargs['previous'] for c in ready.call_args_list],
                             [None,samples[0],None,samples[2]])

    def test_user_can_stop_resource_wait_without_launching_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);run=Mock()
            def wait(_):
                state=json.loads((data/'learning/autoplay-state.json').read_text())
                self.assertEqual(state['status'],'waiting_for_resources')
                (data/'learning/pause-after-game.json').write_text('{"reason":"user_requested_stop"}')
            with patch('ranked_autoplay.readiness',return_value={'ready':False,'reasons':['disk reserve']}):
                state=run_autoplay({'api_key':'fake-secret'},None,data,run,Mock(),wait)
            self.assertEqual(state['status'],'cancelled');run.assert_not_called()
            self.awake.return_value.terminate.assert_called_once()

    def test_second_autoplay_cannot_start_during_resource_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);run=Mock()
            def wait(_):
                with self.assertRaisesRegex(RuntimeError,'已有自动天梯'):
                    run_autoplay({'api_key':'fake-secret'},None,data,run,Mock(),Mock())
                raise AutoplayCancelled('stop')
            with patch('ranked_autoplay.readiness',return_value={'ready':False,'reasons':['disk reserve']}):
                with self.assertRaises(AutoplayCancelled):
                    run_autoplay({'api_key':'fake-secret'},None,data,run,Mock(),wait)
            run.assert_not_called()
