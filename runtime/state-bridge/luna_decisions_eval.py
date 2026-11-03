"""Paired Decisions vs production Sol evaluation. No Hearthstone input."""
import copy
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
from luna_decisions import decision_payload, emergency_clears, parse_choice, parse_scores, prepare_backup, recovery_inputs, tactical_payload
from model_comparison import CaptureAdvisor, comparison_cases, prepare
from openai_advisor import (AdvisorError, AdvisorTimeout, CATALOG, OpenAIAdvisor, action_menu,
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


def cases_for(output, *, holdout=False, recovery=False):
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
             if (name in ('real_double_timeout','mirror_clear_after_attacks') if recovery
                 else not holdout or name=='real_double_timeout')]
    basic = HOLDOUT_BASIC if holdout else BASIC
    hard = HOLDOUT_HARD if holdout else HARD
    if recovery:
        basic = ('spells_before_own_tax','handbuff_dk_deny_lifesteal_race','priest_preserve_ruin_threshold',
                 'zakajz_cost_change_lethal_order','double_damage_through_divine_shield')
        hard = ('buff_to_preserve_the_spell_tax_aura',)
    for names, factory, prepare_case, group in ((basic,comparison_cases,prepare,'basic'),
                                               (hard,hard_cases,prepare_hard,'complex')):
        catalog = {c['name']:c for c in factory()}
        for name in names:
            c = catalog[name]
            view, menu = prepare_case(c)
            cases.append((name, group, view, menu, c['expected']))
    if len(cases)!=(8 if recovery else 12 if holdout else 16) or any(not expected or not all(a in menu.values() for a in expected)
                             for _,_,_,menu,expected in cases):
        raise ValueError('Invalid benchmark answer key')
    return cases


def blind_menu(name, menu):
    actions = list(menu.values())
    random.Random('zoo-pair-v1/'+name).shuffle(actions)
    return {'m%d'%i:a for i,a in enumerate(actions)}


def evaluate(config, output, *, holdout=False, recovery=False):
    output = Path(output)
    cases = cases_for(output, holdout=holdout,recovery=recovery)
    advisor = OpenAIAdvisor({**config, 'model': SOL})
    config['api_key'] = ''
    rows, blocked = [], set()
    try:
        with output.open('w') as f:
            if recovery:
                # One harmless schema probe. A 200 would establish acceptance,
                # not that the model actually used a higher reasoning budget.
                probe = {'model':'gpt-6-luna','input':'Two is greater than one.',
                         'questions':[{'type':'predicate','name':'test','instructions':'Is this true?'}],
                         'reasoning':{'effort':'high'}}
                try:
                    reply = advisor._transport(probe,endpoint='decisions')
                    info = {'accepted':True,'echo':reply.get('reasoning')}
                except AdvisorError as exc:
                    info = {'accepted':False,'error':str(exc)[:160]}
                f.write(json.dumps({'effort_probe':info})+'\n'); f.flush()
            for index,(name,group,view,menu,expected) in enumerate(cases):
                # Identical neutral IDs and evidence for both APIs. No scores,
                # past answer, case title or future events are sent.
                menu = blind_menu(name,menu)
                sol,_ = request_payload(SOL,view,menu,service_tier='priority',effort='medium')
                original = decision_payload(sol,menu)
                luna = tactical_payload(view,menu) if holdout else original
                assert sol['input'] == original['input']
                digest = hashlib.sha256(sol['input'].encode()).hexdigest()
                arms = ('luna','raw','sol') if holdout else ('luna','sol')
                wires = {'luna':luna,'raw':original,'sol':sol}
                if recovery:
                    compact,scored = recovery_inputs(sol,view,menu)
                    wires = {'score':scored,'compact_sol':compact,'sol':sol}
                    arms = tuple(wires)
                order = index % len(arms)
                for arm in arms[order:]+arms[:order]:
                    row = {'case':name,'group':group,'arm':arm,'evidence_sha256':digest,'expected':expected}
                    wire = wires[arm]
                    row['wire_bytes'] = len(json.dumps(wire,separators=(',', ':')).encode())
                    if arm in blocked:
                        row['error'] = 'endpoint unavailable earlier in run'
                    else:
                        started = time.monotonic()
                        try:
                            if arm=='score':
                                reply = advisor._transport(wire,endpoint='decisions')
                                action, scores, tokens = parse_scores(reply,menu)
                                row['candidate_scores'] = scores
                            elif arm in ('luna','raw'):
                                reply = advisor._transport(wire,endpoint='decisions')
                                action, confidence, tokens = parse_choice(reply, menu)
                                row['confidence'] = confidence
                            else:
                                reply = advisor._transport(wire)
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


