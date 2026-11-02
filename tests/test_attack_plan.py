import copy
import json
import time
import unittest
from unittest.mock import patch

import attack_plan
from openai_advisor import ContextTooLarge, OpenAIAdvisor, action_menu, public_view, request_payload


def body(eid, card, attack, health, pos):
    row = dict(id=eid, card_id=card, attack=attack, health=health, position=pos,
               attacks_this_turn=0, modifiers=[])
    for flag in ('immune', 'divine_shield', 'windfury', 'mega_windfury', 'lifesteal',
                 'poisonous', 'reborn', 'deathrattle', 'dormant', 'exhausted'):
        row[flag] = False
    return row


def boards():
    # Two of the Rush trades from run-20261102-134732. The first attacker survives.
    before = dict(game_type='GT_RANKED', format='FT_STANDARD', game_turn='18', own_turn=True,
                  phase='main', mana=1, options_id=81, end_turn_allowed=True, my_hand=[],
                  my_hero={'id':74, 'health':9, 'armor':0}, enemy_hero={'id':72, 'health':29, 'armor':0},
                  my_board=[body(53,'TIME_029',10,10,1), body(233,'TIME_029',6,6,2)],
                  enemy_board=[body(28,'BE_017',7,9,1), body(25,'CATA_469',9,4,2)],
                  enemy_board_slots_used=2, enemy_corpses=19, enemy_secret_count=0,
                  legal_options=[{'id':i, 'type':'ATTACK', 'targets':[28,25]} for i in (53,233)])
    after = copy.deepcopy(before)
    after.update(options_id=82, enemy_board=after['enemy_board'][1:], enemy_board_slots_used=1,
                 enemy_corpses=20, legal_options=[{'id':233, 'type':'ATTACK', 'targets':[25]}])
    after['enemy_board'][0]['position'] = 1
    after['my_board'][0].update(health=3, exhausted=True, attacks_this_turn=1)
    return before, after


