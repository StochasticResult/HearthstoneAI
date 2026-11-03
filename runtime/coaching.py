"""Small public decision samples retained across detailed match-log cleanup."""

def lesson_role(event):
    if not event.get('tactical_lesson') or not event.get('model_action'):
        return None
    if event.get('event')=='decision_assist_shadow':
        return 'shadow'
    if event.get('event')=='proposed':
        return 'backup' if event.get('policy')=='sol_timeout_backup_v1' else 'primary'
    return None


def strategy_sample(events, capped=False):
    confirmed = {e.get('decision_id') for e in events
                 if e.get('event') in ('confirmed', 'confirmed_choice', 'confirmed_target')}
    expired = {e.get('decision_id') for e in events if e.get('event') == 'expired_without_submission'}
    shadows = {e.get('for_decision'):e for e in events
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
            labels.append(('decisions_shadow',int(shadows[did]['action']!=selected)))
        role = lesson_role(event)
        if role:
            labels.append(('lesson_'+role,int(did in expired)))
        if did in shadows and lesson_role(shadows[did]):
            labels.append(('lesson_shadow',1))
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
                sample['cases'][name]['unused_suggestion'] = shadows[did]['action']
            if name.startswith('lesson_'):
                judged = shadows[did] if name=='lesson_shadow' else event
                sample['cases'][name].update(model=judged.get('model'),model_action=judged['model_action'],
                    reviewed_action=judged['action'],tactical_lesson=judged['tactical_lesson'])
    sample['observed_own_turns'] = len(turns)
    sample['whole_turn_pass_with_options'] = [t for t, (played, other) in turns.items() if not played and other][:20]
    return sample


def record_clean_losses(learning, last_run, previous):
    """Write a bounded local evidence review for three disjoint, verified losses."""
    import sqlite3
    import json
    from coach_policy import atomic,read_json
    from ranked_autoplay import stop_reason
    history=read_json(learning/'match-history.json',256*1024,[])
    if not isinstance(history,list) or not all(isinstance(g,dict) for g in history[-3:]):
        raise ValueError('Invalid match history for loss review')
    games=history[-3:]
    runs=[g.get('run') for g in games]
    if (len(runs)!=3 or runs[-1]!=last_run or previous in runs or len(set(runs))!=3
            or any(g.get('outcome')!='LOST' or stop_reason(g) for g in games)):
        return None
    path=learning/'loss-streak-reviews.json'
    reviews=read_json(path,512*1024,[])
    if not isinstance(reviews,list) or not all(isinstance(r,dict) and isinstance(r.get('runs'),list) for r in reviews):
        raise ValueError('Invalid recorded loss review')
    if any(set(runs).intersection(record['runs']) for record in reviews):
        return None
    samples={}
    database=learning/'match-stats.sqlite3'
    if database.is_file() and not database.is_symlink():
        try:
            db=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=.3)
            try:
                for run,raw in db.execute('SELECT run,sample FROM strategy_samples WHERE run IN (?,?,?) AND length(sample)<=65536',runs):
                    data=json.loads(raw)
                    if isinstance(data,dict) and isinstance(data.get('cases',{}),dict):
                        samples[run]=data
            finally:
                db.close()
        except sqlite3.Error:
            pass  # Counters and versioned results remain available from match history.
    evidence=[]
    for g in games:
        sample=samples.get(g['run'],{})
        cases=[{'kind':kind, **{k:c[k] for k in ('turn','decision_id','status','action') if k in c},
                'alternatives':c.get('alternatives',[])[:3]} for kind,c in sample.get('cases',{}).items() if isinstance(c,dict)]
        evidence.append({k:g[k] for k in ('run','runtime_version','coach_revision','confirmed_decisions','expired_decisions',
                         'api_timeouts','median_decision_ms','late_turn_responses','turn_budget_backups') if k in g}
                        | {'decision_sample_available':bool(sample),'key_turns':cases,
                           'whole_turn_pass_with_options':sample.get('whole_turn_pass_with_options') if sample else None})
    reviews.append({'runs':runs,'games':evidence})
    atomic(path,reviews[-12:])
    return runs
