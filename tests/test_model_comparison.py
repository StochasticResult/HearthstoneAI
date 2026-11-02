import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_comparison import MODELS, comparison_cases, evaluate, prepare
from openai_advisor import OpenAIAdvisor, request_payload


class ComparisonTests(unittest.TestCase):
    def test_identical_payloads_and_feasible_expected_actions(self):
        suite = comparison_cases()
        self.assertEqual(len(suite),16)
        self.assertEqual(len({c['name'] for c in suite}),16)
        for case in suite:
            view,menu = prepare(case)
            self.assertTrue(all(a in menu.values() for a in case['expected']),case['name'])
            payloads=[]
            for model in MODELS:
                payload,size=request_payload(model,view,menu)
                self.assertLessEqual(size,24000)
                payload.pop('model'); payloads.append(payload)
            self.assertEqual(payloads[0],payloads[1]);self.assertEqual(payloads[0],payloads[2])
            if case.get('ranked'):
                self.assertTrue(view['strategy']['matchup']['candidates'],case['name'])
        kazakus=next(c for c in suite if c['name']=='kazakus_prior_effects_and_deadline')
        view,_=prepare(kazakus)
        self.assertEqual(len(view['confirmed_choices']),2)
        self.assertIn('CAP_405t8',view['card_definitions'])

    def test_blind_output_and_request_bounds(self):
        seen=[]
        def fake(self,payload):
            seen.append(payload)
            enum=payload['tools'][0]['parameters']['properties']['action_id']['enum']
            return {'status':'completed','model':payload['model'],'usage':{'total_tokens':100},
                    'output':[{'type':'function_call','name':'choose_action','arguments':json.dumps({'action_id':enum[0],'reason':'test reason'})}]}
        with tempfile.TemporaryDirectory() as tmp,patch.object(OpenAIAdvisor,'_transport',fake),patch('builtins.print'):
            path=Path(tmp)/'results.jsonl'
            evaluate({'api_key':'fake-key-not-a-secret-123'},path)
            raw=path.read_text(); rows=[json.loads(x) for x in raw.splitlines()]
            self.assertEqual(len(seen),48);self.assertEqual(len(rows),49)
            self.assertEqual(rows[-1]['summary']['api_calls'],48)
            for model in MODELS:self.assertNotIn(model,raw)
            for p in Path(tmp).iterdir():self.assertNotIn('fake-key',p.read_text())
            self.assertEqual(set(json.loads((path.parent/'model-key.json').read_text()).values()),set(MODELS))
            for i in range(16):
                bodies=[]
                for p in seen[i*3:i*3+3]:
                    p=dict(p);p.pop('model');bodies.append(p)
                self.assertEqual(bodies[0],bodies[1]);self.assertEqual(bodies[0],bodies[2])
