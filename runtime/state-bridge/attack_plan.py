"""One conditional follow-up to a model-selected pair of minion trades."""

import hashlib
import json


def removals(view, menu):
    """Legal single hits where the displayed Attack covers the target's Health."""
    friendly = {c['id']: c for c in view.get('my_board', [])}
    enemy = {c['id']: c for c in view.get('enemy_board', [])}
    hits = {}
    for key, action in menu.items():
        if action.get('kind') != 'attack':
            continue
        attacker = friendly.get(action.get('source_id'))
        target = enemy.get(action.get('target_id'))
        if not attacker or not target or any(type(c.get(k)) is not int or c[k] <= 0
                for c in (attacker, target) for k in ('attack', 'health')):
            continue
        if attacker['attack'] < target['health'] or any(target.get(k) is not False
                for k in ('immune', 'divine_shield', 'reborn', 'deathrattle', 'dormant')):
            continue
        hits[key] = (attacker, target)
    return hits


def options(view, menu):
    """Offer pairs only on long, attack-only turns. Singles stay available."""
    if ((view.get('turn_pace') or {}).get('seconds_since_first_decision', 0) < 25
            or view.get('phase') != 'main' or view.get('enemy_secret_count') != 0
            or view.get('unavailable_interactions') or view.get('my_locations') or view.get('enemy_locations')
            or len(menu) > 13):
        return {}
    friend_ids = {c['id'] for c in view.get('my_board', [])}
    enemy = view.get('enemy_board', [])
    enemy_ids = {c['id'] for c in enemy}
    if (view.get('enemy_board_slots_used') != len(enemy)
            or {c.get('position') for c in enemy} != set(range(1, len(enemy)+1))):
        return {}
    if any(a.get('kind') != 'end_turn' and (a.get('kind') != 'attack' or
            a.get('source_id') not in friend_ids or a.get('target_id') not in enemy_ids) for a in menu.values()):
        return {}
    hits = removals(view, menu)
    pairs = {}
    for first, (attacker, victim) in hits.items():
        if (attacker['health'] <= victim['attack'] or victim.get('poisonous') is not False
                or any(attacker.get(k) is not False for k in ('immune', 'divine_shield', 'windfury',
                                                             'mega_windfury', 'lifesteal'))
                or type(attacker.get('attacks_this_turn')) is not int):
            continue
        for second, (other, target) in hits.items():
            if attacker['id'] != other['id'] and victim['id'] != target['id']:
                pairs[f'{first}+{second}'] = {'kind':'attack_plan', 'first':first, 'then_if_unchanged':second}
    return pairs if len(pairs) <= 16 else {}


def fingerprint(view):
    # The dead card can drop out of the static rules dictionary. All live card IDs,
    # stats and modifiers remain in the projection. Corpse changes are checked below.
    ignored = {'turn_pace', 'recent_confirmed_actions', 'card_definitions', 'enemy_corpses'}
    data = {k:v for k,v in view.items() if k not in ignored}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).digest()


def menu_key(menu):
    return frozenset((v['kind'], v.get('source_id'), v.get('target_id')) for v in menu.values())


def prepare(view, menu, plan, decision_id):
    """Predict just an ordinary surviving attacker killing one defending minion."""
    first, second = menu[plan['first']], menu[plan['then_if_unchanged']]
    source, target = first['source_id'], first['target_id']
    victim = next(c for c in view['enemy_board'] if c['id'] == target)
    expected = dict(view)
    expected['my_board'] = [
        dict(c, health=c['health']-victim['attack'], exhausted=True, attacks_this_turn=c['attacks_this_turn']+1)
        if c['id'] == source else c for c in view['my_board']]
    expected['enemy_board'] = [
        dict(c, position=c['position']-1) if c['position'] > victim['position'] else c
        for c in view['enemy_board'] if c['id'] != target]
    expected['enemy_board_slots_used'] = len(expected['enemy_board'])
    next_menu = {k:a for k,a in menu.items() if a.get('source_id') != source and a.get('target_id') != target}
    return {'first':first, 'second':second, 'decision_id':decision_id, 'turn':view['game_turn'],
            'expected':fingerprint(expected), 'menu':menu_key(next_menu), 'corpses':view.get('enemy_corpses'),
            'confirmed_options_id':None}


def checked_action(plan, view, menu, options_id, decision_id):
    if (not plan or plan['confirmed_options_id'] is None or options_id is None
            or plan['confirmed_options_id'] == options_id or plan['decision_id'] != decision_id
            or plan['turn'] != view.get('game_turn') or plan['menu'] != menu_key(menu)):
        return None
    corpses, before = view.get('enemy_corpses'), plan['corpses']
    if corpses != before and not (type(before) is int and corpses == before+1):
        return None
    if fingerprint(view) == plan['expected'] and plan['second'] in menu.values():
        return plan['second']
    return None
