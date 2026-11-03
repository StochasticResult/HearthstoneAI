"""A bounded comparison of two orders for an observed buff and spell tax."""


def buff_before_tax(view, menu):
    """Return a buff-first witness only when it unlocks a surviving trade into Taunt.

    Each key is a legal first move. The hypothetical attack and the remaining
    mana explain its value; neither is an instruction to send a second input.
    """
    if (view.get('game_type') != 'GT_RANKED' or view.get('format') != 'FT_STANDARD'
            or view.get('phase') != 'main' or view.get('enemy_secret_count') != 0
            or view.get('unavailable_interactions')):
        return {}
    hand = {c['id']:c for c in (view.get('my_hand') or []) if isinstance(c,dict) and 'id' in c}
    plays = [(key, action, hand[action['source_id']]) for key,action in menu.items()
             if action.get('kind') == 'play' and action.get('source_id') in hand]
    taxes = [(key, c) for key,a,c in plays if c.get('card_id') == 'BE_729' and a.get('target_id') is None]
    buffs = [(key, a.get('target_id'), c) for key,a,c in plays if c.get('card_id') == 'BE_094']
    mana = view.get('mana')
    if not taxes or not buffs or type(mana) is not int:
        return {}
    # Unknown passive or triggered effects can change both costs and trades.
    if any(view.get(k) != [] for k in ('my_locations','enemy_locations','my_objectives','enemy_objectives',
                                       'my_secrets','my_player_effects','active_rituals','enemy_active_rituals')):
        return {}
    effects = view.get('enemy_player_effects')
    if not isinstance(effects,list) or any(e.get('card_id') != 'JAIL_509e' for e in effects):
        return {}  # Godfrey's Atlas only affects overdraw; this sequence draws nothing.
    if (view.get('my_weapon') or view.get('enemy_weapon') or any(c.get('card_id')=='GAME_005' for c in hand.values())
            or any(not isinstance(h,dict) or h.get('modifiers')!=[] or h.get('immune') is not False
                   for h in (view.get('my_hero'),view.get('enemy_hero')))):
        return {}
    friends = {c['id']:c for c in view.get('my_board', [])}
    enemies = {c['id']:c for c in view.get('enemy_board', [])}
    if view.get('my_board_slots_used') != len(friends) or len(friends) >= 7:
        return {}
    ordinary = {'TLC_603','CORE_SCH_713','CORE_UNG_205','BE_728','BE_090','BE_085t','BE_013t5',
                'TIME_025','CATA_210t','BE_014'}
    stat_mods = {'GBL_003e','GBL_016e','TLC_603e3','TIME_026e','BE_094e'}
    definitions = view.get('card_definitions',{})
    if (definitions.get('BE_729',{}).get('text') != 'ALL spells cost (1) more.' or
            definitions.get('BE_094',{}).get('text') != "Give a minion and a random one in your hand stats equal to double this card's Cost."):
        return {}
    for c in [*friends.values(),*enemies.values()]:
        definition = definitions.get(c.get('card_id'),{})
        if (definition.get('type') != 'MINION' or
                (c.get('card_id') not in ordinary and (definition.get('text') or '').strip().lower() not in ('','taunt'))
                or c.get('unknown_modifier_count') or not isinstance(c.get('modifiers'),list)
                or any(m.get('card_id') not in stat_mods for m in c['modifiers'])
                or any(type(c.get(k)) is not int for k in ('attack','health')) or c['health']<=0 or c['attack']<0
                or any(c.get(k) is not False for k in ('immune','dormant','divine_shield','reborn','poisonous','lifesteal','windfury','mega_windfury'))):
            return {}
    hero = view.get('my_hero') or {}
    if (type(hero.get('health')) is not int or type(hero.get('armor')) is not int
            or hero['health']+hero['armor'] <= sum(max(0,c['attack']) for c in enemies.values())):
        return {}
    # A cost-setting enchantment need not behave like a simple additive tax.
    def known_cost(card):
        return (type(card.get('cost')) is int and card['cost'] >= 0 and not card.get('unknown_modifier_count')
                and isinstance(card.get('modifiers'),list)
                and all(m.get('card_id') in ('GBL_003e','GBL_016e') for m in card['modifiers']))

    attacks = [(a['source_id'],enemies[a['target_id']]) for a in menu.values()
               if a.get('kind')=='attack' and a.get('source_id') in friends and a.get('target_id') in enemies]
    already_removable = {foe['id'] for src,foe in attacks if friends[src]['attack'] >= foe['health']
                         and friends[src]['health'] > foe['attack']}
    result = {}
    for tax_key,tax in taxes:
        for buff_key,target_id,buff in buffs:
            if (target_id not in friends or not known_cost(tax) or not known_cost(buff)
                    or buff['cost'] <= 0 or tax['cost'] + buff['cost'] != mana):
                continue
            attacker = friends[target_id]
            gain = 2 * buff['cost']
            for source,foe in attacks:
                if (source != target_id or foe.get('taunt') is not True or foe.get('deathrattle') is not False
                        or foe['id'] in already_removable):
                    continue
                if attacker['attack']+gain < foe['health'] or attacker['health']+gain <= foe['attack']:
                    continue
                held_discard = (attacker.get('card_id')=='TLC_603' and attacker.get('deathrattle') is True
                                and attacker['health']<=foe['attack'] and any(
                                    m.get('card_id')=='TLC_603e3' and m.get('script_data_1') in hand
                                    and m.get('script_data_1') != buff['id']
                                    for m in attacker['modifiers']))
                result.setdefault(tax_key,{})[buff_key] = {
                    'tax_cost':tax['cost'], 'buff_now':buff['cost'], 'buff_after_tax':buff['cost']+1,
                    'mana_left_if_tax_first':mana-tax['cost'], 'mana_left_if_buff_first':mana-buff['cost'],
                    'attacker':target_id, 'taunt':foe['id'], 'buffed_attack':attacker['attack']+gain,
                    'attacker_health_after_trade':attacker['health']+gain-foe['attack'],
                    'attacker_holds_a_discard':held_discard}
                break  # One witness per first move is enough even with several equivalent Taunts.
    return result


def review_action(menu, action, orders, scores=None):
    """Apply the same lesson to any advisor's one proposed move, with a public explanation."""
    tax = next((aid for aid in orders if menu[aid]==action),None)
    if tax is None:
        return action, None
    candidates = orders[tax]
    # Prefer to save a known held card, then more surviving health. A model
    # that supplied individual scores can break a remaining strategic tie.
    scores = scores or {}
    ranks = {aid:(line['attacker_holds_a_discard'],line['attacker_health_after_trade'],scores.get(aid,0))
             for aid,line in candidates.items()}
    highest = max(ranks.values())
    preferred = [aid for aid,value in ranks.items() if value==highest]
    if len(preferred)!=1:
        return action, None
    buff = preferred[0]
    return menu[buff], {'rule':'buff_before_spell_tax_v1','tax_action':tax,'first_action':buff,**candidates[buff]}
