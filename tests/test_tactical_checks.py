import copy
import unittest
from unittest.mock import patch

from hard_cases import hard_cases
from rule_transfer import cases
from openai_advisor import CATALOG, OpenAIAdvisor
from tactical_checks import glare_survival, forced_face_attack
from strategy_eval import base, card


def fixture(state=None):
    if state is None:
        state=next(c['state'] for c in hard_cases() if c['name']=='lifesteal_overkill_survival_target')
    s=copy.deepcopy(state)
    s.update(healing_as_damage=0,healing_double=0)
    for k in ('my_hero','enemy_hero'):
        s[k].update(max_health=30,modifiers=[])
    return s


class TacticalChecksTests(unittest.TestCase):
    def test_only_plain_face_attacks_can_skip_model_but_other_choices_cannot(self):
        s=base();s['enemy_hero'].update(modifiers=[])
        s['my_board']=[card('TIME_025',10),card('TLC_603',11)]
        menu={'a0':{'kind':'attack','source_id':10,'target_id':2},
              'a1':{'kind':'attack','source_id':11,'target_id':2},'a2':{'kind':'end_turn'}}
        self.assertEqual(forced_face_attack(s,menu)['action'],menu['a0'])
        for mutate in (lambda s:s.update(enemy_secret_count=1),
                       lambda s:s.update(enemy_board=[card('CS2_172',20)]),
                       lambda s:s['enemy_hero'].update(modifiers=[{'card_id':'UNKNOWN'}]),
                       lambda s:s['my_board'][0].update(modifiers=[{'card_id':'UNKNOWN_ATTACK_TRIGGER'}])):
            changed=copy.deepcopy(s);mutate(changed)
            self.assertIsNone(forced_face_attack(changed,menu))
        self.assertIsNone(forced_face_attack(s,{**menu,'a3':{'kind':'hero_power','source_id':3}}))

    def test_original_and_changed_numbers_keep_attack_independent_of_damage(self):
        for s,attack,hp in ((fixture(),8,1),(fixture(cases()[0]['state']),10,1),(fixture(cases()[1]['state']),5,3)):
            result=glare_survival(s,[20,21],CATALOG)
            self.assertEqual(result['action']['target_id'],21)
            chosen=result['evidence'][1]
            self.assertEqual(chosen['healed'],5)
            self.assertEqual(chosen['remaining_attack'],attack)
            self.assertEqual(chosen['effective_health_after_visible_attacks'],hp)
        shield=glare_survival(fixture(cases()[1]['state']),[20,21],CATALOG)
        self.assertEqual(shield['evidence'][0]['healed'],0)
        self.assertEqual(shield['evidence'][0]['remaining_attack'],7)

    def test_healing_cap_armor_and_windfury_are_real_arithmetic(self):
        s=fixture();s['my_hero'].update(health=4,max_health=5,armor=4)
        proof=glare_survival(s,[20,21],CATALOG)
        self.assertEqual(proof['evidence'][1]['healed'],1)
        self.assertEqual(proof['evidence'][1]['effective_health_after_visible_attacks'],1)
        s=fixture();s['enemy_board'][0]['windfury']=True
        self.assertIsNone(glare_survival(s,[20,21],CATALOG))

    def test_unsupported_effects_and_other_followups_defer_to_model(self):
        mutations=[lambda s:s['my_hero'].pop('max_health'),
                   lambda s:s.update(healing_as_damage=1),lambda s:s.update(healing_double=1),
                   lambda s:s['my_hero'].update(modifiers=[{'card_id':'UNKNOWN_HEALING_EFFECT'}]),
                   lambda s:s.update(my_board=[{'id':42}]),lambda s:s.update(my_hand=[{'id':43}]),
                   lambda s:s.update(mana=1),lambda s:s.update(enemy_secret_count=1),
                   lambda s:s.update(enemy_weapon={'attack':1}),lambda s:s.update(enemy_active_rituals=[{'card_id':'BE_085'}]),
                   lambda s:s['enemy_board'][1].update(card_id='BE_861',reborn=True),
                   lambda s:s['enemy_board'][1].update(modifiers=[{'card_id':'ENRAGE'}]),
                   lambda s:s['enemy_board'][1].update(card_id='UNKNOWN'),
                   lambda s:s['enemy_board'][0].update(frozen=True)]
        for mutate in mutations:
            s=fixture();mutate(s)
            self.assertIsNone(glare_survival(s,[20,21],CATALOG))
        s=fixture();s['my_hero']['health']=15
        self.assertIsNone(glare_survival(s,[20,21],CATALOG))  # Both survive: strategic choice.

    def test_proven_target_uses_no_network_and_records_its_basis(self):
        s=fixture()
        ch={'type':'TARGET','source_card_id':'BE_726','count_min':1,'count_max':1,
            'items':[{'id':c['id'],'card_id':c['card_id']} for c in s['enemy_board']]}
        advisor=OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'},lambda p:self.fail('Unexpected API call'))
        with patch.object(advisor,'record') as record,patch('builtins.print'):
            action=advisor.target(ch,s)
        self.assertEqual(action,{'kind':'choose_target','target_id':21})
        self.assertEqual(record.call_args.args[0]['local_rule'],'glare_visible_survival_v1')
        self.assertEqual((advisor.calls,advisor.tokens),(0,0))