def verify_rescue(config, output):
    output = Path(output)
    cases = cases_for(output,recovery=True)
    name,_,view,menu,expected = next(c for c in cases if c[0]=='real_double_timeout')
    menu = blind_menu(name,menu)
    eligible = emergency_clears(view,menu)
    if not eligible or not all(a in expected for a in eligible):
        raise ValueError('Archived board does not pass the independent defense check')
    advisor = OpenAIAdvisor({**config,'model':SOL},audit_path=output.with_name('decision.jsonl'))
    config['api_key'] = ''
    try:
        def simulated_deadline(_):
            time.sleep(16)
            raise AdvisorTimeout('injected Sol deadline in no-input replay')
        advisor.transport = simulated_deadline
        started = time.monotonic()
        try:
            action = advisor.select(view,menu)
        except AdvisorError:
            action = None
        elapsed = round(1000*(time.monotonic()-started))
        summary = {'mode':'no-input replay','simulated_sol_deadline_ms':16000,'action':action,'eligible':eligible,
                   'selected_verified_defense':action is not None and action in eligible,
                   'elapsed_ms':elapsed,'handoff_over_deadline_ms':elapsed-16000}
        with output.open('w') as report:
            correct = int(action in expected)
            for case,_,v,m,gold in cases:
                if case==name:
                    continue
                m = blind_menu(case,m)
                sol,_ = request_payload(SOL,v,m,service_tier='priority',effort='medium')
                result = prepare_backup(sol,v,m,advisor.decisions_transport)()
                passed = result.get('action') in gold
                correct += int(passed)
                report.write(json.dumps({'case':case,'correct':passed,**result})+'\n')
            summary.update(retest_correct=correct,retest_cases=len(cases))
            report.write(json.dumps({'summary':summary})+'\n')
    finally:
        advisor.close()


def replay_state(row):
    state = copy.deepcopy(row['observation'])
    # Retain every menu entry, including distinct engine options with identical
    # visible source/target. The replay must round-trip the archived action IDs.
    options = [{'id':a['source_id'],'type':'POWER','targets':[a['target_id']] if 'target_id' in a else []}
               for a in row['actions'].values() if 'source_id' in a]
    state.update(own_turn=True,options_id=row['server_options_id'],end_turn_allowed=True,legal_options=options)
    if action_menu(state)!=row['actions']:
        raise ValueError('Archived legal menu could not be reconstructed')
    return state


def replay_turn_plan(advisor, name, steps):
    advisor.confirmed = []
    advisor.pending_attack = None
    advisor.pace_turn = None
    advisor.consecutive_timeouts = 0
    transport = advisor.transport
    report = {'case':name,'arm':'turn_plan','matched_receipts':0,'checked_followups':0}
    def would_replan(_):
        raise AdvisorError('fresh decision required at this boundary')
    try:
        for i,row in enumerate(steps):
            state = replay_state(row)
            action = advisor.ask(state)
            if i==0:
                event = advisor.last_event
                report.update(action=action,model=event.get('model'),served_tier=event.get('served_tier'),
                              elapsed_ms=event.get('elapsed_ms'),sequence=event.get('conditional_sequence',[]))
                advisor.transport = would_replan
            elif advisor.last_event.get('local_rule')=='verified_turn_plan_v1':
                report['checked_followups'] += 1
            if action!=row['confirmed_action']:
                break  # No historical receipt to carry into the next position.
            report['matched_receipts'] += 1
            advisor.confirm(action,state)
            if not advisor.pending_attack:
                break
    except AdvisorError as exc:
        report['error'] = str(exc)[:160]
    finally:
        advisor.transport = transport
    return report


