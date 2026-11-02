"""Verified log boundaries between games in one retained Hearthstone client."""
from pathlib import Path
from navigation import Stop

LOG_ROOT = Path('/Applications/Hearthstone/Logs')


class LogBoundary:
    def __init__(self, cursor, root=LOG_ROOT):
        self.path = Path(cursor['source'])
        self.identity = (cursor['device'], cursor['inode'])
        self.offset = cursor['offset']
        if (self.path.is_symlink() or root.resolve() not in self.path.resolve().parents
                or self.path.name != 'Power.log' or type(self.offset) is not int or self.offset < 0
                or any(type(x) is not int for x in self.identity)):
            raise Stop('Invalid retained game log boundary')
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
