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
                self.assertFalse(batches.review_status(db,data)['due'])
                with self.assertRaises(ValueError):batches.record(db,runs,'f','c','v')
                game(6,'WON',version='v2')
                runs.append('run-6')
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

    def test_full_review_cadence(self):
        for marks, expected in [('LLW',3),('LWL',3),('WLL',3),('WLTW',4),
                                ('WWTW',4),('WWWWL',5),('WWWWW',5)]:
            with self.subTest(marks=marks):
                outcomes = {'W':'WON','L':'LOST','T':'TIED'}
                games = [(str(i),'v',outcomes[m]) for i,m in enumerate(marks)]
                for i in range(1,expected):
                    self.assertLess(i, batches.review_size(games[:i]))
                self.assertEqual(expected,batches.review_size(games))

    def test_cross_batch_quick_check_is_durable_and_does_not_consume_a_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp); learning=data/'learning'
            def game(n,outcome,version='v1',kind='GT_RANKED'):
                version_stats.record(learning,dict(run=f'run-{n:02}',runtime_version=version,
                    outcome=outcome,game_type=kind),strategy_sample={'observed_own_turns':1,'case':n})
            game(0,'WON')
            with batches.open_review(data,create=True) as db:
                batches.initialize(db,'run-00')
                for n,o in enumerate(['WON','WON','WON','WON','LOST'],1):
                    game(n,o)
                batch=batches.review_status(db,data)
                self.assertTrue(batch['due'])
                batches.record(db,[g['run'] for g in batch['games']],'f','c','v')
                game(6,'WON',kind='GT_VS_AI');game(7,'INCOMPLETE')
                game(8,'LOST','v2')
                quick=batches.review_status(db,data,samples=True)
                ids=['run-05','run-08']
                self.assertEqual([(g['run'],g['version'],g['strategy_sample']['case']) for g in quick['quick_check']],
                                 [('run-05','v1',5),('run-08','v2',8)])
                self.assertFalse(quick['due'])
                batches.record_quick(db,ids,'Both outcomes were confirmed')
                with self.assertRaises(ValueError):batches.record_quick(db,ids,'duplicate')
            db.close()
            with batches.open_review(data) as db:
                checked=batches.review_status(db,data)
                self.assertFalse(checked['quick_check'])
                self.assertEqual([g['run'] for g in checked['games']],['run-08'])
                self.assertEqual(checked['prior_quick_checks'][0]['runs'],ids)
                game(9,'LOST','v2')
                self.assertFalse(batches.review_status(db,data)['quick_check'])
                # A delayed heartbeat handles only the oldest due prefix.
                for n in range(10,15):game(n,'WON','v2')
                full=batches.review_status(db,data)
                self.assertTrue(full['due'])
                runs=[g['run'] for g in full['games']]
                self.assertEqual(runs,['run-08','run-09','run-10'])
                self.assertEqual(full['prior_quick_checks'][0]['finding'],'Both outcomes were confirmed')
                batches.record(db,runs,'f','c','v')
                waiting=batches.review_status(db,data)
                self.assertFalse(waiting['due'])
                self.assertEqual(len(waiting['games']),4)
            db.close()

    def test_third_result_supersedes_unrecorded_quick_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);learning=data/'learning'
            def game(n,o,version='v'):
                version_stats.record(learning,dict(run=f'run-{n}',runtime_version=version,outcome=o,game_type='GT_RANKED'))
            game(0,'WON');game(1,'LOST');game(2,'LOST')
            db=batches.open_review(data,create=True)
            try:
                batches.initialize(db,'run-0')
                self.assertTrue(batches.review_status(db,data)['quick_check'])
                game(1,'WON')
                self.assertFalse(batches.review_status(db,data)['quick_check'])
                game(1,'LOST','corrected')
                self.assertEqual(batches.review_status(db,data)['quick_check'][0]['version'],'corrected')
                game(3,'LOST')
                self.assertTrue(batches.review_status(db,data)['due'])
                with self.assertRaises(ValueError):batches.record_quick(db,['run-1','run-2'],'stale')
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
