import copy
import json
import unittest
from unittest.mock import Mock

from luna_decisions import emergency_clears, parse_choice, parse_scores, prepare_backup, recovery_inputs
from model_comparison import comparison_cases, prepare
from openai_advisor import AdvisorError, AdvisorTimeout, OpenAIAdvisor, action_menu, public_view, request_payload
from strategy_eval import base, card, option


def crisis():
    s = base()
    s.update(game_type='GT_RANKED',mana=7,enemy_secret_count=0)
    for hero in ('my_hero','enemy_hero'):
        s[hero].update(immune=False,modifiers=[])
    s['my_hero']['health']=4
    s['my_board']=[card('BE_090',28,attack=6,health=6)]
    s['enemy_board']=[card('BE_729',58,attack=4,health=4),card('TIME_025',41,attack=9,health=4),
                      card('CAP_405',48,attack=7,health=7)]
    for c in s['my_board']+s['enemy_board']:
        c.update(immune=False,divine_shield=False,reborn=False,dormant=False,deathrattle=False,modifiers=[])
    s['my_hand']=[card('BE_086',18,cost=5),card('BE_086',25,cost=5),card('BE_728',23,cost=4)]
    s['legal_options']=[option(18),option(25),option(23),option(28,2,41,48,58)]
    return s


class DecisionsTests(unittest.TestCase):
    def test_double_clear_gate_uses_observed_stats_and_legal_actions(self):
        s=crisis()
        def allowed(st):return emergency_clears(public_view(st),action_menu(st))
        self.assertEqual({a['source_id'] for a in allowed(s)},{18,25})
        for field,value in [('enemy_secret_count',1),('mana',4),('enemy_locations',[{'id':80}])]:
            self.assertFalse(allowed(dict(s,**{field:value})))
        for zone,index,change in [('my_hand',2,{'cost':6}),('my_hand',2,{'card_id':'CORE_UNG_205'}),
                                  ('enemy_board',1,{'frozen':True}),
                                  ('my_board',0,{'taunt':True}),('enemy_board',0,{'divine_shield':True})]:
            modified=copy.deepcopy(s);modified[zone][index].update(change)
            self.assertFalse(allowed(modified))
        s['enemy_hero']['health']=6
        self.assertFalse(allowed(s))

    def test_tax_and_freeze_previews_describe_the_specific_legal_target_and_budget(self):
        cases = {c['name']:c for c in comparison_cases()}
        def previews(name,extra_mana=0):
            view, menu = prepare(cases[name])
            view['mana'] += extra_mana
            sol,_ = request_payload('gpt-6.1-sol',view,menu)
            _,packet = recovery_inputs(sol,view,menu)
            outcomes = json.loads(packet['input'])['one_step_arithmetic']
            return [(a,outcomes[key]) for key,a in menu.items() if key in outcomes]
        for name in ('spells_before_own_tax','zakajz_cost_change_lethal_order'):
            outcome = dict(next(o for a,o in previews(name) if a.get('source_id')==101))
            spell, = outcome['spells_priced_out_if_tax_played_first']
            self.assertEqual((spell['id'],spell['cost_now'],spell['cost_after_tax']),
                             (100,outcome['mana_left_after_play'],outcome['mana_left_after_play']+1))
            self.assertFalse(any('spells_priced_out_if_tax_played_first' in o for _,o in previews(name,1)))
        values = {a['target_id']:o for a,o in previews('handbuff_dk_deny_lifesteal_race') if a.get('kind')=='play'}
        self.assertEqual((values[20]['attack_now'],values[20]['has_lifesteal']),(8,True))
        self.assertFalse(values[2]['has_lifesteal'])

    def test_bad_or_missing_decisions_cannot_name_an_action(self):
        m={'a0':{'kind':'end_turn'},'a1':{'kind':'play','source_id':18}}
        basic={'model':'gpt-6-luna','usage':{'total_tokens':10}}
        for rows in ([],[{'type':'refusal'}],[{'type':'choice','name':'next_action','choice':'ghost','confidence':1}]):
            with self.assertRaises(AdvisorError):parse_choice({**basic,'answers':rows},m)
        valid=[{'type':'score','name':k,'score':1.5} for k in m]
        for rows in (valid[:1],[valid[0],valid[0]],[valid[0],dict(valid[1],score=float('nan'))]):
            with self.assertRaises(AdvisorError):parse_scores({**basic,'answers':rows},m)
        for parse in (parse_choice,parse_scores):
            with self.assertRaises(AdvisorError):parse([],m)

    def test_tied_distinct_moves_decline_without_selecting_by_menu_order(self):
        s=crisis()
        s['enemy_secret_count']=1
        view,menu=public_view(s),action_menu(s)
        sol,_=request_payload('gpt-6.1-sol',view,menu)
        def tied(packet):
            return {'model':'gpt-6-luna','usage':{'total_tokens':17},'answers':[
                {'type':'score','name':q['name'],'score':1} for q in packet['questions']]}
        result=prepare_backup(sol,view,menu,tied)()
        self.assertNotIn('action',result)
        self.assertEqual((result['sent'],result['tokens']),(True,17))

    def test_scoring_explains_damage_and_cannot_abandon_a_verified_double_clear(self):
        s=crisis();v=public_view(s);m=action_menu(s)
        sol,_=request_payload('gpt-6.1-sol',v,m)
        _,packet=recovery_inputs(sol,v,m)
        evidence=json.loads(packet['input'])
        clear=next(k for k,a in m.items() if a.get('source_id')==18)
        facts=evidence['one_step_arithmetic'][clear]
        self.assertEqual((facts['waves'],facts['original_enemy_minion_hp_after_spell_before_triggers']['41']),(2,0))
        def score(wire):
            return {'model':'gpt-6-luna','usage':{'total_tokens':10},'answers':[
                {'type':'score','name':q['name'],'score':3 if m[q['name']]['kind']=='end_turn' else 1}
                for q in wire['questions']]}
        adv=OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'},Mock(side_effect=AdvisorTimeout('injected')),
                          decisions_transport=score)
        self.addCleanup(adv.close)
        self.assertIn(adv.select(v,m),emergency_clears(v,m))
        self.assertEqual((adv.consecutive_timeouts,adv.summary['api_responses']),(0,1))
