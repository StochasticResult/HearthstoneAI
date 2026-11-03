"""Refresh public Standard lists off the game thread and match only publicly played cards."""
import collections
import datetime
import fcntl
import html
import json
import plistlib
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

BASE = 'https://metastats.net/hearthstone/'
CLASSES = ('DeathKnight','DemonHunter','Druid','Hunter','Mage','Paladin','Priest','Rogue','Shaman','Warlock','Warrior')
DAY = 86400


def client_build():
    try:
        return plistlib.loads(Path('/Applications/Hearthstone/Hearthstone.app/Contents/Info.plist').read_bytes())['CFBundleVersion']
    except (OSError,ValueError,KeyError,plistlib.InvalidFileException):
        return None


def fetch_class(name):
    url = BASE + 'class/decks/' + name + '/'
    req = urllib.request.Request(url, headers={'User-Agent':'HearthstoneAI/0.2 (public Standard deck reference)'})
    with urllib.request.urlopen(req,timeout=5) as reply:
        if reply.geturl() != url:
            raise ValueError('Unexpected source redirect')
        data = reply.read(2*1024*1024+1)
    if len(data) > 2*1024*1024:
        raise ValueError('Deck source exceeded its bound')
    return data.decode('utf-8')


def parse_page(page, name, today):
    title = re.search(r'<title>([^<]+)</title>',page,re.I)
    if not title or 'Standard Ladder' not in title[1] or name not in title[1].replace(' ',''):
        raise ValueError('Unexpected deck source or format')
    stamp = re.search(r'<!--\s*Last Update:\s*(\d{1,2})(?:st|nd|rd|th) (\w+ \d{4})',page,re.I)
    if not stamp:
        raise ValueError('Missing source date')
    date = datetime.datetime.strptime(f'{stamp[1]} {stamp[2]}','%d %B %Y').date()
    if not -1 <= (today-date).days <= 7:
        raise ValueError('Stale source date')
    results = {}
    sections = re.split(r"""<h4\b[^>]*>\s*<a\s+href=['"]/hearthstone/deck/(\d+)/['"][^>]*>\s*([^<]+)""",page,flags=re.I)
    for ident,label,section in zip(sections[1::3],sections[2::3],sections[3::3]):
        sample = re.search(r'#Games:\s*([\d,]+)',section)
        listing = re.search(r"""<ul\s+class=['"]card-list['"]\s*>(.*?)</ul>""",section,re.S|re.I)
        if not sample or not listing:
            continue
        games = int(sample[1].replace(',',''))
        if games < 30:
            continue
        cards = {}
        for item in re.findall(r'<li\b[^>]*>(.*?)</li>',listing[1],re.I|re.S):
            cid = re.search(r'https://art\.hearthstonejson\.com/[^"\s<>]+/([A-Za-z0-9_]+)\.png',item)
            count = re.search(r"""class=['"]card-quantity['"]\s*>\s*x([12])\s*<""",item)
            if not cid or not count:
                cards = {}; break
            key = cid[1].removeprefix('CORE_')
            if key in cards:
                cards = {}; break
            cards[key] = int(count[1])
        if sum(cards.values()) != 30:
            continue
        signature = json.dumps(cards,sort_keys=True,separators=(',',':'))
        label = html.unescape(label).strip()[:80]
        if signature not in results or games > results[signature][3]:
            results[signature] = (name.upper(),label,signature,games,BASE+'deck/'+ident+'/',date.isoformat())
    if not results:
        raise ValueError('No complete 30-card Standard lists')
    return list(results.values())


def refresh(learning, build, fetch=fetch_class):
    if not build or not learning.is_dir():
        return
    with (learning/'opponent-decks.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        db = sqlite3.connect(learning/'opponent-decks.sqlite3',timeout=1)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS decks (build TEXT,class TEXT,label TEXT,cards TEXT,games INTEGER,source TEXT,as_of TEXT,PRIMARY KEY(build,class,cards))')
                db.execute('CREATE TABLE IF NOT EXISTS sync (class TEXT PRIMARY KEY,build TEXT,next_check REAL,status TEXT)')
            for name in CLASSES:
                now = time.time()
                last = db.execute('SELECT build,next_check FROM sync WHERE class=?',(name,)).fetchone()
                if last and last[0] == build and now < last[1]:
                    continue
                try:
                    rows = parse_page(fetch(name),name,datetime.date.today())
                except (OSError,ValueError) as error:
                    with db:db.execute('INSERT OR REPLACE INTO sync VALUES (?,?,?,?)',(name,build,now+3600,type(error).__name__))
                    continue
                with db:
                    db.execute('DELETE FROM decks WHERE class=?',(name.upper(),))
                    db.executemany('INSERT INTO decks VALUES (?,?,?,?,?,?,?)',[(build,*row) for row in rows])
                    db.execute('INSERT OR REPLACE INTO sync VALUES (?,?,?,?)',(name,build,now+DAY,'ok'))
        finally:db.close()


def start_refresh(learning):
    def worker():
        try:refresh(learning,client_build())
        except (OSError,ValueError,sqlite3.Error):pass  # Optional reference data cannot stop a ranked queue.
    threading.Thread(target=worker,daemon=True,name='opponent-deck-refresh').start()


