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


def shield_trades(view, menu):
    """Two currently legal hits on one shield. Only the first hit is predicted."""
    if (view.get('phase')!='main' or view.get('enemy_secret_count')!=0 or view.get('unavailable_interactions')
            or any(view.get(k) for k in ('my_locations','enemy_locations','my_secrets'))):
        return {}
    friends={c['id']:c for c in view.get('my_board',[]) if type(c.get('attack')) is int and c['attack']>0
             and type(c.get('health')) is int and c['health']>0 and c.get('attacks_this_turn')==0
             and type(c.get('windfury')) is bool and type(c.get('divine_shield')) is bool
             and all(c.get(k) is False for k in ('exhausted','immune','frozen','mega_windfury','lifesteal',
                                                  'poisonous','dormant','stealth'))}
    enemies={c['id']:c for c in view.get('enemy_board',[]) if c.get('divine_shield') is True
             and type(c.get('attack')) is int and c['attack']>0 and type(c.get('health')) is int and c['health']>0
             and all(c.get(k) is False for k in ('immune','dormant','poisonous','lifesteal','deathrattle','reborn'))}
    hero=(view.get('enemy_hero') or {}).get('id')
    face={a['source_id'] for a in menu.values() if a.get('kind')=='attack' and a.get('target_id')==hero}
    attacks=[(aid,a['source_id'],a['target_id']) for aid,a in menu.items() if a.get('kind')=='attack'
             and a.get('source_id') in friends and a.get('target_id') in enemies]
    result={}
    for first,source,target in attacks:
        unit,foe=friends[source],enemies[target]
        first_hp=unit['health']-(0 if unit.get('divine_shield') else foe['attack'])
        if first_hp<=0:
            continue
        candidates=[]
        for second,other,victim in attacks:
            finisher=friends[other]
            if victim!=target or finisher['attack']<foe['health'] or (source==other and not unit.get('windfury')):
                continue
            counter=foe['attack'] if other==source or not finisher.get('divine_shield') else 0
            second_hp=max(0,(first_hp if other==source else finisher['health'])-counter)
            cost=(unit['attack'] if source in face else 0)+(finisher['attack'] if other in face else 0)
            facts={'target':target,'target_attack':foe['attack'],'breaker_hp_after_first':first_hp,
                   'finisher_hp_after_second':second_hp,'face_damage_forgone_if_available':cost}
            pressure=(view.get('life_tap') or {}).get('unblocked_board_and_weapon_attack')
            if (type(pressure) is int and not any(foe.get(k) for k in ('windfury','mega_windfury','frozen',
                                                                       'cant_attack','cant_attack_heroes')) and pressure>=foe['attack']):
                facts.update(unblocked_enemy_attack_now=pressure,unblocked_enemy_attack_after_pair=pressure-foe['attack'])
            candidates.append(((other!=source,second_hp==0,cost,-second_hp,second),second,facts))
        if candidates:
            _,second,facts=min(candidates,key=lambda row:row[0])
            result[first+'+'+second]={'kind':'attack_plan','first':first,'then_if_unchanged':second,'shield_trade':facts}
    return dict(sorted(result.items(),key=lambda row:-row[1]['shield_trade']['target_attack'])[:16])


def fingerprint(view):
    # The dead card can drop out of the static rules dictionary. All live card IDs,
    # stats and modifiers remain in the projection. Corpse changes are checked below.
    ignored = {'turn_pace', 'recent_confirmed_actions', 'card_definitions', 'enemy_corpses','learned_matchup_notes','opponent_list_hypothesis'}
    data = {k:v for k,v in view.items() if k not in ignored}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).digest()


def menu_key(menu):
    return tuple(sorted(json.dumps(a,sort_keys=True,separators=(',',':')) for a in menu.values()))


