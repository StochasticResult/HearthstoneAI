import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hard_cases import hard_cases
from hard_comparison import MODELS,TEST_INSTRUCTIONS,trial_input,evaluate
from openai_advisor import request_payload,OpenAIAdvisor
from tactical_witness import check
from rule_transfer import cases as transfer_cases


class HardComparisonTests(unittest.TestCase):
    def test_inputs_are_equal_and_reordering_preserves_semantics(self):
        suite=hard_cases();self.assertEqual(len(suite),12)
        for case in suite:
            v0,m0=trial_input(case,0);v1,m1=trial_input(case,1)
            self.assertEqual(v0,v1)
            self.assertEqual(list(m0.values()),list(reversed(list(m1.values()))))
            self.assertTrue(all(a in m0.values() for a in case['expected']),case['name'])
            for rep in (0,1):
                view,menu=trial_input(case,rep);payloads=[]
                for model in MODELS:
                    payload,size=request_payload(model,view,menu,TEST_INSTRUCTIONS)
                    payload.pop('model');payloads.append(payload);self.assertLessEqual(size,24000)
                self.assertEqual(*payloads)
    def test_multi_action_witnesses_reach_exact_lethal(self):
        traces=[check(c) for c in hard_cases() if c.get('witness')]
        self.assertEqual(len(traces),4)
        for trace in traces:self.assertEqual(trace[-1]['enemy_health'],0)

    def test_transfer_changes_material_facts_not_only_names(self):
        suite=transfer_cases();self.assertEqual(len(suite),4)
        self.assertEqual(suite[0]['state']['my_hero']['health'],6)
        self.assertTrue(suite[1]['state']['enemy_board'][0]['divine_shield'])
        self.assertEqual(suite[2]['state']['my_board'][2]['health'],1)
        self.assertEqual(suite[3]['state']['my_hand'][2]['cost'],2)
        for c in suite:
            view,menu=trial_input(c,0)
            self.assertTrue(all(a in menu.values() for a in c['expected']))
            for model in MODELS:request_payload(model,view,menu,TEST_INSTRUCTIONS)
            if c.get('witness'):self.assertEqual(check(c)[-1]['enemy_health'],0)
    def test_48_bounded_requests_stay_blind(self):
        sent=[]
        def fake(self,p):
            sent.append(p);enum=p['tools'][0]['parameters']['properties']['action_id']['enum']
            return {'status':'completed','model':p['model'],'usage':{'total_tokens':100},'output':[{'type':'function_call','name':'choose_action','arguments':json.dumps({'action_id':enum[0],'reason':'test'})}]}
        with tempfile.TemporaryDirectory() as tmp,patch.object(OpenAIAdvisor,'_transport',fake),patch('builtins.print'):
            path=Path(tmp)/'results.jsonl';evaluate({'api_key':'fake-key-not-a-secret-123'},path)
            rows=[json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(len(sent),48);self.assertEqual(rows[-1]['summary']['api_calls'],48)
            for model in MODELS:self.assertNotIn(model,path.read_text())
            for p in Path(tmp).iterdir():self.assertNotIn('fake-key',p.read_text())
