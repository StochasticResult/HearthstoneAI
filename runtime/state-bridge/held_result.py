"""Recover the verified local player's result after a protected ranked match ends."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_boundary import FaultedMatchEnd, LOG_ROOT, cursor_source
from incremental_board import IncrementalBoard, board
from match_review import save


def verified_result(cursor, end, root=LOG_ROOT):
    own = cursor.get('verified_owner')
    if type(own) is not int or own not in (1,2):
        return None
    path,identity,start = cursor_source(cursor,root,'start_offset')
    if type(end) is not int or not start <= cursor['offset'] <= end or start == end:
        return None
    parser = IncrementalBoard()
    games = 0
    terminal = False
    with path.open('rb') as stream:
        stat = os.fstat(stream.fileno())
        if (stat.st_dev,stat.st_ino) != identity or stat.st_size < end:
            return None
        stream.seek(start)
        while stream.tell() < end:
            line = stream.readline(min(65537,end-stream.tell()))
            if not line.endswith(b'\n') or len(line) > 65536:
                return None
            if FaultedMatchEnd.create.search(line):
                games += 1
                if games > 1:
                    return None
            terminal |= games == 1 and bool(FaultedMatchEnd.terminal.search(line))
            parser.feed(line)
    game = parser.game
    if games != 1 or not terminal or not game or game['meta'].get('GameType') != 'GT_RANKED' or game['meta'].get('FormatType') != 'FT_STANDARD':
        return None
    players = board._players(game)
    result = {board._tag_int(p,'PLAYER_ID'):board._playstate(p) for p in players}
    if len(players) != 2 or set(result) != {1,2} or (result[own],result[3-own]) not in (('WON','LOST'),('LOST','WON'),('TIED','TIED')):
        return None
    return {'outcome':result[own], 'method':'verified_player_terminal', 'owner':own,
            'device':identity[0], 'inode':identity[1], 'start_offset':start, 'end_offset':end}


if __name__ == '__main__':
    dest = Path(sys.argv[1])
    path = dest/'log-cursor.json'
    if path.is_symlink() or path.stat().st_size > 2048:
        raise SystemExit(1)
    result = verified_result(json.loads(path.read_bytes()),int(sys.argv[2]))
    if not result:
        raise SystemExit(1)
    save(dest/'held-result.json',{'run':dest.name,**result})
