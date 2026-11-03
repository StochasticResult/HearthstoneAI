import copy
import json
import unittest
from pathlib import Path

from luna_decisions import recovery_inputs
from openai_advisor import request_payload
from resource_facts import blasphemy_pressure


class BlasphemyPressureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.win,cls.egg=json.loads((Path(__file__).parent/'fixtures/blasphemy-pressure-20261102.json').read_text())

    def test_same_real_spell_and_two_attacks_reach_sol_and_luna(self):
        v,m=self.win['view'],self.win['menu']
        before=copy.deepcopy(m)
        sol,_=request_payload('gpt-6.1-sol',v,m)
        _,luna=recovery_inputs(sol,v,m)
        facts=json.loads(sol['input'])['blasphemy_pressure']
        self.assertEqual(json.loads(luna['input'])['blasphemy_pressure'],facts)
        f=facts['a1']
        self.assertEqual((f['waves'],f['spell_face_damage'],f['potential_followup_face_damage']),(2,4,13))
        receipts=self.win['confirmed']
        self.assertEqual(m['a1'],receipts[0]['action'])
        self.assertEqual({int(id) for id in f['one_face_swing_by_source']},{r['action']['source_id'] for r in receipts[1:]})
        self.assertTrue(all(r['action']['target_id']==v['enemy_hero']['id'] for r in receipts[1:]))
        self.assertEqual(m,before)

    def test_surviving_or_replacement_taunt_and_secret_do_not_predict_open_face(self):
        self.assertFalse(blasphemy_pressure(self.egg['view'],self.egg['menu']))
        for change in ('secret','shield','higher_cost','reborn','immune','missing card text'):
            with self.subTest(change=change):
                v=copy.deepcopy(self.win['view'])
                if change=='secret':v['enemy_secret_count']=1
                if change=='shield':v['enemy_board'][0]['divine_shield']=True
                if change=='higher_cost':v['my_hand'][0]['cost']=6
                if change=='reborn':v['enemy_board'][0]['reborn']=True
                if change=='immune':v['enemy_hero']['immune']=True
                if change=='missing card text':v['card_definitions']={}
                self.assertFalse(blasphemy_pressure(v,self.win['menu']))

    def test_counts_only_current_attacks_newly_unblocked_once(self):
        m=copy.deepcopy(self.win['menu'])
        m['extra']={'kind':'attack','source_id':49,'target_id':64}
        v=copy.deepcopy(self.win['view'])
        v['my_board'][0]['windfury']=True
        self.assertEqual(blasphemy_pressure(v,m)['a1']['one_face_swing_by_source'],{103:6})
        for flag in ('rush','cant_attack_heroes','frozen','exhausted'):
            v['my_board'][0][flag]=True
            self.assertFalse(blasphemy_pressure(v,m))
            v['my_board'][0][flag]=False
