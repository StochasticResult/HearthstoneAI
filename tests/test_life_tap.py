import json
import unittest
from unittest.mock import patch
from openai_advisor import OpenAIAdvisor, CATALOG, request_payload
from life_tap import life_tap_facts

TAP = {'kind':'hero_power','source_id':57}
END = {'kind':'end_turn'}


def body(i, atk, hp, **flags):
    return dict(id=i, card_id='TLC_603', attack=atk, health=hp, **flags)


def priest():
    # Prior to the 09:53 Priest game's last Tap. Only confirmed public bodies
    # and legal attacks from two still-ready sources are needed for the trade.
    v = dict(my_hero_power={'id':57,'card_id':'HERO_07bp','cost':2},
             my_hero={'health':14,'armor':0}, mana=8,my_deck_count=18,my_fatigue=0,
             my_board=[body(203,6,8,deathrattle=True),body(197,4,3)],
             enemy_board=[body(185,5,6),body(186,5,3),body(188,5,6),body(189,5,3),body(108,1,1,deathrattle=True)],
             my_hand=[])
    menu={'tap':TAP,'end':END}
    for c in v['my_board']:
        for t in v['enemy_board']:
            menu[str(len(menu))]={'kind':'attack','source_id':c['id'],'target_id':t['id']}
    return v,menu


class TapTests(unittest.TestCase):
    def test_ghuun_health_penalty_is_already_in_the_observed_health(self):
        view, _ = priest()
        hero = {'id':68,'health':2,'armor':0,'immune':False,'modifiers':[{'card_id':'BE_287e2'}]}
        view.update(my_hero=hero, my_board=[], my_player_effects=[], my_deck_count=12,
                    strategy={'deck':{'reconciled_remaining_counts':{'TIME_025t':2}}})
        menu = {'tap':TAP,'end':END}
        def facts(current, catalog=CATALOG):
            return life_tap_facts(dict(view,my_hero=current), menu, catalog)
        for health, armor, base, single in ((2,0,0,-3), (7,0,5,2), (2,4,4,1)):
            with self.subTest(health=health,armor=armor):
                result = facts(dict(hero,health=health,armor=armor))
                self.assertEqual(result['effective_health_tap_before_other_effects'], base)
                self.assertEqual(result['if_one_shred_at_printed_damage']['effective_health'], single)
        for change in ({'modifiers':None}, {'modifiers':[{'card_id':'BE_287e2'},{'card_id':'UNKNOWN'}]},
                       {'unknown_modifier_count':1}, {'divine_shield':True}):
            self.assertNotIn('if_one_shred_at_printed_damage', facts(dict(hero,**change)))
        changed = dict(CATALOG, BE_287e2={'type':'ENCHANTMENT','text':'Your hero takes 1 less damage.'})
        self.assertNotIn('if_one_shred_at_printed_damage', facts(hero,changed))

    def test_real_priest_trade_margin_and_legal_payload(self):
        v,m=priest();facts=life_tap_facts(v,m,CATALOG)
        self.assertEqual((facts['unblocked_board_and_weapon_attack'],facts['unblocked_attack_after_example']), (21,11))
        self.assertEqual((facts['margin_hold'],facts['margin_tap']),(3,1))
        request,_=request_payload('gpt-6.1-sol',{**v,'life_tap':facts},m)
        parsed=json.loads(request['input'])
        self.assertEqual(parsed['observation']['life_tap'],facts)
        self.assertIn(TAP,parsed['actions'].values())
        self.assertEqual(len(facts['example_trades']),2)

    def test_ruthless_and_double_activation_are_separate_from_weapon_and_board(self):
        v,m=priest();v.update(my_hero={'health':18,'armor':0},my_board=[body(3,7,7)],enemy_board=[body(7,12,12),body(4,1,3),body(9,3,3,taunt=True,deathrattle=True)],
                              my_player_effects=[{'card_id':'TIME_064e'}],enemy_hero_power={'card_id':'CATA_190p','cost':2})
        fact=life_tap_facts(v,m,CATALOG)
        self.assertEqual((fact['effective_health_hold'],fact['effective_health_tap_before_other_effects']),(18,14))
        self.assertEqual(fact['enemy_hero_power_attack_if_usable'],5)
        self.assertEqual(fact['draws'],2)

    def test_at_high_health_or_when_tap_illegal_context_is_absent(self):
        v,m=priest();v['my_hero']['health']=30;v['enemy_board']=[]
        self.assertIsNone(life_tap_facts(v,m,CATALOG))
        v['my_hero']['health']=14
        self.assertIsNone(life_tap_facts(v,{'end':END},CATALOG))
        v['my_hero_power']['card_id']='HERO_08bp'
        self.assertIsNone(life_tap_facts(v,m,CATALOG))

    def test_shreds_with_an_unreconciled_deck_reach_both_model_inputs(self):
        from luna_decisions import tactical_payload
        v,m=priest()
        v.update(my_hero={'id':68,'health':7,'armor':0,'immune':False,'modifiers':[]},
                 my_board=[],enemy_board=[body(61,8,11,frozen=True)],my_deck_count=16,
                 my_player_effects=[],own_known_deck=[{'id':86,'card_id':'TIME_025t'},
                     {'id':127,'card_id':'TIME_025t'}, {'id':86,'card_id':'TIME_025t'}])
        menu={'tap':TAP,'end':END}
        facts=life_tap_facts(v,menu,CATALOG)
        self.assertNotIn('verified_shreds_in_deck',facts)
        self.assertEqual(facts['known_shreds_at_least'],2)
        self.assertEqual(facts['effective_health_tap_before_other_effects'],5)
        branch=facts['if_one_shred_at_printed_damage']
        self.assertEqual((branch['effective_health'],branch['margin_after_example_trades']),(2,2))
        primary,_=request_payload('gpt-6.1-sol',dict(v,life_tap=facts),menu)
        backup=tactical_payload(dict(v,life_tap=facts),menu)
        self.assertEqual(json.loads(primary['input'])['observation']['life_tap'],facts)
        self.assertEqual(json.loads(backup['input'])['public_position']['life_tap'],facts)
        self.assertIn(TAP,json.loads(primary['input'])['actions'].values())

        exact=dict(v, strategy={'deck':{'reconciled_remaining_counts':{'TIME_025t':3}}})
        both=life_tap_facts(exact,menu,CATALOG)
        self.assertEqual((both['verified_shreds_in_deck'],both['known_shreds_at_least']),(3,3))
        for change in ({'own_known_deck':[]}, {'my_deck_count':0}, {'my_hand':[{}]*10},
                       {'my_player_effects':[{'card_id':'TIME_064e'}]},
                       {'my_hero':dict(v['my_hero'],modifiers=[{'card_id':'unknown'}])},
                       {'my_board':[body(3,1,1,spell_damage=1)]}):
            self.assertNotIn('if_one_shred_at_printed_damage',life_tap_facts(dict(v,**change),menu,CATALOG))

    def test_frozen_dormant_shield_reborn_and_shared_attackers(self):
        v,m=priest();v.update(my_board=[body(203,5,5)],enemy_board=[body(185,6,1,frozen=True),body(186,6,1,dormant=True),
            body(188,4,1,divine_shield=True),body(189,2,1,reborn=True),body(108,1,1,windfury=True)])
        f=life_tap_facts(v,m,CATALOG);self.assertEqual(f['unblocked_board_and_weapon_attack'],8)
        self.assertEqual(f['example_trades'],[[203,108]])
        v['enemy_board'][4]['cant_attack_heroes']=True
        self.assertEqual(life_tap_facts(v,m,CATALOG)['example_trades'],[])

    def test_fatigue_weapon_and_an_unknown_deck_are_conditional(self):
        v,m=priest();v.update(my_hero={'health':10,'armor':3},my_deck_count=0,my_fatigue=2,
                               my_player_effects=[{'card_id':'TIME_064e'}],my_board=[],enemy_board=[],
                               enemy_weapon={'attack':3,'durability':2,'windfury':True})
        f=life_tap_facts(v,m,CATALOG)
        self.assertEqual((f['fatigue_if_no_casts_when_drawn'],f['effective_health_tap_before_other_effects']),(7,2))
        self.assertEqual((f['margin_hold'],f['margin_tap']),(7,-4))
        v['my_deck_count']=None
        self.assertIsNone(life_tap_facts(v,m,CATALOG))

    def test_urgent_draw_is_still_available_to_the_normal_selector(self):
        v,m=priest();v.update(my_hero={'health':7,'armor':0},my_board=[],enemy_board=[body(5,9,9)])
        a=OpenAIAdvisor({'api_key':'offline-test-placeholder'})
        def choose(view,menu,*,server_options_id):
            self.assertEqual(server_options_id,17)
            self.assertEqual(view['life_tap']['margin_hold'],-2)
            self.assertEqual(view['life_tap']['margin_tap'],-4)
            return menu['tap']
        with patch.object(a,'view',return_value=v),patch('openai_advisor.action_menu',return_value=(m,[])), \
             patch('tactical_checks.forced_face_attack',return_value=None),patch.object(a,'select',side_effect=choose):
            self.assertEqual(a.ask({'options_id':17}),TAP)


