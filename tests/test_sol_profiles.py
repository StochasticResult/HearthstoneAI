import json
import tempfile
import unittest
from pathlib import Path

from openai_advisor import AdvisorError
from session import SessionError, validate_config
from sol_profile_eval import CASES, MODEL, evaluate


class SolProfileTests(unittest.TestCase):
    def test_native_model_field_preserves_model_and_selects_separate_api_parameters(self):
        settings = {'model':MODEL+' fast xhigh','api_key':'synthetic-local-key-123456'}
        config = validate_config(settings)
        self.assertEqual((config['model'],config['service_tier'],config['reasoning_effort']),
                         (MODEL,'priority','xhigh'))
        for bad in ('fast fast','high max','fast unknown'):
            with self.assertRaises(SessionError):
                validate_config({**settings,'model':MODEL+' '+bad})

    def run_profiles(self, downgrade=False, reject=False, fast_only=False):
        calls=[]
        def transport(request):
            calls.append(request)
            if reject:raise AdvisorError('API request failed (HTTP 401)')
            action_id=request['tools'][0]['parameters']['properties']['action_id']['enum'][0]
            return {'model':MODEL,'status':'completed','service_tier':'default' if downgrade or request['service_tier']=='default' else 'fast',
                    'reasoning':request['reasoning'],'usage':{'total_tokens':100},
                    'output':[{'type':'function_call','name':'choose_action',
                               'arguments':json.dumps({'action_id':action_id,'reason':'test'})}]}
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'results.jsonl'
            results=evaluate({'api_key':'synthetic-local-key-123456'},p,transport,fast_efforts_only=fast_only)
            rows=[json.loads(line) for line in p.read_text().splitlines()][:-1]
        return calls,results,rows

    def test_identical_cases_and_verified_tiers_for_all_efforts(self):
        calls,result,rows=self.run_profiles()
        self.assertEqual(result['attempts'],24)
        self.assertEqual(result['verified_low_pairs'],4)
        self.assertEqual(result['effort_comparison_tier'],'default')
        self.assertTrue(all(c['model']==MODEL and c['max_output_tokens']==1536 for c in calls))
        for case in CASES:
            group=[r for r in rows if r['case']==case]
            self.assertEqual(len({r['input_digest'] for r in group}),1)
            self.assertEqual({r['effort'] for r in group},{'low','medium','high','xhigh','max'})

    def test_fast_effort_followup_only_fills_missing_cells(self):
        _,_,standard=self.run_profiles()
        _,summary,fast=self.run_profiles(fast_only=True)
        self.assertEqual(summary['attempts'],16)
        self.assertEqual(len({(r['case'],r['effort'],r['requested_tier']) for r in standard+fast}),40)
        self.assertTrue(all(p['tier_verified']==4 for p in summary['profiles']))

    def test_downgrade_is_not_counted_as_fast_and_bad_key_stops_batch(self):
        _,result,_=self.run_profiles(downgrade=True)
        self.assertEqual(result['verified_low_pairs'],0)
        self.assertEqual(result['effort_comparison_tier'],'default')
        calls,result,_=self.run_profiles(reject=True)
        self.assertEqual(len(calls),1)
        self.assertIsNone(result['effort_comparison_tier'])
