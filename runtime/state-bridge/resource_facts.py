"""Bounded arithmetic from observed resources/costs, recomputed every action.

Only Coin's immediate mana effect is calculated. Mawman-removal rows explicitly
assume no other effect changes; these are arithmetic aids, not playable plans.
"""

def health_only_hero_modifiers(hero, catalog):
    """These effects are already reflected in Health and leave incoming damage unchanged."""
    modifiers = hero.get('modifiers')
    if not isinstance(modifiers, list) or hero.get('unknown_modifier_count'):
        return False
    for modifier in modifiers:
        card_id = modifier.get('card_id') if isinstance(modifier, dict) else None
        card = catalog.get(card_id, {}) if isinstance(card_id, str) else {}
        if card.get('type') != 'ENCHANTMENT':
            return False
        text = card.get('text')
        if text == '-1 Health.':
            continue
        if (modifier == {'card_id':'TLC_835e','script_data_1':40} and text == 'Increased Health.'
                and hero.get('max_health') == 40
                and ' '.join((catalog.get('TLC_835', {}).get('text') or '').split()) == "Set your hero's Health to 40."):
            continue
        if (modifier == {'card_id':'EDR_810e','script_data_1':1} and text == 'Reduced Health.'
                and ' '.join((catalog.get('EDR_810t', {}).get('text') or '').split())
                == 'At the end of your turn, your hero steals 1 Health from the lowest Health enemy.'):
            continue
        return False
    return True


def coin_result(mana, cost, cap):
    if not all(type(x) is int for x in (mana, cost, cap)) or not 0 <= cost <= mana <= cap <= 100:
        return None
    after = min(cap, mana - cost + 1)
    return {'mana_after': after, 'net_gain': after - mana,
            'wasted_at_cap': max(0, mana - cost + 1 - cap)}


def blasphemy_damage(view, card):
    """Printed waves and health changes before deathrattles, Reborn or other triggers."""
    hand, foes = view.get('my_hand',[]), view.get('enemy_board',[])
    costs = [row.get('cost') for row in hand]
    if (card.get('card_id')!='BE_086' or not costs or type(card.get('cost')) is not int
            or any(type(n) is not int for n in costs) or any(type(x.get('health')) is not int for x in foes)):
        return None
    waves = 2 if card['cost']==max(costs) else 1
    per = 2+sum(x.get('spell_damage') or 0 for x in view.get('my_board',[])
               if not x.get('dormant') and not x.get('silenced'))
    remaining, triggers = {}, []
    for x in foes:
        hits = 0 if x.get('immune') or x.get('dormant') else max(0,waves-int(bool(x.get('divine_shield'))))
        remaining[x['id']] = max(0,x['health']-hits*per)
        if not remaining[x['id']] and (x.get('deathrattle') or x.get('reborn')):
            triggers.append(x['id'])
    hero = view.get('enemy_hero') or {}
    return {'waves':waves,'damage_per_wave_before_replacements':per,
            'original_enemy_minion_hp_after_spell_before_triggers':remaining,
            'destroyed_with_deathrattle_or_reborn':triggers,
            'enemy_hero_damage_before_other_effects':0 if hero.get('immune') else per*max(0,waves-int(bool(hero.get('divine_shield'))))}


