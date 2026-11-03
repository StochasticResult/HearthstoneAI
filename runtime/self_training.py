"""Optional post-match coach. Its only output is local evidence and advisory strategy."""
import argparse
import fcntl
import http.client
import json
import os
import signal
import sys
import time
from pathlib import Path

from coach_evidence import recent, sources
from coach_delivery import TrainingError, begin, fetch, finish, ledger, restore
from coach_policy import PHASES, ZONES, app_reviews_enabled, atomic, card_present, card_rules, matchup_applies, read_json, revision, settings

DATA = Path.home()/'Library/Application Support/HearthstoneAI'
CONFIRMED = {'confirmed', 'confirmed_choice', 'confirmed_target'}
INSTRUCTIONS = """Review every public decision of these completed Standard Zoo Warlock matches, including wins,
mulligans, order, missed lethal, survival trades and time pressure. Read each game's events in chronological order.
The first change is the board; apply each next change to the preceding board. An empty array or null clears a field.
Entity IDs and option labels are local to a position. Card definitions may include cards revealed later;
use only what was public at that decision. Match a proposal's decision_id with its later receipt.
An expired/withdrawn proposal or a shadow Luna suggestion is NOT a played move. terminal_receipts record attacks
submitted before a turn handoff or an opponent's concession without observed combat. Infer no hidden hand or RNG.
Matchup drills compare one move against conditional replies. Their grades are not full games or win rates.
Set opponent_class and opponent_archetype only for a supported matchup-specific lesson. Otherwise use null.
Give each run a brief Chinese review (at most 100 characters), naming key turns, alternative lines and
uncertainty. For transferable gameplay advice, give only high-confidence, conditional, falsifiable lessons.
Cite a confirmed decision with a materially better option explicitly offered in its actions dictionary.
Use a second, distinct confirmed decision with this same visible card as a control; explain when the advice would not apply.
Scope to one visible card ID and its zone in the cited decision. Advice must be usable by both Sol and Luna and
future models. Local legal actions and deterministic safety checks always take precedence. Do not encode historical
entity/option IDs in the advice. No executable code or instructions about files, UI or APIs. If no such finding
exists, use an empty findings array. Give at most two lessons, with short advice and exceptions.
A loss or model disagreement alone is not evidence of a mistake."""

def obj(props):
    return {'type':'object','properties':props,'required':list(props),'additionalProperties':False}

STR = {'type':'string'}
INT = {'type':'integer'}
REF = obj({'run':STR, 'decision':INT})
FINDING = obj({'evidence':REF, 'alternative':STR, 'control':REF,
               'phase':{'type':'string','enum':list(PHASES)}, 'zone':{'type':'string','enum':list(ZONES)},
               'card':STR, 'advice':STR, 'exception':STR, 'opponent_class':{'type':['string','null']},
               'opponent_archetype':{'type':['string','null']}})
SCHEMA = obj({'reviews':{'type':'array','items':obj({'run':STR,'review':STR})},
              'findings':{'type':'array','items':FINDING}})


def response_payload(prefs, games, lessons):
    return {'model':prefs['coach_model'], 'reasoning':{'effort':prefs['coach_effort']},
            'store':False, 'background':True, 'service_tier':'default', 'max_output_tokens':16384, 'instructions':INSTRUCTIONS,
            'text':{'format':{'type':'json_schema','name':'match_review','strict':True,'schema':SCHEMA}},
            'input':json.dumps({'prior_lessons':lessons,'games':games}, ensure_ascii=False,separators=(',',':'))}


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
        if any(f.get(k) is not None and not clean_text(f[k],80) for k in ('opponent_class','opponent_archetype')):
            continue
        if not matchup_applies(f,board) or not matchup_applies(f,index[control][0]):
            continue
        lesson.update({k:f[k] for k in ('opponent_class','opponent_archetype') if f.get(k)})
        game = next(g for g in games if g['run']==evidence[0])
        lesson['rules'] = card_rules(game['cards'].get(f['card'],{}))
        accepted.append(lesson)
    return reviews, accepted, len(findings)-len(accepted)


