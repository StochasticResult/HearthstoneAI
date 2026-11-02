import json
import unittest
from unittest.mock import patch

from incremental_board import IncrementalBoard, UnsafeState
from openai_advisor import OpenAIAdvisor, AdvisorError, action_menu
from practice_driver import Driver, Stop
from test_incremental_board import fixture, power
from test_openai_advisor import response


def choice_line(message):
    return ("D 10:00:01.0000000 GameState.DebugPrintEntityChoices() - " + message + "\n").encode()


def target_fixture(owner_name="own-name", count_max=1):
    p=IncrementalBoard()
    p.feed(fixture().replace(b"GT_RANKED",b"GT_VS_AI"))
    p.game["names"]["own-name"]=2
    p.game["names"]["enemy-name"]=3
    for eid, cid, owner, ctype, zone in ((30,"BE_726",1,"SPELL","SECRET"),(40,"CS2_127",2,"MINION","PLAY")):
        p.feed((power("FULL_ENTITY - Creating ID=%d CardID=%s" % (eid,cid))+
                power("  tag=CARDTYPE value="+ctype)+power("  tag=ZONE value="+zone)+
                power("  tag=CONTROLLER value=%d" % owner)+power("  tag=HEALTH value=7")+
                power("  tag=ATK value=1")+power("  tag=ZONE_POSITION value=1")).encode())
    p.feed(choice_line("id=5 Player=%s TaskList= ChoiceType=TARGET CountMin=1 CountMax=%d" % (owner_name,count_max))+
           choice_line("Source=[id=30 cardId=BE_726 player=1]")+
           choice_line("Entities[0]=[id=40 cardId=CS2_127 player=2]"))
    p.game["blocks"]=[{"type":"TRIGGER"}]
    return p


