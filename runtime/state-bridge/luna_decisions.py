"""One typed choice from the same public evidence used by the Sol advisor."""
import json
import math

from openai_advisor import AdvisorError, ContextTooLarge, MAX_INPUT_BYTES

MODEL = 'gpt-6-luna'


def tactical_payload(view, menu):
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
