"""Read a bounded HSTracker log tail without printing any log content."""

import os
import re
import time
from pathlib import Path

MAX_TAIL = 256 * 1024
START = b"----- Game Started -----"
END = b"----- Game End -----"
LOCAL_ID = re.compile(rb"localPlayer:[^\r\n]*?playerId:\s*([12])\b")
UPDATED_PLAYERS = re.compile(rb"Game\.updatePlayers\(\):[^\r\n]*?\[PlayerId=([12])\]\s+vs\s+[^\r\n]*?\[PlayerId=([12])\]")


class IdentityUnavailable(Exception):
    pass


def read_tracker_player_id(path, now=None, max_age_s=120):
    path = Path(path)
    now = time.time() if now is None else now
    with path.open("rb") as handle:
        st = os.fstat(handle.fileno())
        if now - st.st_mtime > max_age_s or st.st_mtime - now > 60:
            raise IdentityUnavailable("tracker log is stale")
        handle.seek(max(0, st.st_size - MAX_TAIL))
        tail = handle.read(MAX_TAIL)
    start = tail.rfind(START)
    if start < 0:
        raise IdentityUnavailable("active game start not in bounded tail")
    current = tail[start:]
    if END in current:
        raise IdentityUnavailable("tracker reports the game has ended")
    ids = set(LOCAL_ID.findall(current))
    for own, other in UPDATED_PLAYERS.findall(current):
        if own == other:
            raise IdentityUnavailable("tracker lists duplicate player IDs")
        ids.add(own)
    if len(ids) != 1:
        raise IdentityUnavailable("tracker identity missing or conflicting")
    return int(next(iter(ids)))


if __name__ == "__main__":
    try:
        read_tracker_player_id(Path.home() / "Library/Logs/HSTracker/hstracker.log")
        print("active identity found")
    except (IdentityUnavailable, OSError) as error:
        print("blocked: " + str(error))
