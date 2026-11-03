"""Persistent app settings and bounded, advisory lessons pinned at match start."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

DEFAULTS = dict(coach_model='gpt-6-astra', coach_effort='max', games=3, scheduled=False,
                interval_minutes=60, use_lessons=True, standby=True, standby_delay=8, multi_step=True)
MODEL = re.compile(r'[A-Za-z0-9._-]{1,80}')
RUN = re.compile(r'run-\d{8}-\d{6}')
ZONES = ('my_hand', 'my_board', 'enemy_board', 'my_weapon', 'enemy_weapon', 'my_locations',
         'enemy_locations', 'my_hero_power', 'enemy_hero_power', 'choices', 'target_effect')
PHASES = ('main', 'choose_one', 'target')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


def read_json(path, limit, default=None):
    if not path.exists():
        return default
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('Invalid local coaching record')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Local coaching record grew beyond its limit')
    return json.loads(raw)


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False, encoding='utf-8') as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def settings(learning):
    raw = read_json(learning/'coach-settings.json', 4096, {})
    result = DEFAULTS | raw
    if (set(result) != set(DEFAULTS) or not isinstance(result['coach_model'], str)
            or not MODEL.fullmatch(result['coach_model']) or result['coach_effort'] not in EFFORTS
            or any(type(result[k]) is not bool for k in ('scheduled','use_lessons','standby','multi_step'))
            or any(type(result[k]) is not int or not a <= result[k] <= b
                   for k,a,b in [('games',1,5),('interval_minutes',15,1440),('standby_delay',0,12)])):
        raise ValueError('Invalid coaching settings')
    return result


def card_present(view, zone, card):
    group = view.get(zone) or []
    rows = group if isinstance(group, list) else [group]
    return any(isinstance(row, dict) and row.get('card_id') == card for row in rows)


def hints(policy, view):
    return [{'card':r['card'], 'consider':r['advice'], 'exception':r['exception']} for r in
            reversed(policy.get('lessons', [])) if r['phase'] == view.get('phase', 'main')
            and card_present(view, r['zone'], r['card'])
            and (not r.get('rules') or r['rules']==card_rules(view.get('card_definitions',{}).get(r['card'],{})))][:2]


def card_rules(definition):
    return {k:definition[k] for k in ('text','cost','type') if k in definition}


def revision(lessons):
    return hashlib.sha256(json.dumps(lessons, sort_keys=True).encode()).hexdigest()[:12] if lessons else 'baseline'


def snapshot(learning):
    try:
        prefs = settings(learning)
    except (OSError, ValueError, TypeError):
        prefs = DEFAULTS
    lessons = []
    if prefs['use_lessons']:
        try:
            state = read_json(learning/'coach-state.json', 384*1024, {})
            candidates = state.get('lessons', [])[-12:]
            if (isinstance(candidates,list) and state.get('revision') == revision(candidates)
                    and all(isinstance(r,dict) and r.get('phase') in PHASES and r.get('zone') in ZONES
                            and all(isinstance(r.get(k),str) and 0<len(r[k])<=300 for k in ('card','advice','exception'))
                            for r in candidates)):
                lessons = candidates
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return {'revision': revision(lessons), 'lessons':lessons,
            **{k:prefs[k] for k in ('standby','standby_delay','multi_step')}}
