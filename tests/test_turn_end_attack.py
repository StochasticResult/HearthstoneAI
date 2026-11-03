"""A local submission and a server handoff are distinct from a combat receipt."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from incremental_board import IncrementalBoard
from match_review import summarize
from openai_advisor import OpenAIAdvisor, action_menu, public_view
from practice_driver import ActionUnconfirmed, Driver
from ranked_autoplay import stop_reason
from test_incremental_board import fixture, power
from test_public_mechanics import entity
from turn_end_attack import board_signature, verify

OPTIONS = 'D 10:00:01.0000000 GameState.DebugPrintOptions() - '
SEND = b'D 10:00:02.0000000 GameState.SendOption() - selectedOption=2 selectedSubOption=-1 selectedTarget=208 selectedPosition=0\n'
HEADER = b'D 10:00:03.0000000 GameState.DebugPrintPowerList() - Count=10\n'
ATTACK = {'kind':'attack','source_id':52,'target_id':208}


def tags(lines):
    return ''.join(power(line) for line in lines).encode()


def setup_log():
    return (fixture() + tags(['TAG_CHANGE Entity=2 tag=MULLIGAN_STATE value=DONE',
                              'TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1',
                              'TAG_CHANGE Entity=GameEntity tag=TURN value=18',
                              'TAG_CHANGE Entity=1 tag=STEP value=MAIN_ACTION'])
            + entity(52,'TIME_025','MINION',1,ATK=2,HEALTH=8,ZONE_POSITION=1)
            + entity(208,'END_002','MINION',2,ATK=4,HEALTH=2,TAUNT=1,REBORN=1,ZONE_POSITION=1)
            + (OPTIONS+'id=76\n'+OPTIONS+'option 2 type=POWER mainEntity=52 error=NONE\n'
               +OPTIONS+'target 0 entity=208 error=NONE\n').encode())


ENDING = tags(['TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_END',
               'TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0','TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1',
               'TAG_CHANGE Entity=GameEntity tag=TURN value=19'])
TERMINAL = tags(['TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON','TAG_CHANGE Entity=3 tag=PLAYSTATE value=LOST',
                 'TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE'])


class TurnEndAttackTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.dest=Path(tmp.name)/'run-example';self.dest.mkdir()
        self.parser=IncrementalBoard();self.parser.feed(setup_log())
        self.state=self.parser.snapshot(1,extended=True);self.view=public_view(self.state)
        self.case={'decision_id':48,'turn':18,'source':52,'target':208,'signature':board_signature(self.view,ATTACK),
                   'options_id':76,'sent_before':0}

    def save_log(self, data):
        (self.dest/'Power.log').write_bytes(data)
        (self.dest/'log-cursor.json').write_text(json.dumps(dict(verified_owner=1,start_offset=100,offset=100+len(data))))

    def test_capture_and_review_classify_without_crediting_or_resending(self):
        self.save_log(setup_log()+SEND+HEADER+ENDING+TERMINAL)
        receipt=verify(self.dest,'WON',[self.case])
        self.assertEqual(receipt,[dict(decision_id=48,game_turn=18,options_id=76,source_id=52,target_id=208,next_turn=19)])
        proposed=dict(event='proposed',decision_id=48,action=ATTACK,observation=self.view,actions=action_menu(self.state))
        legacy_marker=dict(event='resumed_after_uncertain_input',decision_id=48,action=ATTACK,game_turn='18')
        events=[proposed, legacy_marker, dict(event='game_complete',decision_id=48,outcome='WON')]
        (self.dest/'decisions.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        (self.dest/'actions.log').write_text('mulligan: confirmed; replaced 1/3\n{"game_result":"WON"}\n')
        summary,queue=summarize(self.dest)
        self.assertEqual(summary['turn_end_attacks'],receipt)
        self.assertEqual((summary['confirmed_decisions'],summary['expired_decisions'],summary['unconfirmed_decisions']),(0,0,[]))
        self.assertEqual(queue,[])
        self.assertIsNone(stop_reason(dict(summary,game_closed=True)))
        # A different request group must stay outstanding even on a win.
        legacy_marker['options_id']=75
        (self.dest/'decisions.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        bad,_=summarize(self.dest)
        self.assertEqual(bad['unconfirmed_decisions'],[48])
        self.assertEqual(stop_reason(dict(bad,game_closed=True)),'unconfirmed_action')

    def test_ambiguous_or_partial_evidence_never_clears_a_submission(self):
        combat=tags(['BLOCK_START BlockType=ATTACK Entity=52 EffectCardId= EffectIndex=0 Target=208 SubOption=-1','BLOCK_END'])
        cases=[
            setup_log()+ENDING+TERMINAL,
            setup_log()+SEND.replace(b'selectedTarget=208',b'selectedTarget=52')+ENDING+TERMINAL,
            setup_log()+SEND.replace(b'selectedOption=2',b'selectedOption=3')+ENDING+TERMINAL,
            setup_log()+SEND+SEND+ENDING+TERMINAL,
            setup_log()+SEND+combat+ENDING+TERMINAL,
            setup_log()+SEND+ENDING.replace(b'CURRENT_PLAYER value=0',b'CURRENT_PLAYER value=1')+TERMINAL,
            setup_log()+SEND+ENDING.replace(b'tag=TURN value=19',b'tag=TURN value=20')+TERMINAL,
            setup_log()+SEND+ENDING.replace(power('TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1').encode(),
                                             tags(['TAG_CHANGE Entity=208 tag=DAMAGE value=1','TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1']))+TERMINAL,
            setup_log()+SEND+TERMINAL, setup_log()+SEND+ENDING, setup_log()+SEND+ENDING+TERMINAL[:-1],
            setup_log()+SEND+ENDING+TERMINAL+setup_log()]
        for i,raw in enumerate(cases):
            with self.subTest(i=i):
                self.save_log(raw)
                self.assertEqual(verify(self.dest,'WON',[self.case]),[])
        self.save_log(setup_log()+SEND+ENDING+TERMINAL)
        self.assertEqual(verify(self.dest,'LOST',[self.case]),[])
        self.assertEqual(verify(self.dest,'WON',[dict(self.case,signature=[])]),[])
        self.assertEqual(verify(self.dest,'WON',[self.case,dict(self.case,decision_id=49)]),[])
        (self.dest/'log-cursor.json').write_text('{"verified_owner":1,"start_offset":0,"offset":1000000}')
        self.assertEqual(verify(self.dest,'WON',[self.case]),[])

    def test_opponent_concedes_immediately_after_a_face_attack_submission(self):
        action = dict(ATTACK, target_id=11)
        opening = setup_log().replace(b'target 0 entity=208', b'target 0 entity=11')
        sent = SEND.replace(b'selectedTarget=208', b'selectedTarget=11')
        concession = tags(['TAG_CHANGE Entity=3 tag=PLAYSTATE value=CONCEDED'])
        parser = IncrementalBoard()
        parser.feed(opening)
        state = parser.snapshot(1, extended=True)
        case = dict(self.case, target=11, signature=board_signature(public_view(state), action))
        data = opening + sent + HEADER + concession + TERMINAL
        self.save_log(data)
        proof = dict(decision_id=48, game_turn=18, options_id=76, source_id=52, target_id=11,
                     reason='opponent_conceded')
        self.assertEqual(verify(self.dest, 'WON', [case]), [proof])
        events = [dict(event='proposed', decision_id=48, action=action, observation=public_view(state),
                       actions=action_menu(state), server_options_id=76),
                  dict(event='resumed_after_uncertain_input', decision_id=48, action=action,
                       game_turn='18', options_id=76, sent_before=0),
                  dict(event='game_complete', decision_id=48, outcome='WON')]
        (self.dest/'decisions.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        (self.dest/'actions.log').write_text('mulligan: confirmed; replaced 1/3\n{"game_result":"WON"}\n')
        summary, queue = summarize(self.dest)
        self.assertEqual(summary['concession_attacks'], [proof])
        self.assertEqual(summary['strategy_sample']['concession_attacks'], [proof])
        self.assertEqual((summary['confirmed_decisions'], summary['expired_decisions'],
                          summary['unconfirmed_decisions'], queue), (0, 0, [], []))
        self.assertIsNone(stop_reason(dict(summary, game_closed=True)))

        combat = tags(['BLOCK_START BlockType=ATTACK Entity=52 EffectCardId= EffectIndex=0 Target=11 SubOption=-1',
                       'BLOCK_END'])
        for label, invalid in {
            'no concession': data.replace(concession, b''),
            'own concession': data.replace(concession, concession.replace(b'Entity=3', b'Entity=2')),
            'combat first': data.replace(concession, combat + concession),
            'damage after concession': data.replace(concession, concession + tags(['TAG_CHANGE Entity=11 tag=DAMAGE value=5'])),
            'wrong submission target': data.replace(sent, SEND),
            'second submission': data.replace(concession, sent + concession),
            'incomplete terminal': data[:-1],
        }.items():
            with self.subTest(label=label):
                self.save_log(invalid)
                self.assertEqual(verify(self.dest, 'WON', [case]), [])
        self.save_log(data)
        for key, value in (('options_id', 75), ('options_id', None), ('sent_before', None), ('sent_before', 1)):
            with self.subTest(key=key, value=value):
                self.assertEqual(verify(self.dest, 'WON', [dict(case, **{key:value})]), [])

    def test_game_result_alone_does_not_confirm_an_attack(self):
        driver=Driver.__new__(Driver)
        driver.follower=Mock(path=self.dest/'Power.log')
        driver.parser=self.parser;driver.owner=1;driver.identity_game=self.parser.game
        driver.advisor=Mock();driver.revalidate=Mock()
        def observe(predicate,_seconds):
            self.parser.feed(SEND+ENDING+TERMINAL)
            predicate()
        driver.wait_until=observe
        with patch('practice_driver.RUN_MODE','ranked'),patch('practice_driver.checked_geometry'), \
             patch('practice_driver.plan_inputs',return_value=('drag',(1,1),(2,2))), \
             patch('practice_driver.drag') as drag,patch('builtins.print'):
            with self.assertRaises(ActionUnconfirmed):
                driver.execute_main_action(1,self.state,ATTACK,False,0)
        drag.assert_called_once()
        driver.advisor.confirm.assert_not_called()

    def test_matching_server_combat_can_confirm_even_at_natural_terminal(self):
        driver=Driver.__new__(Driver);driver.parser=self.parser;driver.owner=1
        driver.follower=Mock(path=self.dest/'Power.log')
        driver.identity_game=self.parser.game;driver.advisor=Mock();driver.revalidate=Mock()
        def observe(predicate,_seconds):
            self.parser.feed(SEND+tags(['BLOCK_START BlockType=ATTACK Entity=52 EffectCardId= EffectIndex=0 Target=208 SubOption=-1',
                                        'BLOCK_END'])+TERMINAL)
            self.assertTrue(predicate())
        driver.wait_until=observe
        with patch('practice_driver.RUN_MODE','ranked'),patch('practice_driver.checked_geometry'), \
             patch('practice_driver.plan_inputs',return_value=('drag',(1,1),(2,2))), \
             patch('practice_driver.drag') as drag,patch('builtins.print'):
            driver.execute_main_action(1,self.state,ATTACK,False,0)
        drag.assert_called_once()
        driver.advisor.confirm.assert_called_once_with(ATTACK,self.state)

    def test_pace_with_a_taunt_uses_only_legal_combat_pairs(self):
        a=OpenAIAdvisor({'api_key':'offline-test-placeholder'})
        m={'attack':ATTACK,'another':dict(ATTACK,target_id=207),'end':{'kind':'end_turn'}}
        with patch('openai_advisor.time.monotonic',side_effect=[100.,140.]):
            a.note_turn_pace(self.view,m);a.note_turn_pace(self.view,m)
        self.assertEqual(self.view['turn_pace']['legal_taunt_attacks_now'],[[52,208]])
        self.assertNotIn('legal_face_attackers_now',self.view['turn_pace'])
