import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from held_result import verified_result
from incremental_board import IncrementalBoard
from match_review import write_review
from openai_advisor import AdvisorTimeout
from practice_driver import Driver, Stop, SupersededChoice
from ranked_autoplay import stop_reason
from test_incremental_board import fixture, power
from test_target_choice import choice_line


class ChoiceExpiryTests(unittest.TestCase):
    def setUp(self):
        self.p = IncrementalBoard()
        self.p.feed(fixture() + power('TAG_CHANGE Entity=1 tag=TURN value=13').encode())
        self.p.game['names']['local'] = 2
        self.p.feed(choice_line('id=9 Player=local TaskList=1 ChoiceType=GENERAL CountMin=1 CountMax=1') +
                    choice_line('Source=[id=18 cardId=EDR_102t player=1]') +
                    choice_line('Entities[0]=[id=198 cardId=TIME_025 player=1]') +
                    choice_line('Entities[1]=[id=200 cardId=BE_086 player=1]') +
                    choice_line('Entities[2]=[id=202 cardId=TIME_029 player=1]') +
                    b'D 08:54:20.0 ChoiceCardMgr.WaitThenShowChoices() - id=9 BEGIN\n')
        self.ch = self.p.pending_choice(1)
        self.s = self.p.snapshot(1,extended=True,choice_id=9)
        self.d = Driver.__new__(Driver)
        self.d.parser = self.p
        self.d.owner = 1
        self.d.identity_game = self.p.game
        self.d.poll = Mock()
        self.d.advisor = Mock()

    def resolve(self, selected=202, locally_sent=False):
        if locally_sent:
            self.p.feed(b'D 08:54:25.0 GameState.SendChoices() - id=9 ChoiceType=GENERAL\n')
        prefix='D 08:54:26.0 GameState.DebugPrintEntitiesChosen() - '
        self.p.feed((prefix+'id=9 Player=local EntitiesCount=1\n'+prefix+
                     '  Entities[0]=[id=%d player=1]\n'%selected).encode())

    def test_server_selected_while_advisor_ran_continues_in_same_game(self):
        def decide(*_):
            self.resolve()
            self.p.feed((power('TAG_CHANGE Entity=1 tag=TURN value=14')+
                         power('TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0')+
                         power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1')).encode())
            return 0
        self.d.advisor.choose.side_effect=decide
        self.d.identify=Mock(return_value=1)
        def advance(_):
            if self.d.opponent_turn():
                self.p.feed(power('TAG_CHANGE Entity=2 tag=PLAYSTATE value=LOST').encode())
        with patch('practice_driver.mirror',return_value={}),patch('practice_driver.click') as click, \
             patch('practice_driver.time.sleep',side_effect=advance),patch('builtins.print'):
            self.d.run(5,0)
        click.assert_not_called()
        self.d.advisor.confirm_choice.assert_not_called()
        self.d.advisor.remember_choice.assert_called_once_with(self.ch,'TIME_029')
        events=[c.args[0] for c in self.d.advisor.record.call_args_list]
        self.assertEqual([x['event'] for x in events],['expired_without_submission','game_complete'])
        self.assertEqual((events[0]['proposed_entity'],events[0]['observed_entity']),(198,202))
        self.assertEqual(events[-1]['outcome'],'LOST')

    def test_external_local_selection_is_distinct_and_has_no_second_click(self):
        self.resolve(198,locally_sent=True)
        with patch('practice_driver.click') as click,self.assertRaises(SupersededChoice):
            self.d.execute_discover(1,self.ch,self.s,0)
        click.assert_not_called()
        self.assertEqual(self.d.advisor.record.call_args.args[0]['event'],'superseded_by_client_choice')

    def test_timeout_then_valid_index_zero_performs_only_one_discover_click(self):
        self.d.advisor.choose.side_effect=[AdvisorTimeout('injected timeout'),0]
        self.d.advisor.consecutive_timeouts=1
        self.d.identify=Mock(return_value=1)
        with patch('practice_driver.RUN_MODE','ranked'),patch('practice_driver.mirror',return_value={}), \
             patch('practice_driver.checked_geometry'),patch('practice_driver.time.sleep'), \
             patch('practice_driver.click',side_effect=lambda _:self.resolve(198,True)) as click,patch('builtins.print'):
            self.d.run(4,1)
        click.assert_called_once()
        self.d.advisor.confirm_choice.assert_called_once_with(self.ch,0)
        self.assertEqual(self.d.advisor.choose.call_count,2)

    def test_timeout_after_server_choice_observes_actual_card_without_proposal(self):
        def no_reply(*_):
            self.resolve()
            self.p.feed((power('TAG_CHANGE Entity=1 tag=TURN value=14')+
                         power('TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0')+
                         power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1')).encode())
            raise AdvisorTimeout('injected timeout')
        self.d.advisor.choose.side_effect=no_reply
        self.d.advisor.consecutive_timeouts=1
        self.d.identify=Mock(return_value=1)
        def advance(_):
            if self.d.opponent_turn():
                self.p.feed(power('TAG_CHANGE Entity=2 tag=PLAYSTATE value=LOST').encode())
        with patch('practice_driver.RUN_MODE','ranked'),patch('practice_driver.mirror',return_value={}), \
             patch('practice_driver.time.sleep',side_effect=advance),patch('practice_driver.click') as click,patch('builtins.print'):
            self.d.run(4,0)
        click.assert_not_called()
        self.d.advisor.choose.assert_called_once()
        self.d.advisor.confirm_choice.assert_not_called()
        self.d.advisor.remember_choice.assert_called_once_with(self.ch,'TIME_029')
        self.assertEqual([q.args[0]['event'] for q in self.d.advisor.record.call_args_list],['choice_observed_after_timeout','game_complete'])

    def test_own_click_needs_matching_local_and_server_receipts(self):
        with patch('practice_driver.mirror',return_value={}),patch('practice_driver.checked_geometry'), \
             patch('practice_driver.click',side_effect=lambda _:self.resolve(198,True)) as click:
            self.d.execute_discover(1,self.ch,self.s,0)
        click.assert_called_once()
        self.d.advisor.confirm_choice.assert_called_once_with(self.ch,0)

    def test_choice_finishing_during_geometry_check_does_not_receive_a_click(self):
        with patch('practice_driver.mirror',return_value={}),patch('practice_driver.checked_geometry',side_effect=self.resolve), \
             patch('practice_driver.click') as click,self.assertRaises(SupersededChoice):
            self.d.execute_discover(1,self.ch,self.s,0)
        click.assert_not_called()
        self.d.advisor.confirm_choice.assert_not_called()

    def test_different_reply_after_own_submission_still_stops(self):
        with patch('practice_driver.mirror',return_value={}),patch('practice_driver.checked_geometry'), \
             patch('practice_driver.click',side_effect=lambda _:self.resolve(202,True)), \
             self.assertRaisesRegex(Stop,'different choice'):
            self.d.execute_discover(1,self.ch,self.s,0)
        self.d.advisor.confirm_choice.assert_not_called()

    def test_replaced_game_cannot_consume_old_receipt(self):
        self.resolve();self.p.feed(fixture())
        with self.assertRaisesRegex(Stop,'identity changed'):
            self.d.execute_discover(1,self.ch,self.s,0)
        self.d.advisor.record.assert_not_called()


class HeldResultTests(unittest.TestCase):
    def test_only_single_complete_verified_ranked_result_counts_and_still_requests_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dest=root/'run-test';dest.mkdir();f=root/'Power.log'
            start=b'old game bytes\n'
            playing=fixture()
            ending=(power('TAG_CHANGE Entity=2 tag=PLAYSTATE value=LOST')+
                    power('TAG_CHANGE Entity=3 tag=PLAYSTATE value=WON')+
                    power('TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE')).encode()
            f.write_bytes(start+playing+ending);st=f.stat()
            cursor={'source':str(f),'device':st.st_dev,'inode':st.st_ino,'start_offset':len(start),
                    'offset':len(start)+len(playing),'verified_owner':1}
            result=verified_result(cursor,st.st_size,root)
            self.assertEqual(result['outcome'],'LOST')
            self.assertIsNone(verified_result(cursor,cursor['offset'],root))
            self.assertIsNone(verified_result({**cursor,'verified_owner':None},st.st_size,root))
            self.assertIsNone(verified_result({**cursor,'inode':0},st.st_size,root))
            with f.open('ab') as h:h.write(playing)
            self.assertIsNone(verified_result(cursor,f.stat().st_size,root))
            (dest/'log-cursor.json').write_text(json.dumps(cursor))
            (dest/'decisions.jsonl').write_text(json.dumps({'event':'proposed','decision_id':1,
                'observation':{'game_type':'GT_RANKED','game_turn':'13','source_card':'EDR_102t',
                               'choices':[{'card_id':'A'},{'card_id':'B'},{'card_id':'C'}]},'action':{'index':0}})+'\n')
            (dest/'session-events.jsonl').write_text('{"game_mode":"ranked"}\n{"holding_match":true}\n{"match_ended_after_fault":true}\n')
            old=write_review(dest,root/'learning',game_closed=True)
            self.assertEqual((old['outcome'],old['unconfirmed_decisions']),('INCOMPLETE',[1]))
            result['replayed_choice']={'decision_id':1,'event':'expired_without_submission','proposed_index':0,
                'game_turn':'13','source_card':'EDR_102t','offered_cards':['A','B','C']}
            (dest/'held-result.json').write_text(json.dumps({'run':dest.name,**result}))
            r=write_review(dest,root/'learning',game_closed=True)
            self.assertEqual((r['outcome'],r['version_stats']['losses']),('LOST',1))
            self.assertEqual((r['expired_decisions'],r['unconfirmed_decisions'],r['version_stats']['incomplete_runs']),(1,[],0))
            self.assertEqual(write_review(dest,root/'learning')['version_stats']['losses'],1)
            self.assertTrue(r['result_after_ai_fault'])
            self.assertEqual(stop_reason(r),'ai_fault_during_match')
