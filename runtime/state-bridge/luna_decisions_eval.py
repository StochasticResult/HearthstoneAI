"""Paired Decisions vs production Sol evaluation. No Hearthstone input."""
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time

from hard_cases import hard_cases, prepare as prepare_hard
from incremental_board import IncrementalBoard
from life_tap import life_tap_facts
from luna_decisions import decision_payload, parse_choice, tactical_payload
from model_comparison import comparison_cases, prepare
from openai_advisor import (AdvisorError, CATALOG, OpenAIAdvisor, action_menu,
                            enrich, parse_response, public_view, request_payload)

SOL = 'gpt-6.1-sol'
BASIC = ('take_visible_lethal', 'current_cost_lessons_lethal', 'freeze_buffed_weapon_hero',
         'tied_highest_cost_is_lethal', 'temporary_discover_playable', 'egg_priest_do_not_advance_egg')
HARD = ('coin_tax_spellpower_and_highest_cost', 'lifesteal_overkill_survival_target',
        'reborn_heals_twice_and_windfury', 'take_a_calculated_not_guaranteed_out',
        'generated_card_is_not_archetype_evidence', 'reserve_two_slots_for_next_ritual_wave')
HOLDOUT_BASIC = ('enable_double_blasphemy','freeze_visible_threat','cthun_discover_lethal',
                 'cthun_recover_full_board','double_damage_through_divine_shield','kazakus_prior_effects_and_deadline')
HOLDOUT_HARD = ('buff_before_sacrificing_own_tax','two_auras_buff_before_enemy_aura_dies',
                'priest_two_ruins_spent','priest_two_moonwells_spent','weapon_trigger_versus_visible_eight_damage')


def read_bounded(path, limit):
    if path.is_symlink():
        raise ValueError('Invalid replay path')
    with path.open('rb') as f:
        data = f.read(limit+1)
    if len(data) > limit:
        raise ValueError('Replay exceeds size limit')
    return data


def real_cases(runs):
    results = []
    for run, number, name, expected in (
        ('164413', 23, 'warrior_face_lethal', [{'kind':'attack','source_id':s,'target_id':66} for s in (8,11,22)]),
        ('170336', 23, 'mirror_clear_after_attacks', [{'kind':'play','source_id':46}]),
        ('170336', 26, 'mirror_draw_for_freeze', [{'kind':'hero_power','source_id':67}])):
        path = runs/('run-20261102-'+run)/'decisions.jsonl'
        events = map(json.loads, read_bounded(path, 2*1024*1024).splitlines())
        e = next(x for x in events if x.get('event')=='proposed' and x.get('decision_id')==number)
        results.append((name, e['observation'], e['actions'], expected))
    d = runs/'run-20261102-171203'
    cursor = json.loads(read_bounded(d/'log-cursor.json', 4096))
    parser = IncrementalBoard()
    log = read_bounded(d/'Power.log', 2*1024*1024)
    if len(log) != cursor['offset']-cursor['start_offset']:
        raise ValueError('Timeout replay cursor mismatch')
    for start in range(0, len(log), 131072):
        parser.feed(log[start:start+131072])
    state = parser.snapshot(cursor['verified_owner'], extended=True)
    if parser.options_id != 49 or str(state['game_turn']) != '13' or not state['own_turn']:
        raise ValueError('Timeout replay does not match the recorded window')
    events = [json.loads(l) for l in read_bounded(d/'decisions.jsonl', 2*1024*1024).splitlines()]
    prev = next(x['observation'] for x in reversed(events) if x.get('event')=='proposed')
    state['starting_deck'] = prev['starting_deck']
    state['client_build'] = prev['client_build']
    view = enrich(public_view(state))
    menu = action_menu(state)
    facts = life_tap_facts(view, menu, CATALOG)
    if facts:
        view['life_tap'] = facts
    expected = [{'kind':'play','source_id':s} for s in (18,25)]
    expected.append({'kind':'attack','source_id':28,'target_id':48})
    results.insert(0, ('real_double_timeout', view, menu, expected))
    return results


