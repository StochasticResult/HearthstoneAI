"""Optional post-match coach. Its only output is local evidence and advisory strategy."""
import argparse
import fcntl
import http.client
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from coach_evidence import recent
from coach_policy import PHASES, ZONES, atomic, card_present, card_rules, read_json, revision, settings

DATA = Path.home()/'Library/Application Support/HearthstoneAI'
CONFIRMED = {'confirmed', 'confirmed_choice', 'confirmed_target'}
INSTRUCTIONS = """Review every public decision of these completed Standard Zoo Warlock matches, including wins,
mulligans, order, missed lethal, survival trades and time pressure. Read each game's events in chronological order.
The first change is the board; apply each next change to the preceding board. An empty array or null clears a field.
Entity IDs and option labels are local to a position. Card definitions may include cards revealed later;
use only what was public at that decision. Match a proposal's decision_id with its later receipt.
An expired/withdrawn proposal or a shadow Luna suggestion is NOT a played move. terminal_receipts record attacks
submitted as the turn ended without observed combat. Infer no hidden hand or RNG.
Return a concise Chinese review for each supplied run, giving key turn/decision numbers, alternative lines and
uncertainty. For transferable gameplay advice, give only high-confidence, conditional, falsifiable lessons.
Cite a confirmed decision with a materially better option explicitly offered in its actions dictionary.
Use a second, distinct confirmed decision with this same visible card as a control; explain when the advice would not apply.
Scope to one visible card ID and its zone in the cited decision. Advice must be usable by both Sol and Luna and
future models. Local legal actions and deterministic safety checks always take precedence. Do not encode historical
entity/option IDs in the advice. No executable code or instructions about files, UI or APIs. If no such finding
exists, use an empty findings array. A loss or model disagreement alone is not evidence of a mistake."""

def obj(props):
    return {'type':'object','properties':props,'required':list(props),'additionalProperties':False}

STR = {'type':'string'}
INT = {'type':'integer'}
REF = obj({'run':STR, 'decision':INT})
FINDING = obj({'evidence':REF, 'alternative':STR, 'control':REF,
               'phase':{'type':'string','enum':list(PHASES)}, 'zone':{'type':'string','enum':list(ZONES)},
               'card':STR, 'advice':STR, 'exception':STR})
SCHEMA = obj({'reviews':{'type':'array','items':obj({'run':STR,'review':STR})},
              'findings':{'type':'array','items':FINDING}})


def response_payload(prefs, games, lessons):
    return {'model':prefs['coach_model'], 'reasoning':{'effort':prefs['coach_effort']},
            'store':False, 'max_output_tokens':16384, 'instructions':INSTRUCTIONS,
            'text':{'format':{'type':'json_schema','name':'match_review','strict':True,'schema':SCHEMA}},
            'input':json.dumps({'prior_lessons':lessons,'games':games}, ensure_ascii=False,separators=(',',':'))}


class TrainingError(Exception):
    pass


