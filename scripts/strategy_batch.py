#!/usr/bin/env python3
"""Find and record adaptive ranked reviews, independently of the active game."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from coaching import strategy_sample
from match_review import rows

DEFAULT = Path.home() / 'Library/Application Support/HearthstoneAI'
MAX_BATCH = 5


def open_review(data, *, create=False, readonly=False):
    learning = data / 'learning'
    progress = learning / 'strategy-reviews.sqlite3'
    matches = learning / 'match-stats.sqlite3'
    if progress.is_symlink() or matches.is_symlink() or not matches.is_file():
        raise ValueError('Missing or invalid match ledger')
    mode = 'rwc' if create else 'ro' if readonly else 'rw'
    db = sqlite3.connect(progress.resolve().as_uri()+'?mode='+mode, uri=True, timeout=2)
    try:
        if create:
            progress.chmod(0o600)
        db.execute('ATTACH DATABASE ? AS stats', (matches.resolve().as_uri()+'?mode=ro',))
        if readonly:
            db.execute('PRAGMA query_only=ON')
        return db
    except BaseException:
        db.close()
        raise


def initialize(db, after):
    if not db.execute("SELECT 1 FROM stats.matches WHERE run=? AND mode='ranked' AND outcome IN ('WON','LOST','TIED')",
                      (after,)).fetchone():
        raise ValueError('Start after a verified completed ranked run')
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), start_after TEXT NOT NULL)')
        db.execute('''CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, reviewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                      finding TEXT NOT NULL, change_made TEXT NOT NULL, validation TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS reviewed (run TEXT PRIMARY KEY, version TEXT NOT NULL,
                      outcome TEXT NOT NULL, batch_id INTEGER NOT NULL REFERENCES batches(id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS batch_games (batch_id INTEGER NOT NULL, run TEXT NOT NULL,
                      version TEXT NOT NULL, outcome TEXT NOT NULL, PRIMARY KEY(batch_id,run))''')
        db.execute('INSERT OR IGNORE INTO batch_games SELECT batch_id,run,version,outcome FROM reviewed')
        db.execute('INSERT OR IGNORE INTO settings VALUES (1,?)',(after,))
        if db.execute('SELECT start_after FROM settings WHERE id=1').fetchone()[0] != after:
            raise ValueError('Review baseline already initialized at a different game')


def review_size(games):
    outcomes = [row[2] for row in games]
    if outcomes[:3].count('LOST') >= 2:
        return 3
    if any(outcome != 'WON' for outcome in outcomes[:4]):
        return 4
    return MAX_BATCH


def pending(db):
    games = db.execute('''SELECT m.run,m.version,m.outcome FROM stats.matches AS m
        LEFT JOIN reviewed AS r ON r.run=m.run WHERE m.run>(SELECT start_after FROM settings WHERE id=1)
        AND m.mode='ranked' AND m.outcome IN ('WON','LOST','TIED')
        AND (r.run IS NULL OR r.outcome<>m.outcome OR r.version<>m.version) ORDER BY m.run LIMIT ?''', (MAX_BATCH,)).fetchall()
    return games[:review_size(games)]


def loss_pairs(db, games):
    """First two settled losses of a streak, even when the first was in a previous batch."""
    has_checks = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='loss_checks'").fetchone()
    for row in games:
        if row[2] != 'LOST':
            continue
        preceding = db.execute('''SELECT run,version,outcome FROM stats.matches WHERE run<?
            AND run>(SELECT start_after FROM settings WHERE id=1) AND mode='ranked'
            AND outcome IN ('WON','LOST','TIED') ORDER BY run DESC LIMIT 2''', (row[0],)).fetchall()
        if not preceding or preceding[0][2] != 'LOST' or (len(preceding) == 2 and preceding[1][2] == 'LOST'):
            continue
        first = preceding[0]
        checked = None
        if has_checks:
            checked = db.execute('''SELECT finding FROM loss_checks WHERE first_run=? AND first_version=?
                AND last_run=? AND last_version=?''', (first[0],first[1],row[0],row[1])).fetchone()
        yield (first,row), checked[0] if checked else None


def record_quick(db, runs, finding):
    if not finding.strip() or len(finding) > 1200:
        raise ValueError('Supply a short finding for the two losses')
    with db:
        db.execute('BEGIN IMMEDIATE')
        games = pending(db)
        if len(games) == review_size(games):
            raise ValueError('A full review is due; run pending again')
        pair = next((p for p,note in loss_pairs(db,games) if note is None), ())
        if not pair or [row[0] for row in pair] != runs:
            raise ValueError('Runs do not match the next unchecked loss pair')
        first,last = pair
        db.execute('''CREATE TABLE IF NOT EXISTS loss_checks (first_run TEXT NOT NULL, first_version TEXT NOT NULL,
                   last_run TEXT NOT NULL, last_version TEXT NOT NULL, finding TEXT NOT NULL,
                   PRIMARY KEY(first_run,first_version,last_run,last_version))''')
        db.execute('INSERT INTO loss_checks VALUES (?,?,?,?,?)', (first[0],first[1],last[0],last[1],finding))


def sample_for(db, data, run):
    if db.execute("SELECT 1 FROM stats.sqlite_master WHERE type='table' AND name='strategy_samples'").fetchone():
        saved = db.execute('SELECT sample FROM stats.strategy_samples WHERE run=?',(run,)).fetchone()
        if saved:
            return json.loads(saved[0])
    if Path(run).name != run or not run.startswith('run-'):
        return None
    dest = data / 'runs' / run
    path = dest / 'review.json'
    if dest.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        return None
    review = json.loads(path.read_bytes())
    if review.get('run') != run or review.get('game_type') != 'GT_RANKED':
        return None
    events = rows(dest / 'decisions.jsonl', 2*1024*1024)
    return strategy_sample(events, review.get('diagnostics_capped',False)) if events else None


def record(db, runs, finding, change_made, validation):
    if any(not s.strip() or len(s) > 1200 for s in (finding,change_made,validation)):
        raise ValueError('Supply a finding, an action and validation')
    with db:
        db.execute('BEGIN IMMEDIATE')
        batch = pending(db)
        if len(batch) != review_size(batch) or [r[0] for r in batch] != runs:
            raise ValueError('Runs do not match the next due ranked review')
        cursor = db.execute('INSERT INTO batches (finding,change_made,validation) VALUES (?,?,?)',(finding,change_made,validation))
        db.executemany('INSERT INTO batch_games VALUES (?,?,?,?)', [(cursor.lastrowid,*row) for row in batch])
        db.executemany('INSERT OR REPLACE INTO reviewed VALUES (?,?,?,?)', [(*row,cursor.lastrowid) for row in batch])
        return cursor.lastrowid


def review_status(db, data, samples=False):
    batch = pending(db)
    due = len(batch) == review_size(batch)
    pairs = list(loss_pairs(db,batch))
    quick = () if due else next((p for p,note in pairs if note is None), ())
    games = [dict(run=r,version=v,outcome=o) for r,v,o in batch]
    quick_games = [dict(run=r,version=v,outcome=o) for r,v,o in quick]
    if samples:
        for game in (games if due else quick_games):
            game['strategy_sample'] = sample_for(db,data,game['run'])
    return {'due':due, 'games':games, 'quick_check':quick_games,
            'prior_quick_checks':[{'runs':[r[0] for r in pair], 'finding':note} for pair,note in pairs if note]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=DEFAULT)
    sub = parser.add_subparsers(dest='cmd',required=True)
    sub.add_parser('init').add_argument('--after',required=True)
    show = sub.add_parser('pending')
    show.add_argument('--samples',action='store_true')
    write = sub.add_parser('record')
    write.add_argument('runs',nargs='+')
    for key in ('finding','change','validation'):
        write.add_argument('--'+key,required=True)
    quick = sub.add_parser('record-quick')
    quick.add_argument('runs',nargs=2)
    quick.add_argument('--finding',required=True)
    args=parser.parse_args()
    db=open_review(args.data,create=args.cmd=='init',readonly=args.cmd=='pending')
    try:
        if args.cmd=='init':
            initialize(db,args.after)
            print('Review baseline:',args.after)
        elif args.cmd=='record':
            print('Recorded batch',record(db,args.runs,args.finding,args.change,args.validation))
        elif args.cmd=='record-quick':
            record_quick(db,args.runs,args.finding)
            print('Recorded two-loss check')
        else:
            print(json.dumps(review_status(db,args.data,args.samples),ensure_ascii=False))
    finally:
        db.close()


if __name__=='__main__':
    main()
