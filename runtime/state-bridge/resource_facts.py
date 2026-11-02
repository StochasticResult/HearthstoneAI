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
