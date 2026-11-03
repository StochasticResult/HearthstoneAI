"""Bounded post-game evidence for coaching; never labels a stopped run a win.

No API calls, raw Power.log copies, account fields, or automatic policy edits.
Persistent history contains metrics; at most ten public-state review cases.
"""
import json
import os
import statistics
from pathlib import Path


def rows(path, limit):
    if not path.is_file() or path.is_symlink():
        return []
    if path.stat().st_size > limit:
        raise ValueError('Review input exceeds its bound')
    result = []
    for line in path.read_text(errors='replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def held_result(dest, outcomes):
    paths = [dest/'held-result.json',dest/'log-cursor.json']
    if any(not p.is_file() or p.is_symlink() or p.stat().st_size > 2048 for p in paths):
        return None
    try:
        held,cursor = (json.loads(p.read_bytes()) for p in paths)
        if (not isinstance(held,dict) or not isinstance(cursor,dict)
                or held.get('method') != 'verified_player_terminal' or held.get('run') != dest.name
                or held.get('outcome') not in ('WON','LOST','TIED') or any(r != held['outcome'] for r in outcomes)
                or any(type(held.get(k)) is not int or held[k] != cursor.get(k) for k in ('device','inode','start_offset'))
                or type(held.get('end_offset')) is not int or not held['start_offset'] <= cursor['offset'] <= held['end_offset']
                or type(held.get('owner')) is not int or held['owner'] not in (1,2)
                or cursor.get('verified_owner',held['owner']) != held['owner']):
            return None
        return held
    except (OSError,ValueError,TypeError,KeyError):
        return None


def summarize(dest):
    from collections import Counter
    from coaching import lesson_role
    decisions = rows(dest / 'decisions.jsonl', 2 * 1024 * 1024)
    actions = rows(dest / 'actions.log', 4 * 1024 * 1024) + rows(dest / 'actions-tail.log', 64 * 1024)
    events = rows(dest / 'session-events.jsonl', 1024 * 1024)
    proposed = [r for r in decisions if r.get('event') == 'proposed']
    confirmed = {r.get('decision_id') for r in decisions
                 if r.get('event') in ('confirmed', 'confirmed_choice', 'confirmed_target')}
    expired = {r.get('decision_id') for r in decisions if r.get('event') == 'expired_without_submission'}
    external = {r.get('decision_id') for r in decisions if r.get('event') == 'superseded_by_client_choice'}
    withdrawn = {r.get('decision_id') for r in decisions if r.get('event') == 'withdrawn_before_input'}
    resolved = confirmed | expired | external | withdrawn
    pending = [r for r in proposed if r.get('decision_id') not in resolved]
    outcomes = [r['game_result'] for r in actions if r.get('game_result') in ('WON', 'LOST', 'TIED')]
    outcome = outcomes[-1] if len(set(outcomes)) == 1 else 'INCOMPLETE'
    timings = [r['elapsed_ms'] for r in proposed if type(r.get('elapsed_ms')) is int]
    mode = next((r['observation'].get('game_type') for r in proposed if isinstance(r.get('observation'), dict)), None)
    metrics = {key: {'peak_sampled_rss_mib': 0, 'peak_sampled_cpu_pct': 0, 'peak_thermal_state': 0}
               for key in ('game', 'ai')}
    stops = []
    for event in events:
        m = event.get('metrics')
        if not isinstance(m, dict):
            continue
        kind = 'game' if event.get('source') == '游戏守护' else 'ai'
        for key, value in (('peak_sampled_rss_mib', m.get('rss_mib')),
                           ('peak_sampled_cpu_pct', m.get('cpu_pct')),
                           ('peak_sampled_rss_mib', m.get('peak_sampled_rss_mib')),
                           ('peak_sampled_cpu_pct', m.get('peak_sampled_cpu_pct')),
                           ('peak_thermal_state', m.get('thermal_state'))):
            if type(value) in (float, int):
                metrics[kind][key] = max(metrics[kind][key], value)
        result = m.get('result', {})
        if isinstance(result, dict):
            for key in ('peak_sampled_rss_mib', 'peak_sampled_cpu_pct'):
                value = result.get(key)
                if type(value) in (float, int):
                    metrics[kind][key] = max(metrics[kind][key], value)
            if result.get('reason'):
                stops.append(str(result['reason'])[:120])
        if m.get('watchdog_result'):
            stops.append(str(m['watchdog_result'])[:120])
    summary = {'run': dest.name, 'game_type': mode, 'outcome': outcome,
               'api_timeouts': sum(r.get('event') == 'api_timeout' for r in decisions),
               'decisions_proposed': sum(r.get('policy') == 'sol_timeout_backup_v1' for r in proposed),
               'decisions_shadow': sum(r.get('event') == 'decision_assist_shadow' for r in decisions),
               'decisions_declined': sum(r.get('event') == 'decision_assist_declined' for r in decisions),
               'lesson_corrections': dict(Counter(role for r in decisions if (role:=lesson_role(r)))),
               'turn_plans_chosen': sum(bool(r.get('conditional_sequence')) for r in proposed),
               'turn_plan_steps': sum(r.get('local_rule')=='verified_turn_plan_v1' for r in proposed),
               'turn_plans_discarded': sum(r.get('event')=='turn_plan_discarded' for r in decisions),
               'requested_mode': next((r['game_mode'] for r in events if r.get('game_mode') in ('practice', 'ranked')), None),
               'first_decision_game_turn': next((r['observation'].get('game_turn') for r in proposed if isinstance(r.get('observation'), dict)), None),
               'models': sorted({r['model'] for r in proposed if isinstance(r.get('model'), str)}),
               'api_responses': len(timings), 'api_tokens': sum(r.get('tokens', 0) for r in proposed),
               'median_decision_ms': statistics.median(timings) if timings else None,
               'confirmed_decisions': len(confirmed),
               'expired_decisions': len(expired), 'external_choices': len(external), 'withdrawn_decisions': len(withdrawn),
               'unconfirmed_decisions': [r.get('decision_id') for r in pending],
               'local_calculations': sum(bool(r.get('local_rule')) for r in proposed),
               'resources': metrics, 'guard_stop_reasons': stops,
               'game_closed': next((r['game_closed'] for r in reversed(events) if 'game_closed' in r), None),
               'needs_review': outcome in ('LOST', 'INCOMPLETE') or bool(pending)}
    action_path = dest / 'actions.log'
    if action_path.is_file() and not action_path.is_symlink():
        action_text = action_path.read_text(errors='replace')
        tail_path = dest / 'actions-tail.log'
        if tail_path.is_file() and not tail_path.is_symlink():
            if tail_path.stat().st_size > 64*1024:
                raise ValueError('Action tail exceeds its bound')
            action_text += '\n' + tail_path.read_text(errors='replace')
        summary['reconnect_recovery_used'] = any(word in action_text for word in
             ('reconnect options incomplete', 'restored explicitly observed', 'restored previous legal options'))
        summary['mulligan_confirmed'] = ('mulligan: confirmed;' in action_text
                                         or 'mulligan: kept starting hand' in action_text)
        summary['started_without_mulligan'] = bool(proposed) and not summary['mulligan_confirmed']
        errors = [line.partition('practice driver stopped: ')[2] for line in action_text.splitlines()
                  if line.startswith('practice driver stopped: ')]
        if errors:
            summary['driver_stop_detail'] = errors[-1][:240]
    compact_path = dest / 'decision-summary.json'
    if compact_path.is_file() and not compact_path.is_symlink():
        if compact_path.stat().st_size > 8192:
            raise ValueError('Decision summary exceeds its bound')
        compact = json.loads(compact_path.read_text())
        summary['expired_decisions'] = compact.get('expired_decisions', len(expired))
        summary['external_choices'] = compact.get('external_choices', len(external))
        summary['withdrawn_decisions'] = compact.get('withdrawn_decisions', len(withdrawn))
        summary['api_timeouts'] = compact.get('api_timeouts', summary['api_timeouts'])
        for key in ('decisions_proposed','decisions_shadow','decisions_declined',
                    'turn_plans_chosen','turn_plan_steps','turn_plans_discarded'):
            summary[key] = compact.get(key,summary[key])
        summary['lesson_corrections'] = compact.get('lesson_corrections',summary['lesson_corrections'])
        for key in ('api_responses', 'api_tokens', 'confirmed_decisions', 'unconfirmed_decisions',
                    'local_calculations', 'models', 'game_type', 'first_decision_game_turn',
                    'mulligan_confirmed', 'median_decision_ms', 'latency_sample_size', 'diagnostics_capped'):
            summary[key] = compact[key]
        if compact.get('outcome') in ('WON', 'LOST', 'TIED'):
            summary['outcome'] = compact['outcome']
        summary['started_without_mulligan'] = bool(compact['confirmed_decisions'] or compact['unconfirmed_decisions']) and not compact['mulligan_confirmed']
        summary['needs_review'] = summary['outcome'] in ('LOST','INCOMPLETE') or bool(compact['unconfirmed_decisions'])
        # A capped prefix may end between a proposal and its confirmation.
        # The runtime counters, not a missing diagnostic line, are authority.
        pending = [r for r in proposed if r.get('decision_id') in compact['unconfirmed_decisions']]
    # Proposed actions may have failed to execute; this is a review queue, not
    # an assertion that every selected decision was bad chess/card play.
    if summary.get('game_type') == 'GT_RANKED':
        held = held_result(dest,outcomes)
        if held and summary['outcome'] in ('INCOMPLETE',held['outcome']):
            summary.update(outcome=held['outcome'],result_after_ai_fault=True,needs_review=True)
            # A replay of an older run can resolve a pending recommendation
            # without altering the original worker's audit or its compact counters.
            expiry = held.get('replayed_choice') or {}
            for row in pending:
                obs = row.get('observation',{})
                if (expiry.get('event') == 'expired_without_submission'
                        and row.get('decision_id') == expiry.get('decision_id')
                        and row.get('action') == {'index':expiry.get('proposed_index')}
                        and obs.get('source_card') == expiry.get('source_card')
                        and obs.get('game_turn') == expiry.get('game_turn')
                        and [c.get('card_id') for c in obs.get('choices',[])] == expiry.get('offered_cards')
                        and row['decision_id'] in summary['unconfirmed_decisions']):
                    summary['unconfirmed_decisions'].remove(row['decision_id'])
                    summary['expired_decisions'] += 1
                    pending = [r for r in pending if r is not row]
                    break
    outcome = summary['outcome']
    if summary.get('game_type') == 'GT_RANKED' and outcome in ('WON', 'LOST', 'TIED'):
        if pending and not summary.get('diagnostics_capped'):
            from turn_end_attack import reconcile
            proofs = reconcile(dest, outcome, pending, decisions)
            if proofs:
                resolved_at_end = {proof['decision_id'] for proof in proofs}
                summary['turn_end_attacks'] = proofs
                summary['unconfirmed_decisions'] = [i for i in summary['unconfirmed_decisions'] if i not in resolved_at_end]
                pending = [r for r in pending if r.get('decision_id') not in resolved_at_end]
                summary['needs_review'] = (outcome == 'LOST' or bool(summary['unconfirmed_decisions'])
                                           or bool(summary.get('result_after_ai_fault')))
        from coaching import strategy_sample
        summary['strategy_sample'] = strategy_sample(decisions, summary.get('diagnostics_capped', False))
        if summary.get('turn_end_attacks'):
            summary['strategy_sample']['turn_end_attacks'] = summary['turn_end_attacks']
    samples = pending[-1:]
    if not samples and outcome == 'LOST' and not summary.get('diagnostics_capped'):
        samples = [r for r in proposed if r.get('decision_id') in confirmed][-1:]
    review_cases = [{'run': dest.name, 'outcome': outcome, 'status': 'awaiting_review',
                    'reason_for_review': 'unconfirmed_input' if pending else 'lost_game_final_decision',
                    'decision': {k: r[k] for k in ('decision_id', 'observation', 'actions', 'action', 'reason', 'model') if k in r}}
                   for r in samples]
    return summary, review_cases


def save(path, obj):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(obj, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temporary.replace(path)


def write_review(dest, learning, *, game_closed=None, game_preserved=False, match_workers_closed=None, game_pid=None):
    summary, cases = summarize(dest)
    strategy_sample = summary.pop('strategy_sample', None)
    from build_identity import read_build
    build=read_build(dest/'run-version.json')
    summary['runtime_version']=build['version_id']
    summary['version_identity_verified']=build['identity_verified']
    from coach_policy import read_json
    pinned = read_json(dest/'run-policy.json', 32768, {})
    if pinned.get('revision'):
        summary['coach_revision'] = pinned['revision']
    if game_closed is not None:
        summary['game_closed'] = game_closed
    summary.update(game_preserved=game_preserved,match_workers_closed=match_workers_closed,game_pid=game_pid)
    learning.mkdir(parents=True, exist_ok=True, mode=0o700)
    history_path, queue_path = learning / 'match-history.json', learning / 'review-queue.json'
    def previous(path, limit):
        if not path.exists():
            return []
        if path.is_symlink() or path.stat().st_size > limit:
            raise ValueError('Review history exceeds its bound')
        value = json.loads(path.read_text())
        if not isinstance(value, list):
            raise ValueError('Invalid review history')
        return [row for row in value if isinstance(row, dict) and row.get('run') != dest.name]
    history = (previous(history_path, 256 * 1024) + [summary])[-50:]
    losses = 0
    for row in reversed(history):
        if row.get('game_type') != summary['game_type']:
            break
        if row['outcome'] == 'INCOMPLETE':
            continue
        if row['outcome'] != 'LOST':
            break
        losses += 1
    summary['consecutive_losses'] = losses
    summary['review_before_next_game'] = summary['needs_review'] or losses >= 3
    from version_stats import record
    summary['version_stats']=record(learning,summary,history,strategy_sample=strategy_sample)
    save(dest / 'review.json', summary)
    save(history_path, history)
    save(queue_path, (previous(queue_path, 1024 * 1024) + cases)[-10:])
    # Optional coach storage cannot change the result or the ranked lifecycle.
    try:
        from coach_evidence import archive
        archive(dest, summary, learning)
    except (OSError, ValueError, KeyError, TypeError):
        pass  # The trainer can still read a complete retained run directly.
    return summary
