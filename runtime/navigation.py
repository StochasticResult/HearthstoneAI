"""Previously calibrated practice navigation and bounded child lifecycle helpers."""
import json
import os
import select
import subprocess
import time

FRAMEWORK = "/Applications/HSTracker.app/Contents/Frameworks/HearthMirror.framework/Versions/A/HearthMirror"
DECK_ID = 3384315883

class Stop(Exception):
    pass


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