class TurnPaceTests(unittest.TestCase):
    def test_long_turn_uses_only_current_legal_attacks_and_resets_next_turn(self):
        a=OpenAIAdvisor({'api_key':'offline-test-placeholder'})
        v=dict(game_turn='16',phase='main',my_board=[body(201,17,17),body(177,12,12)],
               enemy_hero={'id':70},my_hero={'id':68,'attack':0})
        menu={'a':{'kind':'attack','source_id':201,'target_id':70},'b':{'kind':'attack','source_id':177,'target_id':123}}
        with patch('openai_advisor.time.monotonic',side_effect=[100.,110.,125.,145.,160.]):
            a.note_turn_pace(v,menu);a.note_turn_pace(v,menu);self.assertNotIn('turn_pace',v)
            a.note_turn_pace(v,menu)
            self.assertEqual(v['turn_pace']['legal_face_attackers_now'],[[201,17]])
            self.assertEqual(v['turn_pace']['seconds_since_first_decision'],25)
            fresh=dict(v,game_turn='18');del fresh['turn_pace']
            a.note_turn_pace(fresh,menu);self.assertNotIn('turn_pace',fresh)
            a.note_turn_pace(fresh,menu);self.assertNotIn('turn_pace',fresh)

    def test_optional_coaching_at_size_boundary_preserves_all_legal_options(self):
        v,m=priest();base,size=request_payload('gpt-6.1-sol',v,m)
        v['life_tap']={'note':'X'*65000};v['turn_pace']={'note':'test'}
        payload,result=request_payload('gpt-6.1-sol',v,m)
        decoded=json.loads(payload['input'])
        self.assertLess(result,64000)
        self.assertNotIn('life_tap',decoded['observation'])
        self.assertEqual(payload['tools'][0]['parameters']['properties']['action_id']['enum'],list(m))
        self.assertEqual(len(decoded['action_table']['rows']),len(m))
