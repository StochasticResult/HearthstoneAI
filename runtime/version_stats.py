"""Durable per-build results. SQLite makes duplicate review writes idempotent.

Detailed logs can be cleaned independently. Match totals contain run/build/model
identifiers and outcomes. A separate capped table retains public decision samples.
"""
import json
import os
import sqlite3
from pathlib import Path

RATE_DEFINITION='wins / (wins + losses); draws and incomplete runs excluded'


def result_row(summary):
    run=summary.get('run')
    if not isinstance(run,str) or not run or len(run)>120:
        raise ValueError('Missing match identity for statistics')
    version=summary.get('runtime_version') or 'unversioned'
    if not isinstance(version,str) or len(version)>80:
        raise ValueError('Invalid statistics version')
    game_type=summary.get('game_type')
    mode='ranked' if game_type=='GT_RANKED' else 'practice' if game_type=='GT_VS_AI' else summary.get('requested_mode')
    if mode not in ('ranked','practice'):mode='unknown'
    outcome=summary.get('outcome')
    if outcome not in ('WON','LOST','TIED') or game_type not in ('GT_RANKED','GT_VS_AI'):
        outcome='INCOMPLETE'
    models=','.join(sorted(str(x)[:80] for x in summary.get('models',[])[:4]))
    return run,version,mode,outcome,models


def connect(learning):
    path=Path(learning)/'match-stats.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.is_symlink():raise ValueError('Invalid statistics database path')
    connection=sqlite3.connect(path,timeout=2)
    os.chmod(path,0o600)
    connection.execute('PRAGMA cache_size=-1024')
    connection.execute('PRAGMA journal_mode=DELETE')
    connection.execute('PRAGMA temp_store=FILE')
    connection.execute('CREATE TABLE IF NOT EXISTS matches (run TEXT PRIMARY KEY, version TEXT NOT NULL, mode TEXT NOT NULL, outcome TEXT NOT NULL, models TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS match_policies (run TEXT PRIMARY KEY, revision TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS matchups (run TEXT PRIMARY KEY, deck TEXT NOT NULL, opponent_class TEXT NOT NULL, archetype TEXT NOT NULL, evidence TEXT NOT NULL, client_build TEXT NOT NULL)')
    return connection


def grouped(connection, version=None, mode=None, limit=100):
    where=[];args=[]
    if version is not None:where.append('version=?');args.append(version)
    if mode is not None:where.append('mode=?');args.append(mode)
    sql='''SELECT version,mode,SUM(outcome='WON'),SUM(outcome='LOST'),SUM(outcome='TIED'),
           SUM(outcome='INCOMPLETE'),MIN(run),MAX(run) FROM matches'''
    if where:sql+=' WHERE '+' AND '.join(where)
    sql+=' GROUP BY version,mode ORDER BY MAX(run) DESC LIMIT ?';args.append(limit)
    result=[]
    for version,mode,wins,losses,draws,incomplete,first,last in connection.execute(sql,args):
        decisive=wins+losses
        result.append({'version':version,'mode':mode,'wins':wins,'losses':losses,'draws':draws,
                       'completed_games':decisive+draws,'decisive_games':decisive,'incomplete_runs':incomplete,
                       'win_rate_pct':round(100*wins/decisive,2) if decisive else None,
                       'first_run':first,'last_run':last,'legacy_unversioned':version=='unversioned'})
    return result


def export_report(connection,learning,current):
    export={'schema':1,'rate_definition':RATE_DEFINITION,'current':current,
            'legacy_note':'Unversioned counts include only retained historical records; older deleted records cannot be reconstructed.',
            'versions':grouped(connection),'export_limit':100,'all_results_database':'match-stats.sqlite3'}
    rows = connection.execute("""SELECT version,mode,revision,SUM(outcome='WON'),SUM(outcome='LOST'),
        SUM(outcome='TIED'),SUM(outcome='INCOMPLETE') FROM matches JOIN match_policies USING(run)
        GROUP BY version,mode,revision ORDER BY MAX(run) DESC LIMIT 100""")
    export['coach_policies'] = [dict(version=v,mode=m,revision=p,wins=w,losses=l,draws=d,incomplete_runs=i,
                                     decisive_games=w+l,win_rate_pct=round(100*w/(w+l),2) if w+l else None)
                                for v,m,p,w,l,d,i in rows]
    from match_review import save
    save(Path(learning)/'version-stats.json',export)
    matchups = {}
    for level,field in [('by_class','opponent_class'),('by_inferred_deck','opponent_class,archetype')]:
        data = connection.execute(f"""WITH ranked AS (SELECT m.run,m.version,m.outcome,coalesce(d.deck,'unknown') AS deck,
            coalesce(d.client_build,'unknown') AS client_build,coalesce(d.opponent_class,'UNKNOWN') AS opponent_class,
            coalesce(d.archetype,'unknown') AS archetype FROM matches m LEFT JOIN matchups d USING(run)
            WHERE m.mode='ranked' AND m.outcome IN ('WON','LOST','TIED'))
            SELECT version, deck, client_build, {field}, SUM(outcome='WON'), SUM(outcome='LOST'), SUM(outcome='TIED') FROM ranked
            GROUP BY version,deck,client_build,{field} ORDER BY MAX(run) DESC LIMIT 400""")
        groups = []
        for row in data:
            wins,losses,draws = row[-3:]
            group=dict(version=row[0],deck=row[1],client_build=row[2],opponent_class=row[3],
                       wins=wins,losses=losses,draws=draws,decisive_games=wins+losses,
                       win_rate_pct=round(100*wins/(wins+losses),2) if wins+losses else None)
            if level=='by_inferred_deck':group['inferred_archetype']=row[4]
            groups.append(group)
        matchups[level]=groups
    save(Path(learning)/'matchup-stats.json',dict(rate_definition=RATE_DEFINITION,**matchups))


