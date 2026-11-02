"""Paired latency and reasoning evaluation using the production decision contract."""
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

from hard_cases import hard_cases, prepare
from openai_advisor import AdvisorError, EFFORTS, OpenAIAdvisor, parse_response, request_payload

MODEL = 'gpt-6.1-sol'
CASES = ('buff_before_sacrificing_own_tax', 'lifesteal_overkill_survival_target',
         'take_a_calculated_not_guaranteed_out', 'coin_tax_spellpower_and_highest_cost')


def trial(case, view, menu, tier, effort, transport):
    payload, _ = request_payload(MODEL, view, menu, service_tier=tier, effort=effort)
    fixed = {k:v for k,v in payload.items() if k not in ('service_tier','reasoning')}
    digest = hashlib.sha256(json.dumps(fixed,sort_keys=True).encode()).hexdigest()
    row = {'case':case['name'], 'requested_tier':tier, 'effort':effort, 'input_digest':digest}
    started = time.monotonic()
    try:
        reply = transport(payload)
        model = reply.get('model')
        if not isinstance(model,str) or (model != MODEL and not model.startswith(MODEL+'-')):
            raise AdvisorError('Returned model does not match GPT-6.1 Sol')
        row.update(model=model, served_tier=reply.get('service_tier'),
                   echoed_effort=(reply.get('reasoning') or {}).get('effort'))
        usage = reply.get('usage') or {}
        row['tokens'] = {k:usage.get(k) for k in ('input_tokens','output_tokens','total_tokens')}
        row['tokens']['cached_input'] = (usage.get('input_tokens_details') or {}).get('cached_tokens')
        row['tokens']['reasoning'] = (usage.get('output_tokens_details') or {}).get('reasoning_tokens')
        if (reply.get('incomplete_details') or {}).get('reason') == 'max_output_tokens':
            raise AdvisorError('output token limit')
        action, reason, _ = parse_response(reply,menu)
        row.update(action=action, reason=reason, passed=action in case['expected'])
    except AdvisorError as error:
        row['error'] = str(error)[:180]
    row['elapsed_ms'] = round((time.monotonic()-started)*1000)
    return row


def tier_matches(row):
    requested = row['requested_tier']
    served = row.get('served_tier')
    return served in ('fast','priority') if requested=='priority' else served==requested


def summarize(rows, effort_tier):
    profiles = []
    for tier, effort in dict.fromkeys((r['requested_tier'],r['effort']) for r in rows):
        attempted = [r for r in rows if (r['requested_tier'],r['effort'])==(tier,effort)]
        complete = [r for r in attempted if 'action' in r]
        profiles.append({'tier':tier, 'effort':effort, 'attempts':len(attempted),
                         'completed':len(complete), 'correct':sum(r['passed'] for r in complete),
                         'timeout':sum('timed out' in r.get('error','') for r in attempted),
                         'output_limited':sum(r.get('error')=='output token limit' for r in attempted),
                         'tier_verified':sum(tier_matches(r) for r in complete),
                         'median_ms':statistics.median(r['elapsed_ms'] for r in complete) if complete else None,
                         'reasoning_tokens':sum((r.get('tokens') or {}).get('reasoning') or 0 for r in attempted)})
    pairs = []
    for name in CASES:
        results = {r['requested_tier']:r for r in rows if r['case']==name and r['effort']=='low'
                   and tier_matches(r) and 'action' in r}
        if set(results)=={'default','priority'}:
            pairs.append([results[t]['elapsed_ms'] for t in ('default','priority')])
    return {'model':MODEL, 'attempts':len(rows), 'effort_comparison_tier':effort_tier,
            'profiles':profiles,'verified_low_pairs':len(pairs),
            'fast_quicker_pairs':sum(fast<standard for standard,fast in pairs),
            'median_paired_ms_saved':statistics.median(standard-fast for standard,fast in pairs) if pairs else None}


def evaluate(config, output, transport=None, *, fast_efforts_only=False):
    cases = {c['name']:c for c in hard_cases()}
    prepared = [(cases[name], *prepare(cases[name])) for name in CASES]
    advisor = OpenAIAdvisor({**config, 'model':MODEL})
    config['api_key'] = ''
    rows = []
    blocked_fast = False
    auth_blocked = False
    effort_tier = None
    try:
        with Path(output).open('w') as stream:
            def run(case_args, tier, effort):
                nonlocal blocked_fast, auth_blocked
                row = trial(*case_args,tier,effort,transport or advisor._transport)
                rows.append(row)
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                stream.flush()
                error = row.get('error','')
                denied = any(f'HTTP {n}' in error for n in (400,403,404))
                blocked_fast |= tier=='priority' and denied
                auth_blocked |= ('HTTP 401' in error or 'HTTP 429' in error or (tier=='default' and denied))
                print(json.dumps({'profile_trial':len(rows),'tier':tier,'effort':effort,
                                  'elapsed_ms':row['elapsed_ms'],'error':error or None}),flush=True)

            if not fast_efforts_only:
                for index, case_args in enumerate(prepared):
                    order = ('default','priority') if index%2==0 else ('priority','default')
                    for tier in order:
                        if not auth_blocked and not (tier=='priority' and blocked_fast):
                            run(case_args,tier,'low')
            if not auth_blocked:
                effort_tier = 'priority' if fast_efforts_only else 'default'
                efforts = EFFORTS[1:]
                for index, case_args in enumerate(prepared):
                    for effort in efforts[index:]+efforts[:index]:
                        if not auth_blocked and not (effort_tier=='priority' and blocked_fast):
                            run(case_args,effort_tier,effort)
            for name in CASES:
                if len({r['input_digest'] for r in rows if r['case']==name})>1:
                    raise ValueError('Paired prompts differ')
            summary = summarize(rows,effort_tier)
            stream.write(json.dumps({'summary':summary},ensure_ascii=False)+'\n')
            return summary
    finally:
        advisor.close()


if __name__=='__main__':
    if len(sys.argv) not in (2,3) or (len(sys.argv)==3 and sys.argv[2]!='--fast-efforts'):
        raise SystemExit('Expected a local result path and optional --fast-efforts')
    evaluate(json.loads(sys.stdin.readline(4097)),sys.argv[1],fast_efforts_only=len(sys.argv)==3)
