"""Two-model, two-order advanced test; no game input, at most 48 requests."""
import contextlib
import copy
import hashlib
import io
import json
import os
import random
import sys
import time
from pathlib import Path

from hard_cases import SOURCES, hard_cases, prepare
from model_comparison import CaptureAdvisor, save
from openai_advisor import AdvisorError, INSTRUCTIONS, request_payload

MODELS=('gpt-6.1-sol','gpt-6.1-terra')
TEST_INSTRUCTIONS=INSTRUCTIONS+"\nFor this difficult synthetic test, make the short reason include the intended continuation and the decisive arithmetic or opponent response. Distinguish known information from plausible hidden cards and random outcomes. Do not give a long reasoning transcript.\n"
RUBRIC={
 'construction':'Twelve constructed current-card stress positions, NOT claimed as real-game replays. Extra public-history facts are explicitly supplied to both models.',
 'tactical':'Each of five arithmetic positions: 0-2. 2 = winning/surviving first action with a correct decisive continuation/arithmetic; 1 = correct first action but incomplete or materially mistaken explanation; 0 = loses the available forced line. A mismatched action and reason is graded by the actual action.',
 'uncertainty':'Random-out case: 0-2. 2 = Wrath and explicitly uncertain favorable chance; 1 = correct choice with no uncertainty calibration; 0 = wrong choice or false guarantee. Exact probability not required.',
 'judgment':'Each of six strategic positions: 0-4. 2 for a defensible action/continuation, 1 for the decisive public-information constraint, 1 for sound counterplay/uncertainty. Do not grade one subjective preference as uniquely optimal.',
 'repeats':'Each model gets each position twice. Second pass reverses menu enumeration and uses different opaque action IDs; semantic actions and all observations remain identical. Models get identical bytes except their model ID within each paired trial.',
 'selection':'Prioritize fewer critical tactical failures and consistent execution. Then compare strategic/calibration grades. Small-sample uncertainty must be reported. No win-rate estimate.',
 'bounds':'48 requests total, production low reasoning, 1536 output tokens, 18-second request deadline, no retries; fresh 12-call budget per model per pass. Whole run guard 300 seconds and 200 MiB.',
}


def trial_input(case, repetition):
    view,menu=prepare(case)
    if repetition:
        menu={'z%d'%i:action for i,action in enumerate(reversed(list(menu.values())))}
    return view,menu


def evaluate(config,output,cases_factory=hard_cases,rubric=RUBRIC):
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    models=list(MODELS);random.SystemRandom().shuffle(models);mapping=dict(zip(('P','Q'),models))
    save(output.with_name('model-key.json'),mapping)
    suite=cases_factory(); prepared=[]; manifest=[]
    for repetition in range(2):
        for case in suite:
            view,menu=trial_input(case,repetition); hashes=set()
            for model in MODELS:
                payload,_=request_payload(model,view,menu,TEST_INSTRUCTIONS);payload.pop('model')
                hashes.add(hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest())
            if len(hashes)!=1: raise ValueError('Unequal paired prompts')
            digest=next(iter(hashes)); prepared.append((repetition,case,view,menu,digest))
            manifest.append({'case':case['name'],'repetition':repetition,'category':case['category'],
                             'criterion':case['criterion'],'expected':case['expected'],'witness':case.get('witness'),
                             'view':view,'menu':menu,'payload_sha256':digest})
    save(output.with_name('cases.json'),{'rubric':rubric,'instructions':TEST_INSTRUCTIONS,'sources':SOURCES,'cases':manifest})
    advisors={(label,rep):CaptureAdvisor({**config,'model':model}) for label,model in mapping.items() for rep in range(2)}
    config['api_key']='';rows=[];metrics=[]
    try:
        with output.open('w') as report:
            os.chmod(output,0o600)
            for trial,(rep,case,view,menu,digest) in enumerate(prepared):
                labels=['P','Q'] if trial%2==0 else ['Q','P']
                for label in labels:
                    advisor=advisors[(label,rep)];advisor.instructions=TEST_INSTRUCTIONS;advisor.last_event={}
                    started=time.monotonic();before=advisor.tokens
                    try:
                        with contextlib.redirect_stdout(io.StringIO()): action=advisor.select(copy.deepcopy(view),copy.deepcopy(menu))
                        row={'case':case['name'],'candidate':label,'repetition':rep,'category':case['category'],
                             'action':action,'reason':advisor.last_event.get('reason',''),
                             'expected_action':action in case['expected'],'payload_sha256':digest}
                    except Exception as error:
                        row={'case':case['name'],'candidate':label,'repetition':rep,'category':case['category'],
                             'error':type(error).__name__,'expected_action':False,'payload_sha256':digest}
                        # AdvisorError contains our fixed diagnostics, never raw
                        # provider bodies. Do not serialize arbitrary exceptions.
                        if isinstance(error, AdvisorError):
                            row['error_detail']=str(error)[:240]
                    rows.append(row);report.write(json.dumps(row,ensure_ascii=False)+'\n');report.flush()
                    metrics.append({'case':case['name'],'candidate':label,'repetition':rep,
                                    'elapsed_ms':round((time.monotonic()-started)*1000),'tokens':advisor.tokens-before})
                    print(json.dumps({'hard_comparison_progress':len(rows),'total':len(prepared)*2,'case':case['name'],'candidate':label,'repetition':rep}),flush=True)
            summary={label:{'trials':sum(r['candidate']==label for r in rows),'errors':sum(r['candidate']==label and 'error' in r for r in rows)} for label in mapping}
            summary.update(api_calls=sum(a.calls for a in advisors.values()),api_tokens=sum(a.tokens for a in advisors.values()),blind=True)
            report.write(json.dumps({'summary':summary})+'\n');save(output.with_name('metrics.json'),metrics)
    finally:
        for advisor in advisors.values():advisor.close()


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: hard_comparison.py OUTPUT_JSONL; config over stdin')
    evaluate(json.loads(sys.stdin.readline(4097)),sys.argv[1])
