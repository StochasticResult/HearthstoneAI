"""Small, conservative arithmetic proofs, not a general Hearthstone simulator.

The Glare rule only chooses automatically with an empty friendly board/hand,
zero mana, unmodified heroes, and simple opposing minions. Any unmodeled effect
returns None. Hidden future cards remain unknown even when the rule applies.
"""


def extra_public_effects(state):
    """Arithmetic shortcuts must opt out of newly observed mechanics."""
    if any(state.get(k) for k in ('my_locations','enemy_locations','my_player_effects',
                                  'enemy_player_effects','my_secrets','enemy_objectives')):
        return True
    return any(r.get('card_id') not in ('BE_085','BE_726') for r in state.get('my_objectives',[]))


def forced_face_attack(state, menu):
    """Skip a paid choice when only harmless, ordinary face attacks remain.

    Never handles combat trades, hero attacks, secrets, enemy board triggers,
    unknown buffs, weapons, or an alternative playable card/power.
    """
    if (extra_public_effects(state) or state.get('enemy_board') != [] or state.get('enemy_secret_count', 0)
            or state.get('my_weapon') or state.get('enemy_weapon') or state.get('enemy_active_rituals')
            or (state.get('my_hero') or {}).get('modifiers')
            or state.get('healing_as_damage', 0) or state.get('phase', 'main') != 'main'):
        return None
    if any(r.get('card_id') not in ('BE_085', 'BE_726') for r in state.get('active_rituals', [])):
        return None
    enemy = state.get('enemy_hero', {})
    from card_metadata import CATALOG
    hero_definition = CATALOG.get(enemy.get('card_id'), {})
    power = state.get('enemy_hero_power') or {}
    if power:
        power_definition = CATALOG.get(power.get('card_id'), {})
        if (power_definition.get('type') != 'HERO_POWER' or 'passive' in power_definition.get('text','').lower()
                or 'PASSIVE' in power_definition.get('mechanics', ())):
            return None
    if (enemy.get('modifiers') != [] or enemy.get('attack') != 0 or enemy.get('immune')
            or hero_definition.get('type') != 'HERO' or hero_definition.get('text') or 'cost' in hero_definition
            or type(enemy.get('health')) is not int or enemy['health'] <= 0):
        return None
    moves = [a for a in menu.values() if a.get('kind') != 'end_turn']
    if not moves or any(a.get('kind') != 'attack' or a.get('target_id') != enemy.get('id') for a in moves):
        return None
    passive_ids = {'TLC_603', 'CORE_UNG_205', 'CATA_210t', 'CATA_210', 'CORE_SCH_713',
                   'BE_085t', 'TIME_025', 'BE_729', 'BE_728', 'CAP_405'}
    bodies = {c['id']: c for c in state.get('my_board', [])}
    for body in bodies.values():
        if (body.get('card_id') not in passive_ids or body.get('unknown_modifier_count')
                or any(mod.get('card_id') not in ('TIME_026e','BE_094e') for mod in body.get('modifiers',[]))):
            return None
    safe = []
    for action in moves:
        c = bodies.get(action.get('source_id'), {})
        if (c.get('stealth') or c.get('lifesteal') or type(c.get('attack')) is not int or c['attack'] <= 0):
            return None
        safe.append((c['attack'], action))
    return {'rule': 'ordinary_face_attacks_only_v1', 'action': max(safe, key=lambda pair: pair[0])[1],
            'reason': '敌方空场、无奥秘和受击效果；当前只剩普通随从打脸与结束回合，先完成有效攻击。',
            'evidence': {'available_attackers': len(safe), 'enemy_health': enemy['health']}}


def glare_survival(state, targets, catalog):
    if (extra_public_effects(state) or state.get('phase') != 'target' or not state.get('own_turn')
            or state.get('mana') != 0 or state.get('my_board') != []
            or state.get('my_hand') != [] or state.get('enemy_weapon')
            or state.get('my_weapon') or state.get('enemy_active_rituals')
            or state.get('enemy_secret_count', 0)):
        return None
    if state.get('healing_as_damage') != 0 or state.get('healing_double') != 0:
        return None
    if any(r.get('card_id') != 'BE_726' for r in state.get('active_rituals', [])):
        return None
    hero, enemy = state.get('my_hero', {}), state.get('enemy_hero', {})
    for row in (hero, enemy):
        if (row.get('modifiers') != [] or row.get('immune')
                or catalog.get(row.get('card_id'), {}).get('text')
                or row.get('card_id') not in ('HERO_07', 'HERO_09', 'HERO_01', 'HERO_02',
                                               'HERO_03', 'HERO_04', 'HERO_05', 'HERO_06', 'HERO_08')):
            return None
    if enemy.get('attack') != 0:
        return None
    hp, maximum, armor = hero.get('health'), hero.get('max_health'), hero.get('armor')
    if (any(type(x) is not int for x in (hp, maximum, armor))
            or not 0 < hp <= maximum <= 100 or not 0 <= armor <= 100):
        return None
    rows = state.get('enemy_board', [])
    if not 2 <= len(rows) <= 7 or len(targets) != len(set(targets)):
        return None
    for row in rows:
        definition = catalog.get(row.get('card_id'), {})
        # Permit only vanilla bodies or exact, passive combat keywords. A
        # Battlecry may have left a player-level effect, so it is excluded too.
        text = ' '.join(definition.get('text', '').replace(',', ' ').split()).lower()
        if (definition.get('type') != 'MINION'
                or text not in ('', 'taunt', 'divine shield', 'windfury')
                or not set(definition.get('mechanics', [])) <= {'TAUNT', 'DIVINE_SHIELD', 'WINDFURY'}
                or row.get('modifiers', []) or any(row.get(k) for k in ('reborn', 'dormant', 'frozen',
                    'mega_windfury','titan','deathrattle','poisonous','lifesteal'))
                or type(row.get('attack')) is not int or not 0 <= row['attack'] <= 100
                or type(row.get('health')) is not int or not 0 < row['health'] <= 100):
            return None
    if not set(targets) <= {r['id'] for r in rows}:
        return None
    evidence = []
    for target in targets:
        victim = next(r for r in rows if r['id'] == target)
        dealt = 0 if victim.get('divine_shield') or victim.get('immune') else 5
        killed = dealt >= victim['health']
        healed = min(maximum, hp + dealt) - hp
        attack = sum(r['attack'] * (2 if r.get('windfury') else 1)
                     for r in rows if r['id'] != target or not killed)
        evidence.append({'target_id': target, 'damage_dealt': dealt, 'healed': healed,
                         'target_removed': killed, 'remaining_attack': attack,
                         'effective_health_after_visible_attacks': hp + healed + armor - attack})
    surviving = [r for r in evidence if r['effective_health_after_visible_attacks'] > 0]
    if len(surviving) != 1:
        return None
    winner = surviving[0]
    return {'rule': 'glare_visible_survival_v1', 'action': {'kind': 'choose_target', 'target_id': winner['target_id']},
            'reason': '凝视回复%d血，移除该目标后剩余%d攻击；仅此目标能挡住当前可见场攻，余%d有效生命。未知手牌另计。' %
                      (winner['healed'], winner['remaining_attack'], winner['effective_health_after_visible_attacks']),
            'evidence': evidence}
