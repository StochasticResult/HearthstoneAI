"""Local Zoo opening policy and verified input for the constructed mulligan."""
from __future__ import annotations

import time
from typing import NamedTuple

from incremental_board import UnsafeState
from mirror_identity import mulligan_identity
from strategy import PACK

# ActorStateType values used by the client's constructed choice highlight.
REPLACE, KEEP, HOVER = 1, 5, 7
PARK = (160, 635)
CONFIRM = (735, 708)
# The constructed tracker centers its 1,020-unit row in a 1,080-high layout.
# The calibrated Hearthstone content here is 1470 x 819 below the title bar.
CENTERS = {3: (480, 737, 995), 4: (447, 640, 834, 1027)}


class Card(NamedTuple):
    entity: int
    card_id: str
    position: int
    cost: int | None


def opening_cards(parser, own, choice):
    cards = []
    for item in choice['items']:
        if item['card_id'] == 'GAME_005':
            continue
        entity = parser.game['entities'].get(item['id'], {})
        if (not item['card_id'] or entity.get('cardId') != item['card_id']
                or entity.get('controller') != own or entity.get('zone') != 'HAND'
                or type(entity.get('zone_pos')) is not int):
            raise UnsafeState('opening entity is not verified in our hand')
        cards.append(Card(item['id'], item['card_id'], entity['zone_pos'], item.get('cost')))
    cards.sort(key=lambda c: c.position)
    if (len(cards) not in (3, 4) or len({c.entity for c in cards}) != len(cards)
            or [c.position for c in cards] != list(range(1, len(cards) + 1))):
        raise UnsafeState('opening card positions are not distinct and complete')
    return tuple(cards)


def replacement_ids(cards, deck):
    if deck != PACK['deck']['cards']:
        return []
    one_drops = {'TLC_603', 'CATA_210', 'CORE_UNG_205'}
    bodies = sum(c.card_id in one_drops | {'TIME_025', 'CORE_SCH_713'} for c in cards)
    turn_one = any(c.card_id in one_drops | {'BE_726'} for c in cards)
    turn_two = any(c.card_id in {'TIME_025', 'CORE_SCH_713', 'BE_085'} for c in cards)
    keep_buffs = set()
    replace = []
    for card in cards:
        cid = card.card_id
        toss = card.cost is not None and card.cost >= 4
        if cid in {'TIME_026', 'BE_094'}:
            minimum = 2 if cid == 'TIME_026' else 1
            toss = not turn_one or bodies < minimum or cid in keep_buffs
            keep_buffs.add(cid)
        elif cid in {'CAP_405', 'BE_729'}:
            toss = not (turn_one and turn_two)
        elif cid == 'BE_085':
            toss = not turn_one
        if toss:
            replace.append(card.entity)
    return replace


def actor_states(parser, own, choice_id, cards, status, mode):
    """Bind each live actor to a distinct original entity, including duplicates."""
    if (not status or any(status.get(flag) is not False for flag in ('popup', 'shop_open', 'game_menu'))
            or mulligan_identity(parser, status, mode) != own):
        raise UnsafeState('opening surface is not available')
    choice = parser.pending_choice(own)
    if not choice or choice['id'] != choice_id or opening_cards(parser, own, choice) != cards:
        raise UnsafeState('opening choice changed')
    rows = status['mulligan']['mulliganCards']
    by_position = {row.get('zonePosition'): row for row in rows}
    if (len(by_position) != len(cards) or any(
            c.position not in by_position or by_position[c.position].get('cardId') != c.card_id
            or type(by_position[c.position].get('state')) is not int for c in cards)):
        raise UnsafeState('opening mirror positions or cards disagree')
    states = [by_position[c.position]['state'] for c in cards]
    tooltip = status.get('mulligan_hover') or {}
    slot = tooltip.get('position')
    if (tooltip.get('size') == len(cards) and type(slot) is int and 1 <= slot <= len(cards)
            and HOVER not in states and states[slot - 1] == KEEP):
        states[slot - 1] = HOVER
    return tuple(states)


def mark_and_submit(cards, planned, observe, move, click, clock=time.monotonic, sleep=time.sleep,
                    check_window=None):
    """Mark only in the verified opening layout. The server owns the final kept set."""
    detail = {'submitted': False, 'marked_ids': [], 'center_probes': [], 'observations': []}
    deadline = clock() + 12
    def read():
        states = observe()
        if states is not None and len(detail['observations']) < 24:
            detail['observations'].append(states)
        return states

    def at(point, accepts):
        if clock() >= deadline or read() is None:
            return None
        move(point)
        for _ in range(3):
            sleep(.1)
            states = read()
            if states is None or clock() >= deadline:
                return None
            if accepts(states):
                return states
        return None

    def verified_click(point, expected):
        if check_window is not None:
            check_window()
        if clock() >= deadline or read() != tuple(expected):
            return False
        click(point)
        return True

    neutral = at(PARK, lambda s: all(v in (KEEP, REPLACE) for v in s))
    if neutral is None:
        return detail
    expected = list(neutral)
    # Preserve an existing human selection. Only operate on the default hand.
    if REPLACE not in neutral:
        for index, card in enumerate(cards):
            if card.entity not in planned:
                continue
            point = (CENTERS[len(cards)][index], 450)
            hovered = expected.copy()
            hovered[index] = HOVER
            before = tuple(expected)
            if at(point, lambda s: s == tuple(hovered)) is None:
                # Some cards expose neither tooltip nor hover highlight. Only a
                # freshly calibrated center in an otherwise identical opening
                # may receive a reversible mark. Never probe offsets or Confirm.
                if check_window is None or read() != before:
                    break
                detail['center_probes'].append(card.entity)
                hovered = before
            if not verified_click(point, hovered):
                return detail
            after = at(PARK, lambda s: s != before and all(v in (KEEP, REPLACE) for v in s))
            correct = list(before)
            correct[index] = REPLACE
            if after != tuple(correct):
                # If exactly one other card got a cross at this point, remove
                # that cross at the same point and verify the original mask.
                if after is not None:
                    changed = [i for i,v in enumerate(before) if v != after[i]]
                    if len(changed) == 1 and after[changed[0]] == REPLACE:
                        over_wrong = list(after)
                        over_wrong[changed[0]] = HOVER
                        stable = at(point, lambda s: s in (after, tuple(over_wrong)))
                        if stable and verified_click(point, stable):
                            detail['restored_original_marks'] = at(PARK, lambda s: s == before) is not None
                return detail
            expected = correct
            detail['marked_ids'].append(card.entity)
    # A missed hover can leave the cursor over a different card. Validate the
    # entire neutral set once more, including every slot we did not touch.
    if at(PARK, lambda s: s == tuple(expected)) is not None:
        detail['submitted'] = verified_click(CONFIRM, expected)
    return detail
