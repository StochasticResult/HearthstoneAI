import unittest
import copy
import json
import tempfile
import time
from pathlib import Path
from game_mode import matching_game, expected_type
from resource_guard import swap_exceeded, MIB
from openai_advisor import action_menu, AdvisorError
from strategy_eval import cases
from navigation import DECK_ID, Stop, next_ranked_navigation
from game_request import consume_game_request
from strategy import PACK


class GameModeTests(unittest.TestCase):
    def test_ranked_menu_requires_loaded_standard_and_verified_full_deck(self):
        status={'scene':{'mode':7,'sceneLoaded':True,'transitioning':False},
                'selected_deck':DECK_ID,
                'selected_deck_details':{'id':DECK_ID,'cards':[{'cardId':c,'count':n} for c,n in PACK['deck']['cards'].items()]},
                'deck_picker':{'visualsFormatType':2,'selectedDeck':DECK_ID}}
        self.assertEqual(next_ranked_navigation(1,status),(1070,735))
        for key,value in [('visualsFormatType',1),('selectedDeck',123)]:
            changed=copy.deepcopy(status);changed['deck_picker'][key]=value
            with self.assertRaises(Stop): next_ranked_navigation(1,changed)
        for change in ({'selected_deck':123},{'selected_deck_details':None}):
            with self.assertRaises(Stop): next_ranked_navigation(1,{**status,**change})
        for change in ({'popup':True},{'shop_open':True},{'scene':{'mode':7,'sceneLoaded':False}}):
            self.assertIsNone(next_ranked_navigation(1,{**status,**change}))
        self.assertIsNone(next_ranked_navigation(2,status))

    def test_ranked_request_is_explicit_expires_and_is_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);p=data/'game-request.json'
            self.assertEqual(consume_game_request(data),'practice')
            p.write_text(json.dumps({'mode':'ranked','expires_at':time.time()+100}))
            self.assertEqual(consume_game_request(data),'ranked');self.assertFalse(p.exists())
            p.write_text(json.dumps({'mode':'ranked_batch','expires_at':time.time()+100}))
            self.assertEqual(consume_game_request(data),'ranked_batch');self.assertFalse(p.exists())
            p.write_text(json.dumps({'mode':'ranked_auto','expires_at':time.time()+100}))
            self.assertEqual(consume_game_request(data),'ranked_auto');self.assertFalse(p.exists())
            for request in ({'mode':'ranked','expires_at':time.time()-1},
                            {'mode':'wild','expires_at':time.time()+100}):
                p.write_text(json.dumps(request))
                with self.assertRaises(ValueError): consume_game_request(data)
                self.assertFalse(p.exists())

    def test_ranked_requires_both_independent_readers_and_standard(self):
        meta={'GameType':'GT_RANKED','FormatType':'FT_STANDARD'}
        mirror={'game_type':7,'format':2,'scene':{'mode':4}}
        self.assertTrue(matching_game(meta,mirror,'ranked'))
        self.assertFalse(matching_game(meta,mirror))
        for change in ({'GameType':'GT_CASUAL'},{'FormatType':'FT_WILD'}):
            self.assertFalse(matching_game({**meta,**change},mirror,'ranked'))
        for change in ({'game_type':8},{'format':1},{'scene':{'mode':3}}):
            self.assertFalse(matching_game(meta,{**mirror,**change},'ranked'))
        with self.assertRaises(ValueError): expected_type('arena')

    def test_advisor_only_accepts_standard_ranked_or_practice(self):
        state=cases()[0]['state'];state.update(game_type='GT_RANKED',format='FT_STANDARD')
        self.assertTrue(action_menu(state))
        for change in ({'game_type':'GT_CASUAL'},{'format':'FT_WILD'},{'format':None}):
            with self.assertRaises(AdvisorError): action_menu({**state,**change})

    def test_swap_recovery_does_not_disable_growth_guard(self):
        self.assertFalse(swap_exceeded(533*MIB,533*MIB))
        self.assertTrue(swap_exceeded(790*MIB,533*MIB))
        self.assertFalse(swap_exceeded(1025*MIB,1000*MIB))
        self.assertTrue(swap_exceeded(1025*MIB,1000*MIB,maximum=1024*MIB))
        self.assertFalse(swap_exceeded(300*MIB,533*MIB))
