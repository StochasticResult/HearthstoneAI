"""One typed choice from the same public evidence used by the Sol advisor."""
import json
import math
import time

from openai_advisor import AdvisorError, ContextTooLarge, MAX_INPUT_BYTES
from spell_order import buff_before_tax, review_action
from card_metadata import CATALOG
from resource_facts import blasphemy_damage, blasphemy_pressure, shred_battlecry_menu

MODEL = 'gpt-6-luna'

def prepare_backup(sol, view, menu, transport, *, orders=None):
    """Prepare from this exact window now; the returned call can be dispatched later."""
    view, menu = shred_battlecry_menu(view,menu,CATALOG)
    if not menu:
        return lambda:{'sent':False,'error':'no safe supported action'}
    if len(menu)==1:
        return lambda: {'action':next(iter(menu.values())),'mode':'forced','sent':False}
    clears = emergency_clears(view,menu)
    allowed = clears or list(menu.values())
    tap_hp = (view.get('life_tap') or {}).get('effective_health_tap_before_other_effects')
    if type(tap_hp) is int and tap_hp<=0:
        allowed = [a for a in allowed if a.get('kind')!='hero_power']
    orders = buff_before_tax(view,menu) if orders is None else orders
    try:
        _,packet = recovery_inputs(sol,view,menu,orders=orders)
        if len(json.dumps(packet,separators=(',', ':')).encode())>MAX_INPUT_BYTES:
            raise ContextTooLarge('Score request exceeds the existing limit')
        mode = 'score'
    except ContextTooLarge:
        try:
            packet = tactical_payload(view,menu,orders=orders)
        except ContextTooLarge:
            packet = decision_payload(sol,menu)
        mode = 'choice'
    def send():
        started = time.monotonic()
        info = {'sent':True,'mode':mode,'restricted_menu':len(allowed)!=len(menu)}
        try:
            reply = transport(packet)
            scores = {}
            if mode=='score':
                action,scores,tokens = parse_scores(reply,menu)
                eligible = [aid for aid in scores if menu[aid] in allowed]
                if eligible:
                    best = max(scores[aid] for aid in eligible)
                    leaders = [menu[aid] for aid in eligible if scores[aid]==best]
                    # With equivalent double-clear copies either move has a
                    # checked effect. An unresolved tie elsewhere has no basis.
                    action = leaders[0] if len(leaders)==1 or clears else None
                info['candidate_scores'] = scores
            else:
                action,_,tokens = parse_choice(reply,menu)
            info.update(model=reply['model'],tokens=tokens)
            if action in allowed:
                revised,lesson = review_action(menu,action,orders,scores)
                if lesson and revised in allowed:
                    info.update(model_action=action,tactical_lesson=lesson)
                    action = revised
            if action in allowed:
                info['action'] = action
            else:
                info['error'] = 'no unique eligible recommendation' if action is None else 'selected move outside checked alternatives'
        except AdvisorError as error:
            info['error'] = str(error)[:160]
        info['elapsed_ms'] = round(1000*(time.monotonic()-started))
        return info
    return send