class PlannedTradeTests(unittest.TestCase):
    def setUp(self):
        self.before, self.after = boards()
        self.first = {'kind':'attack', 'source_id':53, 'target_id':28}
        self.second = {'kind':'attack', 'source_id':233, 'target_id':25}
        self.calls = []
        def transport(payload):
            menu = json.loads(payload['input'])['actions']
            pairs = [key for key, a in menu.items() if a.get('kind') == 'attack_plan'
                     and menu[a['first']] == self.first and menu[a['then_if_unchanged']] == self.second]
            self.calls.append(menu)
            selected = pairs[0] if pairs else next(key for key,a in menu.items() if a.get('kind') == 'end_turn')
            return {'status':'completed', 'model':'gpt-6.1-sol', 'usage':{'total_tokens':1}, 'output':[
                {'type':'function_call', 'name':'choose_action', 'arguments':json.dumps({'action_id':selected, 'reason':'test'})}]}
        self.ai = OpenAIAdvisor({'api_key':'offline-test-placeholder', 'model':'gpt-6.1-sol'}, transport)
        self.ai.pace_turn = '18'
        self.ai.pace_started = time.monotonic()-50

    @patch('builtins.print')
    def test_normal_entry_point_rechecks_each_combat_and_consumes_plan_once(self, _):
        self.assertEqual(self.ai.ask(self.before), self.first)
        self.ai.confirm(self.first, self.before)
        self.assertEqual(self.ai.ask(self.after), self.second)
        self.ai.confirm(self.second, self.after)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ai.summary['local_calculations'], 1)
        self.assertEqual(self.ai.summary['confirmed_decisions'], 2)
        self.assertEqual(self.ai.summary['unconfirmed_decisions'], [])
        self.assertEqual(self.ai.ask(self.after), {'kind':'end_turn'})
        self.assertEqual(len(self.calls), 2)

    @patch('builtins.print')
    def test_no_receipt_or_same_options_goes_back_to_the_selector(self, _):
        self.ai.ask(self.before)
        self.assertEqual(self.ai.ask(self.after), {'kind':'end_turn'})
        self.ai.ask(self.before)
        self.ai.confirm(self.first, self.before)
        self.assertEqual(self.ai.ask(dict(self.after,options_id=81)), {'kind':'end_turn'})
        self.ai.ask(self.before)
        self.ai.confirm(self.first, dict(self.before,options_id=79))
        self.assertEqual(self.ai.ask(self.after), {'kind':'end_turn'})
        self.assertEqual(len(self.calls), 6)

    def test_trigger_new_target_or_changed_public_state_invalidates_second_move(self):
        view = public_view(self.before)
        menu = action_menu(self.before)
        self.ai.note_turn_pace(view, menu)
        plan = next(p for p in attack_plan.options(view, menu).values()
                    if menu[p['first']] == self.first and menu[p['then_if_unchanged']] == self.second)
        saved = attack_plan.prepare(view, menu, plan, 39)
        saved['confirmed_options_id'] = 81
        for change in (lambda s:s['enemy_board'][0].update(health=5),
                       lambda s:s['my_board'][0].update(health=2),
                       lambda s:s['my_hero'].update(health=8),
                       lambda s:s.update(enemy_secret_count=1),
                       lambda s:s.update(mana=2), lambda s:s.update(game_turn='20'),
                       lambda s:s.update(enemy_corpses=22),
                       lambda s:s['legal_options'][0]['targets'].append(72),
                       lambda s:s.update(my_board=list(reversed(s['my_board'])))):
            changed = copy.deepcopy(self.after)
            change(changed)
            self.assertIsNone(attack_plan.checked_action(saved, public_view(changed), action_menu(changed), 82, 39))
        self.assertIsNone(attack_plan.checked_action(saved, public_view(self.after), action_menu(self.after), 82, 40))

    @patch('builtins.print')
    def test_optional_pairs_yield_to_original_request_at_payload_limit(self, _):
        def capped(model, view, menu, *args, **kwargs):
            if any(a['kind'] == 'attack_plan' for a in menu.values()):
                raise ContextTooLarge('artificial size boundary')
            return request_payload(model,view,menu,*args,**kwargs)
        with patch('openai_advisor.request_payload', side_effect=capped):
            self.assertEqual(self.ai.ask(self.before), {'kind':'end_turn'})
        self.assertEqual(self.calls, [action_menu(self.before)])
        self.assertIsNone(self.ai.pending_attack)

    def test_less_expensive_rush_trade_visible_before_optional_buff(self):
        v = public_view(self.before)
        v['my_board'][1]['attack'] = 5
        m = {**action_menu(self.before), 'buff':{'kind':'play','source_id':70,'target_id':53}}
        self.ai.note_turn_pace(v,m)
        facts = v['turn_pace']['removals_before_other_effects']
        self.assertIn({'attacker_id':233, 'target_id':25, 'target_attack':9, 'target_health':4},facts)
        self.assertFalse(attack_plan.options(v,m))

    def test_secrets_mixed_options_and_nonstandard_combat_offer_only_single_actions(self):
        m = action_menu(self.before)
        v = public_view(self.before)
        self.ai.note_turn_pace(v,m)
        self.assertTrue(attack_plan.options(v,m))
        self.assertFalse(attack_plan.options(dict(v,turn_pace={}),m))
        self.assertFalse(attack_plan.options(dict(v,enemy_secret_count=1),m))
        self.assertFalse(attack_plan.options(dict(v,unavailable_interactions=[{}]),m))
        self.assertFalse(attack_plan.options(v,dict(m,p={'kind':'play','source_id':1})))
        self.assertFalse(attack_plan.options(v,dict(m,f={'kind':'attack','source_id':53,'target_id':72})))
        for status in ('divine_shield', 'immune', 'reborn', 'deathrattle'):
            changed = copy.deepcopy(v)
            changed['enemy_board'][0][status] = True
            self.assertFalse(attack_plan.options(changed,m))
        changed = copy.deepcopy(v)
        changed['my_board'][0]['health'] = 7
        self.assertFalse(attack_plan.options(changed,m))