def cases_for(output, *, holdout=False):
    # Keep the public snapshots independently of the five-run rotating logs.
    archive = output.parent.parent/'decisions-sol-evaluation'/'public-replays-v1.json'
    if archive.exists():
        live = json.loads(read_bounded(archive, 512*1024))
    else:
        live = real_cases(output.parent.parent/'runs')
        with tempfile.NamedTemporaryFile(mode='w', dir=output.parent, delete=False) as f:
            json.dump(live, f, ensure_ascii=False)
        os.replace(f.name, archive)
    cases = [(name, 'replay', view, menu, expected) for name,view,menu,expected in live
             if not holdout or name=='real_double_timeout']
    for names, factory, prepare_case, group in ((HOLDOUT_BASIC if holdout else BASIC,comparison_cases,prepare,'basic'),
                                                (HOLDOUT_HARD if holdout else HARD,hard_cases,prepare_hard,'complex')):
        catalog = {c['name']:c for c in factory()}
        for name in names:
            c = catalog[name]
            view, menu = prepare_case(c)
            cases.append((name, group, view, menu, c['expected']))
    if len(cases)!=(12 if holdout else 16) or any(not expected or not all(a in menu.values() for a in expected)
                             for _,_,_,menu,expected in cases):
        raise ValueError('Invalid benchmark answer key')
    return cases


def evaluate(config, output, *, holdout=False):
    output = Path(output)
    cases = cases_for(output, holdout=holdout)
    advisor = OpenAIAdvisor({**config, 'model': SOL})
    config['api_key'] = ''
    rows, blocked = [], set()
    try:
        with output.open('w') as f:
            for index,(name,group,view,menu,expected) in enumerate(cases):
                # Identical neutral IDs and evidence for both APIs. No scores,
                # past answer, case title or future events are sent.
                actions = list(menu.values())
                random.Random('zoo-pair-v1/'+name).shuffle(actions)
                menu = {'m%d'%i:a for i,a in enumerate(actions)}
                sol,_ = request_payload(SOL,view,menu,service_tier='priority',effort='medium')
                original = decision_payload(sol,menu)
                luna = tactical_payload(view,menu) if holdout else original
                assert sol['input'] == original['input']
                digest = hashlib.sha256(sol['input'].encode()).hexdigest()
                arms = ('luna','raw','sol') if holdout else ('luna','sol')
                order = index % len(arms)
                for arm in arms[order:]+arms[:order]:
                    row = {'case':name,'group':group,'arm':arm,'evidence_sha256':digest,'expected':expected}
                    wire = original if arm=='raw' else luna if arm=='luna' else sol
                    row['wire_bytes'] = len(json.dumps(wire,separators=(',', ':')).encode())
                    if arm in blocked:
                        row['error'] = 'endpoint unavailable earlier in run'
                    else:
                        started = time.monotonic()
                        try:
                            if arm != 'sol':
                                reply = advisor._transport(wire,endpoint='decisions')
                                action, confidence, tokens = parse_choice(reply, menu)
                                row['confidence'] = confidence
                            else:
                                reply = advisor._transport(sol)
                                model = reply.get('model','')
                                if (not (model==SOL or model.startswith(SOL+'-')) or
                                        reply.get('service_tier') not in ('fast','priority') or
                                        (reply.get('reasoning') or {}).get('effort','medium')!='medium'):
                                    raise AdvisorError('Sol fast medium profile not confirmed')
                                action, _, tokens = parse_response(reply,menu)
                                row['served_tier'] = reply['service_tier']
                            row.update(action=action, correct=action in expected, model=reply['model'], tokens=tokens)
                        except AdvisorError as exc:
                            row['error'] = str(exc)[:180]
                            if any('HTTP '+str(code) in row['error'] for code in (401,403,404,429)):
                                blocked.add(arm)
                        row['elapsed_ms'] = round(1000*(time.monotonic()-started))
                    rows.append(row)
                    f.write(json.dumps(row,ensure_ascii=False)+'\n'); f.flush()
            summary = {}
            for arm in arms:
                results = [r for r in rows if r['arm']==arm]
                completed = [r for r in results if 'action' in r]
                summary[arm] = {'cases':len(results),'valid':len(completed),
                    'correct':sum(r['correct'] for r in completed),
                    'median_ms':statistics.median(r['elapsed_ms'] for r in completed) if completed else None}
            f.write(json.dumps({'summary':summary})+'\n')
    finally:
        advisor.close()


if __name__ == '__main__':
    evaluate(json.loads(sys.stdin.readline(4097)),sys.argv[1],holdout=sys.argv[2:]==['--holdout'])
