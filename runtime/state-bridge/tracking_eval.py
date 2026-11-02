"""Four production-path checks for secrets, known hand, and tracked deck risk."""
import json
import sys
import time
from pathlib import Path
from model_comparison import CaptureAdvisor, save
from strategy import PACK
from strategy_eval import base, card, option


def cases():
    rows=[]
    s=base();s.update(enemy_secret_count=1,enemy_secret_ids=[90]);s['enemy_hero'].update(card_id='HERO_08',health=4)
    s['my_hand']=[card('GAME_005',100),card('BE_086',101)]
    s['legal_options']=[option(100),option(101)]
    rows.append({'name':'probe_unknown_secret_before_key_spell','state':s,
                 'expected':[{'kind':'play','source_id':100}],
                 'criterion':'Zero-cost Coin tests spell interaction first, retaining sufficient mana for lethal Blasphemy. Do not claim unknown secret identity.'})
    s=base();s.update(mana=0,enemy_secret_count=1,enemy_secret_ids=[90]);s['enemy_hero'].update(card_id='HERO_05',health=8)
    s['enemy_known_secrets']=[{'id':90,'card_id':'CORE_EX1_611','basis':'explicit_hand_reveal'}]
    s['my_board']=[card('CORE_UNG_205',10,attack=1,health=5),card('TIME_025',11,attack=8,health=8)]
    s['legal_options']=[option(10,2),option(11,2)]
    rows.append({'name':'publicly_known_freezing_trap','state':s,
                 'expected':[{'kind':'attack','source_id':10,'target_id':2}],
                 'criterion':'The cheap small attacker absorbs the publicly known trap; preserve the eight-attack lethal body.'})
    s=base();s.update(mana=1,enemy_mana_crystals=5);s['enemy_hero']['health']=23
    s['enemy_known_hand']=[{'id':91,'card_id':'CORE_EX1_197','basis':'explicit_hand_reveal'}]
    s['my_board']=[card('TIME_025',10),card('TIME_025',11)];s['my_hand']=[card('TIME_026',100)]
    s['legal_options']=[option(100),option(10,2),option(11,2)]
    rows.append({'name':'respect_publicly_known_ruin','state':s,
                 'expected':[{'kind':'attack','source_id':i,'target_id':2} for i in (10,11)],
                 'criterion':'Keep four-attack bodies below the known Ruin threshold; the buff does not shorten the three-turn damage clock.'})
    s=base();s.update(mana=2,my_deck_count=1,deck_tracking_from_mulligan=True,own_card_identity_changed=False)
    s['starting_deck']=PACK['deck']['cards'];s['my_hero']['health']=4
    s['own_seen_original']=[{'id':200+i,'card_id':cid} for i,cid in enumerate(cid for cid,n in PACK['deck']['cards'].items() for _ in range(n))]
    s['own_known_deck']=[{'id':300,'card_id':'TIME_025t','generated':True}]
    s['my_hand']=[card('CORE_UNG_205',100)];s['enemy_board']=[card('CS2_172',20,attack=8,health=8)]
    s['legal_options']=[option(100,2,20),option(3)]
    rows.append({'name':'avoid_known_shred_draw_death','state':s,
                 'expected':[{'kind':'play','source_id':100,'target_id':20}],
                 'criterion':'The reconciled deck contains one Shred. Life Tap leaves two health then draws three self-damage; freeze the visible attacker instead.'})
    for case in rows:
        case['state']['game_type']='GT_RANKED'
        for zone in ('my_hand','my_board','enemy_board'):
            for i,c in enumerate(case['state'][zone]):c['position']=i+1
    return rows


def evaluate(config, output):
    output=Path(output);output.parent.mkdir(exist_ok=True,parents=True)
    suite=cases();save(output.with_name('cases.json'),suite)
    advisor=CaptureAdvisor(config);rows=[]
    try:
        with output.open('w') as stream:
            for case in suite:
                before=advisor.tokens;started=time.monotonic();action=advisor.ask(case['state'])
                row={'case':case['name'],'action':action,'reason':advisor.last_event.get('reason'),
                     'passed':action in case['expected'],'model':advisor.model,'tokens':advisor.tokens-before,
                     'elapsed_ms':round((time.monotonic()-started)*1000),
                     'observation':advisor.last_event.get('observation')}
                stream.write(json.dumps(row,ensure_ascii=False)+'\n');stream.flush();rows.append(row)
                print(json.dumps({'tracking_check':case['name'],'passed':row['passed']}),flush=True)
            summary={'passed':sum(r['passed'] for r in rows),'total':len(rows),'api_calls':advisor.calls,'api_tokens':advisor.tokens,
                     'model':advisor.model,'limitation':'Four synthetic checks, not a win-rate estimate or full secret-engine validation'}
            stream.write(json.dumps({'summary':summary})+'\n')
    finally:advisor.close()


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Output path required; configuration via stdin')
    evaluate(json.loads(sys.stdin.readline(4097)),sys.argv[1])