def emergency_clears(view, menu):
    """Identify double clears that remove two independently lethal attackers."""
    if (view.get('game_type')!='GT_RANKED' or view.get('format')!='FT_STANDARD' or view.get('phase')!='main'
            or view.get('enemy_secret_count')!=0 or view.get('unavailable_interactions') or
            any(view.get(k) for k in ('enemy_locations','my_locations','enemy_objectives','enemy_active_rituals',
                                      'my_player_effects','enemy_player_effects','my_secrets','my_weapon','enemy_weapon'))
            or any(x.get('card_id') not in ('BE_085','BE_726','BE_090t1','BE_090t2','BE_090t3')
                   for x in view.get('my_objectives',[]))):
        return []
    hero, enemy = view.get('my_hero') or {}, view.get('enemy_hero') or {}
    if any(type(h.get(k)) is not int or h[k]<0 for h in (hero,enemy) for k in ('health','armor')):
        return []
    if any(h.get('immune') is not False or h.get('modifiers')!=[] for h in (hero,enemy)):
        return []
    friendly, foes = view.get('my_board',[]),view.get('enemy_board',[])
    if (not foes or len(friendly)>1 or any(x.get('taunt') is not False or x.get('lifesteal')
           or x.get('windfury') or x.get('mega_windfury') for x in friendly)):
        return []
    harmless = {'CORE_UNG_205','CORE_SCH_713','BE_728','BE_729','BE_090','CAP_405','TIME_025',
                'TIME_029','EDR_856','CATA_210t','BE_085t'}
    for x in [*friendly,*foes]:
        egg = x.get('card_id')=='CATA_210'
        if (x.get('card_id') not in harmless and not egg
                or any(x.get(k) is not False for k in ('immune','divine_shield','reborn','dormant'))
                or (x.get('deathrattle') is not False and not egg)
                or (egg and (type(x.get('script_data_1')) is not int or type(x.get('script_data_2')) is not int))
                or type(x.get('attack')) is not int or type(x.get('health')) is not int
                or x.get('unknown_modifier_count') or any(m.get('card_id') not in
                    ('TIME_026e','BE_094e','CAP_405t5e','BE_013t2e1') for m in x.get('modifiers',[]))):
            return []
    ready = {a['source_id'] for a in menu.values() if a.get('kind')=='attack' and a.get('target_id')==enemy.get('id')}
    if sum(x['attack'] for x in friendly if x['id'] in ready)>=enemy['health']+enemy['armor']:
        return []
    life = hero['health']+hero['armor']
    hand = view.get('my_hand',[])
    # A different hand or power can offer a better answer, including lethal.
    if not hand or any(type(x.get('cost')) is not int or x.get('card_id') not in ('BE_086','BE_728') for x in hand):
        return []
    if any(a.get('kind')=='hero_power' for a in menu.values()):
        power = view.get('my_hero_power') or {}
        if (view.get('card_definitions') or {}).get(power.get('card_id'),{}).get('name')!='Life Tap':
            return []
    highest = max(x['cost'] for x in hand)
    ids = {x['id'] for x in hand if x.get('card_id')=='BE_086' and x['cost']==highest
           and type(view.get('mana')) is int and 0<=x['cost']<=view['mana']}
    clean_lethal = [x for x in foes if x.get('frozen') is False and not x.get('cant_attack')
                    and not x.get('cant_attack_heroes') and x.get('deathrattle') is False
                    and x['attack']>=life>0 and 0<x['health']<=4]
    return [a for a in menu.values() if len(clean_lethal)>=2 and a.get('kind')=='play'
            and a.get('target_id') is None and a.get('source_id') in ids]

def _hitpoints(unit, damage, poisonous=False):
    if unit.get('dormant') or unit.get('immune') or damage <= 0 or unit.get('divine_shield'):
        return unit['health']
    return 0 if poisonous else max(0,unit['health']-damage)


def _previews(view, menu):
    """One ordinary combat or printed damage spell, before triggered effects."""
    friendly = {x['id']:x for x in view.get('my_board',[])}
    enemies = {x['id']:x for x in view.get('enemy_board',[])}
    hand = {x['id']:x for x in view.get('my_hand',[])}
    hero = view.get('enemy_hero') or {}
    result = {}
    playable = {a.get('source_id') for a in menu.values() if a.get('kind')=='play'}
    mana = view.get('mana')
    for aid,a in menu.items():
        source,target = a.get('source_id'),a.get('target_id')
        played = hand.get(source,{}) if a.get('kind')=='play' else {}
        if played.get('card_id') in ('BE_729','BE_728') and type(mana) is int and type(played.get('cost')) is int:
            # Both cards raise the cost of spells in hand by one. The figures
            # isolate this effect; other triggers may create other lines.
            budget = mana-played['cost']
            spells = [{'id':s['id'],'cost_now':s['cost'],'cost_after_tax':s['cost']+1}
                      for s in hand.values() if s['id'] in playable and s.get('card_type')=='SPELL'
                      and type(s.get('cost')) is int and s['cost']<=budget<s['cost']+1]
            if spells:
                result[aid] = {'mana_left_after_play':budget,'spells_priced_out_if_tax_played_first':spells}
        if played.get('card_id')=='CORE_UNG_205':
            victim = hero if target==hero.get('id') else enemies.get(target,{})
            if type(victim.get('attack')) is int and not any(victim.get(k) for k in ('dormant','frozen','cant_attack')):
                result[aid] = {'freeze_target_id':target,'attack_now':victim['attack'],
                               'has_lifesteal':bool(victim.get('lifesteal')),
                               'can_normally_hit_face':not victim.get('cant_attack_heroes')}
        if a.get('kind')=='attack' and source in friendly:
            unit,other = friendly[source],enemies.get(target)
            if type(unit.get('attack')) is not int:
                continue
            if target==hero.get('id') and all(type(hero.get(k)) is int for k in ('health','armor')):
                result[aid] = {'face_damage_before_triggers': 0 if hero.get('immune') or hero.get('divine_shield') else unit['attack'],
                               'enemy_health_with_armor': hero['health']+hero['armor']}
            elif other and all(type(v) is int for v in (unit.get('health'),other.get('health'),other.get('attack'))):
                result[aid] = {'defender_hp_after_hit':_hitpoints(other,unit['attack'],unit.get('poisonous')),
                               'attacker_hp_after_counter':_hitpoints(unit,other['attack'],other.get('poisonous'))}
        if played.get('card_id')=='BE_086':
            damage = blasphemy_damage(view,played)
            if damage:
                result[aid] = damage
    return result


