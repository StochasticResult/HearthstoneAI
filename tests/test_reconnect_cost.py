import copy
import unittest

from incremental_board import IncrementalBoard
from reconnect_options import restore_zero_hand_costs
from test_incremental_board import fixture, power


def pair():
    old = IncrementalBoard()
    old.feed(fixture().replace(b'GT_RANKED', b'GT_VS_AI') +
        (power('TAG_CHANGE Entity=GameEntity tag=GAME_SEED value=123') +
         power('TAG_CHANGE Entity=GameEntity tag=TURN value=9') +
         power('TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION') +
         power('TAG_CHANGE Entity=20 tag=COST value=0')).encode())
    current = copy.deepcopy(old)
    current.game['entities'][20]['tags'].pop('COST')
    return current, old


class ReconnectCostTests(unittest.TestCase):
    def test_exact_same_game_restores_only_explicit_observed_zero(self):
        current, old = pair()
        self.assertTrue(restore_zero_hand_costs(current, old, 1))
        self.assertEqual(current.snapshot(1, allow_practice=True)['my_hand'][0]['cost'], 0)

    def test_changed_seed_turn_resource_or_action_counter_rejects_and_rolls_back(self):
        for eid, tag, value in ((1,'GAME_SEED','456'), (1,'TURN','10'),
                                (2,'RESOURCES','3'), (2,'NUM_OPTIONS_PLAYED_THIS_TURN','1')):
            with self.subTest(tag=tag):
                current, old = pair()
                current.game['entities'][eid]['tags'][tag] = value
                self.assertFalse(restore_zero_hand_costs(current, old, 1))
                self.assertNotIn('COST', current.game['entities'][20]['tags'])

    def test_never_guesses_missing_old_cost_or_changes_current_explicit_cost(self):
        current, old = pair()
        old.game['entities'][20]['tags'].pop('COST')
        self.assertFalse(restore_zero_hand_costs(current, old, 1))
        current, old = pair()
        current.game['entities'][20]['tags']['COST'] = '2'
        self.assertFalse(restore_zero_hand_costs(current, old, 1))
        self.assertEqual(current.game['entities'][20]['tags']['COST'], '2')

    def test_transformed_card_or_missing_seed_rejects(self):
        current, old = pair()
        current.game['entities'][20]['cardId'] = 'BE_094'
        self.assertFalse(restore_zero_hand_costs(current, old, 1))
        current, old = pair()
        current.game['entities'][1]['tags'].pop('GAME_SEED')
        self.assertFalse(restore_zero_hand_costs(current, old, 1))
        self.assertNotIn('COST', current.game['entities'][20]['tags'])
