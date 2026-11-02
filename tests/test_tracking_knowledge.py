import copy
import json
import unittest
from unittest.mock import patch

from incremental_board import IncrementalBoard
from openai_advisor import OpenAIAdvisor, public_view, enrich, request_payload, action_menu
from strategy import PACK, deck_context, matchup_context
from strategy_eval import base, card, option
from test_incremental_board import fixture, power
from test_openai_advisor import response
from datetime import date


class TrackingKnowledgeTests(unittest.TestCase):
    def test_production_tracking_cases_have_legal_actions_and_fit_budget(self):
        from tracking_eval import cases
        suite=cases();self.assertEqual(len(suite),4)
        for case in suite:
            state=case['state'];menu=action_menu(state)
            self.assertTrue(all(a in menu.values() for a in case['expected']))
            request_payload('gpt-6.1-sol',enrich(public_view(state)),menu)
    def parser(self):
        p=IncrementalBoard();p.feed(fixture());p.game['gate']=True;p.game['grace']=False
        return p

    def test_bounced_public_card_is_known_until_a_shuffle_breaks_identity(self):
        p=self.parser()
        p.feed(''.join(power(x) for x in ['FULL_ENTITY - Creating ID=40 CardID=CS2_172',
             '  tag=CARDTYPE value=MINION','  tag=ZONE value=PLAY','  tag=CONTROLLER value=2',
             '  tag=ATK value=3','  tag=HEALTH value=2','  tag=ZONE_POSITION value=1',
             'TAG_CHANGE Entity=40 tag=ZONE value=HAND','HIDE_ENTITY - Entity=40']).encode())
        s=p.snapshot(1,extended=True)
        self.assertEqual(s['enemy_known_hand'][0]['card_id'],'CS2_172')
        self.assertNotIn('ENEMY_PRIVATE_CARD',json.dumps(public_view(s)))
        p.feed((power('TAG_CHANGE Entity=40 tag=ZONE value=DECK')+power('TAG_CHANGE Entity=40 tag=ZONE value=HAND')).encode())
        self.assertFalse(p.snapshot(1,extended=True)['enemy_known_hand'])

    def test_raw_secret_id_stays_hidden_but_prior_public_reveal_is_retained(self):
        p=self.parser()
        p.feed(''.join(power(x) for x in ['FULL_ENTITY - Creating ID=44 CardID=CORE_EX1_287',
             '  tag=CARDTYPE value=SPELL','  tag=ZONE value=SECRET','  tag=CONTROLLER value=2']).encode())
        s=p.snapshot(1,extended=True)
        self.assertEqual(s['enemy_secret_ids'],[44]);self.assertFalse(s['enemy_known_secrets'])
        self.assertNotIn('CORE_EX1_287',json.dumps(public_view(s)))
        p.feed(''.join(power(x) for x in ['FULL_ENTITY - Creating ID=45 CardID=',
             '  tag=CARDTYPE value=SPELL','  tag=ZONE value=HAND','  tag=CONTROLLER value=2',
             'SHOW_ENTITY - Updating Entity=45 CardID=CORE_EX1_611',
             'TAG_CHANGE Entity=45 tag=ZONE value=SECRET']).encode())
        s=p.snapshot(1,extended=True)
        self.assertEqual([r['card_id'] for r in s['enemy_known_secrets']],['CORE_EX1_611'])
        self.assertIn('CORE_EX1_611',public_view(s)['card_definitions'])

    def test_hand_transformation_invalidates_old_known_card(self):
        p=self.parser()
        p.feed(power('SHOW_ENTITY - Updating Entity=21 CardID=CORE_EX1_197').encode())
        self.assertEqual(p.snapshot(1,extended=True)['enemy_known_hand'][0]['card_id'],'CORE_EX1_197')
        p.feed(power('CHANGE_ENTITY - Updating Entity=21 CardID=').encode())
        self.assertFalse(p.snapshot(1,extended=True)['enemy_known_hand'])

    def test_remaining_deck_requires_provenance_and_count_reconciliation(self):
        s=base();s['starting_deck']=PACK['deck']['cards']
        s.update(deck_tracking_from_mulligan=True,own_card_identity_changed=False,my_deck_count=32)
        s['own_seen_original']=[{'id':1,'card_id':'TIME_025'},{'id':2,'card_id':'TIME_025'}]
        s['own_known_deck']=[{'id':i+100,'card_id':'TIME_025t','generated':True} for i in range(4)]
        result=deck_context(s)['reconciled_remaining_counts']
        self.assertEqual(sum(result.values()),32);self.assertEqual(result['TIME_025t'],4)
        self.assertNotIn('TIME_025',result)
        for k,v in [('my_deck_count',31),('deck_tracking_from_mulligan',False),('own_card_identity_changed',True)]:
            changed=copy.deepcopy(s);changed[k]=v
            self.assertIsNone(deck_context(changed)['reconciled_remaining_counts'])

    def test_secrets_known_hand_and_observed_probes_reach_actual_request(self):
        s=base();s.update(enemy_secret_count=1,enemy_secret_ids=[90])
        s['my_hand']=[card('GAME_005',100)];s['legal_options']=[option(100)]
        s['enemy_known_hand']=[{'id':91,'card_id':'CORE_EX1_197','basis':'explicit_hand_reveal'}]
        sent=[];advisor=OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'},lambda p:sent.append(p) or response())
        with patch('builtins.print'):action=advisor.ask(s)
        advisor.confirm(action,s);s['mana']=5
        with patch('builtins.print'):advisor.ask(s)
        view=json.loads(sent[-1]['input'])['observation']
        self.assertEqual(view['enemy_known_hand'][0]['card_id'],'CORE_EX1_197')
        self.assertIn('CORE_EX1_197',view['card_definitions'])
        self.assertEqual(view['secret_context']['observed_probes'][-1]['still_active_ids'],[90])
        self.assertFalse(view['secret_context']['observed_probes'][-1]['other_secret_left'])

    def test_template_matching_counts_original_copies_not_generated_or_repeat_events(self):
        s=base();s.update(game_type='GT_RANKED',client_build=PACK['reviewed_client_build'])
        s['enemy_hero']['card_id']='HERO_09'
        s['opponent_public_cards']=[{'id':1,'card_id':'DINO_410'},
             {'id':2,'card_id':'CORE_EX1_197'},{'id':3,'card_id':'CORE_EX1_197'},
             {'id':2,'card_id':'CORE_EX1_197'},{'id':4,'card_id':'TIME_025','generated':True}]
        ctx=matchup_context(s,date(2026,11,1));candidate=ctx['candidates'][0]
        self.assertEqual(candidate['id'],'egg_priest')
        self.assertEqual(candidate['if_template_matches_unseen_watch_cards']['CORE_EX1_197'],0)
        self.assertNotIn('TIME_025',candidate['unmatched_original_cards'])
        self.assertTrue(ctx['unlisted_or_modified_deck_possible'])
