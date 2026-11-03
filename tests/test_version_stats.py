import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from build_identity import current_build, read_build
from version_stats import record, grouped, connect, display, status
from match_review import write_review


def result(run,version='0.2-build-a',outcome='WON',mode='ranked'):
    return {'run':run,'runtime_version':version,'outcome':outcome,'models':['gpt-6.1-sol'],
            'requested_mode':mode,'game_type':'GT_RANKED' if mode=='ranked' else 'GT_VS_AI'}


class VersionStatisticsTests(unittest.TestCase):
    def test_matchup_groups_remain_separate_and_unknowns_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            sample={'observed_own_turns':1,'played_deck':'zoo','opponent_class':'ROGUE','opponent_build':'patch',
                    'inferred_archetype':'Combo','archetype_evidence':['A','B']}
            record(p,result('r1'),strategy_sample=sample)
            record(p,result('r1'),strategy_sample=sample)
            record(p,result('r2',outcome='LOST'),strategy_sample=dict(sample,inferred_archetype=None))
            record(p,result('r3',outcome='TIED'),strategy_sample=sample)
            record(p,result('r4',version='v2'),strategy_sample=sample)
            record(p,result('r5'),strategy_sample=dict(sample,played_deck='other'))
            record(p,result('r6',mode='practice'),strategy_sample=sample)
            data=json.loads((p/'matchup-stats.json').read_bytes())
            rows=[r for r in data['by_class'] if r['version']=='0.2-build-a' and r['deck']=='zoo']
            self.assertEqual([(x['wins'],x['losses'],x['draws'],x['win_rate_pct']) for x in rows],[(1,1,1,50.0)])
            kinds={r['inferred_archetype']:(r['wins'],r['losses']) for r in data['by_inferred_deck']
                   if r['version']=='0.2-build-a' and r['deck']=='zoo'}
            self.assertEqual(kinds,{'Combo':(1,0),'unknown':(0,1)})

    def test_startup_imports_legacy_and_new_build_shows_no_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=result('old');old.pop('runtime_version')
            (root/'match-history.json').write_text(json.dumps([old]))
            for _ in range(2):current=status(root,'new-build')
            self.assertIsNone(current['win_rate_pct']);self.assertEqual(current['completed_games'],0)
            export=json.loads((root/'version-stats.json').read_text())
            self.assertEqual(export['current']['version'],'new-build')
            self.assertEqual(export['versions'][0]['wins'],1)
            self.assertEqual(export['versions'][0]['version'],'unversioned')

    def test_versions_and_practice_are_separate_and_draws_do_not_change_win_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for row in (result('run-1'),result('run-2',outcome='LOST'),result('run-3',outcome='TIED'),
                        result('run-4',outcome='INCOMPLETE'),result('run-5',version='0.2-build-b'),
                        result('run-6',mode='practice')):
                record(root,row)
            db=connect(root)
            try:
                a=grouped(db,'0.2-build-a','ranked')[0]
                b=grouped(db,'0.2-build-b','ranked')[0]
                practice=grouped(db,'0.2-build-a','practice')[0]
            finally:db.close()
            self.assertEqual((a['wins'],a['losses'],a['draws'],a['incomplete_runs']),(1,1,1,1))
            self.assertEqual(a['win_rate_pct'],50);self.assertEqual(a['decisive_games'],2)
            self.assertEqual((b['wins'],b['win_rate_pct']),(1,100))
            self.assertEqual(practice['wins'],1)
            self.assertIn('2 局胜负样本',display(a))

    def test_repeated_reviews_and_history_imports_do_not_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=result('legacy');old.pop('runtime_version')
            for _ in range(4):record(root,result('new'),[old,result('new')])
            stats=record(root,result('later'),[old,result('new')])
            self.assertEqual(stats['wins'],2)
            db=connect(root)
            try:
                self.assertEqual(grouped(db,'unversioned','ranked')[0]['wins'],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM matches').fetchone()[0],3)
            finally:db.close()
            report=json.loads((root/'version-stats.json').read_text())
            self.assertIn('older deleted',report['legacy_note'])

    def test_result_correction_is_atomic_and_retains_totals_without_detailed_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            record(root,result('same',outcome='INCOMPLETE'))
            current=record(root,result('same',outcome='WON'))
            self.assertEqual((current['wins'],current['incomplete_runs']),(1,0))
            current=record(root,result('new',outcome='LOST'),history=[])
            self.assertEqual((current['wins'],current['losses']),(1,1))

    def test_unknown_game_kind_and_only_draws_do_not_create_fake_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            row=result('unknown');row['game_type']=None
            stats=record(Path(tmp),row)
            self.assertEqual(stats['wins'],0);self.assertIsNone(stats['win_rate_pct'])
            stats=record(Path(tmp),result('draw',outcome='TIED'))
            self.assertEqual(stats['draws'],1);self.assertIsNone(stats['win_rate_pct'])

    def test_build_is_frozen_in_run_manifest_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'run-20261101-000000';run.mkdir()
            manifest={'version_id':'0.2-old-build','content_sha256':'a'*64,'git_revision':'old'}
            (run/'run-version.json').write_text(json.dumps(manifest))
            (run/'actions.log').write_text('mulligan: kept starting hand\n{"game_result":"WON"}\n')
            (run/'decisions.jsonl').write_text(json.dumps({'event':'proposed','decision_id':1,'observation':{'game_type':'GT_RANKED'}})+'\n'+json.dumps({'event':'confirmed','decision_id':1})+'\n')
            learning=root/'learning'
            a=write_review(run,learning,game_closed=True)
            b=write_review(run,learning,game_closed=True)
            self.assertEqual(a['runtime_version'],'0.2-old-build')
            self.assertTrue(a['version_identity_verified']);self.assertEqual(b['version_stats']['wins'],1)
            self.assertEqual(len(json.loads((learning/'match-history.json').read_text())),1)

    def test_missing_build_stays_unversioned_and_invalid_build_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertEqual(current_build(root),{'version_id':'unversioned','identity_verified':False})
            snapshot=root/'run-version.json';snapshot.write_text(json.dumps(current_build(root)))
            self.assertEqual(read_build(snapshot)['version_id'],'unversioned')
            path=root/'build-info.json';path.write_text('{}')
            with self.assertRaises(ValueError):current_build(root)
            path.write_text(json.dumps({'version_id':'0.2-correct','content_sha256':'b'*64}))
            self.assertTrue(current_build(root)['identity_verified'])
