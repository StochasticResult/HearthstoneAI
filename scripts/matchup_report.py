"""Replay a finished ranked run or recover matchup labels from retained public evidence."""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'runtime'),str(ROOT/'runtime/state-bridge')]
from coach_policy import read_json, revision
from coaching import strategy_sample
from coach_evidence import recent
from deck_observation import public_selected_deck
from match_review import rows
from matchup_drills import review_drills
from version_stats import connect, export_report


def evidence(folder):
    review=read_json(folder/'review.json',32768,{})
    if review.get('game_type')!='GT_RANKED' or review.get('outcome') not in ('WON','LOST','TIED'):
        return None
    events=rows(folder/'decisions.jsonl',2097152)
    if not events:return None
    sample=strategy_sample(events)
    deck=public_selected_deck(read_json(folder/'selected-deck.json',8192))
    return events,sample,revision(deck['cards']) if deck else 'unknown'


def main():
    tool=argparse.ArgumentParser(description=__doc__)
    tool.add_argument('--data',type=Path,default=Path.home()/'Library/Application Support/HearthstoneAI')
    action=tool.add_mutually_exclusive_group(required=True)
    action.add_argument('--run')
    action.add_argument('--backfill',action='store_true')
    args=tool.parse_args();learning=args.data/'learning'
    if args.run:
        import re
        if not re.fullmatch(r'run-\d{8}-\d{6}',args.run):tool.error('Invalid run name')
        found=evidence(args.data/'runs'/args.run)
        if not found:tool.error('No retained, settled ranked replay')
        events,sample,_=found
        print(json.dumps(review_drills(events,learning,sample.get('opponent_build')),ensure_ascii=False,indent=2))
        return
    db=connect(learning)
    try:
        with db:
            for folder in sorted((args.data/'runs').glob('run-*'))[-24:]:
                if folder.is_symlink():continue
                found=evidence(folder)
                if not found:continue
                _,s,deck=found
                db.execute("""INSERT OR IGNORE INTO matchups SELECT run,?,?,?,?,? FROM matches WHERE run=? AND mode='ranked'""",
                    (deck,s.get('opponent_class','UNKNOWN'),s.get('inferred_archetype') or 'unknown',
                     json.dumps(s.get('archetype_evidence',[])),s.get('opponent_build','unknown'),folder.name))
            # Old complete archives retained the initial class but not necessarily the exact selected deck or client build.
            games,_ = recent(args.data,'',24)
            for game in games:
                cls='UNKNOWN'; build='unknown'
                for e in game.get('events',[]):
                    change=e.get('change') or {}
                    if change.get('client_build'):build=change['client_build']
                    observed=(change.get('opponent_estimate') or {}).get('enemy_class')
                    if cls=='UNKNOWN' and observed and observed!='UNKNOWN':cls=observed
                if cls!='UNKNOWN':
                    db.execute("""INSERT OR IGNORE INTO matchups SELECT run,'unknown',?,'unknown','[]',? FROM matches
                                  WHERE run=? AND version=? AND mode='ranked'""",(cls,build,game['run'],game['version']))
        current=read_json(learning/'version-stats.json',512*1024,{}).get('current')
        if current:export_report(db,learning,current)
        print('Retained matchup records:',db.execute('SELECT count(*) FROM matchups').fetchone()[0])
    finally:db.close()


if __name__=='__main__':main()