def request(key, payload):
    connection = http.client.HTTPSConnection('api.openai.com', timeout=240)
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(',',':')).encode()
        connection.request('POST','/v1/responses',body,{'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        response = connection.getresponse()
        raw = response.read(512*1024 + 1)
        if response.status != 200:
            try:
                code = json.loads(raw)['error']['code']
            except (ValueError,TypeError,KeyError):
                code = None
            label = code if isinstance(code,str) and re.fullmatch(r'[\w.-]{1,80}',code) else 'request_failed'
            raise TrainingError(f'API HTTP {response.status} ({label})')
        if len(raw)>512*1024:
            raise TrainingError('API 回包超过上限')
        reply = json.loads(raw)
        model = reply.get('model','')
        if reply.get('status')!='completed' or not (model == payload['model'] or model.startswith(payload['model']+'-')):
            raise TrainingError('模型不匹配或评审未完整返回')
        content = [c['text'] for m in reply.get('output',[]) if m.get('type')=='message'
                   for c in m.get('content',[]) if c.get('type')=='output_text']
        if len(content)!=1:
            raise TrainingError('未收到完整结构化评审')
        return json.loads(content[0]), model
    finally:
        connection.close()


def positions(games):
    index = {}
    for game in games:
        view = {}
        receipts = {e['decision_id'] for e in game['events'] if e['event'] in CONFIRMED}
        for e in game['events']:
            if e['event']=='proposed':
                view.update(e['change'])
                index[game['run'],e['decision_id']] = (dict(view),e,e['decision_id'] in receipts)
    return index


def point(ref):
    if not isinstance(ref,dict) or not isinstance(ref.get('run'),str) or type(ref.get('decision')) is not int:
        return None
    return ref['run'],ref['decision']


def clean_text(value, limit):
    return isinstance(value,str) and 0<len(value.strip())<=limit and not any(ord(c)<32 and c not in '\n\t' for c in value)


def validated(result, games):
    reviews = result.get('reviews')
    if not isinstance(reviews,list) or len(reviews)!=len(games) or any(
            not isinstance(r,dict) or not clean_text(r.get('review'),2400) for r in reviews
    ) or {r.get('run') for r in reviews} != {g['run'] for g in games}:
        raise TrainingError('评审没有覆盖所选的每一局')
    findings = result.get('findings')
    if not isinstance(findings,list):
        raise TrainingError('评审格式无效')
    index = positions(games)
    accepted = []
    for f in findings[:12]:
        if not isinstance(f,dict):
            continue
        evidence, control = point(f.get('evidence')),point(f.get('control'))
        if evidence not in index or control not in index or evidence == control:
            continue
        board, event, confirmed = index[evidence]
        menu = event['actions']
        alt = f.get('alternative')
        if (not confirmed or not index[control][2] or not isinstance(alt,str) or alt not in menu
                or menu[alt] == event['action'] or event.get('local_rule') or f.get('phase')!=board.get('phase')
                or f['phase'] not in PHASES or f.get('zone') not in ZONES or not clean_text(f.get('card'),80)
                or not card_present(board,f['zone'],f['card']) or not card_present(index[control][0],f['zone'],f['card'])
                or not clean_text(f.get('advice'),300)
                or not clean_text(f.get('exception'),300)):
            continue
        unsafe = (board.get('shred_battlecry') or {}).get('withheld_sources',[])
        if menu[alt].get('source_id') in unsafe:
            continue
        lesson = {k:f[k] for k in ('phase','zone','card','advice','exception','evidence','control','alternative')}
        game = next(g for g in games if g['run']==evidence[0])
        lesson['rules'] = card_rules(game['cards'].get(f['card'],{}))
        accepted.append(lesson)
    return reviews, accepted, len(findings)-len(accepted)


def train(learning, prefs, state, games, skipped, key, transport=request):
    # Segment only at whole-game boundaries. No sampled decisions or truncated turns.
    groups, size = [[]],0
    for g in games:
        n = len(json.dumps(g, ensure_ascii=False, separators=(',',':')).encode())
        if groups[-1] and size+n>850_000:
            groups.append([])
            size=0
        groups[-1].append(g)
        size+=n
    lessons = list(state.get('lessons',[]))
    reports, additions, rejected, models = [],[],0,[]
    for group in groups:
        result, model = transport(key, response_payload(prefs,group,lessons))
        reviews, valid, refused = validated(result, group)
        reports.extend(reviews)
        additions.extend(valid)
        rejected += refused
        models.append(model)
    for lesson in additions:
        lessons = [r for r in lessons if (r['phase'],r['zone'],r['card']) != (lesson['phase'],lesson['zone'],lesson['card'])]
        lessons.append(lesson)
    lessons = lessons[-12:]
    report = {'at':time.time(), 'model':models[0], 'models':models, 'effort':prefs['coach_effort'],
              'games':len(games),'actions':sum(g['actions'] for g in games), 'reviews':reports,
              'added':len(additions),'rejected':rejected,'skipped':skipped,'revision':revision(lessons),
              'findings':additions, 'calls':len(groups)}
    updated = {'last_run':games[-1]['run'],'lessons':lessons,'revision':report['revision'],
               'reports':(state.get('reports',[])+[report])[-8:]}
    if len(json.dumps(updated,ensure_ascii=False).encode())>380*1024:
        updated['reports'] = updated['reports'][-2:]
    atomic(learning/'coach-state.json',updated)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--trigger',choices=('manual','scheduled'),required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.nice(10)
    learning = DATA/'learning'
    learning.mkdir(mode=0o700,parents=True,exist_ok=True)
    def message(s):
        print(s,flush=True)
    with (learning/'coach.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            message('已有训练正在进行');return
        prefs = settings(learning)
        now = time.time()
        clock = read_json(learning/'coach-clock.json',4096,{})
        if args.trigger=='scheduled' and (not prefs['scheduled'] or now < clock.get('last_attempt',0)+prefs['interval_minutes']*60):
            return
        def status(text):
            atomic(learning/'coach-clock.json',{'last_attempt':now,'status':text})
            message(text)
        try:
            state = read_json(learning/'coach-state.json',384*1024,{})
            games, skipped = recent(DATA,state.get('last_run',''),prefs['games'])
            if not games:
                status('没有新的完整标准天梯记录可训练');return
            from maintenance import MIB, readiness, swap_used
            ranked = read_json(learning/'autoplay-state.json',16384,{})
            baseline = ranked.get('swap_baseline_mib')
            health = readiness(baseline*MIB if type(baseline) in (int,float) else swap_used())
            if not health['ready']:
                status('训练推迟 · '+', '.join(health['reasons']));return
            status(f"正在复盘 {len(games)} 局，共 {sum(g['actions'] for g in games)} 个决策")
            message('NEED_KEY')
            key = sys.stdin.readline(515).strip()
            if not 20<=len(key)<=512 or any(c.isspace() for c in key):
                raise TrainingError('请在应用内允许读取钥匙串后重试')
            def deadline(signum, frame):
                raise TrainingError('本次离线评审超时，可稍后重试')
            signal.signal(signal.SIGALRM,deadline)
            signal.alarm(300*len(games))
            report = train(learning,prefs,state,games,skipped,key)
            status(f"已复盘 {report['games']} 局、{report['actions']} 步，采纳 {report['added']} 条经验")
        except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, TrainingError) as exc:
            status('训练失败 · '+(str(exc) if isinstance(exc,TrainingError) else type(exc).__name__))
            return 1
        finally:
            signal.alarm(0)
    return 0


if __name__=='__main__':
    sys.exit(main())
