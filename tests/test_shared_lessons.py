import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coaching import strategy_sample
from life_tap import life_tap_facts
from luna_decisions import recovery_inputs
from openai_advisor import CATALOG, AdvisorTimeout, OpenAIAdvisor, request_payload
from spell_order import buff_before_tax, review_action
from test_openai_advisor import response


FIXTURES = Path(__file__).parent / 'fixtures'


class SharedLessonTests(unittest.TestCase):
    def setUp(self):
        _, self.case = json.loads((FIXTURES/'standby-ranked-20261102.json').read_text())
        self.view, self.menu = self.case['observation'], self.case['actions']
        self.original = self.case['unused_luna_action']
        self.sol_first = self.case['confirmed_sol_action']

    def test_one_lesson_in_primary_backup_future_model_and_retained_receipts(self):
        wire, _ = request_payload('gpt-6.1-sol',self.view,self.menu)
        primary = json.loads(wire['input'])['observation']['buff_first_trades_by_tax_action']
        _, scored = recovery_inputs(wire,self.view,self.menu)
        self.assertEqual(primary,json.loads(scored['input'])['buff_first_trades_by_tax_action'])
        aid = next(key for key,a in self.menu.items() if a==self.original)
        def old_luna(packet):
            return {'model':'gpt-6-luna','usage':{'total_tokens':7},'answers':[
                {'type':'score','name':q['name'],'score':self.case['recorded_luna_scores'][q['name']]}
                for q in packet['questions']]}
        for model,timeout in [('gpt-6.1-sol',False),('gpt-6.1-sol',True),('test-future-advisor',False)]:
            def choose(_):
                if timeout:
                    raise AdvisorTimeout('simulated deadline')
                return dict(response(aid),model=model)
            with self.subTest(model=model,timeout=timeout), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'decisions.jsonl'
                advisor = OpenAIAdvisor({'model':model,'api_key':'offline-fake-key-for-regression'},choose,
                                        audit_path=path,decisions_transport=old_luna if timeout else None)
                with patch('builtins.print'):
                    try:
                        selected = advisor.select(copy.deepcopy(self.view),self.menu)
                        # A synthetic receipt is only used to exercise the storage path.
                        advisor.record({'event':'confirmed'})
                    finally:
                        advisor.close()
                self.assertEqual(selected,self.sol_first)
                events = list(map(json.loads,path.read_text().splitlines()))
                p = next(e for e in events if e['event']=='proposed')
                role = 'backup' if timeout else 'primary'
                self.assertEqual((p['model_action'],p['tactical_lesson']['rule']),(self.original,'buff_before_spell_tax_v1'))
                self.assertEqual(advisor.summary['lesson_corrections'],{role:1})
                self.assertEqual(strategy_sample(events)['cases']['lesson_'+role]['reviewed_action'],selected)

    def test_transfer_and_cases_in_which_the_original_choice_must_stand(self):
        # One more point of enemy Health leaves a different friendly target as the only valid buff.
        stronger = copy.deepcopy(self.view)
        for foe in stronger['enemy_board']:
            if foe.get('taunt'):
                foe['attack']+=1; foe['health']+=1
                foe['modifiers'].append({'card_id':'TIME_026e'})
        shuffled = {'x'+str(i):a for i,a in enumerate(reversed(list(self.menu.values())))}
        revised,proof = review_action(shuffled,self.original,buff_before_tax(stronger,shuffled))
        self.assertEqual(revised,{'kind':'play','source_id':55,'target_id':49})
        self.assertEqual(proof['attacker_health_after_trade'],2)
        unrelated = {'kind':'play','source_id':57}
        self.assertEqual(review_action(self.menu,unrelated,buff_before_tax(self.view,self.menu)),(unrelated,None))
        for key,value in [('mana',5),('mana',3),('enemy_secret_count',1),('my_board_slots_used',7),
                          ('unavailable_interactions',[{'source_id':99}]),('enemy_player_effects',[{'card_id':'unknown'}])]:
            self.assertFalse(buff_before_tax(dict(self.view,**{key:value}),self.menu),key)
        for field in ('divine_shield','deathrattle','reborn','poisonous'):
            with self.subTest(field=field):
                v=copy.deepcopy(self.view)
                for c in v['enemy_board']:
                    if c['taunt']:c[field]=True
                self.assertFalse(buff_before_tax(v,self.menu))
        no_attacks = {k:a for k,a in self.menu.items() if a.get('kind')!='attack'}
        self.assertFalse(buff_before_tax(self.view,no_attacks))
        safe=copy.deepcopy(self.view)
        safe['my_board'][0].update(attack=5,health=6)
        self.assertFalse(buff_before_tax(safe,self.menu))

    def test_sol_freeze_draw_case_is_conditional_and_scoped(self):
        r=json.loads((FIXTURES/'shared-freeze-out-20261102.json').read_text())
        v,menu=r['observation'],r['actions']
        def fact(view):
            return (life_tap_facts(view,menu,CATALOG) or {}).get('conditional_freeze_draw')
        facts=fact(v)
        self.assertIn('uncertain',facts['count_basis'])
        self.assertEqual(facts['if_drawn_and_playable'],[{'target_id':11,'attack_prevented':8,
                                                         'remaining_visible_attack':3,'health_margin':1}])
        self.assertIsNone(fact(dict(v,my_board_slots_used=7)))
        self.assertIsNone(fact(dict(v,mana=2)))
        self.assertIsNone(fact(dict(v,my_deck_count=0)))
        for cost,value in [('my_hero',{'health':2}),('enemy_board',{'stealth':True})]:
            changed=copy.deepcopy(v)
            (changed[cost][0] if isinstance(changed[cost],list) else changed[cost]).update(value)
            self.assertIsNone(fact(changed))
        deck=v['strategy']['deck']
        deck['reconciled_remaining_counts']={'CORE_UNG_205':0}
        self.assertIsNone(fact(v))
        deck['reconciled_remaining_counts']={'CORE_UNG_205':1}
        self.assertEqual(fact(v)['count_basis'],'reconciled deck')

    def test_native_style_advisor_can_flush_its_audit_without_pythonpath(self):
        bridge=Path(__file__).resolve().parents[1]/'runtime/state-bridge'
        with tempfile.TemporaryDirectory() as tmp:
            script=("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                    "from openai_advisor import OpenAIAdvisor; "
                    "a=OpenAIAdvisor({'api_key':'offline-fake-key-for-regression'},audit_path=Path('audit.jsonl')); "
                    "a.close(); assert Path('decision-summary.json').is_file()")
            env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}
            subprocess.run([sys.executable,'-I','-B','-c',script,str(bridge)],cwd=tmp,env=env,
                           check=True,capture_output=True,timeout=10)