def recovery_inputs(sol, view, menu, *, orders=None):
    """Give Sol and the independent Luna grader the same named moves and local arithmetic."""
    typed = tactical_payload(view,menu,orders=orders)
    desc = {x['value']:x['description'] for x in typed['questions'][0]['choices']}
    ev = json.loads(typed['input'])
    ev['candidate_moves'] = {i:{'action':a,'description':desc[i]} for i,a in menu.items()}
    ev['one_step_arithmetic'] = _previews(view,menu)
    ev['scope'] = ('Arithmetic applies only to the listed hit(s) before further triggers, protection effects, '
                   'Deathrattle, Reborn, hidden plays or future draws. Recheck the live menu after each move.')
    wire = json.dumps(ev, ensure_ascii=True, separators=(',', ':'))
    compact = dict(sol,input=wire,instructions=typed['questions'][0]['instructions']+
                   ' Return only a choose_action function call with a short Chinese reason and the chosen action_id.')
    levels = [{'label':'losing','description':'Misses a visible kill or fails to address a bigger immediate threat.'},
              {'label':'weak','description':'Legal but leaves avoidable danger, wastes resources or misses tempo.'},
              {'label':'reasonable','description':'A defensible line with useful pressure or survival chances.'},
              {'label':'best','description':'The best first step of a feasible winning or defensive sequence.'}]
    scored = {'model':MODEL,'input':wire,
              'questions':[{'name':aid,'type':'score','instructions':f'Rate candidate {aid}: {desc[aid]}. '
                            'Compare its next moves against all candidates. Prioritize certain lethal, then surviving '
                            'visible threats. Respect timing, costs, Taunt, Lifesteal and possible triggers.',
                            'levels':levels} for aid in menu]}
    if 'blasphemy_pressure' in ev and any(len(json.dumps(p,separators=(',', ':')).encode()) > MAX_INPUT_BYTES for p in (compact,scored)):
        del ev['blasphemy_pressure']
        compact['input'] = scored['input'] = json.dumps(ev,ensure_ascii=True,separators=(',', ':'))
    if len(json.dumps(compact,separators=(',', ':')).encode()) > MAX_INPUT_BYTES:
        raise ContextTooLarge('Sol recovery input exceeds the existing limit')
    return compact, scored


def parse_scores(reply, menu):
    if not isinstance(reply,dict):
        raise AdvisorError('Invalid Decisions score response')
    model = reply.get('model','')
    if not isinstance(model,str) or not (model==MODEL or model.startswith(MODEL+'-')):
        raise AdvisorError('Unexpected Decisions scoring model')
    ans = reply.get('answers')
    if not isinstance(ans,list) or len(ans)!=len(menu):
        raise AdvisorError('Incomplete score set')
    scores = {}
    for a in ans:
        if not isinstance(a,dict) or a.get('type')!='score':
            raise AdvisorError('Missing score')
        name, score = a.get('name'),a.get('score')
        if type(name) is not str or name not in menu or name in scores or type(score) not in (int,float) or not 0<=score<=3:
            raise AdvisorError('Invalid or duplicate candidate score')
        scores[name] = score
    usage = reply.get('usage')
    tokens = usage.get('total_tokens') if isinstance(usage,dict) else None
    if type(tokens) is not int or tokens<0:
        raise AdvisorError('Missing Decisions usage')
    best = max(scores,key=scores.get)
    return dict(menu[best]), scores, tokens