class TargetChoiceTests(unittest.TestCase):
    def test_chooser_identity_is_distinct_from_target_owner(self):
        p=target_fixture()
        ch=p.pending_choice(1)
        self.assertEqual(ch["type"],"TARGET")
        self.assertEqual(ch["owner"],1)
        self.assertEqual(ch["items"][0]["owner"],2)
        self.assertIsNone(p.pending_choice(2))
        s=p.snapshot(1,allow_practice=True,extended=True,choice_id=5)
        self.assertEqual(s["phase"],"target")
        self.assertFalse(s["legal_options"])
        self.assertNotIn("own-name",json.dumps(s))
        with self.assertRaises(AdvisorError):action_menu(s)
        with self.assertRaises(UnsafeState):p.snapshot(1,allow_practice=True,extended=True)

    def test_unknown_enemy_or_multiple_target_choices_do_not_allow_input(self):
        self.assertIsNone(target_fixture("unknown-name").pending_choice(1))
        self.assertIsNone(target_fixture("enemy-name").pending_choice(1))
        p=target_fixture(count_max=2)
        with self.assertRaises(UnsafeState):p.snapshot(1,allow_practice=True,extended=True,choice_id=5)

    def test_single_legal_target_has_no_paid_decision_and_unknown_effect_is_rejected(self):
        p=target_fixture();ch=p.pending_choice(1)
        s=p.snapshot(1,allow_practice=True,extended=True,choice_id=5)
        advisor=OpenAIAdvisor({"api_key":"fake-key-not-a-secret-123"},lambda _:self.fail("unexpected API call"))
        with patch("builtins.print"):
            self.assertEqual(advisor.target(ch,s),{"kind":"choose_target","target_id":40})
        self.assertEqual(advisor.calls,0)
        with self.assertRaises(AdvisorError):advisor.target({**ch,"source_card_id":"UNKNOWN"},s)
        s["enemy_board"]=[]
        with self.assertRaises(AdvisorError):advisor.target(ch,s)

    def test_client_send_is_not_server_confirmation(self):
        p=target_fixture()
        p.feed(b"D 10:00:02.0000000 GameState.SendChoices() - id=5 ChoiceType=TARGET\n")
        self.assertFalse(p.choices[5]["confirmed"])
        p.feed(b"D 10:00:02.1000000 GameState.DebugPrintEntitiesChosen() - id=5 Player=own-name EntitiesCount=1\n"
               b"D 10:00:02.1000000 GameState.DebugPrintEntitiesChosen() -   Entities[0]=[id=40 cardId=CS2_127 player=2]\n")
        self.assertTrue(p.choices[5]["confirmed"])
        self.assertIsNone(p.pending_choice(1))

    def test_server_reply_resolves_reconnect_renderer_choice_only_after_confirmation(self):
        p=target_fixture()
        p.game['blocks']=[]
        p.game['gate']=False
        p.feed(choice_line('id=6 Player=own-name TaskList= ChoiceType=GENERAL CountMin=1 CountMax=1')+
               choice_line('Source=[id=30 cardId=EDR_856 player=1]')+
               choice_line('Entities[0]=[id=50 cardId=TLC_603 player=1]'))
        p.feed(b'D 10:00:02.0000000 GameState.SendChoices() - id=6 ChoiceType=GENERAL\n')
        self.assertEqual(p.game['ec_choice']['id'],6)
        p.feed(b'D 10:00:02.1000000 GameState.DebugPrintEntitiesChosen() - id=6 Player=own-name EntitiesCount=1\n')
        self.assertEqual(p.game['ec_choice']['id'],6)
        p.feed(b'D 10:00:02.1000000 GameState.DebugPrintEntitiesChosen() -   Entities[0]=[id=50 cardId=TLC_603 player=1]\n')
        self.assertTrue(p.choices[6]['confirmed'])
        self.assertIsNone(p.game['ec_choice'])
        self.assertIsNone(p.game['ec_confirm'])

    def test_all_replaced_mulligan_accepts_empty_server_kept_list(self):
        p=target_fixture()
        p.feed(choice_line('id=6 Player=own-name TaskList= ChoiceType=MULLIGAN CountMin=0 CountMax=3')+
               choice_line('Entities[0]=[id=50 cardId=TLC_603 player=1]'))
        p.feed(b'D 10:00:02.1000000 GameState.DebugPrintEntitiesChosen() - id=6 Player=own-name EntitiesCount=0\n')
        self.assertTrue(p.choices[6]['confirmed'])
        self.assertEqual(p.choices[6]['chosen_ids'],[])

    def target_driver(self):
        p=target_fixture(); ch=p.pending_choice(1)
        s=p.snapshot(1,allow_practice=True,extended=True,choice_id=5)
        driver=Driver.__new__(Driver);driver.parser=p;driver.poll=lambda:None
        driver.revalidate_target=lambda *args:None
        return driver,ch,s

    def test_ignored_click_retries_only_the_same_pending_choice(self):
        driver,ch,s=self.target_driver()
        with patch.object(driver,'wait_until',side_effect=[Stop('action was not confirmed in game log'),None]), \
             patch('practice_driver.click') as click,patch('builtins.print'):
            driver.execute_target(1,ch,s,{'kind':'choose_target','target_id':40})
            self.assertEqual(click.call_count,2)
            self.assertEqual(click.call_args_list[0],click.call_args_list[1])

    def test_sent_but_unconfirmed_choice_is_never_clicked_again(self):
        driver,ch,s=self.target_driver()
        def sent(_point):driver.parser.choices[5]['sent']=True
        with patch.object(driver,'wait_until',side_effect=[Stop('action was not confirmed in game log'),None]), \
             patch('practice_driver.click',side_effect=sent) as click,patch('builtins.print'):
            driver.execute_target(1,ch,s,{'kind':'choose_target','target_id':40})
            self.assertEqual(click.call_count,1)

    def test_stale_target_never_gets_a_click(self):
        driver,ch,s=self.target_driver()
        with patch.object(driver,'revalidate_target',side_effect=Stop('target changed')),patch('practice_driver.click') as click:
            with self.assertRaisesRegex(Stop,'target changed'):
                driver.execute_target(1,ch,s,{'kind':'choose_target','target_id':40})
            click.assert_not_called()

    def test_confirmed_different_target_stops_without_more_input(self):
        driver,ch,s=self.target_driver()
        driver.parser.choices[5].update(confirmed=True, chosen_ids=[999])
        with patch('practice_driver.click') as click:
            with self.assertRaisesRegex(Stop,'different choice'):
                driver.execute_target(1,ch,s,{'kind':'choose_target','target_id':40})
            click.assert_not_called()

    def test_matching_confirmed_target_requires_no_more_input(self):
        driver,ch,s=self.target_driver()
        driver.parser.choices[5].update(confirmed=True, chosen_ids=[40])
        with patch('practice_driver.click') as click:
            driver.execute_target(1,ch,s,{'kind':'choose_target','target_id':40})
            click.assert_not_called()