def train(learning, prefs, state, games, skipped, key, transport=None, job=None):
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
    if transport is None and job is None:
        job=begin(learning,prefs,games,skipped)
    for index,group in enumerate(groups):
        payload=response_payload(prefs,group,lessons)
        result,model=(transport(key,payload) if transport else fetch(learning,job,index,key,payload))
        try:
            reviews, valid, refused = validated(result, group)
        except (ValueError, KeyError, TypeError, TrainingError):
            if job:
                finish(learning,job,'invalid_review')
            raise
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
    if job:
        report['api_usage']=[p.get('usage',{}) for p in job['parts'].values()]
    updated = {'last_run':games[-1]['run'],'lessons':lessons,'revision':report['revision'],
               'reports':(state.get('reports',[])+[report])[-8:]}
    if len(json.dumps(updated,ensure_ascii=False).encode())>380*1024:
        updated['reports'] = updated['reports'][-2:]
    atomic(learning/'coach-state.json',updated)
    if job:
        finish(learning,job,'complete')
    return report


def wait_for_resources(learning, trigger, status):
    from maintenance import MIB, RESOURCE_CHECK_SECONDS, readiness, swap_used
    ranked = read_json(learning/'autoplay-state.json',16384,{})
    baseline = ranked.get('swap_baseline_mib')
    baseline = baseline*MIB if type(baseline) in (int,float) else swap_used()
    previous, reported = None, None
    while True:
        if not app_reviews_enabled(learning):
            status('当前由 Codex 复盘，应用的付费复盘已暂停')
            return False
        health = readiness(baseline,previous)
        if health['ready']:
            return True
        reasons = tuple(health['reasons'])
        if reasons != reported:
            status('训练等待资源，恢复后自动继续 · '+', '.join(reasons))
            reported = reasons
        previous = health
        time.sleep(RESOURCE_CHECK_SECONDS if health.get('memory_pressure_level')==2 else 60)
        if os.getppid()==1:
            return False
        if trigger=='scheduled' and not settings(learning)['scheduled']:
            status('定时自训练已关闭')
            return False


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
        now = time.time()
        if not app_reviews_enabled(learning):
            msg='当前由 Codex 复盘，应用的付费复盘已暂停'
            atomic(learning/'coach-clock.json',{'last_attempt':now,'status':msg})
            message(msg)
            return
        prefs = settings(learning)
        clock = read_json(learning/'coach-clock.json',4096,{})
        if args.trigger=='scheduled' and (not prefs['scheduled'] or now < clock.get('last_attempt',0)+prefs['interval_minutes']*60):
            return
        def status(text):
            atomic(learning/'coach-clock.json',{'last_attempt':now,'status':text})
            message(text)
        try:
            state = read_json(learning/'coach-state.json',384*1024,{})
            after = state.get('last_run','')
            previous = ledger(learning)
            job = previous if previous.get('status')=='open' and previous['through']>after else None
            after = max(after,previous.get('through',''))
            archived,runs = sources(DATA)
            if not job and not any(name>after for name in archived.keys() | runs.keys()):
                status('没有新的完整标准天梯记录可训练');return
            if not wait_for_resources(learning,args.trigger,status):
                return
            if job:
                prefs,games,skipped = restore(learning,job)
            else:
                prefs = settings(learning)
                games, skipped = recent(DATA,after,prefs['games'])
            if not games:
                status('没有新的完整标准天梯记录可训练');return
            status(f"准备复盘 {len(games)} 局、{sum(g['actions'] for g in games)} 个决策，等待读取应用密钥")
            message('NEED_KEY')
            key = sys.stdin.readline(515).strip()
            if not 20<=len(key)<=512 or any(c.isspace() for c in key):
                raise TrainingError('请在应用内允许读取钥匙串后重试')
            status(f"正在复盘 {len(games)} 局，共 {sum(g['actions'] for g in games)} 个决策")
            def deadline(signum, frame):
                raise TrainingError('本次离线评审超时，可稍后重试')
            signal.signal(signal.SIGALRM,deadline)
            signal.alarm(600*len(games))
            report = train(learning,prefs,state,games,skipped,key,**({'job':job} if job else {}))
            message_text = f"已复盘 {report['games']} 局、{report['actions']} 步，采纳 {report['added']} 条经验"
            usage = report.get('api_usage',[])
            if usage and all('output_tokens' in entry for entry in usage):
                message_text += f"，评审输出 {sum(entry['output_tokens'] for entry in usage)} tokens"
            status(message_text)
        except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, TrainingError) as exc:
            status('训练失败 · '+(str(exc) if isinstance(exc,TrainingError) else type(exc).__name__))
            return 1
        finally:
            signal.alarm(0)
    return 0


if __name__=='__main__':
    sys.exit(main())