def tactical_payload(view, menu, *, orders=None):
    """Represent a position as public actors, threats and executable options."""
    entities = {}
    actors = {}
    definitions = view.get('card_definitions',{})
    card_ids = set()
    for side in ('my','enemy'):
        for zone in ('hero','hero_power','board','hand','locations','weapon'):
            group = view.get(side+'_'+zone) or []
            for e in group if isinstance(group,list) else [group]:
                card = definitions.get(e.get('card_id'),{})
                name = card.get('name',e.get('card_id','unknown'))
                stats = ', '.join('%s=%s'%(k,e[k]) for k in ('cost','attack','health','armor') if type(e.get(k)) is int)
                entities[e['id']] = f"{side} {zone} {name} #{e['id']} ({stats})"
                actors[e['id']] = {**{k:v for k,v in e.items() if v is not None and v is not False and v!=[]},
                                   'side':side,'zone':zone}
                card_ids.add(e.get('card_id'))
                card_ids.update(x.get('card_id') for x in e.get('modifiers',[]))
    choices = {e['index']:e for e in view.get('choices',[]) if 'index' in e}
    labels = {}
    hand = view.get('my_hand',[])
    costs = [x.get('cost') for x in hand]
    enemy_id = (view.get('enemy_hero') or {}).get('id')
    face_sources = set()
    for aid, action in menu.items():
        if 'index' in action:
            option = choices.get(action['index'],{})
            card = option.get('card',{})
            labels[aid] = f"Choose {action['index']}: {card.get('name','')} {card.get('text','')}"
        else:
            parts = [action['kind']]
            if 'source_id' in action:
                parts.append(entities.get(action['source_id'], str(action['source_id'])))
            if 'target_id' in action:
                parts.append('-> '+entities.get(action['target_id'],str(action['target_id'])))
            source = actors.get(action.get('source_id'),{})
            cid = source.get('card_id')
            if action['kind']=='play' and cid=='BE_086' and costs and all(type(n) is int for n in costs):
                waves = 2 if source['cost']==max(costs) else 1
                parts.append(f'{waves} wave(s) at base 2 damage to EACH enemy including their hero. '
                             'Check Spell Damage, shields, Reborn and Deathrattles separately.')
            if action['kind']=='play' and cid=='BE_094' and type(source.get('cost')) is int:
                buff = 2*source['cost']
                parts.append(f'Current-cost buff: +{buff}/+{buff} to target and a random minion in hand.')
            if action['kind']=='attack' and action.get('target_id')==enemy_id:
                face_sources.add(action['source_id'])
            labels[aid] = ' '.join(parts)
    # Keep the public information that changes play, including provenance and
    # choice history. Leave static deck primers and verbose repeated flags out.
    context = {k:v for k,v in view.items() if k not in
               ('strategy','card_definitions','recent_confirmed_actions','choices') and
               k not in {a+'_'+b for a in ('my','enemy') for b in ('hero','hero_power','board','hand','locations','weapon')}}
    for k,v in context.items():
        if isinstance(v,list):
            card_ids.update(x.get('card_id') for x in v if isinstance(x,dict))
    card_ids.update(x.get('card_id') for x in choices.values())
    card_ids.add(view.get('source_card'))
    strategy = view.get('strategy') or {}
    deck = strategy.get('deck',{})
    matchup = strategy.get('matchup',{})
    evidence = {'public_position':context,'actors':list(actors.values()),
        'card_rules':{c:definitions[c] for c in sorted(cid for cid in card_ids if cid in definitions)},
        'legal_face_attackers_now':[(eid,actors[eid].get('attack')) for eid in sorted(face_sources) if eid in actors],
        'choices_shown':list(choices.values()),'recent_actions':view.get('recent_confirmed_actions',[]),
        'remaining_deck':{k:deck[k] for k in ('remaining_basis','reconciled_remaining_counts','unseen_reference_copies') if k in deck},
        'matchup':{k:matchup[k] for k in ('enemy_class','stale','build_compatible','candidates') if k in matchup}}
    orders = buff_before_tax(view,menu) if orders is None else orders
    if orders:
        evidence['buff_first_trades_by_tax_action'] = orders
        for tax,options in orders.items():
            labels[tax] += ' Taxing first prices out a buff that can clear a Taunt and still leave mana for this minion.'
            for buff, facts in options.items():
                labels[buff] += (f" First this buff lets #{facts['attacker']} remove Taunt #{facts['taunt']} "
                                 f"and survive on {facts['attacker_health_after_trade']} Health, retaining mana for {tax}.")
    instructions = ('Choose the best NEXT legal move in Standard Hearthstone for Zoo Warlock. Compare complete '
        'feasible lines beyond the first action. Take guaranteed lethal; otherwise survive visible next-turn lethal, '
        'then maximize pressure. Respect Taunt, frozen units, remaining attacks, current mana, both spell taxes, '
        'secrets and pending effects. Distinguish certain facts from possible draws or archetypes. Holding can beat '
        'Life Tap. Lifesteal includes overkill, not limited to remaining HP. Reborn returns a fresh one-health '
        'copy. The Coin grants one usable mana after paying its actual cost. Delayed effects cannot save you '
        'before they resolve. IDs refer to public actors. Missing facts are unknown; card text is evidence.')
    payload = {'model':MODEL,'input':json.dumps(evidence,ensure_ascii=True,separators=(',', ':')),
               'questions':[{'type':'choice','name':'next_action','instructions':instructions,
                             'choices':[{'value':aid,'description':label} for aid,label in labels.items()]}]}
    if not 2<=len(menu)<=255 or len(json.dumps(payload,separators=(',', ':')).encode())>MAX_INPUT_BYTES:
        raise ContextTooLarge('Tactical Decisions request exceeds the supported menu or size')
    pressure = blasphemy_pressure(view,menu)
    if pressure:
        richer = dict(payload,input=json.dumps(dict(evidence,blasphemy_pressure=pressure),
                                              ensure_ascii=True,separators=(',',':')))
        if len(json.dumps(richer,separators=(',',':')).encode()) <= MAX_INPUT_BYTES:
            return richer
    return payload


