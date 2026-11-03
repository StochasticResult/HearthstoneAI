import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import attack_plan
from luna_decisions_eval import replay_state
from openai_advisor import OpenAIAdvisor, parse_response
from test_openai_advisor import response


class RankedTurnPlanTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent/'fixtures'/'ranked-turn-plan-20261102.json'
        fixture=json.loads(path.read_text())
        self.rows = fixture['steps']
        self.coins = fixture['coin_steps']
        self.states = [replay_state(r) for r in self.rows]
        self.calls = 0
        def choose(payload):
            self.calls += 1
            body = json.loads(payload['input'])
            menu = body['actions']
            if self.calls==1:
                expected = [r['confirmed_action'] for r in self.rows]
                a,b,c = [next(i for i,v in menu.items() if v==move) for move in expected]
                self.assertIn(a,body['turn_plan']['predictable_first_attacks'])
                result = response(a)
                result['output'][0]['arguments'] = json.dumps({'reason':'test','action_id':a,'follow_up_ids':[b,c]})
                return result
            return response(next(k for k,v in menu.items() if v['kind']=='end_turn'))
        self.ai = OpenAIAdvisor({'api_key':'offline-test-no-credentials'},choose)
        self.addCleanup(self.ai.close)

    @patch('builtins.print')
    def test_three_recorded_ranked_actions_one_request_and_three_receipts(self,_):
        for state,row in zip(self.states,self.rows):
            action = self.ai.ask(state)
            self.assertEqual(action,row['confirmed_action'])
            self.ai.confirm(action,state)
        self.assertEqual((self.calls,self.ai.summary['confirmed_decisions'],self.ai.summary['local_calculations']),(1,3,2))
        self.assertEqual((self.ai.summary['turn_plans_chosen'],self.ai.summary['turn_plan_steps']),(1,2))
        self.assertIsNone(self.ai.pending_attack)

    @patch('builtins.print')
    def test_changed_state_after_second_receipt_cannot_trigger_the_third_move(self,_):
        for s in self.states[:2]:
            act = self.ai.ask(s)
            self.ai.confirm(act,s)
        third = copy.deepcopy(self.states[2])
        third['enemy_hero']['armor']+=4
        self.assertEqual(self.ai.ask(third),{'kind':'end_turn'})
        self.assertEqual((self.calls,self.ai.summary['turn_plan_steps'],self.ai.summary['turn_plans_discarded']),(2,1,1))

    @patch('builtins.print')
    def test_board_change_or_missing_first_receipt_requeries_before_any_followup(self,_):
        for cause in ('hidden_draw','new_secret','menu','same_options','no_receipt','next_turn'):
            with self.subTest(cause=cause):
                self.calls=0
                first,second=copy.deepcopy(self.states[:2])
                act=self.ai.ask(first)
                if cause!='no_receipt':self.ai.confirm(act,first)
                if cause=='hidden_draw':second['enemy_hand_count']+=1
                if cause=='new_secret':second['enemy_secret_count']=1
                if cause=='menu':second['legal_options']=second['legal_options'][:-1]
                if cause=='same_options':second['options_id']=first['options_id']
                if cause=='next_turn':second['game_turn']='10'
                self.assertEqual(self.ai.ask(second),{'kind':'end_turn'})
                self.assertEqual(self.calls,2)
                self.assertIsNone(self.ai.pending_attack)

    def test_bad_optional_sequence_never_replaces_a_valid_first_action(self):
        menu=self.rows[0]['actions']
        first=next(k for k,a in menu.items() if a==self.rows[0]['confirmed_action'])
        for invalid in (['unknown'],[first],['unknown']*7,'bad',[{}]):
            msg=response(first)
            msg['output'][0]['arguments']=json.dumps({'reason':'test','action_id':first,'follow_up_ids':invalid})
            action,_,_,tail=parse_response(msg,menu,with_plan=True)
            self.assertEqual((action,tail),(menu[first],[]))

    @patch('builtins.print')
    def test_ordinary_combat_keeps_followup_for_all_four_survival_outcomes(self,_):
        from test_attack_plan import boards
        from test_openai_advisor import response
        for friend_hp,foe_hp in ((10,9),(7,9),(7,12),(10,12)):
            with self.subTest(friend_hp=friend_hp,foe_hp=foe_hp):
                first,_unused = boards()
                first.update(my_board_slots_used=2,my_corpses=2)
                first['my_board'][0].update(health=friend_hp)
                first['enemy_board'][0].update(health=foe_hp)
                for card in first['my_board']:
                    card['stealth']=False
                next_state=copy.deepcopy(first)
                next_state['options_id']+=1
                friend,foe=next_state['my_board'][0],next_state['enemy_board'][0]
                friend['health']-=7;foe['health']-=10
                if friend['health']<=0:
                    next_state['my_board'].pop(0);next_state['my_board'][0]['position']=1
                    next_state['my_board_slots_used']=1;next_state['my_corpses']+=1
                else:friend.update(exhausted=True,attacks_this_turn=1)
                if foe['health']<=0:
                    next_state['enemy_board'].pop(0);next_state['enemy_board'][0]['position']=1
                    next_state['enemy_board_slots_used']=1;next_state['enemy_corpses']+=1
                next_state['legal_options']=[dict(id=233,type='ATTACK',targets=[u['id'] for u in next_state['enemy_board']])]
                a={'kind':'attack','source_id':53,'target_id':28}
                b={'kind':'attack','source_id':233,'target_id':25}
                calls=[]
                def choose(payload):
                    calls.append(1); moves=json.loads(payload['input'])['actions']
                    ai_id=next(k for k,v in moves.items() if v==a); bi=next(k for k,v in moves.items() if v==b)
                    msg=response(ai_id); msg['output'][0]['arguments']=json.dumps({'reason':'trade','action_id':ai_id,'follow_up_ids':[bi]})
                    return msg
                self.ai.pending_attack=None;self.ai.transport=choose
                self.assertEqual(self.ai.ask(first),a);self.ai.confirm(a,first)
                self.assertEqual(self.ai.ask(next_state),b)
                self.assertEqual(len(calls),1)

    def test_live_risk_recalculation_matches_but_newly_observed_corpses_still_require_a_decision(self):
        path=Path(__file__).parent/'fixtures'/'turn-plan-risk-20261102.json'
        for (first,second),expected in zip(json.loads(path.read_text())['cases'],('unexpected_corpse_count',None)):
            view,menu=second['observation'],second['actions']
            turn=first['decision_id']
            plan=attack_plan.continuation(first['observation'],first['actions'],first['confirmed_action'],
                                          [second['confirmed_action']],turn,turn)
            plan['confirmed_options_id']=first['options_id']
            self.assertEqual(attack_plan.mismatch_reason(plan,view,menu,second['options_id'],turn),expected)
            if expected is None:
                self.assertNotEqual(first['observation']['life_tap']['margin_tap'],view['life_tap']['margin_tap'])
                self.assertEqual(attack_plan.checked_action(plan,view,menu,second['options_id'],turn),second['confirmed_action'])
                damaged=copy.deepcopy(view)
                damaged['my_hero']['health']-=1
                self.assertEqual(attack_plan.mismatch_reason(plan,damaged,menu,second['options_id'],turn),'public_state_changed')

    @patch('builtins.print')
    def test_coin_into_previously_unaffordable_minion_rechecks_every_resource_and_option(self,_):
        original,after = [replay_state(r) for r in self.coins]
        next_minion=self.coins[1]['confirmed_action']
        self.assertNotIn(next_minion,self.coins[0]['actions'].values())
        for variant in ('matched','mana','unexpected_option','new_card','no_receipt'):
            with self.subTest(variant=variant):
                self.calls=0
                def choose(payload):
                    self.calls+=1
                    offered=json.loads(payload['input'])['actions']
                    key=next((k for k,a in offered.items() if a.get('then_if_affordable')==next_minion),None)
                    return response(key or next(k for k,a in offered.items() if a['kind']=='end_turn'))
                self.ai.transport=choose
                first=self.ai.ask(original)
                self.assertEqual(first,self.coins[0]['confirmed_action'])
                if variant!='no_receipt':self.ai.confirm(first,original)
                fresh=copy.deepcopy(after)
                if variant=='mana':fresh['mana']-=1
                if variant=='unexpected_option':fresh['legal_options']=fresh['legal_options'][:-1]
                if variant=='new_card':fresh['enemy_hand_count']+=1
                second=self.ai.ask(fresh)
                self.assertEqual(second,next_minion if variant=='matched' else {'kind':'end_turn'})
                self.assertEqual(self.calls,1 if variant=='matched' else 2)
