"""Small counterfactual for a currently legal Life Tap, using only public cards."""
from resource_facts import health_only_hero_modifiers, known_shred_count


def _preserves_shred_damage(effect, catalog):
    card_id = effect.get('card_id') if isinstance(effect, dict) else None
    card = catalog.get(card_id, {}) if isinstance(card_id, str) else {}
    if card.get('type') != 'ENCHANTMENT':
        return False
    if card.get('text') == 'Your spells cost (1) more this turn.':
        return effect.keys() <= {'card_id','script_data_1','count'}
    if effect != {'card_id':'CAP_805e'} or card.get('text'):
        return False
    resummon = ' '.join((catalog.get('CAP_805t', {}).get('text') or '').split())
    return resummon == 'Resummon all friendly minions that were slimed.'


def life_tap_facts(view, menu, catalog):
    power = view.get('my_hero_power') or {}
    definition = catalog.get(power.get('card_id'), {})
    if (definition.get('name') != 'Life Tap' or definition.get('type') != 'HERO_POWER'
            or ' '.join(definition.get('text', '').split()) != 'Hero Power Draw a card and take $2 damage.'
            or not any(a.get('kind') == 'hero_power' and a.get('source_id') == power.get('id') for a in menu.values())):
        return None
    hero = view.get('my_hero') or {}
    hp, armor, mana, cost = hero.get('health'), hero.get('armor'), view.get('mana'), power.get('cost')
    if any(type(v) is not int or v < 0 for v in (hp, armor, mana, cost)) or mana < cost or hero.get('immune'):
        return None
    if not isinstance(view.get('enemy_board'), list) or not isinstance(view.get('my_board'), list):
        return None
    enemies = view['enemy_board']
    friendly = view['my_board']
    if any(type(c.get('attack')) is not int or type(c.get('id')) is not int for c in enemies):
        return None
    attacks = {c['id']: max(0, c['attack']) * (4 if c.get('mega_windfury') else 2 if c.get('windfury') else 1)
               for c in enemies if not any(c.get(k) for k in ('dormant', 'frozen', 'cant_attack', 'cant_attack_heroes'))}
    double = (any(c.get('card_id') == 'TIME_064' and not c.get('silenced') and not c.get('dormant') for c in friendly)
              or any(e.get('card_id') == 'TIME_064e' for e in view.get('my_player_effects', [])))
    draws = 2 if double else 1
    deck, fatigue = view.get('my_deck_count'), view.get('my_fatigue')
    if type(deck) is not int or deck < 0 or type(fatigue) is not int or fatigue < 0:
        return None
    missing = max(0, draws - deck)
    fatigue_damage = sum(fatigue + i for i in range(1, missing + 1))
    hit = 2 * draws
    budget = hp + armor
    pressure = sum(attacks.values())
    weapon = view.get('enemy_weapon') or {}
    enemy_frozen = (view.get('enemy_hero') or {}).get('frozen')
    if weapon and (type(weapon.get('attack')) is not int or type(weapon.get('durability')) is not int):
        return None
    if weapon and weapon['durability'] > 0 and not enemy_frozen:
        pressure += max(0, weapon.get('attack', 0)) * (2 if weapon.get('windfury') else 1)
    power_attack = 0
    enemy_power = view.get('enemy_hero_power') or {}
    if enemy_power.get('card_id') == 'CATA_190p' and not enemy_frozen:
        power_attack = 5  # Ruthless. Conditional on paying for it and being able to attack.
    # Avoid adding a checklist to low-pressure early turns. A nearly full hand
    # or fatigue still makes drawing a real decision even at full health.
    hand = view.get('my_hand', [])
    if budget > 18 and budget - hit - fatigue_damage - pressure - power_attack > 6 and not missing and len(hand) + draws <= 10:
        return None
    costs = {'base_self_damage': hit, 'draws': draws, 'fatigue_if_no_casts_when_drawn': fatigue_damage,
             'mana_after_tap': mana - cost, 'effective_health_hold': budget,
             'effective_health_tap_before_other_effects': budget - hit - fatigue_damage,
             'unblocked_board_and_weapon_attack': pressure}
    # Both margins use the same opposing attacks. Exclude uncertain on-death
    # replacements and use at most one attack from each current legal source.
    # This is one example exchange, not an optimal sequence or guaranteed block.
    ours = {c['id']: c for c in friendly}
    targets = {c['id']: c for c in enemies if attacks.get(c['id'], 0) > 0 and not any(
        c.get(k) for k in ('divine_shield', 'immune', 'reborn', 'deathrattle'))}
    candidates = []
    for a in menu.values():
        source, target = a.get('source_id'), a.get('target_id')
        if a.get('kind') != 'attack' or source not in ours or target not in targets:
            continue
        c, t = ours[source], targets[target]
        survives = (c.get('divine_shield') or
                    (not t.get('poisonous') and type(c.get('health')) is int and c['health'] > t['attack']))
        if (type(t.get('health')) is int and type(c.get('attack')) is int
                and 0 < t['health'] <= c['attack'] and (survives or not c.get('deathrattle'))):
            candidates.append((-attacks[target], c['attack'], source, target))
    spent, removed, exchanges = set(), set(), []
    for _, _, source, target in sorted(candidates):
        if source not in spent and target not in removed:
            spent.add(source)
            removed.add(target)
            exchanges.append([source, target])
    after = pressure - sum(attacks[i] for i in removed)
    costs.update(example_trades=exchanges, unblocked_attack_after_example=after,
                 margin_hold=budget-after, margin_tap=budget-hit-fatigue_damage-after)
    taunts = [c['id'] for c in friendly if c.get('taunt') and not c.get('dormant') and not c.get('stealth')]
    if taunts:
        costs['friendly_taunts_before_trades'] = taunts
    if power_attack:
        costs['enemy_hero_power_attack_if_usable'] = power_attack
    deck_info = (view.get('strategy') or {}).get('deck') or {}
    remaining = deck_info.get('reconciled_remaining_counts')
    if isinstance(remaining, dict) and remaining.get('TIME_025t'):
        costs['verified_shreds_in_deck'] = remaining['TIME_025t']
    known = known_shred_count(view, remaining)
    if 0 < known <= deck:
        costs['known_shreds_at_least'] = known
        text = ' '.join((catalog.get('TIME_025t', {}).get('text') or '').split())
        effects = view.get('my_player_effects')
        if (known < deck and draws == 1 and len(hand) < 10 and hero.get('immune') is False
                and health_only_hero_modifiers(hero, catalog) and not hero.get('divine_shield')
                and isinstance(effects, list) and all(_preserves_shred_damage(e, catalog) for e in effects)
                and all(c.get('spell_damage') == 0 for c in friendly if not c.get('dormant') and not c.get('silenced'))
                and text == 'Casts When Drawn Deal $3 damage to your hero.'):
            health = budget - hit - 3
            costs['if_one_shred_at_printed_damage'] = {'effective_health':health, 'margin_after_example_trades':health-after,
                'scope':'One Shred casts for 3; excludes additional Shreds, replacement draws and other effects. Deck order unknown.'}
    if len(hand) + draws > 10:
        costs['hand_slots_before_drawing'] = max(0, 10-len(hand))
    # Compare an actual possible draw with the visible damage. In particular,
    # an unreconciled deck supports a conditional line, never an exact draw rate.
    shard = 'CORE_UNG_205'
    counted = isinstance(remaining,dict)
    copies = (remaining if counted else (deck_info.get('unseen_reference_copies') or {})).get(shard,0)
    shard_cost = catalog.get(shard,{}).get('cost')
    slots = view.get('my_board_slots_used')
    if (not taunts and not missing and draws==1 and len(hand)<10 and costs['margin_hold']<=0
            and 0<costs['effective_health_tap_before_other_effects'] and view.get('enemy_secret_count')==0
            and type(slots) is int and 0<=slots<7 and not any(c.get('card_id')==shard for c in hand)
            and type(shard_cost) is int and shard_cost<=mana-cost and type(copies) is int and copies>0
            and (counted or (deck_info.get('selected_cards') or {}).get(shard,0)>0)):
        targets_after_draw = []
        for foe in enemies:
            prevented = attacks.get(foe['id'],0)
            if (foe['id'] in removed or not prevented or foe.get('stealth') is not False
                    or foe.get('immune') is not False or foe.get('dormant') is not False):
                continue
            connects = after-prevented+power_attack
            margin = costs['effective_health_tap_before_other_effects']-connects
            if margin>0:
                targets_after_draw.append({'target_id':foe['id'],'attack_prevented':prevented,
                                           'remaining_visible_attack':connects,'health_margin':margin})
        if targets_after_draw:
            costs['conditional_freeze_draw'] = {'card_id':shard,'printed_mana_cost':shard_cost,
                'count_basis':'reconciled deck' if counted else 'unseen in verified starting list; presence uncertain',
                'if_drawn_and_playable':targets_after_draw,
                'assumptions':'Draw the plain Shard, play at the affordable cost, target stays frozen; '
                              'ignores other triggers, hidden damage and future draws. Compare drawing before spending mana.'}
    costs['compare'] = ('Compare holding life with drawing. Both margins assume the example trades work and remaining '
                        'minions/weapon connect unblocked. Taunts can prevent that damage. Check hero power, visible effects, '
                        'damage prevention, Shreds, waking minions and plausible reach. '
                        'At a thin margin prefer a useful play/trade already available. For a risky Tap name a specific '
                        'affordable immediate out; if holding already loses, drawing for a live out can be best.')
    return costs