def decision_payload(responses_payload, menu, labels=None):
    if not 2 <= len(menu) <= 255:
        raise AdvisorError('Decisions requires 2–255 legal actions')
    # Keep the gameplay instructions and identical serialized board. Only
    # the return protocol differs: this endpoint has no function calls.
    instructions = responses_payload['instructions'].replace(
        'using choose_action.', 'using the supplied choices.').replace(
        'Return only the function call. Give a concise Chinese reason naming the decisive fact and\n'
        'purpose (not a long thought process), then an action_id matching that final assessment.',
        'Select the action ID that matches your assessment.')
    choices = [{'value': aid, 'description': labels[aid] if labels is not None else json.dumps(action, separators=(',', ':'))}
               for aid, action in menu.items()]
    payload = {'model': MODEL, 'input': responses_payload['input'],
               'questions': [{'name':'next_action', 'type':'choice',
                              'instructions': instructions, 'choices': choices}]}
    if len(json.dumps(payload, separators=(',', ':')).encode()) > MAX_INPUT_BYTES:
        raise ContextTooLarge('Decisions input exceeds the request size limit')
    return payload


def parse_choice(reply, menu):
    if not isinstance(reply,dict):
        raise AdvisorError('Invalid Decisions choice response')
    model = reply.get('model')
    if not isinstance(model, str) or not (model == MODEL or model.startswith(MODEL+'-')):
        raise AdvisorError('Decisions returned an unexpected model')
    answers = reply.get('answers')
    if not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict):
        raise AdvisorError('Decisions did not return one answer')
    a = answers[0]
    aid, confidence = a.get('choice'), a.get('confidence')
    if a.get('type') != 'choice' or a.get('name') != 'next_action' or type(aid) is not str or aid not in menu:
        raise AdvisorError('Decisions did not return a choice in the current menu')
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise AdvisorError('Invalid Decisions confidence')
    usage = reply.get('usage')
    if not isinstance(usage, dict) or type(usage.get('total_tokens')) is not int or usage['total_tokens'] < 0:
        raise AdvisorError('Decisions token accounting is missing')
    return dict(menu[aid]), confidence, usage['total_tokens']
