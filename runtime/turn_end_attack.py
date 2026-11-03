"""Reconcile a submitted minion attack interrupted by turn end or concession."""
import json
import re
import subprocess
import sys
from pathlib import Path


def board_signature(view, action):
    """Public state needed to bind the recommendation to its exact submission."""
    def row(entity, fields):
        return [entity.get(k) for k in fields]
    hero_fields = ('id', 'card_id', 'health', 'armor')
    minion_fields = ('id', 'card_id', 'attack', 'health', 'taunt', 'reborn', 'divine_shield', 'attacks_this_turn')
    source = next((c for c in view.get('my_board', []) if c['id'] == action['source_id']), {})
    target = next((c for c in view.get('enemy_board', []) if c['id'] == action['target_id']), {})
    enemy_hero = view.get('enemy_hero') or {}
    target_row = row(enemy_hero, hero_fields) if enemy_hero.get('id') == action['target_id'] else row(target, minion_fields)
    parts = [row(view.get('my_hero') or {}, hero_fields), row(enemy_hero, hero_fields),
             row(source, minion_fields), target_row]
    if any(None in part or not part[1] for part in parts) or type(view.get('mana')) is not int:
        return None
    return [view['mana'], *parts]


def reconcile(dest, outcome, pending, events):
    recovered = {e.get('decision_id'): e for e in events if e.get('event') == 'resumed_after_uncertain_input'}
    cases = []
    for row in pending:
        did, view, action = row.get('decision_id'), row.get('observation') or {}, row.get('action') or {}
        marker = recovered.get(did) or {}
        if (type(did) is not int or action.get('kind') != 'attack' or marker.get('action') != action
                or marker.get('game_turn') != view.get('game_turn') or view.get('game_type') != 'GT_RANKED'
                or view.get('format') != 'FT_STANDARD' or view.get('phase') != 'main'
                or action not in (row.get('actions') or {}).values()
                or (row.get('server_options_id') is not None and marker.get('options_id') is not None
                    and row['server_options_id'] != marker['options_id'])
                or not all(type(action.get(k)) is int for k in ('source_id','target_id'))):
            continue
        signature = board_signature(view, action)
        if signature and str(view.get('game_turn')).isdigit():
            cases.append({'decision_id': did, 'turn': int(view['game_turn']), 'source': action['source_id'],
                          'target': action['target_id'], 'signature': signature, 'options_id': marker.get('options_id'),
                          'sent_before': marker.get('sent_before')})
    if not cases or len(cases) > 64:
        return []
    # Most games never invoke the replayer. Its parser and card catalog leave
    # memory with this short-lived process, outside the opponent's action window.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(dest)],
                                input=json.dumps({'outcome':outcome,'cases':cases}), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=False)
        if result.returncode == 0 and len(result.stdout) <= 16384:
            proofs = json.loads(result.stdout)
            eligible = {c['decision_id'] for c in cases}
            if isinstance(proofs, list) and all(isinstance(p, dict) and p.get('decision_id') in eligible for p in proofs):
                return proofs
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return []


