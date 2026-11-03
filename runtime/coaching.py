"""Small public decision samples retained across detailed match-log cleanup."""


def strategy_sample(events, capped=False):
    confirmed = {e.get('decision_id') for e in events
                 if e.get('event') in ('confirmed', 'confirmed_choice', 'confirmed_target')}
    expired = {e.get('decision_id') for e in events if e.get('event') == 'expired_without_submission'}
    shadows = {e.get('for_decision'):e['action'] for e in events
               if e.get('event')=='decision_assist_shadow' and isinstance(e.get('action'),dict)}
    sample = {'partial_log': bool(capped), 'whole_turn_pass_with_options': [], 'cases': {}}
    ranks, turns = {}, {}
    for event in events:
        if event.get('event') == 'mulligan_confirmed':
            offered = {c['id']: c.get('card_id') for c in event.get('offered', [])}
            if offered:
                sample['opening'] = {'offered': list(offered.values()),
                                     'replaced': [offered.get(i) for i in event.get('replaced_ids', [])]}
            continue
        if event.get('event') != 'proposed':
            continue
        did = event.get('decision_id')
        ob, selected = event.get('observation') or {}, event.get('action') or {}
        menu = list((event.get('actions') or {}).values())
        turn = ob.get('game_turn')
        if (did not in confirmed and did not in expired) or turn is None:
            continue
        hero, enemy = ob.get('my_hero') or {}, ob.get('enemy_hero') or {}
        hp = hero.get('health')
        kind = selected.get('kind', 'choice')
        if did in confirmed:
            if enemy.get('card_id'):
                sample['opponent_hero'] = enemy['card_id']
            if ob.get('opponent_public_cards'):
                sample['seen_opposing_cards'] = list(dict.fromkeys(c['card_id'] for c in ob['opponent_public_cards']
                                                     if c.get('card_id') and not c.get('generated')))[-16:]
            if kind != 'end_turn':
                sample['last_confirmed_non_pass'] = {'turn': turn, 'decision_id': did, 'action': selected,
                                                     'health_before': [hp, enemy.get('health')]}
            played, other = turns.get(turn, (False, False))
            turns[turn] = (played or kind != 'end_turn', other or any(a.get('kind') != 'end_turn' for a in menu))
        label = None
        if did in expired:
            label, rank = 'expired', 0
        elif (kind == 'hero_power' and type(hp) is int and (hp <= 18 or ob.get('life_tap'))
              and ((ob.get('card_definitions') or {}).get((ob.get('my_hero_power') or {}).get('card_id')) or {}).get('name') == 'Life Tap'):
            label, rank = 'life_tap', -(ob.get('life_tap') or {}).get('margin_tap', hp)
        elif kind == 'end_turn' and any(a.get('kind') not in (None, 'end_turn') for a in menu):
            label, rank = 'end_with_options', ob.get('mana') or 0
        elif kind == 'attack' and selected.get('target_id') != enemy.get('id') and any(
                a.get('kind') == 'attack' and a.get('source_id') == selected.get('source_id')
                and a.get('target_id') == enemy.get('id') for a in menu):
            label, rank = 'trade_or_face', next((c.get('attack') or 0 for c in ob.get('my_board', [])
                                                 if c['id'] == selected.get('source_id')), 0)
        labels = [(label,rank)] if label else []
        if event.get('policy')=='sol_timeout_backup_v1':
            labels.append(('decisions_backup',int(did in expired)))
        if did in shadows:
            labels.append(('decisions_shadow',int(shadows[did]!=selected)))
        labels = [(name,priority) for name,priority in labels if name not in ranks or priority>ranks[name]]
        if not labels:
            continue
        def board(side):
            return [[c.get('id'), c.get('card_id'), c.get('attack'), c.get('health')] for c in ob.get(side, [])[:7]]
        detail = {
            'turn': turn, 'decision_id': did, 'status': 'expired_without_submission' if did in expired else 'confirmed',
            'mana': ob.get('mana'), 'my_health_armor': [hp, hero.get('armor')],
            'enemy_health_armor': [enemy.get('health'), enemy.get('armor')],
            'heroes': [hero.get('id'), enemy.get('id')], 'my_board': board('my_board'), 'enemy_board': board('enemy_board'),
            'hand': [[c.get('id'), c.get('card_id'), c.get('cost')] for c in ob.get('my_hand', [])[:10]],
            'action': selected, 'alternatives': [a for a in menu if a != selected][:8],
            'legal_count': len(menu), 'reason': str(event.get('reason', ''))[:180]}
        for name,priority in labels:
            ranks[name] = priority
            sample['cases'][name] = dict(detail)
            if name=='life_tap' and ob.get('life_tap'):
                sample['cases'][name]['life_tap'] = ob['life_tap']
            if name=='decisions_shadow':
                sample['cases'][name]['unused_suggestion'] = shadows[did]
    sample['observed_own_turns'] = len(turns)
    sample['whole_turn_pass_with_options'] = [t for t, (played, other) in turns.items() if not played and other][:20]
    return sample