def predicted_fingerprint(expected, next_menu):
    # The main advisor recomputes both fields at every decision. A spent
    # attacker, for example, can no longer make Life Tap's example trade.
    for key,facts in (('resource_calculations',resource_facts(expected,CATALOG)),
                      ('life_tap',life_tap_facts(expected,next_menu,CATALOG))):
        if facts is None:
            expected.pop(key,None)
        else:
            expected[key]=facts
    return fingerprint(expected)


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
    candidates = [c for c in hand if c['card_id'] in minions and
                  {'kind':'play','source_id':c['id']} in after.values()]
    if not candidates:
        return {}
    predicted = {'expected':predicted_fingerprint(expected,after),'menu':menu_key(after),'corpses':view.get('enemy_corpses'),
                 'turn':view.get('game_turn'),'allow_corpse_increment':False,'confirmed_options_id':None,'remaining':[]}
    spent, crystals, locked = (view.get(k) for k in ('mana_spent','mana_crystals','overload_locked'))
    if (all(type(x) is int for x in (spent,crystals,locked)) and 0 < spent <= crystals-locked
            and mana == crystals-locked-spent+view['temporary_mana']):
        # The Coin can restore one spent crystal instead of increasing TEMP_RESOURCES.
        refunded = dict(expected,mana_spent=spent-1,temporary_mana=view['temporary_mana'])
        predicted['expected_with_coin_refund'] = predicted_fingerprint(refunded,after)
    return {first+'>'+str(c['id']):({'kind':'coin_plan','first':first,
            'then_if_affordable':{'kind':'play','source_id':c['id']}},predicted) for c in candidates}


def predictable_attacks(view, menu):
    """Identify nonterminal attacks whose ordinary result we can predict."""
    if (view.get('phase')!='main' or view.get('enemy_secret_count')!=0 or view.get('unavailable_interactions')
            or any(view.get(k) for k in ('my_locations','enemy_locations','my_secrets'))):
        return {}
    friends = {c['id']:c for c in view.get('my_board',[])}
    hero = view.get('enemy_hero') or {}
    foes = {c['id']:c for c in view.get('enemy_board',[])}
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
        elif a.get('target_id') in foes:
            foe = foes[a['target_id']]
            if (type(attacker.get('health')) is not int or type(foe.get('health')) is not int
                    or type(foe.get('attack')) is not int or min(attacker['health'],foe['health'])<=0
                    or any(foe.get(k) is not False for k in ('poisonous','lifesteal','immune','divine_shield','reborn','deathrattle','dormant'))
                    or view.get('enemy_board_slots_used')!=len(foes)
                    or {c.get('position') for c in foes.values()}!=set(range(1,len(foes)+1))):
                continue
            if attacker['health'] <= foe['attack'] and (
                    attacker.get('deathrattle') is not False or attacker.get('reborn') is not False
                    or view.get('my_board_slots_used')!=len(friends)
                    or {c.get('position') for c in friends.values()}!=set(range(1,len(friends)+1))):
                continue
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
             'expected':predicted_fingerprint(expected,next_menu),'menu':menu_key(next_menu),'corpses':view.get('enemy_corpses'),
             'confirmed_options_id':None}
    if json.dumps(tail[0],sort_keys=True,separators=(',',':')) not in p['menu']:
        return None
    p.update(remaining=list(tail[1:]),origin=origin,allow_corpse_increment=p.get('allow_corpse_increment',False))
    return p


