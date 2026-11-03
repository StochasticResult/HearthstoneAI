"""Replay one legal choice against printed, conditional responses. No API or game input."""
import re


def damage_effect(card):
    if card.get('type')!='SPELL':return None
    reminder = ' '.join(card.get('text','').lower().split())
    match = re.fullmatch(r'deal \$?(\d+) damage to (an undamaged minion|a minion|all enemy minions|all undamaged enemies)\.(?: draw a card\.| overload: \(\d+\)| add a [^.]+ to your hand\.)?',reminder)
    return (int(match[1]),match[2]) if match else None


def replies(view, lists, catalog):
    cls = view.get('strategy',{}).get('matchup',{}).get('enemy_class')
    hint = view.get('opponent_list_hypothesis') or {}
    conditional = {c['card'] for c in hint.get('unseen_watch',[])}
    pool = [r for r in lists if r[0]==cls]
    support = {}
    mana = view.get('enemy_mana_crystals')
    for _,label,cards,_,_,_ in pool:
        for cid in cards:
            if hint and cid not in conditional:continue
            card = catalog.get('CORE_'+cid) or catalog.get(cid,{})
            if damage_effect(card) and type(card.get('cost')) is int and (type(mana) is not int or card['cost']<=min(10,mana+1)):
                support.setdefault(cid,set()).add(label)
    return [cid for cid in sorted(support,key=lambda cid:(-len(support[cid]),cid))[:3]]


def attack_left(friendlies, effect):
    total = sum(c['attack'] for c in friendlies)
    if not effect:return total
    damage,shape=effect
    lost=[]
    for card in friendlies:
        if 'undamaged' in shape:
            if type(card.get('max_health')) is not int:return None
            if card['health']!=card['max_health']:continue
        if card['health']<=damage and not card.get('divine_shield'):
            if card.get('deathrattle') or card.get('reborn'):return None
            lost.append(card['attack'])
    return total-(sum(lost) if shape.startswith('all ') else max(lost,default=0))


def exercise(event, lists, catalog):
    from attack_plan import predictable_attacks
    view,menu=event.get('observation') or {},event.get('actions') or {}
    if (view.get('game_type')!='GT_RANKED' or view.get('format')!='FT_STANDARD' or view.get('phase')!='main'
            or any(view.get(k) for k in ('enemy_secret_count','unavailable_interactions','my_locations','enemy_locations'))):
        return None
    original=event.get('action') or {}
    friends=view.get('my_board',[]); foes=view.get('enemy_board',[])
    if any(type(c.get('attack')) is not int or type(c.get('health')) is not int for c in friends+foes):return None
    options=predictable_attacks(view,menu)
    hand={c['id']:c for c in view.get('my_hand',[])}
    branch=[]
    for aid,a in menu.items():
        board=list(friends); pressure=sum(c['attack'] for c in foes);face=cost=0
        if aid in options and original.get('kind')=='attack' and a['source_id']==original['source_id']:
            src=next(c for c in board if c['id']==a['source_id'])
            if options[aid]=='face':face=src['attack']
            else:
                target=next(c for c in foes if c['id']==a['target_id'])
                if src['attack']>=target['health']:pressure-=target['attack']
                board=[dict(c,health=c['health']-target['attack']) if c['id']==src['id'] else c for c in board]
                board=[c for c in board if c['health']>0]
        elif original.get('kind') in ('play','end_turn') and a.get('kind')=='play' and a.get('target_id') is None:
            unit=hand.get(a['source_id'],{}); printed=catalog.get(unit.get('card_id'),{})
            if (printed.get('type')!='MINION' or printed.get('text','').strip() not in ('','Taunt')
                    or unit.get('modifiers') or any(type(printed.get(k)) is not int for k in ('attack','health'))
                    or type(unit.get('cost')) is not int):continue
            board.append(dict(attack=printed['attack'],health=printed['health'],max_health=printed['health']))
            cost=unit['cost']
        elif original.get('kind') in ('play','end_turn') and a.get('kind')=='end_turn':pass
        else:continue
        branch.append((a,board,face,pressure,cost))
    if len(branch)<2 or not any(a==original for a, *_ in branch):return None
    counters=replies(view,lists,catalog)
    if not any(r[0]==view.get('strategy',{}).get('matchup',{}).get('enemy_class') for r in lists):return None
    table=[]
    for a,board,face,pressure,cost in branch:
        row={'action':a,'recorded':a==original,'mana_spent':cost,'face_damage_now':face,
             'enemy_board_attack_after':pressure,'friendly_attack_no_reply':attack_left(board,None)}
        row['friendly_attack_if_played']={c:attack_left(board,damage_effect(catalog.get('CORE_'+c) or catalog.get(c,{}))) for c in counters}
        table.append(row)
    played=next(row for row in table if row['recorded'])
    def metrics(row):
        return [row['face_damage_now'],-row['enemy_board_attack_after'],row['friendly_attack_no_reply'],
                *row['friendly_attack_if_played'].values()]
    better=[]
    for row in table:
        if row['recorded'] or row['mana_spent']!=played['mana_spent']:continue
        comparisons=list(zip(metrics(row),metrics(played)))
        if all(x is not None and y is not None and x>=y for x,y in comparisons) and any(x>y for x,y in comparisons):
            better.append(row['action'])
    return {'decision_id':event['decision_id'],'turn':view.get('game_turn'),
            'opponent_class':view.get('strategy',{}).get('matchup',{}).get('enemy_class'),
            'basis':'matched_lists' if view.get('opponent_list_hypothesis') else 'hypothetical_class_examples',
            'scope':'One legal action and the immediate damage of one possible reply. Unknown triggers stay unscored; no hidden hand or game win rate is inferred.',
            'conditional_replies':{c:(catalog.get('CORE_'+c) or catalog[c])['name'] for c in counters},
            'better_on_all_scored_dimensions':better,'alternatives':table}


def replay(events, lists, catalog):
    confirmed={e.get('decision_id') for e in events if e.get('event')=='confirmed'}
    trials=[]
    for e in events:
        if e.get('event')!='proposed' or e.get('decision_id') not in confirmed or e.get('model') is None:continue
        trial=exercise(e,lists,catalog)
        if trial:
            trials.append(trial)
            if len(trials)==3:break
    return trials


def review_drills(events, learning, client_build):
    if not client_build:return []
    import sys
    from pathlib import Path
    from opponent_decks import load_snapshot
    lists,_=load_snapshot(learning, client_build)
    if not lists:return []
    bridge=str(Path(__file__).parent/'state-bridge')
    if bridge not in sys.path:sys.path.append(bridge)
    from card_metadata import CATALOG
    return replay(events,lists,CATALOG)
