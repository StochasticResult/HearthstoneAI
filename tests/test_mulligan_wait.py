import unittest
from unittest.mock import Mock, patch

from incremental_board import IncrementalBoard
from practice_driver import Driver
from test_incremental_board import fixture, power


def changes(*lines):
    return ''.join(power(line) for line in lines).encode()


def waiting_driver():
    p=IncrementalBoard()
    p.feed(fixture()+changes('TAG_CHANGE Entity=GameEntity tag=TURN value=1',
                             'TAG_CHANGE Entity=GameEntity tag=STEP value=BEGIN_MULLIGAN',
                             'TAG_CHANGE Entity=3 tag=MULLIGAN_STATE value=INPUT',
                             'TAG_CHANGE Entity=2 tag=MULLIGAN_STATE value=DONE'))
    p.choices[1]={'id':1,'type':'MULLIGAN','owner':1,'sent':True,'confirmed':True,'items':[{'id':20,'owner':1}]}
    p.choices[2]={'id':2,'type':'MULLIGAN','owner':None,'sent':False,'confirmed':False,'items':[{'id':21,'owner':2}]}
    d=Driver.__new__(Driver);d.parser=p;d.owner=1;d.identity_game=p.game
    d.advisor=Mock();d.reconnect_evidence=();d.poll=Mock();d.identify=Mock(return_value=1)
    return d


class MulliganWaitTests(unittest.TestCase):
    def test_slow_opponent_mulligan_is_passive_until_normal_own_opening_turn(self):
        d=waiting_driver()
        now=[100.]
        def advance(seconds):
            now[0]+=seconds
            if now[0]>=135 and d.waiting_on_opponent():
                d.parser.feed(changes('TAG_CHANGE Entity=3 tag=MULLIGAN_STATE value=DONE',
                                      'TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION'))
        class ReachedMain(Exception):pass
        def snapshot(own):
            self.assertEqual(own,1)
            self.assertEqual(d.parser.game['entities'][1]['tags']['STEP'],'MAIN_ACTION')
            self.assertEqual(d.parser.snapshot(own,extended=True)['my_hand'][0]['id'],20)
            raise ReachedMain
        d.snapshot=snapshot
        with patch('practice_driver.RUN_MODE','ranked'),patch('practice_driver.time.monotonic',side_effect=lambda:now[0]), \
             patch('practice_driver.time.sleep',side_effect=advance),patch('practice_driver.mirror',return_value={}) as mirror, \
             patch('practice_driver.click') as click,patch('practice_driver.drag') as drag:
            with self.assertRaises(ReachedMain):d.run(100,0)
        self.assertGreaterEqual(now[0],135)
        self.assertTrue(all(c.kwargs=={'include_board':False} for c in mirror.call_args_list[:-1]))
        self.assertLess(len(mirror.call_args_list),10)
        click.assert_not_called();drag.assert_not_called();d.advisor.ask.assert_not_called()

    def test_receipt_owner_phase_and_no_pending_own_choice_are_required(self):
        d=waiting_driver()
        self.assertTrue(d.waiting_on_opponent())
        d.parser.choices[1]['confirmed']=False
        self.assertFalse(d.waiting_on_opponent())
        d.parser.choices[1].update(confirmed=True,owner=2)
        self.assertFalse(d.waiting_on_opponent())
        d.parser.choices[1]['owner']=None
        self.assertTrue(d.waiting_on_opponent())  # Only our entities identify the received offer.
        d.parser.choices[3]={'id':3,'type':'TARGET','owner':1,'items':[{'id':21,'owner':2,'card_id':'X'}],
                             'sent':False,'source':20,'source_card_id':'BE_086'}
        self.assertFalse(d.waiting_on_opponent())
        del d.parser.choices[3]
        d.parser.feed(changes('TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY'))
        self.assertFalse(d.waiting_on_opponent())
        d=waiting_driver();d.identity_game={}
        self.assertFalse(d.waiting_on_opponent())
