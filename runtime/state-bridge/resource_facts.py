"""Bounded arithmetic from observed resources/costs, recomputed every action.

Only Coin's immediate mana effect is calculated. Mawman-removal rows explicitly
assume no other effect changes; these are arithmetic aids, not playable plans.
"""


def coin_result(mana, cost, cap):
    if not all(type(x) is int for x in (mana, cost, cap)) or not 0 <= cost <= mana <= cap <= 100:
        return None
    after = min(cap, mana - cost + 1)
    return {'mana_after': after, 'net_gain': after - mana,
            'wasted_at_cap': max(0, mana - cost + 1 - cap)}


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
    known={row['id'] for row in view.get('own_known_deck',[]) if row.get('card_id')=='TIME_025t' and 'id' in row}
    exact=((view.get('strategy') or {}).get('deck') or {}).get('reconciled_remaining_counts')
    exact_count=exact.get('TIME_025t',0) if isinstance(exact,dict) else 0
    known_count=max(len(known),exact_count if type(exact_count) is int else 0)
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