def blasphemy_pressure(view, menu):
    """One potential face swing per current attacker behind ordinary Taunts."""
    cards = {c['id']:c for c in view.get('my_hand',[]) if c.get('card_id')=='BE_086'}
    plays = {k:cards[a['source_id']] for k,a in menu.items()
             if a.get('kind')=='play' and a.get('source_id') in cards}
    if not plays or view.get('phase')!='main' or view.get('enemy_secret_count')!=0 or view.get('unavailable_interactions'):
        return {}
    hero = view.get('enemy_hero') or {}
    if (hero.get('immune') is not False or hero.get('divine_shield') or hero.get('modifiers')!=[]
            or hero.get('unknown_modifier_count') or view.get('enemy_player_effects') or view.get('my_player_effects')):
        return {}
    defs = view.get('card_definitions',{})
    taunts = {u['id'] for u in view.get('enemy_board',[]) if u.get('taunt') and not u.get('stealth') and not u.get('dormant')}
    if not taunts or any((not u.get('silenced') and defs.get(u.get('card_id'),{}).get('text') not in ('','Taunt'))
                         or u.get('unknown_modifier_count') or u.get('modifiers')!=[] for u in view.get('enemy_board',[])):
        return {}
    attacks = [a for a in menu.values() if a.get('kind')=='attack']
    blocked = {a['source_id'] for a in attacks if a.get('target_id') in taunts}
    blocked -= {a['source_id'] for a in attacks if a.get('target_id')==hero.get('id')}
    ready = {u['id']:u['attack'] for u in view.get('my_board',[]) if u['id'] in blocked
             and type(u.get('attack')) is int and u['attack']>0 and not any(u.get(flag) for flag in
             ('rush','frozen','exhausted','dormant','cant_attack','cant_attack_heroes','unknown_modifier_count'))}
    if not ready:
        return {}
    result = {}
    for key,card in plays.items():
        damage = blasphemy_damage(view,card)
        if not damage or damage['destroyed_with_deathrattle_or_reborn']:
            continue
        hp = damage['original_enemy_minion_hp_after_spell_before_triggers']
        if all(hp[t]==0 for t in taunts):
            result[key] = {'waves':damage['waves'],'spell_face_damage':damage['enemy_hero_damage_before_other_effects'],
                           'taunts_cleared':sorted(taunts),'one_face_swing_by_source':ready,
                           'potential_followup_face_damage':sum(ready.values()),
                           'scope':'If the spell resolves as printed and no other effect intervenes. Verify fresh attack targets after resolution.'}
    return result


def known_shred_count(view, remaining):
    """Both observations describe the same deck and can include the same cards."""
    ids = {c['id'] for c in view.get('own_known_deck', []) if c.get('card_id') == 'TIME_025t'
           and type(c.get('id')) is int and c['id'] > 0}
    exact = remaining.get('TIME_025t', 0) if isinstance(remaining, dict) else 0
    return max(len(ids), exact if type(exact) is int else 0)


def shred_battlecry_menu(view, menu, catalog):
    """Separate an engine-legal play from one known to kill our hero on resolution."""
    if view.get('phase')!='main':
        return view,menu
    drakes=[c for c in (view.get('my_hand') or []) if c.get('card_id')=='TIME_029']
    if not drakes:
        return view,menu
    hero=view.get('my_hero') or {}
    hp,armor=hero.get('health'),hero.get('armor')
    if (type(hp) is not int or type(armor) is not int or hp<=0 or armor<0 or hp+armor>3 or hero.get('immune') is not False
            or type(view.get('my_deck_count')) is not int):
        return view,menu
    exact=((view.get('strategy') or {}).get('deck') or {}).get('reconciled_remaining_counts')
    known_count=known_shred_count(view, exact)
    absent=view['my_deck_count']==0 or (isinstance(exact,dict) and not known_count)
    # Before making a hard exclusion check the printed effects and rule out damage
    # prevention or battlecry suppression on unfamiliar boards. Otherwise explain the risk.
    text=lambda cid:' '.join((catalog.get(cid,{}).get('text') or '').split())
    plain={'TLC_603','CORE_SCH_713','CORE_UNG_205','BE_728','BE_729','BE_090','BE_085t','BE_013t5',
           'TIME_025','TIME_029','CATA_210t','CATA_210','BE_014','EDR_856','CAP_405',
           'JAIL_407','TIME_042','BE_725','TLC_249','TLC_482','JAIL_800'}
    uncomplicated=(view.get('game_type')=='GT_RANKED' and view.get('format')=='FT_STANDARD'
        and hero.get('modifiers')==[] and not hero.get('divine_shield') and not hero.get('unknown_modifier_count')
        and view.get('enemy_secret_count')==0 and all(view.get(k)==[] for k in
            ('my_player_effects','enemy_player_effects','my_secrets','my_locations','enemy_locations',
             'my_objectives','enemy_objectives','active_rituals','enemy_active_rituals'))
        and not view.get('my_weapon') and not view.get('enemy_weapon')
        and all(c.get('card_id') in plain and not c.get('unknown_modifier_count') and c.get('modifiers')==[]
                for c in view.get('my_board',[])+view.get('enemy_board',[]))
        and text('TIME_025t')=='Casts When Drawn Deal $3 damage to your hero.'
        and text('TIME_029')=='Rush Battlecry: Cast a Shred of Time from your deck to summon a copy of this.')
    withheld=[c['id'] for c in drakes if uncomplicated and known_count>0 and not absent
              and not c.get('unknown_modifier_count') and isinstance(c.get('modifiers'),list)
              and all(m.get('card_id') in ('GBL_003e','GBL_016e','BE_094e') for m in c['modifiers'])]
    facts={'effective_health':hp+armor,'base_self_damage':3,'known_shreds_at_least':known_count,'known_absent':absent,
           'withheld_sources':withheld,'consequence':'With a Shred, cast it on your own hero before relying on the Rush copy. '
                       'Without a Shred, the Battlecry summons no copy. Consider protection separately.'}
    safe={k:a for k,a in menu.items() if not (a.get('kind')=='play' and a.get('source_id') in withheld)} if withheld else menu
    return dict(view,shred_battlecry=facts),safe


