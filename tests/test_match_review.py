import json
import tempfile
import unittest
from pathlib import Path
from match_review import summarize, write_review


class MatchReviewTests(unittest.TestCase):
    def make_run(self, root, name, outcome=None, confirmed=True):
        dest=root/name;dest.mkdir()
        events=[{'decision_id':1,'event':'proposed','observation':{'game_type':'GT_VS_AI','mana':1},
                 'action':{'kind':'end_turn'},'model':'gpt-6.1-sol','tokens':100,'elapsed_ms':2000}]
        if confirmed:events.append({'decision_id':1,'event':'confirmed'})
        (dest/'decisions.jsonl').write_text('\n'.join(map(json.dumps,events)))
        (dest/'actions.log').write_text(json.dumps({'game_result':outcome}) if outcome else 'practice driver stopped: state changed')
        (dest/'session-events.jsonl').write_text(json.dumps({'game_closed':True}))
        return dest

    def test_incomplete_never_counted_as_loss_or_win_and_unconfirmed_is_reviewed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);dest=self.make_run(p,'run-1',confirmed=False)
            result=write_review(dest,p/'learning')
            self.assertEqual(result['outcome'],'INCOMPLETE')
            self.assertEqual(result['unconfirmed_decisions'],[1])
            self.assertEqual(result['driver_stop_detail'],'state changed')
            self.assertTrue(result['review_before_next_game'])
            queue=json.loads((p/'learning/review-queue.json').read_text())
            self.assertEqual(queue[0]['status'],'awaiting_review')
            self.assertEqual(queue[0]['decision']['observation']['mana'],1)

    def test_actual_result_and_streak_persist_without_duplicate_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            for i in range(3):
                dest=self.make_run(p,'run-%d'%i,'LOST')
                result=write_review(dest,p/'learning')
            self.assertEqual(result['consecutive_losses'],3)
            write_review(dest,p/'learning')
            self.assertEqual(len(json.loads((p/'learning/match-history.json').read_text())),3)
            win=self.make_run(p,'run-3','WON')
            result=write_review(win,p/'learning')
            self.assertEqual(result['consecutive_losses'],0)
            self.assertFalse(result['review_before_next_game'])
            self.assertEqual(result['api_tokens'],100)
            self.assertTrue(result['game_closed'])

    def test_ambiguous_complete_is_not_a_reported_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest=self.make_run(Path(tmp),'run-1','COMPLETE')
            self.assertEqual(summarize(dest)[0]['outcome'],'INCOMPLETE')

    def test_midgame_start_is_reported_even_without_reconnect_recovery_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest=self.make_run(Path(tmp),'run-1')
            rows=[json.loads(line) for line in (dest/'decisions.jsonl').read_text().splitlines()]
            rows[0]['observation']['game_turn']='6'
            (dest/'decisions.jsonl').write_text('\n'.join(map(json.dumps,rows)))
            (dest/'session-events.jsonl').write_text(json.dumps({'game_mode':'ranked'}))
            result=summarize(dest)[0]
            self.assertEqual(result['requested_mode'],'ranked')
            self.assertEqual(result['first_decision_game_turn'],'6')
            self.assertTrue(result['started_without_mulligan'])
            self.assertFalse(result['reconnect_recovery_used'])
