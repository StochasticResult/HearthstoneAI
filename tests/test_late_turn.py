import copy
import json
import sys
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from attack_plan import shield_trades, prepare, mismatch_reason
from decision_standby import DecisionStandby
from luna_decisions import recovery_inputs
from luna_decisions_eval import replay_state
from openai_advisor import OpenAIAdvisor, AdvisorError, TurnWindowBackup, primary_with_ready_backup
from test_openai_advisor import response


class LateTurnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows=json.loads((Path(__file__).parent/'fixtures/shield-trades-20261102.json').read_text())['rows']

    def advisor(self, transport):
        ai=OpenAIAdvisor({'api_key':'offline-credentials-for-replay','reasoning_effort':'medium','compact_output':True},transport)
        self.addCleanup(ai.close)
        return ai

    @patch('builtins.print')
    def test_two_actual_windfury_pairs_one_request_per_pair_four_server_receipts(self,_):
        for first,second in ((self.rows[0],self.rows[1]),(self.rows[2],self.rows[3])):
            payloads=[]
            def choose(payload):
                payloads.append(payload)
                b=json.loads(payload['input']);actions=b['actions']
                first_id=next(k for k,a in first['actions'].items() if a==first['confirmed_action'])
                pair=first_id+'+'+first_id
                self.assertIn(pair,actions)
                reply=response(pair)
                args={'reason':'survive','action_id':pair}
                if 'follow_up_ids' in payload['tools'][0]['parameters']['properties']:args['follow_up_ids']=[]
                reply['output'][0]['arguments']=json.dumps(args)
                return reply
            ai=self.advisor(choose)
            for row in (first,second):
                s=replay_state(row)
                move=ai.ask(s)
                self.assertEqual(move,row['confirmed_action'])
                ai.confirm(move,s)
            self.assertEqual((len(payloads),ai.summary['confirmed_decisions'],ai.summary['turn_plan_steps']),(1,2,1))

    def test_no_followup_on_changed_shield_turn_menu_or_missing_new_receipt(self):
        a,b=self.rows[:2]
        ai=self.advisor(None)
        def view(row):
            v=ai.view(replay_state(row))
            from life_tap import life_tap_facts
            from card_metadata import CATALOG
            v['life_tap']=life_tap_facts(v,row['actions'],CATALOG)
            return v
        v,w=view(a),view(b);m=a['actions'];k=next(i for i,x in m.items() if x==a['confirmed_action'])
        plan=prepare(v,m,shield_trades(v,m)[k+'+'+k],1)
        self.assertEqual(mismatch_reason(plan,w,b['actions'],51,1),'missing_matching_receipt')
        plan['confirmed_options_id']=50
        self.assertEqual(mismatch_reason(plan,w,b['actions'],50,1),'no_new_option_group')
        self.assertIsNone(mismatch_reason(plan,w,b['actions'],51,1))
        odd=copy.deepcopy(w);odd['enemy_board'][1]['divine_shield']=True
        self.assertEqual(mismatch_reason(plan,odd,b['actions'],51,1),'public_state_changed')
        self.assertEqual(mismatch_reason(plan,dict(w,game_turn='13'),b['actions'],51,1),'turn_changed')
        self.assertEqual(mismatch_reason(plan,w,{},51,1),'legal_menu_changed')
        self.assertFalse(shield_trades(dict(v,enemy_secret_count=1),m))

    def test_two_hit_defense_exposes_pressure_and_face_tradeoff_to_both_models(self):
        r=self.rows[-1];m=r['actions'];ai=self.advisor(None);v=ai.view(replay_state(r))
        from openai_advisor import request_payload
        from life_tap import life_tap_facts
        from card_metadata import CATALOG
        v['life_tap']=life_tap_facts(v,m,CATALOG)
        primary,_=request_payload('gpt-6.1-sol',v,m)
        _,standby=recovery_inputs(primary,v,m)
        hints=json.loads(primary['input'])['conditional_shield_trades']
        self.assertEqual(hints,json.loads(standby['input'])['conditional_shield_trades'])
        line=hints['a6+a11']['shield_trade']
        self.assertEqual([line[k] for k in ('target','unblocked_enemy_attack_now','unblocked_enemy_attack_after_pair',
                         'breaker_hp_after_first','finisher_hp_after_second','face_damage_forgone_if_available')],[175,13,8,3,3,17])

    @patch('builtins.print')
    def test_vanishing_shard_freezes_the_dragon_before_another_request(self,_):
        from card_metadata import CATALOG
        from tactical_checks import expiring_freeze
        row=json.loads((Path(__file__).parent/'fixtures/expiring-shard-20261102.json').read_text())
        ai=self.advisor(lambda _:self.fail('clear last-window defense must not wait for Sol'))
        state=replay_state(row)
        ai.pace_turn=state['game_turn']; ai.pace_started=time.monotonic()-60
        with patch.object(ai,'record',wraps=ai.record) as record:
            self.assertEqual(ai.ask(state),row['recorded_followup'])
        proposal=record.call_args.args[0]
        self.assertEqual((proposal['local_rule'],proposal['action']['target_id'],ai.calls),
                         ('expiring_freeze_before_rope_v1',16,0))
        self.assertEqual(proposal['calculation']['printed_attack'],6)
        self.assertEqual(ai.summary['confirmed_decisions'],0)
        view=ai.view(state); menu=row['actions']
        self.assertTrue(next(c for c in view['my_hand'] if c['id']==60)['discarded_at_end_of_turn'])
        self.assertIsNone(expiring_freeze(view,menu,None,CATALOG))
        self.assertIsNone(expiring_freeze(view,menu,23,CATALOG))
        for flag in ('frozen','cant_be_frozen','cant_attack_heroes'):
            changed=copy.deepcopy(view); changed['enemy_board'][0][flag]=True
            self.assertIsNone(expiring_freeze(changed,menu,15,CATALOG))
        normal=copy.deepcopy(view)
        next(c for c in normal['my_hand'] if c['id']==60)['modifiers']=[]
        self.assertIsNone(expiring_freeze(normal,menu,15,CATALOG))
        face=dict(menu,face={'kind':'attack','source_id':162,'target_id':64})
        self.assertIsNone(expiring_freeze(view,face,15,CATALOG))
        self.assertIsNone(expiring_freeze(view,dict(menu,power={'kind':'hero_power','source_id':67}),15,CATALOG))
        competitor=copy.deepcopy(view); competitor['enemy_board'][1]['attack']=7
        self.assertIsNone(expiring_freeze(competitor,menu,15,CATALOG))
        shifted=copy.deepcopy(view)
        shifted['enemy_board'][0]['id']=233
        new_menu={k:dict(a,target_id=233) if a.get('target_id')==16 else a for k,a in menu.items()}
        self.assertEqual(expiring_freeze(shifted,new_menu,9,CATALOG)['action']['target_id'],233)

    @patch('builtins.print')
    def test_lower_effort_tracks_the_turn_clock_not_a_losing_health_total(self,_):
        row=self.rows[-1];cases=[(5,75,'medium'),(49,75,'medium'),(50,75,'low'),(60,None,'medium'),(25,50,'low'),(2,15,'low')]
        for elapsed,clock,desired in cases:
            with self.subTest(elapsed=elapsed,clock=clock):
                def choose(payload):
                    self.assertEqual(payload['reasoning']['effort'],desired)
                    reply=response('a0');args={'reason':'tempo','action_id':'a0'}
                    if 'follow_up_ids' in payload['tools'][0]['parameters']['properties']:args['follow_up_ids']=[]
                    reply['output'][0]['arguments']=json.dumps(args);return reply
                ai=self.advisor(choose);s=replay_state(row);s['turn_timeout_seconds']=clock
                ai.pace_turn=s['game_turn'];ai.pace_started=time.monotonic()-elapsed
                self.assertEqual(ai.ask(s),row['actions']['a0'])

    def test_remaining_moves_budget_uses_real_swings_instead_of_attack_targets(self):
        ai = self.advisor(lambda _: self.fail('replay must not use the network'))
        row = self.rows[0]
        view = ai.view(replay_state(row))
        view['turn_timeout_seconds'] = 75
        ai.pace_turn = view['game_turn']
        ai.pace_started = time.monotonic() - 27
        # The original 12th-turn calls took 9.346s and 11.405s.
        ai.recent_medium_ms = [9346, 11405]
        budget = ai.time_pressure(view, row['actions'])
        self.assertEqual((budget['reason'], budget['legal_swings']), ('remaining_moves', 6))
        self.assertGreater(budget['estimated_work_seconds'], budget['remaining_seconds_upper'])
        self.assertEqual(ai.time_pressure(dict(view, game_type='GT_VS_AI'), row['actions']), {})
        self.assertEqual(ai.time_pressure(dict(view, turn_timeout_seconds=None), row['actions']), {})
        last = self.rows[-1]
        one_source = {k:v for k,v in last['actions'].items() if v.get('kind') != 'attack' or v['source_id'] == 26}
        self.assertGreater(sum(a.get('kind') == 'attack' for a in one_source.values()), 1)
        self.assertEqual(ai.time_pressure(ai.view(replay_state(last)), one_source), {})
        ai.pace_started = time.monotonic() - 3
        self.assertEqual(ai.time_pressure(view, row['actions']), {})
        ai.pace_started = time.monotonic() - 27
        ai.recent_medium_ms = [3200, 3400]
        self.assertEqual(ai.time_pressure(view, row['actions']), {})

    def test_primary_or_same_window_eligible_standby_and_no_spurious_timeout(self):
        cmd=[sys.executable,'-B','-c','import time;time.sleep(.05);print("sol")']
        self.assertEqual(primary_with_ready_backup(cmd,'',.01,lambda:None).stdout.strip(),'sol')
        slow=[sys.executable,'-B','-c','import time;time.sleep(3)']
        with self.assertRaises(TurnWindowBackup) as result:
            primary_with_ready_backup(slow,'',.02,lambda:{'action':{'kind':'end_turn'}})
        self.assertEqual(result.exception.result['action'],{'kind':'end_turn'})
        standby=DecisionStandby(lambda *args:None)
        self.addCleanup(standby.close)
        menu={'a1':{'kind':'attack','source_id':1,'target_id':3}}
        future=Future();job={'origin':9,'future':future};standby.active=job
        self.assertIsNone(standby.take_ready(job,9,menu))
        future.set_result({'action':menu['a1'],'sent':True})
        self.assertIsNone(standby.take_ready(job,10,menu))
        self.assertIsNone(standby.take_ready(job,9,{}))
        self.assertEqual(standby.take_ready(job,9,menu)['action'],menu['a1'])

    @patch('builtins.print')
    def test_late_ranked_handoff_is_not_a_network_failure_or_a_second_input(self,_):
        row=self.rows[-1]
        selected=row['actions']['a6']
        def sol(payload):
            self.assertEqual(payload['reasoning']['effort'],'low')
            self.assertIsNotNone(ai.turn_handoff)
            primary_with_ready_backup([sys.executable,'-B','-c','import time;time.sleep(2)'],'',.03,ai.turn_handoff[1])
            self.fail('qualified standby was not consumed')
        ai=OpenAIAdvisor({'api_key':'offline-credentials-for-replay','reasoning_effort':'medium','compact_output':True},
                         sol,decisions_transport=lambda _:None)
        self.addCleanup(ai.close)
        ai.standby.delay=0
        s=replay_state(row);ai.pace_turn=s['game_turn'];ai.pace_started=time.monotonic()-66
        result={'sent':True,'mode':'score','model':'gpt-6-luna','tokens':11,'elapsed_ms':3,'action':selected}
        with patch('luna_decisions.prepare_backup',return_value=lambda:result):
            self.assertEqual(ai.ask(s),selected)
            ai.confirm(selected,s)
        self.assertEqual((ai.summary['turn_budget_backups'],ai.summary['decisions_proposed'],
                          ai.summary['confirmed_decisions'],ai.summary.get('api_timeouts',0)),(1,1,1,0))
        self.assertIsNone(ai.turn_handoff)