def prepare(view, menu, plan, decision_id):
    """Predict just the first ordinary combat, then demand an exact server match."""
    first, second = menu[plan['first']], menu[plan['then_if_unchanged']]
    source, target = first['source_id'], first['target_id']
    victim = next(c for c in view['enemy_board'] if c['id'] == target)
    expected = dict(view)
    if 'shield_trade' in plan:
        attacker=next(c for c in view['my_board'] if c['id']==source)
        spent=not attacker['windfury']
        expected['my_board']=[dict(c,health=plan['shield_trade']['breaker_hp_after_first'],divine_shield=False,
                                   exhausted=spent,attacks_this_turn=1) if c['id']==source else c for c in view['my_board']]
        expected['enemy_board']=[dict(c,divine_shield=False) if c['id']==target else c for c in view['enemy_board']]
        next_menu={k:a for k,a in menu.items() if not (spent and a.get('kind')=='attack' and a.get('source_id')==source)}
        return {'first':first,'second':second,'remaining':[],'origin':decision_id,'decision_id':decision_id,
                'turn':view['game_turn'],'expected':predicted_fingerprint(expected,next_menu),'menu':menu_key(next_menu),
                'corpses':view.get('enemy_corpses'),'confirmed_options_id':None,'allow_corpse_increment':False}
    attacker = next(c for c in view['my_board'] if c['id']==source)
    dead = set()
    for side,unit,damage in (('my',attacker,victim['attack']),('enemy',victim,attacker['attack'])):
        key = side+'_board'
        survives = unit['health']>damage
        if survives:
            survivor = dict(unit,health=unit['health']-damage)
            if side=='my':
                survivor.update(exhausted=True,attacks_this_turn=unit['attacks_this_turn']+1)
            expected[key] = [survivor if c['id']==unit['id'] else c for c in view[key]]
        else:
            dead.add(unit['id'])
            expected[key] = [dict(c,position=c['position']-1) if c['position']>unit['position'] else c
                             for c in view[key] if c['id']!=unit['id']]
            expected[side+'_board_slots_used'] = len(expected[key])
    next_menu = {k:a for k,a in menu.items() if a.get('source_id')!=source and a.get('target_id') not in dead}
    result = {'first':first,'second':second,'decision_id':decision_id,'turn':view['game_turn'],
              'expected':predicted_fingerprint(expected,next_menu),'menu':menu_key(next_menu),
              'corpses':view.get('enemy_corpses'),'allow_corpse_increment':target in dead,'confirmed_options_id':None}
    if source in dead and type(view.get('my_corpses')) is int:
        result['expected_with_own_corpse'] = predicted_fingerprint(dict(expected,my_corpses=view['my_corpses']+1),next_menu)
    elif source in dead and target in dead and view.get('my_corpses') is None:
        result['expected_first_mutual_corpses'] = predicted_fingerprint(dict(expected,my_corpses=1),next_menu)
    return result


def mismatch_reason(plan, view, menu, options_id, decision_id, first_corpse_attack=None, mutual_corpse_attack=None):
    if not plan or plan['confirmed_options_id'] is None or plan['decision_id']!=decision_id:
        return 'missing_matching_receipt'
    if options_id is None or plan['confirmed_options_id']==options_id:
        return 'no_new_option_group'
    if plan['turn']!=view.get('game_turn'):
        return 'turn_changed'
    if plan['menu']!=menu_key(menu) or plan['second'] not in menu.values():
        return 'legal_menu_changed'
    corpses, before = view.get('enemy_corpses'), plan['corpses']
    if corpses != before:
        first = plan['first']
        newly_observed = (before is None and corpses == 1 and first_corpse_attack ==
                          (plan['confirmed_options_id'], first.get('source_id'), first.get('target_id')))
        if (not plan.get('allow_corpse_increment', True) or type(corpses) is not int
                or not (newly_observed or (type(before) is int and corpses == before+1))):
            return 'unexpected_corpse_count'
    current = fingerprint(view)
    if current in (plan['expected'],plan.get('expected_with_own_corpse'),plan.get('expected_with_coin_refund')):
        return None
    if (current == plan.get('expected_first_mutual_corpses') and type(corpses) is int
            and ((before is None and corpses == 1) or (type(before) is int and corpses == before+1))
            and mutual_corpse_attack == (plan['confirmed_options_id'],plan['first']['source_id'],plan['first']['target_id'])):
        return None
    return 'public_state_changed'


def checked_action(plan, view, menu, options_id, decision_id):
    if mismatch_reason(plan,view,menu,options_id,decision_id) is None:
        return plan['second']
    return None
