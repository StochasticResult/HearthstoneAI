"""Predict ordinary attacks so a model's later moves can be checked against the live state."""

import hashlib
import json
from card_metadata import CATALOG
from life_tap import life_tap_facts
from resource_facts import coin_result, resource_facts


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
    return frozenset(json.dumps(a,sort_keys=True,separators=(',',':')) for a in menu.values())


def coin_options(view,menu):
    """Predict a zero-cost Coin window, including a newly affordable Zoo minion.

    Unknown or targeted newly affordable cards are intentionally left to the
    ordinary selector. Even the calculated menu must match the next server menu.
    """
    if (view.get('phase')!='main' or view.get('enemy_secret_count')!=0 or view.get('unavailable_interactions')
            or view.get('my_locations') or view.get('my_player_effects') or view.get('enemy_player_effects')):
        return {}
    hand = view.get('my_hand') or []
    coins = [c for c in hand if c.get('card_id')=='GAME_005']
    if len(coins)!=1:
        return {}
    coin = coins[0]
    first = next((i for i,a in menu.items() if a=={'kind':'play','source_id':coin['id']}),None)
    mana = view.get('mana')
    gain = coin_result(mana,coin.get('cost'),view.get('mana_cap'))
    slots = view.get('my_board_slots_used')
    if (not first or not gain or gain['net_gain']!=1 or coin.get('cost')!=0 or type(slots) is not int
            or not 0<=slots<7 or type(view.get('temporary_mana')) is not int
            or any(type(c.get('position')) is not int or type(c.get('cost')) is not int for c in hand)):
        return {}
    after = {k:a for k,a in menu.items() if k!=first}
    # These cards' initial play has no target; any resulting choice ends the plan.
    minions = {'CATA_210','TIME_025','TLC_603','CORE_SCH_713','BE_729','BE_728','TIME_029','EDR_856','BE_090','CAP_405'}
    spells = {'BE_085','BE_726','BE_086','TIME_026'}
    for c in hand:
        if c['cost']!=gain['mana_after']:
            continue
        if c.get('card_id') not in minions|spells:
            return {}
        after['new'+str(c['id'])] = {'kind':'play','source_id':c['id']}
    power = view.get('my_hero_power') or {}
    if power.get('cost')==gain['mana_after'] and power.get('exhausted') is not True:
        if CATALOG.get(power.get('card_id'),{}).get('name')!='Life Tap':
            return {}
        after['new_power'] = {'kind':'hero_power','source_id':power['id']}
    expected = dict(view,mana=gain['mana_after'],temporary_mana=view['temporary_mana']+1)
    expected['my_hand'] = [dict(c,position=c['position']-int(c['position']>coin['position']))
                           for c in hand if c['id']!=coin['id']]
    for key,facts in [('resource_calculations',resource_facts(expected,CATALOG)),('life_tap',life_tap_facts(expected,after,CATALOG))]:
        if facts is None:
            expected.pop(key,None)
        else:
            expected[key]=facts
    candidates = [c for c in hand if c['card_id'] in minions and
                  {'kind':'play','source_id':c['id']} in after.values()]
    if not candidates:
        return {}
    predicted = {'expected':fingerprint(expected),'menu':menu_key(after),'corpses':view.get('enemy_corpses'),
                 'turn':view.get('game_turn'),'allow_corpse_increment':False,'confirmed_options_id':None,'remaining':[]}
    return {first+'>'+str(c['id']):({'kind':'coin_plan','first':first,
            'then_if_affordable':{'kind':'play','source_id':c['id']}},predicted) for c in candidates}


def predictable_attacks(view, menu):
    """Identify nonterminal attacks whose ordinary result we can predict."""
    if (view.get('phase')!='main' or view.get('enemy_secret_count')!=0 or view.get('unavailable_interactions')
            or any(view.get(k) for k in ('my_locations','enemy_locations','my_secrets'))):
        return {}
    friends = {c['id']:c for c in view.get('my_board',[])}
    hero = view.get('enemy_hero') or {}
    hits = removals(view,menu)
    result = {}
    for aid,a in menu.items():
        if a.get('kind')!='attack':
            continue
        attacker = friends.get(a.get('source_id'))
        if (not attacker or type(attacker.get('attack')) is not int or attacker['attack']<=0
                or type(attacker.get('attacks_this_turn')) is not int or attacker.get('exhausted') is not False
                or any(attacker.get(k) is not False for k in ('immune','divine_shield','windfury','mega_windfury',
                                                              'lifesteal','poisonous','dormant','stealth'))):
            continue
        if a.get('target_id')==hero.get('id'):
            if (hero.get('immune') is False and hero.get('modifiers')==[] and not hero.get('divine_shield')
                    and all(type(hero.get(k)) is int for k in ('health','armor')) and hero['armor']>=0
                    and attacker['attack']<hero['health']+hero['armor']):
                result[aid]='face'
        elif aid in hits:
            _,foe=hits[aid]
            if (attacker['health']>foe['attack'] and foe.get('poisonous') is False
                    and foe.get('lifesteal') is False and view.get('enemy_board_slots_used')==len(view['enemy_board'])
                    and {c.get('position') for c in view['enemy_board']}==set(range(1,len(view['enemy_board'])+1))):
                result[aid]='trade'
    actionable_sources = {a.get('source_id') for a in menu.values() if a.get('kind') in ('attack','play','hero_power')}
    return result if len(actionable_sources)>1 else {}


def continuation(view, menu, first, tail, decision_id, origin):
    """Predict only the next boundary. Remaining steps stay inert until their own receipt."""
    if not tail or len(tail)>6 or first in tail or tail[0] not in menu.values():
        return None
    first_id = next((aid for aid,a in menu.items() if a==first),None)
    kind = predictable_attacks(view,menu).get(first_id)
    if not kind:
        return None
    second_id = next(aid for aid,a in menu.items() if a==tail[0])
    if kind=='trade':
        p = prepare(view,menu,{'first':first_id,'then_if_unchanged':second_id},decision_id)
    else:
        source = first['source_id']
        attacker = next(c for c in view['my_board'] if c['id']==source)
        hero = view['enemy_hero']
        damage = attacker['attack']
        expected = dict(view,enemy_hero=dict(hero,armor=max(0,hero['armor']-damage),
                        health=hero['health']-max(0,damage-hero['armor'])))
        expected['my_board'] = [dict(c,exhausted=True,attacks_this_turn=c['attacks_this_turn']+1)
                                if c['id']==source else c for c in view['my_board']]
        next_menu = {key:a for key,a in menu.items() if a.get('source_id')!=source}
        p = {'first':first,'second':tail[0],'decision_id':decision_id,'turn':view['game_turn'],
             'expected':fingerprint(expected),'menu':menu_key(next_menu),'corpses':view.get('enemy_corpses'),
             'confirmed_options_id':None}
    if json.dumps(tail[0],sort_keys=True,separators=(',',':')) not in p['menu']:
        return None
    p.update(remaining=list(tail[1:]),origin=origin,allow_corpse_increment=(kind=='trade'))
    return p


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
    if corpses != before and not (plan.get('allow_corpse_increment',True) and type(before) is int and corpses == before+1):
        return None
    if fingerprint(view) == plan['expected'] and plan['second'] in menu.values():
        return plan['second']
    return None