def resource_facts(state, catalog):
    mana, cap = state.get('mana'), state.get('mana_cap')
    hand = state.get('my_hand', [])
    sources = [c['id'] for c in state.get('my_board', []) + state.get('enemy_board', [])
               if c.get('card_id') == 'BE_729' and not c.get('silenced') and not c.get('dormant')]
    result = {'available_mana': mana, 'observed_mana_cap': cap,
              'active_mawman_ids': sources, 'coins': []}
    for coin in hand[:10]:
        if coin.get('card_id') != 'GAME_005':
            continue
        now = coin_result(mana, coin.get('cost'), cap)
        row = {'id': coin['id'], 'current_cost': coin.get('cost'), 'play_now': now}
        # Unknown/copy/set-cost effects are not inverted. Current costs are
        # still shown, but hypothetical aura removal requires the simple
        # observed cost == printed cost + current Mawman count relationship.
        if (now is not None and not coin.get('modifiers') and not coin.get('unknown_modifier_count')
                and coin['cost'] == len(sources) and len(sources) <= 4):
            scenarios = []
            for removed in range(len(sources) + 1):
                cost = coin['cost'] - removed
                table = []
                for spend in sorted({c.get('cost') for c in hand if c.get('card_type') == 'MINION'
                                     and type(c.get('cost')) is int and 0 <= c['cost'] <= mana}):
                    after = coin_result(mana - spend, cost, cap)
                    if after is not None:
                        table.append({'spend_before_coin': spend, 'mana_after_coin': after['mana_after']})
                scenarios.append({'removed_mawmen': removed, 'coin_cost': cost,
                                  'play_now': coin_result(mana, cost, cap), 'spend_first': table})
            row['conditional_scenarios'] = scenarios
        result['coins'].append(row)
    if sources:
        result['spell_cost_if_mawmen_leave'] = [
            {'id': c['id'], 'cost_by_removed_count': [c['cost'] - n for n in range(len(sources) + 1)]}
            for c in hand[:10] if c.get('card_type') == 'SPELL' and not c.get('modifiers') and not c.get('unknown_modifier_count')
            and type(c.get('cost')) is int and len(sources) <= 4
            and c['cost'] == catalog.get(c.get('card_id'), {}).get('cost', -100) + len(sources)]
    if result['coins'] or sources:
        result['scope'] = ('Current costs and mana are observed. Conditional rows assume ONLY the stated Mawman auras '
                           'leave and no other effect changes. Spend-first rows are arithmetic, not proof of a legal '
                           'sequence or a minion battlecry outcome. Re-read actual state after every action.')
        return result
    return None