def load_snapshot(learning, build, today=None):
    """Pin a bounded reference set and only verified same-build observations for this game."""
    today = today or datetime.date.today()
    path = learning/'opponent-decks.sqlite3'
    if not build or not path.is_file() or path.is_symlink():return [],[]
    since = (today-datetime.timedelta(days=7)).isoformat()
    until = (today+datetime.timedelta(days=1)).isoformat()
    try:
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
        try:
            rows = []
            for cls,label,raw,games,source,stamp in db.execute('SELECT class,label,cards,games,source,as_of FROM decks WHERE build=? AND as_of BETWEEN ? AND ? AND length(cards)<2048 ORDER BY class,label,games DESC LIMIT 1024',(build,since,until)):
                cards = json.loads(raw)
                if (isinstance(cards,dict) and all(type(n) is int and n in (1,2) for n in cards.values())
                        and sum(cards.values())==30 and isinstance(label,str) and isinstance(source,str)):
                    rows.append((cls,label,cards,games,source,stamp))
        finally:db.close()
    except (sqlite3.Error,OSError,ValueError):return [],[]
    past = []
    history = learning/'match-stats.sqlite3'
    if history.is_file() and not history.is_symlink():
        try:
            db = sqlite3.connect(history.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
            try:
                for (raw,) in db.execute("""SELECT s.sample FROM strategy_samples s JOIN matches m ON m.run=s.run
                        WHERE m.mode='ranked' AND m.outcome IN ('WON','LOST','TIED') AND length(s.sample)<=65536
                        AND substr(m.run,5,8)>=? ORDER BY m.run DESC LIMIT 64""",(since.replace('-',''),)):
                    sample = json.loads(raw)
                    if (isinstance(sample,dict) and sample.get('opponent_build') == build
                            and isinstance(sample.get('opponent_played_originals'),list)
                            and all(isinstance(c,str) for c in sample['opponent_played_originals'])):
                        past.append((sample.get('opponent_class'),set(sample['opponent_played_originals'])))
            finally:db.close()
        except (sqlite3.Error,OSError,ValueError):pass
    return rows,past


def class_plan(cls, rows, catalog):
    """One vote per published archetype, not per variant or game played."""
    pool = [r for r in rows if r[0]==cls]
    groups = {r[1] for r in pool}
    if not groups:return None
    features = {'early_board':set(),'cheap_removal':set(),'area_damage':set()}
    for _,label,cards,_,_,_ in pool:
        for cid in cards:
            card = catalog.get('CORE_'+cid) or catalog.get(cid,{})
            text = ' '.join(card.get('text','').lower().split())
            cost = card.get('cost')
            if type(cost) is not int:continue
            if card.get('type')=='MINION' and cost<=2:features['early_board'].add(label)
            if card.get('type')=='SPELL' and cost<=2 and re.search(r'\b(deal|destroy|return)\b',text):
                features['cheap_removal'].add(label)
            if card.get('type')=='SPELL' and re.search(r'damage to all|destroy all minions',text):
                features['area_damage'].add(label)
    common = {k:len(v) for k,v in features.items() if len(v)>=2 and len(v)*2>=len(groups)}
    return {'class':cls,'published_archetypes':len(groups),'archetypes_showing_style':common,
            'plan':'Start Zoo on curve. Contest early pressure. Where cheap removal is common, spread pressure across bodies; where area damage is common, keep refill if another body adds little. Update after reveals; do not sacrifice lethal to hedge.'}


def predict(view, rows, past, catalog):
    matchup = view.get('strategy',{}).get('matchup',{})
    if view.get('game_type') != 'GT_RANKED' or view.get('format') != 'FT_STANDARD':return None
    cls = matchup.get('enemy_class')
    cards = {c['id']:c['card_id'].removeprefix('CORE_') for c in view.get('opponent_public_cards',())
             if c.get('played_publicly') and c.get('generated') is False}
    seen = collections.Counter(cards.values())
    if len(seen) < 2:return None
    pool = [r for r in rows if r[0]==cls]
    archetypes = {r[1] for r in pool}
    if len(archetypes)<2 or len({r[0] for r in rows})<3:return None
    signatures = []
    for cid in seen:
        labels = {r[1] for r in pool if cid in r[2]}
        classes = {r[0] for r in rows if cid in r[2]}
        if labels and len(labels)*2 <= len(archetypes) and len(classes)<=2:
            signatures.append(cid)
    if len(signatures)<2:return None
    matches = [r for r in pool if all(r[2].get(c,0)>=n for c,n in seen.items())]
    if not matches:return None
    common = set.intersection(*(set(row[2]) for row in matches))
    possible = set.union(*(set(row[2]) for row in matches))
    similar = [h for klass,h in past if klass==cls and set(signatures)<=h and h<=possible]
    threats = []
    for cid in common:
        printed = catalog.get('CORE_'+cid) or catalog.get(cid,{})
        copies = min(r[2][cid] for r in matches)-seen[cid]
        text = printed.get('text','')
        if copies>0 and re.search(r'\b(deal|destroy|freeze|return|silence|charge)\b',text,re.I):
            hits = sum(cid in h for h in similar)
            threats.append((hits if hits>=3 else 0,printed.get('cost',100),cid,
                            {'card':cid,'name':printed.get('name',cid),'printed_cost':printed.get('cost'),
                             'effect':text[:155],'unseen_if_list_matches':copies}))
    threats.sort(key=lambda t:(-t[0],t[1],t[2]))
    watch = []
    for hits,_,_,card in threats[:3]:
        if hits:card['seen_in_similar_past_games']=hits
        watch.append(card)
    return {'class':cls,'matching_lists':len(matches),'examples':sorted({r[1] for r in matches})[:3],
            'observed_signatures':sorted(signatures),'unseen_watch':watch,'source':matches[0][4],
            'source_dates':sorted({r[5] for r in matches}),
            'scope':'Published Standard lists, region and rank unspecified. Unknown hand. Prefer an equally strong line that plays around these effects; keep lethal and tempo.'}