def verify(dest, outcome, cases):
    from incremental_board import IncrementalBoard, UnsafeState, board
    from game_boundary import FaultedMatchEnd
    cursor_path, log = dest/'log-cursor.json', dest/'Power.log'
    if (dest.is_symlink() or cursor_path.is_symlink() or not cursor_path.is_file() or cursor_path.stat().st_size > 2048
            or log.is_symlink() or not log.is_file()):
        return []
    cursor = json.loads(cursor_path.read_bytes())
    own, start, end = (cursor.get(k) for k in ('verified_owner','start_offset','offset'))
    size = log.stat().st_size
    if (type(own) is not int or own not in (1,2) or type(start) is not int or type(end) is not int
            or start < 0 or not 0 < size == end-start <= 8*1024*1024):
        return []
    parser = IncrementalBoard()
    games = 0
    terminal = False
    watch = None
    matches, proofs = {}, {}
    def current_flags():
        return {board._tag_int(p,'PLAYER_ID'): board._num(p['tags'].get('CURRENT_PLAYER'))
                for p in board._players(parser.game)}
    def bodies(case):
        return [(c.get('cardId'), c.get('controller'), c.get('zone'), c.get('tags',{}).get('DAMAGE','0'),
                 c.get('tags',{}).get('HEALTH')) for c in (parser.game['entities'].get(eid,{})
                          for eid in (case['source'],case['target']))]
    def submission(index, target):
        ent = board._game_entity(parser.game)
        if (not ent or not parser.game['gate'] or ent['tags'].get('STEP') != 'MAIN_ACTION' or parser.game['blocks']
                or current_flags() != {own:1, 3-own:0}):
            return None
        turn = board._num(ent['tags'].get('TURN'))
        selected = parser.targeted_option(index, target)
        if selected is None:
            return None
        candidates = [c for c in cases if (c['turn'],c['source'],c['target']) == (turn,selected[1],target)
                      and c['options_id'] in (None, parser.options_id) and c['sent_before'] in (None, parser.sent_options)]
        if not candidates:
            return None
        for c in candidates:
            did = c['decision_id']
            matches[did] = matches.get(did,0)+1
        if len(candidates) != 1:
            return None
        case = candidates[0]
        try:
            view = parser.snapshot(own,allow_practice=True,extended=True)
        except UnsafeState:
            return None
        if not view['own_turn'] or board_signature(view,{'source_id':case['source'],'target_id':target}) != case['signature']:
            return None
        return {'case':case, 'options_id':parser.options_id, 'status':'submitted', 'bodies':bodies(case),
                'face':target == (view.get('enemy_hero') or {}).get('id'),
                'can_concede':outcome == 'WON' and type(case['options_id']) is int and type(case['sent_before']) is int
                              and not any(p.get('conceded') for p in board._players(parser.game))}
    with log.open('rb') as f:
        while f.tell() < size:
            line = f.readline(min(65537,size-f.tell()))
            if not line.endswith(b'\n') or len(line)>65536:
                return []
            if FaultedMatchEnd.create.search(line):
                games += 1
                if games>1:
                    return []
            terminal |= games == 1 and bool(FaultedMatchEnd.terminal.search(line))
            if b'GameState.SendOption() -' in line:
                watch = None
                sent = re.search(rb'GameState\.SendOption\(\) - selectedOption=(\d+) selectedSubOption=-1 selectedTarget=(\d+) selectedPosition=0\s*$',line)
                if sent and games==1 and parser.game and not terminal:
                    watch = submission(int(sent[1]), int(sent[2]))
            elif watch and b'GameState.' in line:
                _, primary, msg = line.partition(b'GameState.DebugPrintPower() -')
                msg = msg.strip()
                if re.search(rb'GameState\.DebugPrintPowerList\(\) - Count=\d+\s*$',line):
                    pass
                elif not primary:
                    watch = None
                elif watch['status']=='submitted':
                    if not watch['face'] and re.fullmatch(rb'TAG_CHANGE Entity=(?:GameEntity|1) tag=STEP value=MAIN_END',msg):
                        watch['status'] = 'ending'
                    elif watch['can_concede'] and re.fullmatch(rb'TAG_CHANGE Entity=.+ tag=PLAYSTATE value=CONCEDED', msg):
                        watch['status'] = 'conceded'
                    else:
                        watch = None
                elif watch['status'] == 'conceded':
                    # Only the observed terminal bookkeeping may follow the opponent's concession.
                    if not (re.fullmatch(rb'TAG_CHANGE Entity=.+ tag=(?:3479 value=1|NUM_TURNS_IN_HAND value=\d+|'
                                         rb'PLAYSTATE value=(?:LOST|WON)|(?:NEXT_STEP|STEP) value=FINAL_(?:WRAPUP|GAMEOVER)|'
                                         rb'STATE value=COMPLETE)', msg)
                            or re.fullmatch(rb'BLOCK_START BlockType=TRIGGER Entity=(?:GameEntity|1) .* Target=0 .*', msg)
                            or msg == b'BLOCK_END'):
                        watch = None
                elif (not msg.startswith((b'TAG_CHANGE ',b'BLOCK_START BlockType=TRIGGER ',b'BLOCK_END'))
                      or re.search(rb'tag=(?:ATTACKING|DEFENDING|NUM_ATTACKS_THIS_TURN) value=[1-9]\d*\b',msg)):
                    watch = None
            parser.feed(line)
            if watch and watch['status']=='ending' and b'tag=TURN value=' in line:
                case=watch['case']
                ent=board._game_entity(parser.game)
                if ent and board._tag_int(ent,'TURN')==case['turn']+1:
                    if current_flags()=={own:0,3-own:1} and bodies(case)==watch['bodies']:
                        proofs[case['decision_id']] = {'decision_id':case['decision_id'],'game_turn':case['turn'],
                            'options_id':watch['options_id'],'source_id':case['source'],'target_id':case['target'],
                            'next_turn':case['turn']+1}
                    watch=None
    if games!=1 or not terminal or not parser.game or parser.blocked or parser.partial:
        return []
    meta=parser.game['meta']
    states={board._tag_int(p,'PLAYER_ID'):board._playstate(p) for p in board._players(parser.game)}
    if (meta.get('GameType')!='GT_RANKED' or meta.get('FormatType')!='FT_STANDARD'
            or outcome not in ('WON','LOST','TIED') or states!={own:outcome,3-own:{'WON':'LOST','LOST':'WON','TIED':'TIED'}[outcome]}):
        return []
    if watch and watch['status'] == 'conceded':
        case = watch['case']
        conceded = [board._tag_int(p,'PLAYER_ID') for p in board._players(parser.game) if p.get('conceded')]
        if conceded == [3-own] and bodies(case) == watch['bodies']:
            proofs[case['decision_id']] = {'decision_id':case['decision_id'], 'game_turn':case['turn'],
                'options_id':watch['options_id'], 'source_id':case['source'], 'target_id':case['target'],
                'reason':'opponent_conceded'}
    return [proof for did,proof in proofs.items() if matches.get(did)==1]


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parent/'state-bridge'))
    request=json.loads(sys.stdin.buffer.read(65537))
    print(json.dumps(verify(Path(sys.argv[1]),request['outcome'],request['cases'])))
