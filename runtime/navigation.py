"""Previously calibrated practice navigation and bounded child lifecycle helpers."""
import json
import os
import select
import subprocess
import time
from pathlib import Path

FRAMEWORK = "/Applications/HSTracker.app/Contents/Frameworks/HearthMirror.framework/Versions/A/HearthMirror"
DECK_ID = 3384315883

class Stop(Exception):
    pass


def login_intro_backdrop(status, launch_time, log_root=Path('/Applications/Hearthstone/Logs')):
    """Return an empty backdrop point only for the authenticated startup overlay."""
    scene = status.get('scene') or {}
    if (scene.get('mode') != 2 or scene.get('sceneLoaded') is not True
            or scene.get('transitioning') is not False or status.get('popup') is not True
            or status.get('game_type') != 0 or status.get('format') != 0
            or any(status.get(key) is not False for key in ('shop_open','game_menu','mulligan_waiting'))):
        return None
    # Check only this launch's Login.log. Older quest and reconnect messages
    # cannot authorize input. No match or account data is needed here.
    try:
        sessions = [p for p in log_root.glob('Hearthstone_*') if not p.is_symlink()
                    and p.is_dir() and p.stat().st_ctime >= launch_time]
        if len(sessions) != 1 or (sessions[0] / 'Power.log').exists():
            return None
        path = sessions[0] / 'Login.log'
        if path.is_symlink() or not path.is_file() or path.stat().st_mtime < launch_time:
            return None
        with path.open('rb') as stream:
            lines = stream.read(4097)
        login = lines.find(b'We are now logged in, stopping processing challenges')
        if len(lines) > 4096 or login < 0 or b'Initial download done, Waiting Intro popup...' not in lines[login:]:
            return None
    except OSError:
        return None
    # Outside the task cards and any central modal buttons in the calibrated window.
    return (180,155)


def next_navigation(stage, status):
    scene = status.get("scene") or {}
    if not scene.get("sceneLoaded") or scene.get("transitioning") or status.get("popup") or status.get("shop_open"):
        return None
    mode, deck = scene.get("mode"), status.get("selected_deck")
    if stage == 0 and mode == 3:
        return (735, 484)
    if stage == 1 and mode == 16:
        return (1040, 628)
    if stage == 2 and mode == 13 and deck is None:
        return (1060, 735)
    if stage in (3, 4, 5) and mode == 13 and deck == DECK_ID:
        return {3: (1068, 730), 4: (1058, 400), 5: (1080, 716)}[stage]
    return None


def next_ranked_navigation(stage, status):
    """Only launch a verified Standard deck from the observed Tournament menu.

    The format switch is deliberately not guessed: the user has selected
    Standard, and an unexpected format/deck is a stop rather than a queue.
    """
    scene = status.get("scene") or {}
    if not scene.get("sceneLoaded") or scene.get("transitioning") or status.get("popup") or status.get("shop_open"):
        return None
    if stage == 0 and scene.get("mode") == 3:
        return (735, 310)
    if stage == 1 and scene.get("mode") == 7:
        picker = status.get("deck_picker") or {}
        if picker.get("isModeSwitching") or picker.get("setRotationOpen"):
            return None
        from deck_observation import public_selected_deck
        if picker.get("visualsFormatType") != 2:
            raise Stop("选牌界面不是标准模式；未开始匹配")
        if (status.get("selected_deck") != DECK_ID or picker.get("selectedDeck") != DECK_ID
                or not public_selected_deck(status.get("selected_deck_details"))):
            raise Stop("动物园卡组身份或完整 30 张牌未确认；未开始匹配")
        return (1070, 735)
    return None


def read_json_line(process, timeout):
    pending = getattr(process, "_pending_line_bytes", b"")
    deadline = time.monotonic() + timeout
    while b"\n" not in pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise Stop("supervisor did not start in time")
        chunk = os.read(process.stdout.fileno(), 4096)
        if not chunk:
            raise Stop("supervisor exited while starting")
        pending += chunk
        if len(pending) > 16 * 1024:
            raise Stop("supervisor startup output too large")
    line, process._pending_line_bytes = pending.split(b"\n", 1)
    return json.loads(line)


def stop_child(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


