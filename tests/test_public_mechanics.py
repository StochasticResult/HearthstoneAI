import json
import unittest
from unittest.mock import patch

from card_metadata import CATALOG, PUBLIC_OBJECTIVES
from incremental_board import IncrementalBoard, UnsafeState
from mirror_identity import practice_identity
from openai_advisor import public_view, action_menu, OpenAIAdvisor, AdvisorError
from practice_driver import target_point, plan_inputs
from test_incremental_board import fixture, power


def entity(eid,cid,ctype,owner,zone='PLAY',**tags):
    rows=[power('FULL_ENTITY - Creating ID=%d CardID=%s'%(eid,cid))]
    for key,value in dict(CARDTYPE=ctype,ZONE=zone,CONTROLLER=owner,**tags).items():
        rows.append(power('  tag=%s value=%s'%(key,value)))
    return ''.join(rows).encode()


class PublicMechanicsTests(unittest.TestCase):
    def parser(self,extra=b''):
        p=IncrementalBoard();p.feed(fixture()+extra);return p

    def test_real_zero_attack_weapon_uses_observed_health_and_damage(self):
        p=self.parser(entity(27,'BE_227','WEAPON',2,HEALTH=3,DAMAGE=1))
        w=p.snapshot(1,extended=True)['enemy_weapon']
        self.assertEqual((w['attack'],w['durability']),(0,2))
        p.game['entities'][27]['tags']['DURABILITY']='2'
        self.assertEqual(p.snapshot(1,extended=True)['enemy_weapon']['durability'],2)
        del p.game['entities'][27]['tags']['DURABILITY'];del p.game['entities'][27]['tags']['HEALTH']
        with self.assertRaisesRegex(UnsafeState,'missing HEALTH'):p.snapshot(1,extended=True)

    def test_locations_share_board_positions_but_are_not_attackers(self):
        cid=next(c for c,v in CATALOG.items() if v.get('type')=='LOCATION')
        p=self.parser(entity(30,cid,'LOCATION',1,HEALTH=3,DAMAGE=1,ZONE_POSITION=2,COOLDOWN=1)+
                      entity(31,'CS2_172','MINION',1,HEALTH=2,ATK=3,ZONE_POSITION=1))
        p.feed(b'D 10:00:00.0000000 GameState.DebugPrintOptions() - id=7\n'
               b'D 10:00:00.0000000 GameState.DebugPrintOptions() - option 0 type=POWER mainEntity=[id=30] error=NONE\n'
               b'D 10:00:00.0000000 GameState.DebugPrintOptions() - target 0 entity=[id=11] error=NONE\n')
        s=p.snapshot(1,extended=True)
        self.assertEqual(s['my_board_slots_used'],2);self.assertEqual(len(s['my_board']),1)
        self.assertEqual(s['my_locations'][0]['durability'],2)
        self.assertEqual(target_point(s,31),(682,510));self.assertEqual(target_point(s,30),(788,510))
        a={'kind':'activate_location','source_id':30,'target_id':11}
        self.assertIn(a,action_menu(s).values())
        self.assertEqual(plan_inputs(s,a),('ability_target',(788,510),(735,208)))
        mirror={'game_type':7,'format':2,'scene':{'mode':4},'board':{'friendly':{'boardCards':[
            {'entityId':31,'cardId':'CS2_172','zonePosition':1},{'entityId':30,'cardId':cid,'zonePosition':2}]},'opposing':None}}
        self.assertEqual(practice_identity(p.game,mirror,mode='ranked'),1)
        v=public_view(s);self.assertEqual(v['my_locations'][0]['cooldown'],1)
        self.assertIn(cid,v['card_definitions'])

    def test_public_quest_progress_is_distinct_from_hidden_opposing_secret(self):
        cid=next(c for c in sorted(PUBLIC_OBJECTIVES) if c.startswith('UNG_'))
        p=self.parser(entity(30,cid,'SPELL',2,'SECRET',QUEST_PROGRESS=3,QUEST_PROGRESS_TOTAL=6)+
                      entity(31,'EX1_287','SPELL',2,'SECRET')+
                      entity(32,'EX1_289','SPELL',1,'SECRET'))
        s=p.snapshot(1,extended=True);v=public_view(s)
        self.assertEqual(v['enemy_objectives'][0]['quest_progress'],3)
        self.assertEqual(v['enemy_objectives'][0]['quest_progress_total'],6)
        self.assertEqual(s['enemy_secret_count'],1)
        self.assertIn('EX1_289',json.dumps(v));self.assertNotIn('EX1_287',json.dumps(v))
        self.assertNotIn('ENEMY_PRIVATE_CARD',json.dumps(v))

    def test_statuses_and_player_effects_remain_public_and_typed(self):
        p=self.parser(entity(30,'CS2_172','MINION',2,HEALTH=5,ATK=3,ZONE_POSITION=1,
                             DORMANT=1,MEGA_WINDFURY=1,SPELLPOWER=2,REBORN=1,IMMUNE=1)+
                      entity(31,'EX1_001e','ENCHANTMENT',2,ATTACHED=3,TAG_SCRIPT_DATA_NUM_1=2))
        s=p.snapshot(1,extended=True);v=public_view(s);m=v['enemy_board'][0]
        self.assertTrue(m['dormant'] and m['mega_windfury'] and m['reborn'] and m['immune'])
        self.assertEqual(m['spell_damage'],2)
        self.assertEqual(v['enemy_player_effects'][0]['script_data_1'],2)
        s['enemy_locations']=[{'id':99,'card_id':'EX1_287','position':1,'private_account':'secret-name'}]
        self.assertNotIn('secret-name',json.dumps(public_view(s)))

    def test_targeted_hero_power_uses_observed_target_and_two_stage_input(self):
        s=self.parser().snapshot(1,extended=True)
        s.update(options_id=8,my_hero_power={'id':40,'card_id':'HERO_08bp'},
                 legal_options=[{'id':40,'type':'POWER','targets':[11],'requires_target':True}])
        a={'kind':'hero_power','source_id':40,'target_id':11}
        self.assertIn(a,action_menu(s).values())
        self.assertEqual(plan_inputs(s,a),('ability_target',(870,690),(735,208)))

    def test_general_single_target_choice_has_exact_public_entity_mapping(self):
        s=self.parser().snapshot(1,extended=True);s['phase']='target'
        ch={'type':'TARGET','source_card_id':'BE_094','count_min':1,'count_max':1,
            'items':[{'id':10,'card_id':'HERO_FAKE'}]}
        a=OpenAIAdvisor({'api_key':'fake-testing-key-123456789'})
        with patch('builtins.print'):
            self.assertEqual(a.target(ch,s),{'kind':'choose_target','target_id':10})
        self.assertEqual(a.calls,0)

    def test_catalog_weapon_location_and_objective_schemas(self):
        # Generated packets test each catalog ID through the common adapter;
        # this proves schema coverage, not a card-by-card rules simulator.
        for cid,card in CATALOG.items():
            kind=card.get('type')
            if kind not in ('WEAPON','LOCATION') or 'health' not in card:
                continue
            with self.subTest(card_id=cid):
                p=self.parser(entity(30,cid,kind,2,HEALTH=card['health'],ATK=card.get('attack',0),ZONE_POSITION=1))
                s=p.snapshot(1,extended=True)
                row=s['enemy_weapon'] if kind=='WEAPON' else s['enemy_locations'][0]
                self.assertEqual(row['durability'],card['health'])
        for cid in PUBLIC_OBJECTIVES:
            if cid.startswith('CAP_405'):continue # Custom trials require separate component evidence.
            with self.subTest(objective_id=cid):
                p=self.parser(entity(30,cid,'SPELL',2,'SECRET',QUEST_PROGRESS=2,TAG_SCRIPT_DATA_NUM_2=7))
                s=p.snapshot(1,extended=True)
                self.assertEqual(s['enemy_secret_count'],0)
                self.assertEqual(s['enemy_objectives'][0]['script_data_2'],7)

    def test_repeated_attachments_are_counted_instead_of_aborting(self):
        cid=next(k for k,v in CATALOG.items() if v.get('type')=='ENCHANTMENT')
        p=self.parser(b''.join(entity(i,cid,'ENCHANTMENT',2,ATTACHED=11,TAG_SCRIPT_DATA_NUM_1=2)
                              for i in range(40,70)))
        v=public_view(p.snapshot(1,extended=True))
        self.assertEqual(v['enemy_hero']['modifiers'][0]['count'],30)

    def test_unsupported_option_cannot_silently_become_forced_end_turn(self):
        s=self.parser().snapshot(1,extended=True)
        s.update(options_id=8,end_turn_allowed=True)
        for option in ({'id':20,'type':'FORGE','targets':[]},
                       {'id':20,'type':'POWER','targets':[],'has_sub_options':True},
                       {'id':99,'type':'POWER','targets':[]}):
            with self.subTest(option=option):
                s['legal_options']=[option]
                with self.assertRaisesRegex(AdvisorError,'false forced end turn'):action_menu(s)
        s['legal_options'].append({'id':20,'type':'POWER','targets':[]})
        menu,gaps=action_menu(s,with_gaps=True)
        self.assertIn({'kind':'play','source_id':20},menu.values())
        self.assertEqual(len(gaps),1)

    def test_nested_targets_are_not_misread_as_parent_attack_targets(self):
        p=self.parser()
        p.feed(b'D 10:00:00.0000000 GameState.DebugPrintOptions() - id=7\n'
               b'D 10:00:00.0000000 GameState.DebugPrintOptions() - option 0 type=POWER mainEntity=[id=20] error=NONE\n'
               b'D 10:00:00.0000000 GameState.DebugPrintOptions() - subOption 0 type=POWER mainEntity=[id=60] error=NONE\n'
               b'D 10:00:00.0000000 GameState.DebugPrintOptions() - target 0 entity=[id=11] error=NONE\n')
        opt=p.snapshot(1,extended=True)['legal_options'][0]
        self.assertTrue(opt['has_sub_options']);self.assertEqual(opt['targets'],[])

    def test_new_public_effects_disable_narrow_arithmetic_shortcuts(self):
        from tactical_checks import extra_public_effects
        for key in ('my_locations','enemy_locations','my_player_effects','enemy_player_effects','my_secrets','enemy_objectives'):
            with self.subTest(field=key):self.assertTrue(extra_public_effects({key:[{'card_id':'x'}]}))
        self.assertFalse(extra_public_effects({'my_objectives':[{'card_id':'BE_726'}]}))

    def test_weapon_location_and_hero_cards_count_toward_original_deck(self):
        p=self.parser(entity(30,'BE_227','WEAPON',1,'GRAVEYARD')+
                      entity(31,'REV_791','LOCATION',1,'GRAVEYARD')+
                      entity(32,'ICC_831','HERO',1,'GRAVEYARD'))
        ids={r['card_id'] for r in p.snapshot(1,extended=True)['own_seen_original']}
        self.assertTrue({'BE_227','REV_791','ICC_831'}<=ids)
        self.assertNotIn('HERO_FAKE',ids)
