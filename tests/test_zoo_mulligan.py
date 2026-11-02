"""Opening hands preserved from the ranked review and reversible input tests."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import mulligan as m
from incremental_board import UnsafeState
from practice_driver import Driver
from strategy import PACK


def hand(rows):
    return tuple(m.Card(eid, cid, pos, cost) for pos, (eid, cid, cost) in enumerate(rows, 1))


WARLOCK = hand([(18, 'BE_085', 2), (15, 'TIME_029', 5), (33, 'TIME_025', 2)])
PRIEST = hand([(25, 'TIME_026', 1), (47, 'TIME_029', 5), (24, 'TIME_026', 1), (30, 'CORE_UNG_205', 1)])
SHAMAN = hand([(26, 'BE_086', 4), (22, 'EDR_856', 4), (16, 'BE_094', 1)])
CURVE = hand([(45, 'BE_729', 3), (70, 'TLC_603', 1), (42, 'BE_085', 2), (63, 'CATA_210', 1)])


class Surface:
    def __init__(self, cards, states=None):
        self.states = list(states or [m.KEEP] * len(cards))
        self.centers = (400, 735, 1070) if len(cards) == 3 else (300, 590, 880, 1170)
        self.hover = None
        self.clicks = []
        self.active = True

    def move(self, point):
        self.hover = self.centers.index(point[0]) if point != m.PARK and point[0] in self.centers else None

    def observe(self):
        if self.active:
            return tuple(m.HOVER if i == self.hover else v for i, v in enumerate(self.states))

    def click(self, point):
        self.clicks.append(point)
        if point != m.CONFIRM:
            self.states[self.hover] = m.REPLACE

    def run(self, cards, planned):
        return m.mark_and_submit(cards, planned, self.observe, self.move, self.click,
                                 clock=lambda: 0, sleep=lambda _: None)


class ZooMulliganTests(unittest.TestCase):
    def test_three_losing_openers_and_intact_curve(self):
        for cards, expected in ((WARLOCK, [18, 15]), (PRIEST, [25, 47, 24]),
                                (SHAMAN, [26, 22, 16]), (CURVE, [])):
            with self.subTest(cards=cards):
                self.assertEqual(m.replacement_ids(cards, PACK['deck']['cards']), expected)
        self.assertEqual(m.replacement_ids(SHAMAN, {}), [])
        self.assertEqual(m.replacement_ids(SHAMAN, None), [])

    def test_supported_buffs_keep_one_copy_and_preserve_actual_bodies(self):
        cards = hand([(19,'TIME_026',1),(23,'TIME_026',1),(25,'CATA_210',1),(31,'TIME_025',2)])
        self.assertEqual(m.replacement_ids(cards, PACK['deck']['cards']), [23])
        cards = hand([(19,'BE_094',1),(23,'TLC_603',1),(25,'BE_726',1)])
        self.assertEqual(m.replacement_ids(cards, PACK['deck']['cards']), [])

    def test_original_duplicates_are_bound_by_zone_position_and_coin_excluded(self):
        items = [{'id': c.entity, 'card_id': c.card_id, 'cost': c.cost} for c in PRIEST]
        choice = {'id': 3, 'type': 'MULLIGAN', 'items': items + [{'id': 79, 'card_id': 'GAME_005'}]}
        entities = {c.entity: dict(cardId=c.card_id, controller=1, zone='HAND', zone_pos=c.position) for c in PRIEST}
        parser = SimpleNamespace(game={'meta': {'GameType':'GT_RANKED', 'FormatType':'FT_STANDARD'}, 'entities': entities},
                                 pending_choice=lambda owner: choice if owner == 1 else None)
        rows = [dict(zonePosition=c.position, state=m.KEEP, cardId=c.card_id) for c in reversed(PRIEST)]
        rows[1]['state'] = m.REPLACE
        status = dict(game_type=7, format=2, scene={'mode':4}, game_menu=False, shop_open=False, popup=False,
                      mulligan_waiting=True, mulligan={'waitingForUserInput': True, 'mulliganCards': rows})
        self.assertEqual(m.opening_cards(parser,1,choice), PRIEST)
        self.assertEqual(m.actor_states(parser,1,3,PRIEST,status,'ranked'), (m.KEEP,m.KEEP,m.REPLACE,m.KEEP))
        for field, value in [('popup',True), ('format',1)]:
            with self.assertRaises(UnsafeState):
                m.actor_states(parser,1,3,PRIEST,dict(status,**{field:value}),'ranked')
        with self.assertRaises(UnsafeState):
            m.actor_states(parser,1,4,PRIEST,status,'ranked')
        rows[0]['zonePosition'] = rows[1]['zonePosition']
        with self.assertRaises(UnsafeState):
            m.actor_states(parser,1,3,PRIEST,status,'ranked')

    def test_four_slots_mark_three_and_submit_only_after_the_marks_are_observed(self):
        ui = Surface(PRIEST)
        detail = ui.run(PRIEST, [25, 47, 24])
        self.assertTrue(detail['submitted'])
        self.assertEqual(detail['marked_ids'], [25, 47, 24])
        self.assertEqual(ui.clicks, [(300,450),(590,450),(880,450),m.CONFIRM])

    def test_missing_hover_never_marks_a_card(self):
        ui = Surface(SHAMAN)
        ui.move = lambda point: setattr(ui, 'hover', None)
        self.assertTrue(ui.run(SHAMAN, [26])['submitted'])
        self.assertEqual(ui.clicks, [m.CONFIRM])

    def test_incorrect_hover_and_inherited_selection(self):
        ui = Surface(SHAMAN)
        ui.move = lambda point: setattr(ui,'hover', None if point == m.PARK else 2)
        ui.run(SHAMAN, [26])
        self.assertEqual(ui.clicks, [m.CONFIRM])
        ui = Surface(PRIEST, [m.REPLACE,m.KEEP,m.KEEP,m.KEEP])
        self.assertEqual(ui.run(PRIEST, [47])['marked_ids'], [])
        self.assertEqual(ui.clicks, [m.CONFIRM])

    def test_unconfirmed_mark_or_selection_of_another_slot_stops_further_input(self):
        for change in (False,True):
            ui = Surface(SHAMAN)
            def clicked(point):
                ui.clicks.append(point)
                if change:
                    ui.states[1] = m.REPLACE
            ui.click = clicked
            self.assertFalse(ui.run(SHAMAN, [26,22])['submitted'])
            self.assertEqual(ui.clicks, [(400,450)])

    def test_closed_surface_never_receives_a_card_or_confirm_click(self):
        ui = Surface(SHAMAN)
        move = ui.move
        def close_while_hovering(point):
            move(point)
            if point != m.PARK:
                ui.active = False
        ui.move = close_while_hovering
        self.assertFalse(ui.run(SHAMAN,[26])['submitted'])
        self.assertEqual(ui.clicks, [])

    def test_local_submission_is_not_a_receipt_and_empty_kept_list_is_valid(self):
        driver = Driver.__new__(Driver)
        entry = {'type':'MULLIGAN','sent':True,'confirmed':False,'chosen_ids':[]}
        driver.parser = SimpleNamespace(choices={9:entry},game={'meta':{'GameType':'GT_RANKED'}})
        driver.advisor = Mock()
        attempt = {'choice_id':9, 'offered':[{'id':c.entity,'card_id':c.card_id} for c in SHAMAN],
                   'planned_ids':[26], 'marked_ids':[26], 'submitted':True}
        self.assertFalse(driver.record_mulligan_receipt(attempt))
        driver.advisor.record.assert_not_called()
        entry.update(confirmed=True,chosen_ids=[999])
        self.assertFalse(driver.record_mulligan_receipt(attempt))
        for kept in ([],[26,16]):
            entry['chosen_ids'] = kept
            self.assertTrue(driver.record_mulligan_receipt(attempt))
            event = driver.advisor.record.call_args.args[0]
            self.assertEqual(event['kept_ids'], sorted(kept))
            self.assertEqual(event['replaced_ids'], sorted({c.entity for c in SHAMAN} - set(kept)))
