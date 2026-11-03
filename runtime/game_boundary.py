"""Verified log boundaries between games in one retained Hearthstone client."""
import os
import re
import datetime
from pathlib import Path
from navigation import Stop

LOG_ROOT = Path('/Applications/Hearthstone/Logs')


def idle_log_start(started, root=LOG_ROOT):
    """Bind the fresh reader to the verified lobby process, including its not-yet-created Power.log."""
    dirs = []
    for folder in root.glob('Hearthstone_*'):
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            stamp = datetime.datetime.strptime(folder.name, 'Hearthstone_%Y_%m_%d_%H_%M_%S').timestamp()
        except ValueError:
            continue
        if abs(stamp - started) <= 15:
            dirs.append(folder)
    if len(dirs) != 1:
        raise Stop('Cannot identify the retained client log session')
    path = dirs[0] / 'Power.log'
    if path.is_symlink():
        raise Stop('Invalid retained client log')
    if not path.exists():
        return path, 0
    with path.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        if size:
            stream.seek(size - 1)
            if stream.read(1) != b'\n':
                raise Stop('Retained lobby log is between lines')
    return path, size


def cursor_source(cursor, root, offset_key):
    path = Path(cursor['source'])
    identity = (cursor['device'], cursor['inode'])
    offset = cursor[offset_key]
    if (path.is_symlink() or root.resolve() not in path.resolve().parents or path.name != 'Power.log'
            or type(offset) is not int or offset < 0 or any(type(x) is not int for x in identity)):
        raise Stop('Invalid retained game log boundary')
    return path, identity, offset


class FaultedMatchEnd:
    """Read the failed worker's match from its original queue boundary, without input."""
    prefix = rb'^D \d\d:\d\d:\d\d(?:\.\d+)? GameState\.DebugPrintPower\(\) -\s*'
    create = re.compile(prefix + rb'CREATE_GAME\s*$')
    terminal = re.compile(prefix + rb'TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE\s*$')

    def __init__(self, cursor, root=LOG_ROOT):
        self.path, self.identity, self.offset = cursor_source(cursor, root, 'start_offset')
        self.games = 0
        self.ended = False
        self.partial = b''

    def poll(self):
        # Never authorize closing a live game from an old result, a partial
        # line, a truncated/replaced source, or a second CREATE_GAME.
        with self.path.open('rb') as source:
            stat = os.fstat(source.fileno())
            if (stat.st_dev,stat.st_ino) != self.identity or stat.st_size < self.offset:
                raise Stop('Game log changed during fault observation')
            source.seek(self.offset)
            chunk = source.read(128 * 1024)
            self.offset += len(chunk)
        lines = (self.partial + chunk).split(b'\n')
        self.partial = lines.pop()
        if len(self.partial) > 65536:
            raise Stop('Oversized partial line during fault observation')
        for line in lines:
            if self.create.search(line):
                self.games += 1
            elif self.games == 1 and self.terminal.search(line):
                self.ended = True
        latest = self.path.stat()
        return (self.ended and self.games == 1 and not self.partial and self.offset == latest.st_size
                and (latest.st_dev,latest.st_ino) == self.identity)


class LogBoundary:
    def __init__(self, cursor, root=LOG_ROOT):
        self.path, self.identity, self.offset = cursor_source(cursor, root, 'offset')
        self.check()

    def check(self):
        stat = self.path.stat()
        if (stat.st_dev,stat.st_ino) != self.identity or stat.st_size < self.offset:
            raise Stop('Game log changed between matches')
        if stat.st_size-self.offset > 512*1024:
            raise Stop('Unexpected game activity after the recorded result')
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            following = stream.read(512*1024+1)
        if b'CREATE_GAME' in following:
            raise Stop('A new game already started outside the queue controller')
        return stat

    def queue_offset(self):
        """Start a fresh reader before queuing, on a complete line boundary."""
        stat = self.check()
        with self.path.open('rb') as stream:
            start = max(0,stat.st_size-65536)
            stream.seek(start)
            last = stream.read(stat.st_size-start)
        newline = last.rfind(b'\n')
        if newline < 0:
            raise Stop('Game log has no complete boundary line')
        return start+newline+1


def result_dismissal(status, outcome):
    """A continue click is only allowed after a verified completed match."""
    if outcome not in ('WON','LOST','TIED'):
        raise Stop('Cannot dismiss a game without a verified result')
    scene = status.get('scene') or {}
    if (not scene.get('sceneLoaded') or scene.get('transitioning')
            or status.get('popup') or status.get('shop_open') or status.get('game_menu')):
        return None
    if scene.get('mode') == 4 and not status.get('mulligan_waiting'):
        # Calibrated from the actual 1470x846 window at screen y=33:
        # the bottom "点击继续" label is around window y=812, screen y=845.
        # The old y=710 missed the ranked promotion overlay.
        return (735,845)
    return None
