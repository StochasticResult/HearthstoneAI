import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coach_evidence import archive, recent, transcript
from coach_policy import DEFAULTS, atomic, hints, revision, settings, snapshot
from self_training import TrainingError, positions, response_payload, train, validated
from openai_advisor import OpenAIAdvisor
from luna_decisions import recovery_inputs
from luna_decisions_eval import replay_state
from test_openai_advisor import response


class SelfTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.learning = self.data/'learning'
        self.name = 'run-20261102-190457'
        self.dest = self.data/'runs'/self.name
        self.dest.mkdir(parents=True)
        obs = {'game_type':'GT_RANKED','format':'FT_STANDARD','phase':'main','game_turn':'3','mana':3,
               'my_hand':[{'id':22,'card_id':'TEST','cost':3}], 'private_account':'NEVER_EXPORT'}
        self.menu = {'a0':{'kind':'end_turn'},'a1':{'kind':'play','source_id':22}}
        self.events = [{'event':'mulligan_confirmed','decision_id':0,'offered':[{'id':22,'card_id':'TEST'}],
                        'replaced_ids':[],'kept_ids':[22]}]
        for i,receipt in enumerate(('confirmed','confirmed','expired_without_submission'),1):
            self.events += [{'event':'proposed','decision_id':i,'observation':dict(obs,mana=4-i),
                             'actions':self.menu,'action':self.menu['a0'],'reason':'recorded'},
                            {'event':receipt,'decision_id':i,'action':self.menu['a0']}]
        self.events += [{'event':'decision_assist_shadow','decision_id':3,'for_decision':2,'action':self.menu['a1']},
                        {'event':'game_complete','decision_id':3,'outcome':'LOST'}]
        self.review={'run':self.name,'outcome':'LOST','game_type':'GT_RANKED'}
        self.compact={'confirmed_decisions':2,'expired_decisions':1,'diagnostics_capped':False}
        self.write(self.events)
        self.game=transcript(self.dest,self.review)
        self.finding={'evidence':{'run':self.name,'decision':1},'alternative':'a1',
                      'control':{'run':self.name,'decision':2},'phase':'main','zone':'my_hand','card':'TEST',
                      'advice':'核对费用与节奏。','exception':'防守优先时重新计算。'}
        self.reply={'reviews':[{'run':self.name,'review':'三回合检查费用。'}], 'findings':[self.finding]}

    def write(self,events):
        atomic(self.dest/'decision-summary.json',self.compact)
        atomic(self.dest/'review.json',self.review)
        (self.dest/'decisions.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))

    def test_verified_combat_proof_survives_log_rotation_with_its_receipt(self):
        proof = {'from_decision':1, 'previous_options_id':11, 'options_id':12,
                 'first_corpse_attack':[11,30,69], 'mutual_corpse_attack':[11,30,69]}
        self.events[1]['server_options_id'] = 11
        self.events[3].update(local_rule='verified_turn_plan_v1', calculation=dict(proof, private_account='NEVER_EXPORT'))
        self.write(self.events)
        archive(self.dest, self.review, self.learning)
        with gzip.open(self.learning/'coach-replays'/(self.name+'.json.gz'),'rt') as f:
            events = json.load(f)['events']
        self.assertEqual(events[1]['server_options_id'], 11)
        self.assertEqual(events[3]['evidence'], proof)
        self.assertEqual((events[4]['event'],events[4]['decision_id'],events[4]['action']),
                         ('confirmed',events[3]['decision_id'],events[3]['action']))

    def test_archive_whole_trace_and_privacy_even_after_cleanup(self):
        archive(self.dest,self.review,self.learning)
        (self.dest/'decisions.jsonl').unlink()
        games,skipped=recent(self.data,'',3)
        self.assertEqual((games,skipped),([self.game],[]))
        self.assertEqual(self.game['actions'],3)
        sent=response_payload(DEFAULTS,games,[])
        self.assertNotIn('NEVER_EXPORT',sent['input'])
        self.assertIn('decision_assist_shadow',sent['input'])
        self.assertEqual((sent['reasoning'],sent['model'],sent['store']),({'effort':'max'},'gpt-6-astra',False))
        self.assertEqual(recent(self.data,self.name,3),([],[]))
        self.assertEqual(positions(games)[self.name,2][0]['mana'],2)

    def test_capped_prefix_alone_cannot_pass_as_complete_by_a_known_result(self):
        self.compact['diagnostics_capped']=True
        self.write(self.events[:-2])
        self.assertIsNone(transcript(self.dest,self.review))
        (self.dest/'coach-overflow.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in self.events[-2:]))
        self.assertEqual(transcript(self.dest,self.review)['actions'],3)
        self.write(self.events[:3]+self.events[5:-2])
        self.assertIsNone(transcript(self.dest,self.review))

    def test_validation_rejects_phantom_evidence_unsafe_options_or_unconfirmed_suggestions(self):
        for changes in ({'alternative':'a9'}, {'alternative':'a0'}, {'card':'unknown'},
                        {'control':self.finding['evidence']}, {'evidence':{'run':self.name,'decision':3}},
                        {'control':{'run':'not-in-request','decision':5}}):
            candidate=dict(self.finding,**changes)
            _,approved,rejected=validated(dict(self.reply,findings=[candidate]),[self.game])
            self.assertEqual((approved,rejected),([],1))
        game=copy.deepcopy(self.game)
        next(e for e in game['events'] if 'change' in e)['change']['shred_battlecry']={'withheld_sources':[22]}
        self.assertEqual(validated(self.reply,[game])[1],[])
        game=copy.deepcopy(self.game)
        [e for e in game['events'] if 'change' in e][1]['change']['my_hand']=[]
        self.assertEqual(validated(self.reply,[game])[1],[])

    def test_publish_pin_scope_disable_and_failure_preserve_prior_policy(self):
        before=snapshot(self.learning)
        report=train(self.learning,DEFAULTS,{},[self.game],[],None,lambda k,p:(self.reply,p['model']))
        after=snapshot(self.learning)
        self.assertEqual((report['games'],report['actions'],report['added']),(1,3,1))
        self.assertNotEqual(before['revision'],after['revision'])
        self.assertEqual(hints(before,{'my_hand':[{'card_id':'TEST'}]}),[])
        self.assertTrue(hints(after,{'my_hand':[{'card_id':'TEST'}]}))
        self.assertFalse(hints(after,{'enemy_board':[{'card_id':'TEST'}]}))
        file=self.learning/'coach-state.json'
        previous=file.read_bytes()
        with self.assertRaises(TrainingError):
            train(self.learning,DEFAULTS,json.loads(previous),[self.game],[],None,
                  lambda k,p:({'reviews':[],'findings':[]},p['model']))
        self.assertEqual(previous,file.read_bytes())
        atomic(self.learning/'coach-settings.json',dict(DEFAULTS,use_lessons=False))
        self.assertEqual(snapshot(self.learning)['revision'],'baseline')
        atomic(self.learning/'coach-state.json',{'lessons':[{'corrupt':True}]})
        atomic(self.learning/'coach-settings.json',dict(DEFAULTS,games=67))
        self.assertEqual(snapshot(self.learning)['revision'],'baseline')

    @patch('builtins.print')
    def test_both_inference_packets_and_disabled_plan_on_real_ranked_decision(self,_):
        fixture=json.loads((Path(__file__).parent/'fixtures/ranked-turn-plan-20261102.json').read_text())
        state=replay_state(fixture['steps'][0])
        card=state['my_board'][0]['card_id']
        rule=dict(self.finding,zone='my_board',card=card)
        pinned=dict(revision=revision([rule]),lessons=[rule],standby=False,multi_step=False)
        atomic(self.dest/'run-policy.json',pinned)
        seen=[]
        def sol(payload):
            seen.append(payload)
            return response('a0')
        ai=OpenAIAdvisor({'api_key':'no-real-credentials-used'},sol,self.dest/'decisions.jsonl',decisions_transport=lambda p:None)
        self.addCleanup(ai.close)
        ai.ask(state)
        body=json.loads(seen[-1]['input'])
        self.assertNotIn('turn_plan',body)
        self.assertFalse(any(x['kind'].endswith('_plan') for x in body['actions'].values()))
        self.assertIsNone(ai.standby)
        notes=body['observation']['learned_matchup_notes']
        self.assertEqual(notes[0]['consider'],rule['advice'])
        _,luna=recovery_inputs(seen[-1],body['observation'],body['actions'])
        self.assertEqual(json.loads(luna['input'])['public_position']['learned_matchup_notes'],notes)

    def test_timer_and_manual_preflight_skip_without_key_or_model_calls(self):
        from self_training import main
        import io
        with patch('self_training.DATA',self.data), patch('os.nice'), patch('self_training.fetch') as api, patch('sys.stdin',io.StringIO()) as stream:
            with patch('sys.argv',['coach','--trigger','scheduled']):
                self.assertIsNone(main())
                atomic(self.learning/'coach-settings.json',dict(DEFAULTS,scheduled=True))
                atomic(self.learning/'coach-clock.json',{'last_attempt':10**10})
                self.assertIsNone(main())
            atomic(self.learning/'coach-state.json',{'last_run':self.name,'reports':[],'lessons':[]})
            with patch('sys.argv',['coach','--trigger','manual']), patch('builtins.print'):
                self.assertIsNone(main())
            api.assert_not_called()
            self.assertEqual(stream.tell(),0)

    def test_codex_owns_reviews_even_if_manual_or_timer_is_triggered(self):
        import io
        from self_training import main
        from coach_delivery import begin,fetch
        from unittest.mock import Mock
        atomic(self.learning/'review-owner.json',{'owner':'codex'})
        atomic(self.learning/'coach-settings.json',dict(DEFAULTS,scheduled=True))
        with patch('self_training.DATA',self.data), patch('os.nice'), patch('builtins.print') as messages, \
                patch('self_training.wait_for_resources') as preflight, patch('self_training.train') as train_api, \
                patch('sys.stdin',io.StringIO('never-read')) as stream:
            for trigger in ('scheduled','manual'):
                with patch('sys.argv',['coach','--trigger',trigger]):
                    self.assertIsNone(main())
            self.assertFalse(any('NEED_KEY' in str(call) for call in messages.call_args_list))
            self.assertEqual(stream.tell(),0)
            preflight.assert_not_called()
            train_api.assert_not_called()
        send=Mock(); job=begin(self.learning,DEFAULTS,[self.game],[])
        with self.assertRaisesRegex(TrainingError,'已暂停'):
            fetch(self.learning,job,0,'offline',response_payload(DEFAULTS,[self.game],[]),send=send)
        send.assert_not_called()
        from coach_policy import app_reviews_enabled
        for invalid in (None, {}, {'owner':'unknown'}):
            atomic(self.learning/'review-owner.json', invalid)
            self.assertFalse(app_reviews_enabled(self.learning))

    def test_due_schedule_runs_once_and_resource_wait_never_reads_a_key(self):
        from self_training import main
        import io
        atomic(self.learning/'coach-settings.json',dict(DEFAULTS,scheduled=True))
        def run():
            return main()
        def fake(learning,prefs,state,games,skipped,key):
            self.assertEqual([g['run'] for g in games],[self.name])
            return train(learning,prefs,state,games,skipped,key,lambda k,p:(self.reply,p['model']))
        with patch('self_training.DATA',self.data),patch('os.nice'),patch('sys.argv',['coach','--trigger','scheduled']), \
                patch('sys.stdin',io.StringIO('offline-placeholder-for-test\n')) as inp,patch('builtins.print'), \
                patch('maintenance.swap_used',return_value=0),patch('maintenance.readiness') as health, \
                patch('self_training.train',side_effect=fake) as calls:
            health.return_value={'ready':False,'reasons':['thermal']}
            with patch('time.sleep'), patch('os.getppid',return_value=1):
                run()
            self.assertEqual(inp.tell(),0)
            self.assertFalse(calls.called)
            atomic(self.learning/'coach-clock.json',{'last_attempt':0})
            health.return_value={'ready':True}
            run(); run()
            self.assertEqual(calls.call_count,1)
            self.assertEqual(json.loads((self.learning/'coach-state.json').read_text())['last_run'],self.name)

    def test_one_manual_request_survives_resource_wait_with_automatic_recovery(self):
        from self_training import wait_for_resources
        from unittest.mock import Mock
        notify=Mock()
        with patch('maintenance.swap_used',return_value=0), patch('time.sleep') as pause, patch('maintenance.readiness',
                side_effect=[{'ready':False,'reasons':['disk reserve']},{'ready':True}]):
            self.assertTrue(wait_for_resources(self.learning,'manual',notify))
            pause.assert_called_once_with(60)
            notify.assert_called_once()

    def test_optional_training_does_not_aggregate_policy_win_rates(self):
        from version_stats import record
        for i,policy,result in [(1,'baseline','WON'),(2,'new','LOST'),(3,'new','TIED')]:
            record(self.learning,dict(self.review,run='run-20261102-00000'+str(i),runtime_version='same',
                                      coach_revision=policy,outcome=result))
        doc=json.loads((self.learning/'version-stats.json').read_text())
        self.assertEqual(doc['current']['win_rate_pct'],50)
        self.assertEqual({r['revision']:r['win_rate_pct'] for r in doc['coach_policies']},{'baseline':100,'new':0})

    def test_card_text_change_suppresses_old_lesson(self):
        rule=dict(self.finding,rules={'text':'old effect','cost':3})
        view={'phase':'main','my_hand':[{'card_id':'TEST'}],
              'card_definitions':{'TEST':{'text':'old effect','cost':3,'name':'example'}}}
        self.assertEqual(len(hints({'lessons':[rule]},view)),1)
        view['card_definitions']['TEST']['cost']=4
        self.assertEqual(hints({'lessons':[rule]},view),[])

    def test_background_resume_polls_the_original_job_without_a_second_generation(self):
        from coach_delivery import begin,fetch,ledger,restore,finish
        from unittest.mock import Mock
        prefs=DEFAULTS.copy()
        job=begin(self.learning,prefs,[self.game],[])
        payload=response_payload(prefs,[self.game],[])
        self.assertTrue(payload['background'])
        self.assertFalse(payload['store'])
        self.assertEqual(payload['service_tier'],'default')
        service=Mock(side_effect=[{'id':'resp_one','status':'queued'},TimeoutError('read')])
        with self.assertRaises(TimeoutError):
            fetch(self.learning,job,0,'fake',payload,send=service,pause=lambda _:None)
        saved=ledger(self.learning)
        self.assertEqual([call.args[1] for call in service.call_args_list],['POST','GET'])
        self.assertEqual(restore(self.learning,saved),( [prefs,[self.game],[]] ))
        done={'id':'resp_one','status':'completed','model':'gpt-6-astra',
              'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(self.reply)}]}],
              'usage':{'input_tokens':854,'output_tokens':280,'output_tokens_details':{'reasoning_tokens':190}}}
        lookup=Mock(return_value=done)
        self.assertEqual(fetch(self.learning,saved,0,'fake',payload,send=lookup,pause=lambda _:None),(self.reply,'gpt-6-astra'))
        self.assertEqual(lookup.call_args.args[1:3],('GET','/v1/responses/resp_one'))
        self.assertEqual(fetch(self.learning,ledger(self.learning),0,'fake',payload,send=lookup,pause=lambda _:None),(self.reply,'gpt-6-astra'))
        lookup.assert_called_once()
        self.assertEqual(ledger(self.learning)['parts']['0']['usage']['reasoning_tokens'],190)
        finish(self.learning,saved,'complete')
        self.assertFalse((self.learning/'coach-pending.gz').exists())

    def test_unknown_post_is_quarantined_but_an_explicit_rejection_can_be_corrected(self):
        from coach_delivery import begin,fetch,ledger,HTTPFailure
        from unittest.mock import Mock
        payload=response_payload(DEFAULTS,[self.game],[])
        job=begin(self.learning,DEFAULTS,[self.game],[])
        with self.assertRaises(HTTPFailure):
            fetch(self.learning,job,0,'fake',payload,send=Mock(side_effect=HTTPFailure(401,'invalid_key')))
        self.assertEqual((ledger(self.learning)['status'],ledger(self.learning)['through']),('rejected',''))
        job=begin(self.learning,DEFAULTS,[self.game],[])
        with self.assertRaisesRegex(TrainingError,'不自动重发'):
            fetch(self.learning,job,0,'fake',payload,send=Mock(side_effect=TimeoutError('post')))
        self.assertEqual((ledger(self.learning)['status'],ledger(self.learning)['through']),('unknown',self.name))

    def test_interrupted_post_without_id_is_never_issued_again(self):
        from coach_delivery import begin,fetch,ledger
        from unittest.mock import Mock
        payload=response_payload(DEFAULTS,[self.game],[])
        job=begin(self.learning,DEFAULTS,[self.game],[])
        with self.assertRaises(KeyboardInterrupt):
            fetch(self.learning,job,0,'fake',payload,send=Mock(side_effect=KeyboardInterrupt()))
        send=Mock()
        with self.assertRaisesRegex(TrainingError,'不自动重发'):
            fetch(self.learning,ledger(self.learning),0,'fake',payload,send=send)
        send.assert_not_called()
        self.assertEqual(ledger(self.learning)['through'],self.name)
