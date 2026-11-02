import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ranked_autoplay import run_autoplay, stop_reason, AutoplayCancelled


def completed(name='run-1', outcome='WON', losses=0):
    return {'run':name, 'outcome':outcome, 'game_type':'GT_RANKED', 'game_closed':True,
            'unconfirmed_decisions':[], 'mulligan_confirmed':True,
            'started_without_mulligan':False, 'consecutive_losses':losses}


class RankedAutoplayTests(unittest.TestCase):
    def setUp(self):
        self.baseline = 1031 * 1024**2
        mock_swap = patch('ranked_autoplay.swap_used', return_value=self.baseline)
        mock_swap.start();self.addCleanup(mock_swap.stop)

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