def verify_shared_lessons(config, output):
    """Use the selected primary and the Decisions adapter on the same public inputs."""
    output = Path(output)
    trade, tax = json.loads(read_bounded(output.parent/'standby-ranked-20261102.json',128*1024))
    draw = json.loads(read_bounded(output.parent/'shared-freeze-out-20261102.json',64*1024))
    plans = json.loads(read_bounded(output.parent/'ranked-turn-plan-20261102.json',128*1024))
    harder = copy.deepcopy(tax['observation'])
    for foe in harder['enemy_board']:
        if foe.get('taunt'):
            foe['attack'] += 1
            foe['health'] += 1
            if type(foe.get('max_health')) is int:
                foe['max_health'] += 1
            foe['modifiers'].append({'card_id':'TIME_026e'})
    five = dict(tax['observation'],mana=5)
    cases = [
        ('ranked_tax',tax['observation'],tax['actions'],[{'kind':'play','source_id':55,'target_id':i} for i in (41,49)]),
        ('stronger_taunts',harder,tax['actions'],[{'kind':'play','source_id':55,'target_id':49}]),
        ('extra_mana_control',five,tax['actions'],None),
        ('rush_trade_control',trade['observation'],trade['actions'],None),
        ('ranked_freeze_draw',draw['observation'],draw['actions'],draw['rubric'])]
    advisor = CaptureAdvisor(config)
    config['api_key'] = ''
    # Both arms are explicitly evaluated below. Avoid a third speculative call.
    advisor.standby.close()
    advisor.standby = None
    rows = []
    try:
        with output.open('w') as report:
            for i,(name,view,menu,expected) in enumerate(cases):
                view = copy.deepcopy(view)
                tap = life_tap_facts(view,menu,CATALOG)
                if tap:
                    view['life_tap'] = tap
                menu = blind_menu('shared-order/'+name,menu)
                for arm in (('primary','backup') if i%2==0 else ('backup','primary')):
                    row = {'case':name,'arm':arm,'control':expected is None}
                    try:
                        if arm=='primary':
                            advisor.pace_turn = None
                            advisor.pending_attack = None
                            advisor.last_event = {}
                            advisor.select(view,menu)
                            result = advisor.last_event
                        else:
                            sol,_ = request_payload(advisor.model,view,menu,service_tier=advisor.service_tier,effort=advisor.effort)
                            result = prepare_backup(sol,view,menu,advisor.decisions_transport)()
                        row.update({k:result[k] for k in ('action','model','tactical_lesson','elapsed_ms','served_tier',
                                                         'reasoning_effort','error') if k in result})
                        row['model_action'] = result.get('model_action',result.get('action'))
                    except AdvisorError as exc:
                        row['error'] = str(exc)[:160]
                    if expected:
                        row['model_hit'] = row.get('model_action') in expected
                        row['system_hit'] = row.get('action') in expected
                    rows.append(row)
                    report.write(json.dumps(row)+'\n'); report.flush()
            summary = {a:{'answered':sum(bool(r.get('action')) for r in rows if r['arm']==a),
                          'raw_hits':sum(bool(r.get('model_hit')) for r in rows if r['arm']==a),
                          'system_hits':sum(bool(r.get('system_hit')) for r in rows if r['arm']==a),
                          'controls_rewritten':sum(bool(r.get('tactical_lesson')) for r in rows if r['arm']==a and r['control'])}
                       for a in ('primary','backup')}
            for label in ('steps','coin_steps'):
                check = replay_turn_plan(advisor,label,plans[label])
                report.write(json.dumps(check)+'\n')
                summary[label] = {k:check[k] for k in ('matched_receipts','checked_followups')}
            report.write(json.dumps({'summary':summary})+'\n')
    finally:
        advisor.close()


def verify_lethal_shred(config, output):
    output=Path(output)
    case=json.loads(read_bounded(output.parent/'velocidrake-shreds-20261102.json',64*1024))
    view=case['observation']
    menu=blind_menu('shred-battlecry-v1',case['actions'])
    advisor=CaptureAdvisor(config)
    config['api_key']=''
    advisor.standby.close()
    advisor.standby=None
    try:
        advisor.select(view,menu)
        primary=advisor.last_event
        sol,_=request_payload(advisor.model,view,menu,service_tier=advisor.service_tier,effort=advisor.effort)
        backup=prepare_backup(sol,view,menu,advisor.decisions_transport)()
        blocked=primary['observation']['shred_battlecry']['withheld_sources']
        rows=[{'arm':arm,**{k:r[k] for k in ('model','elapsed_ms','action','served_tier','error') if k in r}}
              for arm,r in [('primary',primary),('backup',backup)]]
        summary={'known_shreds_at_least':primary['observation']['shred_battlecry']['known_shreds_at_least'],
                 'both_selected_nonfatal_alternative':all(r.get('action') in menu.values() and not (
                      r['action'].get('kind')=='play' and r['action'].get('source_id') in blocked) for r in rows)}
        output.write_text(''.join(json.dumps(r)+'\n' for r in [*rows,{'summary':summary}]))
    finally:
        advisor.close()


if __name__ == '__main__':
    config = json.loads(sys.stdin.readline(4097))
    if sys.argv[2:]==['--lethal-shred']:
        verify_lethal_shred(config,sys.argv[1])
    elif sys.argv[2:]==['--shared-lessons']:
        verify_shared_lessons(config,sys.argv[1])
    elif sys.argv[2:]==['--verify-rescue']:
        verify_rescue(config,sys.argv[1])
    else:
        evaluate(config,sys.argv[1],holdout=sys.argv[2:]==['--holdout'],recovery=sys.argv[2:]==['--recovery'])
