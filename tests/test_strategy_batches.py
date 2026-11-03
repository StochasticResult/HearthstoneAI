import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import strategy_batch as batches
import version_stats
from coaching import strategy_sample


class StrategyBatchTests(unittest.TestCase):
    def test_standby_samples_keep_unused_suggestions_separate_from_confirmed_moves(self):
        played = {'kind':'play','source_id':42}
        passed = {'kind':'end_turn'}
        proposal = {'event':'proposed','observation':{'game_turn':'7','mana':3},
                    'actions':{'a0':played,'a1':passed},'action':played}
        log = [{**proposal,'decision_id':1},{'event':'confirmed','decision_id':1},
               {**proposal,'decision_id':3,'policy':'sol_timeout_backup_v1'},
               {'event':'expired_without_submission','decision_id':3},
               {'event':'decision_assist_shadow','decision_id':3,'for_decision':1,'action':passed}]
        sample = strategy_sample(log)
        shadow = sample['cases']['decisions_shadow']
        self.assertEqual((shadow['decision_id'],shadow['action'],shadow['unused_suggestion']),(1,played,passed))
        self.assertEqual(sample['cases']['decisions_backup']['status'],'expired_without_submission')
        self.assertEqual(sample['whole_turn_pass_with_options'],[])

    def test_groups_only_settled_ranked_runs_across_versions_and_rechecks_corrections(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);learning=data/'learning'
            def game(n,outcome='WON',version='v1',mode='GT_RANKED',sample=None):
                version_stats.record(learning,dict(run='run-'+str(n),runtime_version=version,outcome=outcome,game_type=mode),strategy_sample=sample)
            game(0);game(1);game(2,mode='GT_VS_AI');game(3,'INCOMPLETE')
            db=batches.open_review(data,create=True)
            try:
                batches.initialize(db,'run-0')
                self.assertEqual([r[0] for r in batches.pending(db)],['run-1'])
                game(4,'TIED',version='v2');game(5,'LOST',version='v2')
                runs=['run-1','run-4','run-5']
                with self.assertRaises(ValueError):batches.record(db,list(reversed(runs)),'f','c','v')
                self.assertEqual(batches.record(db,runs,'finding','changed','checked'),1)
                self.assertEqual(batches.pending(db),[])
                with self.assertRaises(ValueError):batches.record(db,runs,'finding','changed','checked')
                game(3,'LOST');game(5,'WON',version='v2')
                self.assertEqual([r[0] for r in batches.pending(db)],['run-3','run-5'])
                self.assertEqual(db.execute('SELECT outcome FROM batch_games WHERE batch_id=1 AND run=?',('run-5',)).fetchone(),('LOST',))
                with self.assertRaises(ValueError):batches.initialize(db,'run-1')
                batches.initialize(db,'run-0')
            finally:db.close()

    def test_win_sample_survives_detailed_log_removal_and_old_runs_remain_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);learning=data/'learning'
            ev=[{'event':'mulligan_confirmed','offered':[{'id':3,'card_id':'A'}],'replaced_ids':[3]},
                {'decision_id':2,'event':'proposed','observation':{'game_turn':3,'mana':2,'my_hero':{'id':1,'health':14},'enemy_hero':{'id':2,'health':30}},
                 'actions':{'a0':{'kind':'hero_power'},'a1':{'kind':'end_turn'}},'action':{'kind':'end_turn'},'reason':'hold life'},
                {'decision_id':2,'event':'confirmed'}]
            sample=strategy_sample(ev)
            self.assertEqual(sample['whole_turn_pass_with_options'],[3]);self.assertIn('end_with_options',sample['cases'])
            self.assertEqual(sample['opening']['replaced'],['A'])
            summary={'run':'run-2','runtime_version':'v','game_type':'GT_RANKED','outcome':'WON'}
            version_stats.record(learning,summary,strategy_sample=sample)
            version_stats.record(learning,summary)
            db=batches.open_review(data,create=True)
            try:
                batches.initialize(db,'run-2')
                self.assertEqual(batches.sample_for(db,data,'run-2'),sample)
                self.assertIsNone(batches.sample_for(db,data,'run-missing'))
            finally:db.close()
            db=batches.open_review(data,readonly=True)
            try:self.assertEqual(batches.pending(db),[])
            finally:db.close()