def status(learning,version,mode='ranked'):
    connection=connect(learning)
    try:
        history=Path(learning)/'match-history.json'
        if history.exists():
            if history.is_symlink() or history.stat().st_size>256*1024:
                raise ValueError('Invalid retained match history')
            rows=json.loads(history.read_text())
            if not isinstance(rows,list) or len(rows)>50:
                raise ValueError('Invalid retained match history')
            with connection:
                for row in rows:connection.execute('INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?)',result_row(row))
        found=grouped(connection,version,mode)
        current=found[0] if found else {'version':version,'mode':mode,'wins':0,'losses':0,'draws':0,
            'completed_games':0,'decisive_games':0,'incomplete_runs':0,'win_rate_pct':None,
            'first_run':None,'last_run':None,'legacy_unversioned':version=='unversioned'}
        export_report(connection,learning,current)
        return current
    finally:connection.close()


def record(learning, summary, history=(), *, strategy_sample=None):
    """Import retained legacy evidence, then insert/update this exact run once."""
    connection=connect(learning)
    try:
        with connection:
            for old in history:
                connection.execute('INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?)',result_row(old))
            row=result_row(summary)
            if summary.get('coach_revision'):
                connection.execute('INSERT OR REPLACE INTO match_policies VALUES (?,?)',(row[0],summary['coach_revision']))
            connection.execute('''INSERT INTO matches VALUES (?,?,?,?,?) ON CONFLICT(run) DO UPDATE SET
                version=excluded.version,mode=excluded.mode,outcome=excluded.outcome,models=excluded.models''',row)
            if strategy_sample is not None and (strategy_sample['observed_own_turns'] or 'opening' in strategy_sample):
                if row[2]=='ranked':
                    connection.execute('INSERT OR REPLACE INTO matchups VALUES (?,?,?,?,?,?)',
                        (row[0],strategy_sample.get('played_deck','unknown'),strategy_sample.get('opponent_class','UNKNOWN'),
                         strategy_sample.get('inferred_archetype') or 'unknown',
                         json.dumps(strategy_sample.get('archetype_evidence',[])),strategy_sample.get('opponent_build','unknown')))
                connection.execute('CREATE TABLE IF NOT EXISTS strategy_samples (run TEXT PRIMARY KEY, sample TEXT NOT NULL)')
                connection.execute('INSERT OR REPLACE INTO strategy_samples VALUES (?,?)',
                                   (row[0], json.dumps(strategy_sample, ensure_ascii=False, separators=(',', ':'))))
                connection.execute('DELETE FROM strategy_samples WHERE run NOT IN (SELECT run FROM strategy_samples ORDER BY run DESC LIMIT 64)')
        current=grouped(connection,version=row[1],mode=row[2])[0]
        export_report(connection,learning,current)
        return current
    finally:
        connection.close()


def display(stats):
    rate='暂无胜负样本' if stats['win_rate_pct'] is None else '胜率 %g%%（%d 局胜负样本）'%(stats['win_rate_pct'],stats['decisive_games'])
    return '版本 %s · %s · %d 胜 %d 负 %d 平 · %d 次未完成'%(stats['version'],rate,stats['wins'],stats['losses'],stats['draws'],stats['incomplete_runs'])


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--version',required=True,help='exact build ID; unversioned selects retained legacy results')
    parser.add_argument('--mode',choices=('ranked','practice'),default='ranked')
    args=parser.parse_args()
    db=connect(Path.home()/'Library/Application Support/HearthstoneAI/learning')
    try:print(json.dumps(grouped(db,args.version,args.mode),ensure_ascii=False))
    finally:db.close()
