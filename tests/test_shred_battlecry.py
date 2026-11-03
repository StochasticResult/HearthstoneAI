import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import attack_plan
from incremental_board import IncrementalBoard
from life_tap import life_tap_facts
from luna_decisions import prepare_backup
from luna_decisions_eval import replay_state
from openai_advisor import CATALOG, OpenAIAdvisor, action_menu, request_payload
from resource_facts import shred_battlecry_menu
from test_incremental_board import fixture,power
from test_openai_advisor import response


class ShredTests(unittest.TestCase):
    def setUp(self):
        self.case=json.loads((Path(__file__).parent/'fixtures/velocidrake-shreds-20261102.json').read_bytes())
        self.view,self.menu=self.case['observation'],self.case['actions']
        self.drake=self.case['old_primary_action']

    def test_only_publicly_revealed_generated_shards_still_in_our_deck_are_counted(self):
        parser=IncrementalBoard()
        parser.feed(fixture())
        def feed(*lines):
            parser.feed(''.join(power(s) for s in lines).encode())
        def create(eid,cid,owner,zone):
            feed(f'FULL_ENTITY - Creating ID={eid} CardID={cid}',f'    tag=ZONE value={zone}',
                 f'    tag=CONTROLLER value={owner}','    tag=CARDTYPE value=SPELL')
        def insert(eid,owner,creator,reveal=True):
            create(eid,'',owner,'DECK')
            feed(f'TAG_CHANGE Entity={eid} tag=DISPLAYED_CREATOR value={creator}')
            if reveal:
                feed(f'SHOW_ENTITY - Updating Entity={eid} CardID=TIME_025t',
                     f'HIDE_ENTITY - Entity={eid} tag=ZONE value=DECK')
        create(201,'TIME_026',1,'PLAY')
        create(202,'TIME_026',2,'PLAY')
        insert(211,1,201);insert(212,1,201);insert(213,2,202);insert(214,1,201,False)
        insert(215,1,202)  # Wrong controller on the claimed creator.
        def ours():
            return {c['id'] for c in parser.snapshot(1,allow_practice=True,extended=True)['own_known_deck']
                    if c['card_id']=='TIME_025t'}
        self.assertEqual(ours(),{211,212})
        feed('SHOW_ENTITY - Updating Entity=211 CardID=TIME_025t','HIDE_ENTITY - Entity=211 tag=ZONE value=DECK')
        self.assertEqual(ours(),{211,212})
        feed('TAG_CHANGE Entity=211 tag=ZONE value=HAND','TAG_CHANGE Entity=211 tag=ZONE value=DECK',
             'CHANGE_ENTITY - Updating Entity=212 CardID=CS2_172','HIDE_ENTITY - Entity=212 tag=ZONE value=DECK')
        self.assertFalse(ours())
        insert(216,1,201);feed('TAG_CHANGE Entity=216 tag=CONTROLLER value=2')
        self.assertNotIn(216,parser.revealed_deck_shreds)
        parser.feed(fixture())
        self.assertFalse(parser.revealed_deck_shreds)

    def test_known_self_lethal_is_excluded_from_primary_and_standby(self):
        aid=next(k for k,a in self.menu.items() if a==self.drake)
        self.assertEqual(self.case['old_shadow_action'],self.drake)
        def primary(packet):
            body=json.loads(packet['input'])
            self.assertNotIn(aid,body['actions'])
            self.assertEqual(body['observation']['shred_battlecry']['known_shreds_at_least'],4)
            return response(next(k for k,a in body['actions'].items() if a.get('kind')=='attack'))
        advisor=OpenAIAdvisor({'api_key':'offline-test-no-credentials'},primary)
        self.addCleanup(advisor.close)
        with patch('builtins.print'):
            self.assertNotEqual(advisor.select(copy.deepcopy(self.view),self.menu),self.drake)
        def secondary(packet):
            body=json.loads(packet['input'])
            self.assertNotIn(aid,body['candidate_moves'])
            questions=packet['questions']
            return {'model':'gpt-6-luna','usage':{'total_tokens':1},'answers':[
                {'name':q['name'],'type':'score','score':3 if i==0 else 0} for i,q in enumerate(questions)]}
        wire,_=request_payload('gpt-6.1-sol',self.view,self.menu)
        result=prepare_backup(wire,self.view,self.menu,secondary)()
        self.assertIn(result['action'],self.menu.values())
        self.assertNotEqual(result['action'],self.drake)

    def test_unknown_shard_protection_and_nonlethal_health_remain_distinct(self):
        for change in ('empty','unknown','armor','immune','unknown_effect','unknown_body'):
            with self.subTest(change=change):
                v=copy.deepcopy(self.view)
                if change in ('empty','unknown'):
                    v['own_known_deck']=[]
                    v['strategy']['deck']['reconciled_remaining_counts']={} if change=='empty' else None
                elif change=='armor':v['my_hero']['armor']=1
                elif change=='immune':v['my_hero']['immune']=True
                elif change=='unknown_effect':v['my_player_effects']=[{'card_id':'unmodeled'}]
                else:v['my_board'][0]['card_id']='unmodeled'
                observed,safe=shred_battlecry_menu(v,self.menu,CATALOG)
                self.assertIn(self.drake,safe.values())
                if change in ('empty','unknown'):
                    self.assertEqual(observed['shred_battlecry']['known_absent'],change=='empty')

    def test_an_already_cached_play_is_screened_again_after_its_receipt(self):
        state=replay_state(dict(self.case,server_options_id=self.case['options_id']))
        def choose(packet):
            actions=json.loads(packet['input'])['actions']
            return response(next(key for key,a in actions.items() if a['kind']=='end_turn'))
        advisor=OpenAIAdvisor({'api_key':'offline-test-no-credentials'},choose)
        self.addCleanup(advisor.close)
        view=advisor.view(state)
        menu=action_menu(state)
        view['recent_confirmed_actions']=[]
        tap=life_tap_facts(view,menu,CATALOG)
        if tap:view['life_tap']=tap
        advisor.pending_attack={'origin':0,'decision_id':0,'turn':view['game_turn'],'confirmed_options_id':84,
            'expected':attack_plan.fingerprint(view),'menu':attack_plan.menu_key(menu),'corpses':view.get('enemy_corpses'),
            'first':{'kind':'attack','source_id':187,'target_id':8},'second':self.drake,'remaining':[]}
        with patch('builtins.print'):
            self.assertEqual(advisor.ask(state),{'kind':'end_turn'})
        self.assertEqual((advisor.summary['turn_plans_discarded'],advisor.calls),(1,1))
